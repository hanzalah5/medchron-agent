"""Code-owned orchestration of the case pipeline (a fixed DAG).

    ingest -> classify -> segment -> extract (fan-out per document) -> chronology

Design choices (see docs/plan.md and the ADRs):
- **The DAG is code, not an agent.** Autonomy lives only inside bounded
  steps, so any run can be explained and replayed.
- **Checkpointed, idempotent steps.** Each step's output is stored under a key
  derived from its inputs, prompt versions and model settings. Re-running a
  case after a crash, or after changing one document's prompt, recomputes only
  what changed.
- **Failure isolation.** A refused, truncated or failed document becomes a
  reviewer flag (dead letter) instead of failing the whole case.
- **Budget.** The metered provider stops the run cleanly when the per-case
  budget is spent. Completed work is kept.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel, Field

from medchron.agents.classifier import PageLabel, classify_pages, segment
from medchron.agents.extractor import ExtractionResult, extract_document
from medchron.chronology.builder import build_chronology
from medchron.chronology.export import to_csv, to_markdown
from medchron.chronology.models import CaseFlag, Chronology, DocumentSegment, ExtractedEvent
from medchron.ingestion import IngestedCase, ingest_pdf
from medchron.llm.config import effort_for, model_for
from medchron.llm.prompts import load_prompt
from medchron.llm.providers import CallRecord, LLMProvider, MeteredProvider
from medchron.llm.types import BudgetExceededError, LLMError

log = logging.getLogger(__name__)
M = TypeVar("M", bound=BaseModel)


class _ExtractionCheckpoint(BaseModel):
    events: list[dict[str, object]]
    flags: list[CaseFlag]
    rejected: int
    turns: int


class _Labels(BaseModel):
    labels: list[PageLabel]


class RunSummary(BaseModel):
    case_id: str
    status: str  # completed | completed_with_errors | budget_exceeded
    documents: int
    events: int
    failed_documents: list[str] = Field(default_factory=list)
    llm_calls: int
    cost_usd: float
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int
    rejected_events: int
    seconds: float


@dataclass
class RunConfig:
    out_dir: Path
    budget_usd: float | None = None
    concurrency: int = 4
    ocr: bool = True


@dataclass
class RunResult:
    chronology: Chronology
    summary: RunSummary
    calls: list[CallRecord] = field(default_factory=list)


class Checkpoints:
    def __init__(self, root: Path) -> None:
        self.root = root

    @staticmethod
    def key(*parts: str) -> str:
        return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:24]

    def load(self, step: str, key: str, model: type[M]) -> M | None:
        path = self.root / step / f"{key}.json"
        if path.exists():
            return model.model_validate_json(path.read_text(encoding="utf-8"))
        return None

    def save(self, step: str, key: str, value: BaseModel) -> None:
        path = self.root / step / f"{key}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(value.model_dump_json(), encoding="utf-8")
        tmp.replace(path)  # atomic: a crash never leaves a half-written checkpoint


def _settings(route: str, *prompts: str) -> str:
    ids = ",".join(load_prompt(p).id for p in prompts)
    return f"{route}|{model_for(route)}|{effort_for(route)}|{ids}"


def run_case(
    pdf: Path,
    case_id: str,
    provider: LLMProvider,
    config: RunConfig,
    *,
    progress: Callable[[str], None] | None = None,
) -> RunResult:
    say = progress or (lambda msg: log.info(msg))
    start = time.perf_counter()
    cp = Checkpoints(config.out_dir / "checkpoints")
    metered = MeteredProvider(provider, budget_usd=config.budget_usd)
    llm = metered
    status = "completed"

    # 1. Ingest (deterministic)
    data = pdf.read_bytes()
    ingest_key = cp.key(hashlib.sha256(data).hexdigest(), case_id, f"ocr={config.ocr}")
    case = cp.load("ingest", ingest_key, IngestedCase)
    if case is None:
        say("ingest: OCR, de-duplication and chunking")
        case = ingest_pdf(data, case_id, ocr=config.ocr)
        cp.save("ingest", ingest_key, case)
    review_flags = [
        CaseFlag(kind="page_needs_review", page_no=p.page_no, note=f"text source={p.source.value}")
        for p in case.pages
        if p.needs_review and p.duplicate_of is None
    ]

    # 2. Classify pages -> 3. segment into documents
    cls_key = cp.key(ingest_key, _settings("classify", "system_base", "classify_pages"))
    labels = cp.load("classify", cls_key, _Labels)
    if labels is None:
        say("classify: indexing pages into documents")
        labels = _Labels(labels=classify_pages(llm, case))
        cp.save("classify", cls_key, labels)
    documents = segment(labels.labels)

    # 4. Extract events per document (fan-out with bounded concurrency)
    ext_settings = _settings("extract", "system_base", "extract_events")
    results: dict[str, ExtractionResult] = {}
    failed: list[str] = []
    flags: list[CaseFlag] = list(review_flags)

    def work(doc: DocumentSegment) -> tuple[str, ExtractionResult]:
        key = cp.key(ingest_key, doc.model_dump_json(), ext_settings)
        hit = cp.load("extract", key, _ExtractionCheckpoint)
        if hit is not None:
            return doc.doc_id, ExtractionResult(
                events=[ExtractedEvent.model_validate(e) for e in hit.events],
                flags=hit.flags,
                rejected=hit.rejected,
                turns=hit.turns,
            )
        res = extract_document(llm, case, doc)
        cp.save(
            "extract",
            key,
            _ExtractionCheckpoint(
                events=[e.model_dump(mode="json") for e in res.events],
                flags=res.flags,
                rejected=res.rejected,
                turns=res.turns,
            ),
        )
        return doc.doc_id, res

    say(f"extract: {len(documents)} documents, concurrency {config.concurrency}")
    with ThreadPoolExecutor(max_workers=config.concurrency) as pool:
        futures = {pool.submit(work, d): d for d in documents}
        for fut in as_completed(futures):
            doc = futures[fut]
            try:
                doc_id, res = fut.result()
                results[doc_id] = res
            except BudgetExceededError:
                status = "budget_exceeded"
                failed.append(doc.doc_id)
            except (LLMError, LookupError) as e:  # LookupError covers replay misses
                log.warning("extraction failed for %s: %s", doc.doc_id, e)
                failed.append(doc.doc_id)
                flags.append(
                    CaseFlag(
                        kind="extraction_failed",
                        page_no=doc.start_page,
                        note=f"{doc.doc_id} ({doc.doc_type.value}): {type(e).__name__}. "
                        "Needs manual review.",
                    )
                )
    if failed and status == "completed":
        status = "completed_with_errors"

    # 5. Chronology (deterministic)
    events = [e for d in documents if d.doc_id in results for e in results[d.doc_id].events]
    flags += [f for d in documents if d.doc_id in results for f in results[d.doc_id].flags]
    chron = build_chronology(case_id, documents, events, flags)

    usage = metered.usage
    summary = RunSummary(
        case_id=case_id,
        status=status,
        documents=len(documents),
        events=len(chron.events),
        failed_documents=sorted(failed),
        llm_calls=len(metered.records),
        cost_usd=round(metered.spent_usd, 6),
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_input_tokens=usage.cache_read_input_tokens,
        rejected_events=sum(r.rejected for r in results.values()),
        seconds=round(time.perf_counter() - start, 2),
    )
    _write_outputs(config.out_dir, chron, summary, metered.records)
    say(f"done: {summary.events} events, status={summary.status}, cost=${summary.cost_usd:.4f}")
    return RunResult(chronology=chron, summary=summary, calls=list(metered.records))


def _write_outputs(
    out: Path, chron: Chronology, summary: RunSummary, calls: list[CallRecord]
) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "chronology.json").write_text(chron.model_dump_json(indent=2), encoding="utf-8")
    (out / "chronology.md").write_text(to_markdown(chron), encoding="utf-8")
    (out / "chronology.csv").write_text(to_csv(chron), encoding="utf-8")
    (out / "run.json").write_text(summary.model_dump_json(indent=2), encoding="utf-8")
    with (out / "calls.jsonl").open("w", encoding="utf-8") as fh:
        for c in calls:
            fh.write(json.dumps(c.model_dump(mode="json")) + "\n")
