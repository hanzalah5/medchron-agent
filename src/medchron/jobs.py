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
    load_ingested,
    reviewed_chronology,
    save_chronology,
    save_ingested,
    save_issues,
)
from medchron.llm.providers import LLMProvider, MeteredProvider
from medchron.orchestrator.pipeline import RunConfig, run_case
from medchron.retrieval.embeddings import get_embedder
from medchron.storage import LocalStorage

log = logging.getLogger(__name__)

PROVIDERS = {"anthropic", "replay", "oracle"}


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
        job.started_at = dt.datetime.now(dt.UTC)
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
    manifest = json.loads((gold_dir / "manifest.json").read_text(encoding="utf-8"))
    match = next((m for m in manifest if m["pdf_sha256"] == pdf_sha256), None)
    if match is None:
        raise LookupError("oracle mode only works on the bundled synthetic cases")
    gold = CaseGold.model_validate_json((gold_dir / f"{match['case_id']}.gold.json").read_text())
    return OracleProvider(gold)


def process(engine: Engine, job_id: str, storage: LocalStorage | None = None) -> None:
    """Run the pipeline for a claimed job and persist the results."""
    storage = storage or LocalStorage()

    def note(message: str) -> None:
        with Session(engine) as s, s.begin():
            s.add(JobEvent(job_id=job_id, message=message))

    with Session(engine) as s:
        job = s.get(Job, job_id)
        assert job is not None
        matter = s.get(Matter, job.matter_id)
        assert matter is not None
        provider_name, budget, kind = job.provider, job.budget_usd, job.kind
        matter_id, key, sha = matter.matter_id, matter.source_path, matter.source_sha256

    summary: dict[str, Any] | None = None
    try:
        provider = _provider(provider_name, sha)
        if kind == "memo":
            summary = _draft(engine, matter_id, provider, budget, note)
            status, error = "completed", None
        else:
            summary = _pipeline(engine, storage, matter_id, key, provider, budget, note)
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


def _pipeline(
    engine: Engine,
    storage: LocalStorage,
    matter_id: str,
    key: str,
    provider: LLMProvider,
    budget: float | None,
    note: Callable[[str], None],
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmp:
        pdf = Path(tmp) / "production.pdf"
        pdf.write_bytes(storage.get(key))
        out = Path(os.environ.get("MEDCHRON_RUNS_DIR", "runs")) / matter_id
        result = run_case(
            pdf, matter_id, provider, RunConfig(out_dir=out, budget_usd=budget), progress=note
        )
    with Session(engine) as s, s.begin():
        save_ingested(s, result.case, get_embedder())
        save_chronology(s, result.chronology)
        save_issues(s, matter_id, result.issues)
    return result.summary.model_dump(mode="json")


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


def work(engine: Engine, *, once: bool = False, poll_seconds: float = 1.0) -> int:
    """Worker loop. Returns the number of jobs processed (useful with ``once``)."""
    processed = 0
    while True:
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
