"""Postgres-backed job queue and worker.

Postgres is already the system of record, so it doubles as the queue. That
means one less service to run and pay for. Workers claim jobs with
``FOR UPDATE SKIP LOCKED``, so any number can run concurrently without
double-processing. Progress messages are written to ``job_events`` and
streamed to the UI.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import tempfile
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from medchron.agents.drafter import draft_memo
from medchron.db.models import IssueRow, Job, JobEvent, Matter, MemoRow
from medchron.db.store import (
    issue_from_row,
    list_productions,
    load_ingested,
    record_page_ranges,
    reviewed_chronology,
    save_chronology,
    save_ingested,
    save_issues,
    save_version,
)
from medchron.llm.providers import CallRecord, LLMProvider, MeteredProvider
from medchron.observability import setup_tracing
from medchron.orchestrator.pipeline import RunConfig, run_case
from medchron.retrieval.embeddings import get_embedder
from medchron.security import audit
from medchron.storage import Storage, get_storage

log = logging.getLogger(__name__)

PROVIDERS = {"anthropic", "replay", "oracle"}
WORKER = "system:worker"
STALE_AFTER = dt.timedelta(minutes=10)
MAX_ATTEMPTS = 3


def enqueue(
    session: Session,
    matter_id: str,
    provider: str,
    budget_usd: float | None,
    kind: str = "pipeline",
) -> Job:
    if provider not in PROVIDERS:
        raise ValueError(f"provider must be one of {sorted(PROVIDERS)}")
    if kind not in ("pipeline", "memo"):
        raise ValueError("kind must be pipeline or memo")
    job = Job(
        job_id=str(uuid.uuid4()),
        matter_id=matter_id,
        provider=provider,
        budget_usd=budget_usd,
        kind=kind,
    )
    session.add(job)
    session.flush()
    session.add(JobEvent(job_id=job.job_id, message="queued"))
    return job


def notify_worker() -> None:
    """Wake a worker after enqueueing.

    Locally a long-running ``medchron worker`` polls the queue. On Cloud Run
    the worker is a *job* that scales to zero: the API starts an execution
    (``MEDCHRON_WORKER_JOB`` = projects/P/locations/L/jobs/J) and the job
    drains the queue, then exits. Failures are logged, not raised: the queued
    job stays safe in Postgres and the next trigger or schedule picks it up.
    """
    name = os.environ.get("MEDCHRON_WORKER_JOB")
    if not name:
        return
    try:
        import urllib.request

        token_req = urllib.request.Request(
            "http://metadata.google.internal/computeMetadata/v1/instance/"
            "service-accounts/default/token",
            headers={"Metadata-Flavor": "Google"},
        )
        with urllib.request.urlopen(token_req, timeout=3) as r:
            token = json.loads(r.read())["access_token"]
        run_req = urllib.request.Request(
            f"https://run.googleapis.com/v2/{name}:run",
            data=b"{}",
            method="POST",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        urllib.request.urlopen(run_req, timeout=10).close()
    except Exception:  # pragma: no cover - deployed environment only
        log.warning("could not trigger worker job; the queued job will wait for the next run")


def claim_next(session: Session) -> Job | None:
    job = session.scalars(
        select(Job)
        .where(Job.status == "queued")
        .order_by(Job.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    ).first()
    if job is not None:
        job.status = "running"
        job.started_at = job.heartbeat_at = dt.datetime.now(dt.UTC)
        job.attempts += 1
    return job


def _provider(name: str, pdf_sha256: str) -> LLMProvider:
    if name == "anthropic":
        from medchron.llm.anthropic_provider import AnthropicProvider

        return AnthropicProvider()
    if name == "replay":
        from medchron.llm.providers import ReplayProvider

        return ReplayProvider(Path(os.environ.get("MEDCHRON_FIXTURES", "tests/fixtures/llm")))
    # Oracle demo mode: answer from the gold labels of a known synthetic case.
    from medchron.evals.oracle import OracleProvider
    from medchron.synth import CaseGold

    gold_dir = Path(os.environ.get("MEDCHRON_GOLD_DIR", "data/synthetic"))
    for manifest_path in sorted(gold_dir.rglob("manifest.json")):  # dev/, test/, ...
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        match = next((m for m in manifest if m["pdf_sha256"] == pdf_sha256), None)
        if match is not None:
            gold_path = manifest_path.parent / f"{match['case_id']}.gold.json"
            return OracleProvider(CaseGold.model_validate_json(gold_path.read_text()))
    raise LookupError("oracle mode only works on the bundled synthetic cases")


def process(engine: Engine, job_id: str, storage: Storage | None = None) -> None:
    """Run the pipeline for a claimed job and persist the results."""
    storage = storage or get_storage()

    def note(message: str) -> None:
        with Session(engine) as s, s.begin():
            s.add(JobEvent(job_id=job_id, message=message))
            job = s.get(Job, job_id)
            if job is not None:
                job.heartbeat_at = dt.datetime.now(dt.UTC)  # proves the worker is alive

    with Session(engine) as s:
        job = s.get(Job, job_id)
        assert job is not None
        matter = s.get(Matter, job.matter_id)
        assert matter is not None
        provider_name, budget, kind = job.provider, job.budget_usd, job.kind
        matter_id, sha = matter.matter_id, matter.source_sha256
        bates_prefix = matter.bates_prefix or None
        keys = [r.storage_key for r in list_productions(s, matter_id)] or [matter.source_path]

    summary: dict[str, Any] | None = None
    try:
        provider = _provider(provider_name, sha)
        if kind == "memo":
            summary = _draft(engine, matter_id, provider, budget, note)
            status, error = "completed", None
        else:
            summary = _pipeline(
                engine,
                storage,
                matter_id,
                keys,
                provider,
                budget,
                note,
                job_id=job_id,
                bates_prefix=bates_prefix,
            )
            status, error = str(summary["status"]), None
    except Exception as e:  # the job records the failure; the worker keeps running
        log.exception("job %s failed", job_id)
        status, error = "failed", f"{type(e).__name__}: {e}"[:2000]
        note(f"failed: {type(e).__name__}")
    with Session(engine) as s, s.begin():
        job = s.get(Job, job_id)
        assert job is not None
        job.status, job.error, job.summary = status, error, summary
        job.finished_at = dt.datetime.now(dt.UTC)
        s.add(JobEvent(job_id=job_id, message=f"finished: {status}"))
        audit.record(
            s,
            WORKER,
            f"job.{status}",
            matter_id=matter_id,
            object_type="job",
            object_id=job_id,
            details={"kind": kind, "provider": provider_name, "error": (error or "")[:200]},
        )


def _pipeline(
    engine: Engine,
    storage: Storage,
    matter_id: str,
    keys: list[str],
    provider: LLMProvider,
    budget: float | None,
    note: Callable[[str], None],
    job_id: str | None = None,
    bates_prefix: str | None = None,
) -> dict[str, Any]:
    """Run every production of the matter, in order. Earlier work is reused from checkpoints."""
    with tempfile.TemporaryDirectory() as tmp:
        pdfs = []
        for n, key in enumerate(keys, start=1):
            pdf = Path(tmp) / f"production-{n}.pdf"
            pdf.write_bytes(storage.get(key))
            pdfs.append(pdf)
        out = Path(os.environ.get("MEDCHRON_RUNS_DIR", "runs")) / matter_id
        config = RunConfig(out_dir=out, budget_usd=budget, bates_prefix=bates_prefix)
        result = run_case(pdfs, matter_id, provider, config, progress=note)
    with Session(engine) as s, s.begin():
        save_ingested(s, result.case, get_embedder())
        record_page_ranges(s, result.case)
        save_chronology(s, result.chronology)
        save_issues(s, matter_id, result.issues)
        version = save_version(s, result.chronology, job_id=job_id, productions=len(keys))
        _audit_calls(s, matter_id, result.calls)
    summary = result.summary.model_dump(mode="json")
    summary["chronology_version"] = version
    return summary


def _audit_calls(session: Session, matter_id: str, calls: list[CallRecord]) -> None:
    """One audit entry per LLM call: model, tokens, cost and content hashes only."""
    for c in calls:
        audit.record(
            session,
            WORKER,
            "llm.call",
            matter_id=matter_id,
            object_type="llm",
            object_id=c.request_id or c.request_sha256[:16],
            details={
                "route": c.route,
                "model": c.model,
                "prompt": c.prompt_version,
                "request_sha256": c.request_sha256,
                "response_sha256": c.response_sha256,
                "stop": c.stop_reason,
                "input_tokens": c.usage.input_tokens,
                "output_tokens": c.usage.output_tokens,
                "cache_read_tokens": c.usage.cache_read_input_tokens,
                "cost_usd": round(c.cost_usd, 6),
                "fallback": c.served_by_fallback,
            },
        )


def _draft(
    engine: Engine,
    matter_id: str,
    provider: LLMProvider,
    budget: float | None,
    note: Callable[[str], None],
) -> dict[str, Any]:
    """Draft a memo from the *reviewed* chronology and the issues the attorney kept."""
    with Session(engine) as s:
        case = load_ingested(s, matter_id)
        chron = reviewed_chronology(s, matter_id)
        rows = s.scalars(
            select(IssueRow).where(
                IssueRow.matter_id == matter_id,
                IssueRow.triage_status.in_(["pursue", "expert"]),
            )
        ).all()
        issues = [issue_from_row(r) for r in rows]
    note(f"draft: writing memo from {len(chron.events)} events and {len(issues)} issues")
    metered = MeteredProvider(provider, budget_usd=budget)
    memo = draft_memo(metered, case, chron, issues)
    memo_id = str(uuid.uuid4())
    with Session(engine) as s, s.begin():
        _audit_calls(s, matter_id, metered.records)
        s.add(
            MemoRow(
                memo_id=memo_id,
                matter_id=matter_id,
                markdown=memo.markdown,
                claims=[c.model_dump() for c in memo.claims],
            )
        )
    return {
        "memo_id": memo_id,
        "claims": len(memo.claims),
        "cited_ratio": round(memo.cited_ratio, 3),
        "invalid_citations": sum(not c.citation_valid for c in memo.claims),
        "cost_usd": round(metered.spent_usd, 6),
    }


def requeue_stale(engine: Engine, stale_after: dt.timedelta = STALE_AFTER) -> int:
    """Recover jobs whose worker died: requeue them, or fail them after MAX_ATTEMPTS.

    Pipeline steps are checkpointed and idempotent, so a requeued job resumes
    where it stopped, and results are replaced rather than duplicated.
    """
    cutoff = dt.datetime.now(dt.UTC) - stale_after
    n = 0
    with Session(engine) as s, s.begin():
        stale = s.scalars(
            select(Job)
            .where(Job.status == "running", Job.heartbeat_at < cutoff)
            .with_for_update(skip_locked=True)
        ).all()
        for job in stale:
            n += 1
            if job.attempts >= MAX_ATTEMPTS:
                job.status, job.error = "failed", "worker lost too many times"
                job.finished_at = dt.datetime.now(dt.UTC)
            else:
                job.status = "queued"
            s.add(JobEvent(job_id=job.job_id, message=f"recovered stale job -> {job.status}"))
    return n


def work(engine: Engine, *, once: bool = False, poll_seconds: float = 1.0) -> int:
    """Worker loop. Returns the number of jobs processed (useful with ``once``)."""
    setup_tracing("medchron-worker")
    processed = 0
    while True:
        requeue_stale(engine)
        with Session(engine) as s, s.begin():
            job = claim_next(s)
            job_id = job.job_id if job else None
        if job_id:
            process(engine, job_id)
            processed += 1
            continue
        if once:
            return processed
        time.sleep(poll_seconds)
