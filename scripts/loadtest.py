"""Load test: a ~2,000-page production, several workers, and a worker killed mid-run.

What it does (every number in the report is measured by this script):

1. Generates one fictional patient's long record production (default ~2,000
   pages) and a few small cases, all synthetic, with gold labels for the oracle.
2. Runs the big case once in-process, uninterrupted, as the reference.
3. Resets a dedicated database, queues every case as a matter and starts N
   worker processes that share a checkpoint directory.
4. When part of the big case's extraction is checkpointed, kills the worker
   holding it (SIGKILL, so there is no clean shutdown). Another worker must
   detect the stale job, requeue it and resume from the checkpoints.
5. Checks:
   - every job completed
   - the big job took exactly two attempts
   - its chronology in the database equals the reference
   - no rows were duplicated
   - the audit hash chain verifies
6. Writes results JSON and a Markdown summary.

The provider is the gold-label oracle, so this measures the system's plumbing
(ingestion, OCR, de-duplication, queueing, checkpoints, persistence), not
model latency or cost.

    python scripts/loadtest.py --db-url postgresql+psycopg://...medchron_load
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from medchron.chronology.models import Chronology
from medchron.db.models import (
    Base,
    ChronologyVersionRow,
    EventRow,
    Job,
    JobEvent,
    Matter,
    MatterMember,
    PageRow,
)
from medchron.db.store import add_production, load_chronology, make_engine
from medchron.evals.oracle import OracleProvider
from medchron.jobs import enqueue
from medchron.orchestrator.pipeline import RunConfig, run_case
from medchron.security.audit import verify_chain
from medchron.storage import LocalStorage
from medchron.synth.generate import generate_case, generate_long_case, manifest_entry, write_case

ROOT = Path(__file__).resolve().parents[1]
BIG = "load-big"


def _rss_kb(pid: int) -> int:
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    except OSError:
        pass
    return 0


def _event_key(e: Any, matter_id: str) -> tuple[Any, ...]:
    return (
        e.event_id,
        e.date,
        e.type.value,
        e.summary,
        tuple(sorted((s.page_no, s.span_id.split(":", 1)[1], s.quote) for s in e.sources)),
    )


def _machine() -> dict[str, Any]:
    mem_kb = 0
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                mem_kb = int(line.split()[1])
    except OSError:
        pass
    return {
        "cpus": os.cpu_count(),
        "memory_gb": round(mem_kb / 1024 / 1024, 1),
        "platform": platform.platform(),
        "python": platform.python_version(),
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--db-url", default=os.environ.get("LOADTEST_DATABASE_URL"), required=False)
    ap.add_argument("--pages", type=int, default=2000)
    ap.add_argument("--small", type=int, default=3, help="small cases queued alongside")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--kill-at", type=float, default=0.3, help="fraction of extraction done")
    ap.add_argument("--stale-after", type=float, default=20.0, help="seconds")
    ap.add_argument("--timeout", type=float, default=3600.0)
    ap.add_argument("--out", default="runs/loadtest")
    ap.add_argument("--seed", type=int, default=4242)
    args = ap.parse_args()
    if not args.db_url:
        raise SystemExit("set --db-url or LOADTEST_DATABASE_URL (a database this test may reset)")

    out = (ROOT / args.out).resolve()
    shutil.rmtree(out, ignore_errors=True)
    gold_dir = out / "gold"
    results: dict[str, Any] = {
        "machine": _machine(),
        "started": dt.datetime.now(dt.UTC).isoformat(),
    }

    # 1. Synthetic data
    t0 = time.perf_counter()
    big = generate_long_case(args.seed, args.pages, case_id=BIG)
    small = [generate_case(s) for s in range(1, args.small + 1)]
    cases = [big, *small]
    for c in cases:
        write_case(c, gold_dir)
    (gold_dir / "manifest.json").write_text(
        json.dumps([manifest_entry(c) for c in cases], indent=2)
    )
    big_pdf = gold_dir / f"{BIG}.pdf"
    results["data"] = {
        "big_pages": len(big.gold.pages),
        "big_scanned_pages": sum(not p.text_layer for p in big.gold.pages),
        "big_documents": len(big.gold.documents),
        "big_gold_events": len(big.gold.events),
        "small_cases": len(small),
        "small_pages": sum(len(c.gold.pages) for c in small),
        "generate_seconds": round(time.perf_counter() - t0, 1),
    }
    print(f"generated {results['data']}", flush=True)

    # 2. Reference: the big case, uninterrupted, in one process
    t0 = time.perf_counter()
    ref = run_case(big_pdf, BIG, OracleProvider(big.gold), RunConfig(out_dir=out / "reference"))
    ref_seconds = time.perf_counter() - t0
    results["reference"] = {
        "seconds": round(ref_seconds, 1),
        "pages_per_second": round(len(big.gold.pages) / ref_seconds, 1),
        "documents": ref.summary.documents,
        "events": ref.summary.events,
        "issues": ref.summary.issues,
        "llm_calls": ref.summary.llm_calls,
        "status": ref.summary.status,
    }
    print(f"reference {results['reference']}", flush=True)

    # 3. Fresh schema, matters and queued jobs
    engine = make_engine(args.db_url)
    with engine.begin() as conn:
        Base.metadata.drop_all(conn)
        conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
        cfg = Config(str(ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(ROOT / "migrations"))
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")
    from medchron.db.roles import ensure_login_roles, login_roles_from_env, url_for

    roles = login_roles_from_env(allow_defaults=True)  # throwaway load-test database
    ensure_login_roles(engine, roles)  # workers connect as the non-owner worker role
    worker_url = url_for(args.db_url, roles[1])
    storage = LocalStorage(out / "storage")
    with Session(engine) as s, s.begin():
        for c in cases:
            mid = c.gold.case_id if c is big else f"load-{c.gold.case_id}"
            key = storage.put(c.pdf)
            s.add(Matter(matter_id=mid, title=mid, source_sha256=c.sha256, source_path=key,
                         created_by="loadtest", bates_prefix=""))  # fmt: skip
            s.flush()
            s.add(MatterMember(matter_id=mid, user_id="loadtest", added_by="loadtest"))
            add_production(s, mid, c.sha256, key, "loadtest")
            enqueue(s, mid, "oracle", None)
            s.flush()

    # 4. Workers, then the kill
    env = {
        **os.environ,
        "DATABASE_URL": worker_url,
        "MEDCHRON_STORAGE_DIR": str(out / "storage"),
        "MEDCHRON_RUNS_DIR": str(out / "runs"),
        "MEDCHRON_GOLD_DIR": str(gold_dir),
        "MEDCHRON_STALE_AFTER_S": str(args.stale_after),
        "MEDCHRON_HEARTBEAT_S": "3",
        "MEDCHRON_ENV": os.environ.get("MEDCHRON_ENV", "development"),
    }
    procs = {
        f"w{i}": subprocess.Popen(
            [str(ROOT / ".venv/bin/medchron"), "worker"],
            env={**env, "MEDCHRON_WORKER_ID": f"w{i}"},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for i in range(1, args.workers + 1)
    }
    peak_rss: dict[str, int] = dict.fromkeys(procs, 0)
    extract_dir = out / "runs" / BIG / "checkpoints" / "extract"
    expected_docs = ref.summary.documents
    t_start = time.perf_counter()
    killed: dict[str, Any] | None = None
    try:
        while time.perf_counter() - t_start < args.timeout:
            for name, p in procs.items():
                if p.poll() is None:
                    peak_rss[name] = max(peak_rss[name], _rss_kb(p.pid))
            done_extract = len(list(extract_dir.glob("*.json"))) if extract_dir.exists() else 0
            if killed is None and done_extract >= args.kill_at * expected_docs:
                with Session(engine) as s:
                    msg = s.scalars(
                        select(JobEvent.message)
                        .join(Job, Job.job_id == JobEvent.job_id)
                        .where(Job.matter_id == BIG, JobEvent.message.like("attempt 1 started by%"))
                    ).first()
                victim = msg.rsplit(" ", 1)[1] if msg else None
                if victim in procs:
                    procs[victim].send_signal(signal.SIGKILL)
                    killed = {
                        "worker": victim,
                        "after_seconds": round(time.perf_counter() - t_start, 1),
                        "extract_checkpoints_at_kill": done_extract,
                        "documents": expected_docs,
                    }
                    print(f"killed {killed}", flush=True)
            with Session(engine) as s:
                open_jobs = s.scalar(
                    select(func.count())
                    .select_from(Job)
                    .where(Job.status.in_(["queued", "running"]))
                )
            if open_jobs == 0:
                break
            time.sleep(0.2)
    finally:
        total = time.perf_counter() - t_start
        for p in procs.values():
            if p.poll() is None:
                p.terminate()
        for p in procs.values():
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()

    # 5. Checks
    with Session(engine) as s:
        jobs = {j.matter_id: j for j in s.scalars(select(Job))}
        big_job = jobs[BIG]
        db_chron = load_chronology(s, BIG)
        page_rows = s.scalar(
            select(func.count()).select_from(PageRow).where(PageRow.matter_id == BIG)
        )
        event_rows = s.scalar(
            select(func.count()).select_from(EventRow).where(EventRow.matter_id == BIG)
        )
        versions = s.scalar(
            select(func.count())
            .select_from(ChronologyVersionRow)
            .where(ChronologyVersionRow.matter_id == BIG)
        )
        chain_ok, audit_rows, _ = verify_chain(s)
        events_log = [
            m for m in s.scalars(select(JobEvent.message).where(JobEvent.job_id == big_job.job_id))
        ]
    reference: Chronology = ref.chronology
    same = {_event_key(e, BIG) for e in db_chron.events} == {
        _event_key(e, BIG) for e in reference.events
    }
    checks = {
        "all_jobs_completed": all(j.status == "completed" for j in jobs.values()),
        "big_job_attempts": big_job.attempts,
        "big_job_resumed_after_kill": killed is not None and big_job.attempts == 2,
        "chronology_matches_reference": same and len(db_chron.events) == len(reference.events),
        "page_rows": page_rows,
        "pages_expected": len(big.gold.pages),
        "event_rows": event_rows,
        "events_expected": len(reference.events),
        "chronology_versions": versions,
        "audit_chain_ok": chain_ok,
        "audit_rows": audit_rows,
        "reused_documents_on_resume": (big_job.summary or {}).get("reused_documents"),
    }
    results.update(
        {
            "workers": args.workers,
            "stale_after_seconds": args.stale_after,
            "queue_seconds_total": round(total, 1),
            "killed": killed,
            "peak_rss_mb": {k: round(v / 1024, 1) for k, v in peak_rss.items()},
            "checks": checks,
            "big_job_events": events_log,
        }
    )
    ok = (
        checks["all_jobs_completed"]
        and checks["big_job_resumed_after_kill"]
        and checks["chronology_matches_reference"]
        and page_rows == len(big.gold.pages)
        and event_rows == len(reference.events)
        and chain_ok
    )
    results["passed"] = bool(ok)
    (out / "results.json").write_text(json.dumps(results, indent=2, default=str))
    print(
        json.dumps(
            {k: v for k, v in results.items() if k != "big_job_events"}, indent=2, default=str
        )
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
