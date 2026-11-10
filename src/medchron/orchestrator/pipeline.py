"""Code-owned orchestration of the case pipeline (a fixed DAG).

    ingest (per production) -> classify -> segment -> extract (fan-out per document)
        -> chronology -> version + diff -> analyze -> verify

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
- **Incremental productions.** A matter can receive more records later. Pages
  are numbered across productions, and extraction is keyed by each document's
  own content, so a second production reuses every first-production document
  and only new documents are extracted. Each run that changes the chronology
  stores a new version with a diff against the previous one.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, Field

from medchron.agents.analyst import analyze
from medchron.agents.classifier import PageLabel, classify_pages, segment
from medchron.agents.extractor import (
    ExtractionResult,
    extract_document,
    finish_extraction,
    prepare_extraction,
)
from medchron.agents.verifier import verify_issues
from medchron.chronology.builder import build_chronology
from medchron.chronology.diff import ChronologyDiff, diff_chronologies
from medchron.chronology.export import issues_markdown, to_csv, to_markdown
from medchron.chronology.models import (
    CaseFlag,
    Chronology,
    DocumentSegment,
    ExtractedEvent,
    Issue,
)
from medchron.ingestion import IngestedCase
from medchron.ingestion.identity import find_misfiled
from medchron.ingestion.models import Page
from medchron.ingestion.pipeline import assemble_case, extract_production
from medchron.llm.batch import BatchClient, custom_id, default_batch_client, wait_for
from medchron.llm.config import effort_for, model_for
from medchron.llm.loop import ToolLoop
from medchron.llm.prompts import load_prompt
from medchron.llm.providers import CallRecord, LLMProvider, MeteredProvider
from medchron.llm.types import BudgetExceededError, LLMError, LLMTransientError
from medchron.observability import span
from medchron.retrieval.embeddings import get_embedder
from medchron.retrieval.search import MemorySearch

log = logging.getLogger(__name__)
M = TypeVar("M", bound=BaseModel)


class _ExtractionCheckpoint(BaseModel):
    events: list[dict[str, object]]
    flags: list[CaseFlag]
    rejected: int
    turns: int


class _Labels(BaseModel):
    labels: list[PageLabel]


class _Pages(BaseModel):
    pages: list[Page]


class _Issues(BaseModel):
    issues: list[Issue]
    rejected: int


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
    issues: int = 0
    rejected_issues: int = 0
    seconds: float
    productions: int = 1
    reused_documents: int = 0  # extraction loaded from a checkpoint (e.g. an earlier production)


@dataclass
class RunConfig:
    out_dir: Path
    budget_usd: float | None = None
    concurrency: int = 4
    ocr: bool = True
    analyze: bool = True  # run the issue analyst and verifier after the chronology
    bates_prefix: str | None = None  # default: derived from the matter ID
    # Extraction through the Message Batches API (half price, asynchronous).
    extract_mode: str = field(
        default_factory=lambda: os.environ.get("MEDCHRON_EXTRACT_MODE", "sync")
    )
    batch_client: BatchClient | None = None  # default: real API for the live provider
    batch_poll_seconds: float = 30.0


@dataclass
class RunResult:
    chronology: Chronology
    summary: RunSummary
    case: IngestedCase
    issues: list[Issue] = field(default_factory=list)
    calls: list[CallRecord] = field(default_factory=list)
    version: int = 1
    diff: ChronologyDiff | None = None  # against the previous version, when there is one


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
    pdf: Path | Sequence[Path],
    case_id: str,
    provider: LLMProvider,
    config: RunConfig,
    *,
    progress: Callable[[str], None] | None = None,
) -> RunResult:
    with span("case.run", matter_id=case_id) as sp:
        result = _run_case(pdf, case_id, provider, config, progress=progress)
        sp.set_attribute("case.status", result.summary.status)
        sp.set_attribute("case.events", result.summary.events)
        sp.set_attribute("case.cost_usd", result.summary.cost_usd)
        return result


def _run_case(
    pdf: Path | Sequence[Path],
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

    # 1. Ingest each production (deterministic; OCR is the expensive part)
    pdfs = [pdf] if isinstance(pdf, Path) else list(pdf)
    pages: list[Page] = []
    shas: list[str] = []
    for n, path in enumerate(pdfs, start=1):
        data = path.read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        shas.append(sha)
        p_key = cp.key(sha, case_id, f"ocr={config.ocr}", f"production={n}", f"offset={len(pages)}")
        extracted = cp.load("ingest", p_key, _Pages)
        if extracted is None:
            say(f"ingest: production {n}: OCR and text extraction")
            extracted = _Pages(
                pages=extract_production(data, production=n, page_offset=len(pages), ocr=config.ocr)
            )
            cp.save("ingest", p_key, extracted)
        pages += extracted.pages
    case_key = cp.key(*shas, case_id, f"ocr={config.ocr}")
    # Cross-production de-duplication, Bates numbering and chunking.
    case = assemble_case(case_id, pages, shas, bates_prefix=config.bates_prefix)
    cp.save("case", case_key, case)
    review_flags = [
        CaseFlag(kind="page_needs_review", page_no=p.page_no, note=f"text source={p.source.value}")
        for p in case.pages
        if p.needs_review and p.duplicate_of is None
    ]
    misfiled = set(find_misfiled(case.pages))
    review_flags += [
        CaseFlag(
            kind="possible_misfiled_page",
            page_no=n,
            note="The patient name or date of birth in this page's header differs from the "
            "rest of the records. It was left out of extraction; review it before relying on "
            "or producing it.",
        )
        for n in sorted(misfiled)
    ]

    # 2. Classify pages -> 3. segment into documents (per production: a document never
    # spans two uploads)
    cls_settings = _settings("classify", "system_base", "classify_pages")
    documents: list[DocumentSegment] = []
    for n, sha in enumerate(shas, start=1):
        prod_pages = [p for p in case.pages if p.production == n]
        offset = prod_pages[0].page_no - 1 if prod_pages else 0
        dup_sig = ",".join(f"{p.page_no}:{p.duplicate_of or 0}" for p in prod_pages)
        cls_key = cp.key(sha, case_id, f"offset={offset}", dup_sig, cls_settings)
        prod_labels = cp.load("classify", cls_key, _Labels)
        if prod_labels is None:
            say(f"classify: indexing production {n} into documents")
            view = case.model_copy(update={"pages": prod_pages})
            prod_labels = _Labels(labels=classify_pages(llm, view))
            cp.save("classify", cls_key, prod_labels)
        documents += segment(prod_labels.labels)

    # 4. Extract events per document (fan-out with bounded concurrency)
    ext_settings = _settings("extract", "system_base", "extract_events")
    results: dict[str, ExtractionResult] = {}
    failed: list[str] = []
    flags: list[CaseFlag] = list(review_flags)

    reused: list[str] = []  # list.append is thread-safe

    def doc_key(doc: DocumentSegment) -> str:
        """Keyed by the document's own content, so later productions reuse it."""
        spans = [s for s in case.spans if doc.start_page <= s.page_no <= doc.end_page]
        content = hashlib.sha256(
            "\x1e".join(f"{s.span_id}\x1f{s.text}" for s in spans).encode()
        ).hexdigest()
        skipped = ",".join(str(p) for p in sorted(misfiled) if doc.start_page <= p <= doc.end_page)
        return cp.key(case_id, doc.model_dump_json(), content, f"skip={skipped}", ext_settings)

    def from_checkpoint(doc: DocumentSegment) -> ExtractionResult | None:
        hit = cp.load("extract", doc_key(doc), _ExtractionCheckpoint)
        if hit is None:
            return None
        reused.append(doc.doc_id)
        return ExtractionResult(
            events=[ExtractedEvent.model_validate(e) for e in hit.events],
            flags=hit.flags,
            rejected=hit.rejected,
            turns=hit.turns,
        )

    def store(doc: DocumentSegment, res: ExtractionResult) -> None:
        cp.save(
            "extract",
            doc_key(doc),
            _ExtractionCheckpoint(
                events=[e.model_dump(mode="json") for e in res.events],
                flags=res.flags,
                rejected=res.rejected,
                turns=res.turns,
            ),
        )

    def failure(doc: DocumentSegment, e: Exception) -> None:
        nonlocal status
        failed.append(doc.doc_id)
        if isinstance(e, BudgetExceededError):
            status = "budget_exceeded"
            return
        log.warning("extraction failed for %s: %s", doc.doc_id, e)
        flags.append(
            CaseFlag(
                kind="extraction_failed",
                page_no=doc.start_page,
                note=f"{doc.doc_id} ({doc.doc_type.value}): {type(e).__name__}. "
                "Needs manual review.",
            )
        )

    def work(doc: DocumentSegment) -> tuple[str, ExtractionResult]:
        hit = from_checkpoint(doc)
        if hit is not None:
            return doc.doc_id, hit
        res = extract_document(llm, case, doc, exclude_pages=misfiled)
        store(doc, res)
        return doc.doc_id, res

    if config.extract_mode == "batch":
        client = config.batch_client or default_batch_client(provider)
        say(f"extract: {len(documents)} documents through the Message Batches API ({client.name})")
        for doc_id, outcome in _extract_in_batches(
            client, metered, case, documents, misfiled, cp, from_checkpoint, say, config
        ).items():
            doc = next(d for d in documents if d.doc_id == doc_id)
            if isinstance(outcome, ExtractionResult):
                results[doc_id] = outcome
                store(doc, outcome)
            else:
                failure(doc, outcome)
    else:
        say(f"extract: {len(documents)} documents, concurrency {config.concurrency}")
        with ThreadPoolExecutor(max_workers=config.concurrency) as pool:
            futures = {pool.submit(work, d): d for d in documents}
            for fut in as_completed(futures):
                doc = futures[fut]
                try:
                    doc_id, res = fut.result()
                    results[doc_id] = res
                except (LLMError, LookupError) as e:  # LookupError covers replay misses
                    failure(doc, e)
    if failed and status == "completed":
        status = "completed_with_errors"

    # 5. Chronology (deterministic)
    events = [e for d in documents if d.doc_id in results for e in results[d.doc_id].events]
    flags += [f for d in documents if d.doc_id in results for f in results[d.doc_id].flags]
    chron = build_chronology(case_id, documents, events, flags)
    version, diff = _record_version(config.out_dir, chron)
    if diff is not None and not diff.is_empty:
        say(
            f"version {version}: {len(diff.added)} events added, {len(diff.changed)} changed, "
            f"{len(diff.removed)} removed"
        )

    # 6. Issue analyst (agent loop) -> 7. verifier (independent support check)
    issues: list[Issue] = []
    rejected_issues = 0
    if config.analyze and status != "budget_exceeded":
        an_key = cp.key(
            case_key,
            chron.model_dump_json(),
            _settings("analyze", "system_base", "analyze_issues"),
            _settings("verify", "system_base", "verify_claims"),
        )
        cached = cp.load("analyze", an_key, _Issues)
        if cached is None:
            try:
                say("analyze: investigating the chronology for issues")
                found, rejected_issues = analyze(
                    metered, case, chron, MemorySearch(case, get_embedder())
                )
                say(f"verify: checking {len(found)} issues against their evidence")
                cached = _Issues(
                    issues=verify_issues(metered, case, found), rejected=rejected_issues
                )
                cp.save("analyze", an_key, cached)
            except BudgetExceededError:
                status = "budget_exceeded"
            except (LLMError, LookupError) as e:
                log.warning("issue analysis failed: %s", e)
                status = "completed_with_errors"
                chron.flags.append(
                    CaseFlag(
                        kind="analysis_failed", note=f"Issue analysis failed: {type(e).__name__}"
                    )
                )
        if cached is not None:
            issues, rejected_issues = cached.issues, cached.rejected

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
        productions=len(shas),
        reused_documents=len(reused),
        issues=len(issues),
        rejected_issues=rejected_issues,
        seconds=round(time.perf_counter() - start, 2),
    )
    _write_outputs(config.out_dir, chron, issues, summary, metered.records)
    say(
        f"done: {summary.events} events, {summary.issues} issues, status={summary.status}, "
        f"cost=${summary.cost_usd:.4f}"
    )
    return RunResult(
        chronology=chron,
        summary=summary,
        case=case,
        issues=issues,
        calls=list(metered.records),
        version=version,
        diff=diff,
    )


class _BatchRef(BaseModel):
    batch_id: str
    client: str


def _extract_in_batches(
    client: BatchClient,
    metered: MeteredProvider,
    case: IngestedCase,
    documents: list[DocumentSegment],
    misfiled: set[int],
    cp: Checkpoints,
    cached: Callable[[DocumentSegment], ExtractionResult | None],
    say: Callable[[str], None],
    config: RunConfig,
) -> dict[str, ExtractionResult | Exception]:
    """Run every document's extraction loop in rounds, one batch per tool-loop turn.

    Each round sends exactly the request the synchronous loop would send next.
    The batch ID is checkpointed under a key derived from those requests, so a
    crash while waiting resumes polling the same batch instead of paying for a
    new one. A document whose batch request is refused or fails falls back to
    the synchronous path, which has server-side fallbacks.
    """
    out: dict[str, ExtractionResult | Exception] = {}
    active: dict[str, tuple[DocumentSegment, Any, ToolLoop]] = {}
    for doc in documents:
        hit = cached(doc)
        if hit is not None:
            out[doc.doc_id] = hit
            continue
        prepared = prepare_extraction(case, doc, exclude_pages=misfiled)
        if isinstance(prepared, ExtractionResult):
            out[doc.doc_id] = prepared
        else:
            active[doc.doc_id] = (doc, prepared[0], prepared[1])

    def synchronously(doc: DocumentSegment) -> None:
        try:
            out[doc.doc_id] = extract_document(metered, case, doc, exclude_pages=misfiled)
        except (LLMError, LookupError) as e:
            out[doc.doc_id] = e

    rounds = 0
    while active:
        rounds += 1
        try:
            metered.check_budget()
        except BudgetExceededError as e:
            for doc_id in active:
                out[doc_id] = e
            break
        items = [
            (custom_id(case.case_id, doc_id, str(loop.turn)), loop.next_request())
            for doc_id, (_, _, loop) in active.items()
        ]
        ids = list(active)
        key = cp.key(client.name, *(f"{cid}:{req.fingerprint()}" for cid, req in items))
        ref = cp.load("extract-batch", key, _BatchRef)
        if ref is None:
            ref = _BatchRef(batch_id=client.submit(items), client=client.name)
            cp.save("extract-batch", key, ref)
            say(f"extract: batch round {rounds}: {len(items)} document(s) submitted")
        else:
            say(f"extract: batch round {rounds}: resuming batch {ref.batch_id}")
        wait_for(client, ref.batch_id, poll_seconds=config.batch_poll_seconds, progress=say)
        answers = client.results(ref.batch_id)
        for doc_id, (cid, req) in zip(ids, items, strict=True):
            doc, recorder, loop = active[doc_id]
            answer = answers.get(cid, LLMTransientError(f"no batch result for {cid}"))
            try:
                if isinstance(answer, Exception):
                    raise answer
                metered.record(req, answer, batch=True)
                finished = loop.apply(answer)
            except LLMError as e:
                log.info("batch request for %s failed (%s); retrying synchronously", doc_id, e)
                del active[doc_id]
                synchronously(doc)
                continue
            if finished is not None:
                out[doc_id] = finish_extraction(recorder, finished)
                del active[doc_id]
    return out


def _record_version(out: Path, chron: Chronology) -> tuple[int, ChronologyDiff | None]:
    """Store the chronology as a new version when it differs from the latest one.

    Returns the version number and the diff against the previous version (None
    for the first version). Re-running with an unchanged result keeps the
    latest version and its diff.
    """
    folder = out / "versions"
    folder.mkdir(parents=True, exist_ok=True)
    existing = sorted(int(p.stem[1:]) for p in folder.glob("v*.json") if p.stem[1:].isdigit())
    latest = (
        Chronology.model_validate_json((folder / f"v{existing[-1]}.json").read_text("utf-8"))
        if existing
        else None
    )
    if latest is not None and latest.model_dump_json() == chron.model_dump_json():
        diff_path = folder / f"diff-v{existing[-1]}.json"
        prior = (
            ChronologyDiff.model_validate_json(diff_path.read_text("utf-8"))
            if diff_path.exists()
            else None
        )
        return existing[-1], prior
    version = existing[-1] + 1 if existing else 1
    (folder / f"v{version}.json").write_text(chron.model_dump_json(indent=2), encoding="utf-8")
    if latest is None:
        return version, None
    diff = diff_chronologies(latest, chron, from_version=version - 1, to_version=version)
    (folder / f"diff-v{version}.json").write_text(diff.model_dump_json(indent=2), "utf-8")
    return version, diff


def _write_outputs(
    out: Path, chron: Chronology, issues: list[Issue], summary: RunSummary, calls: list[CallRecord]
) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "chronology.json").write_text(chron.model_dump_json(indent=2), encoding="utf-8")
    (out / "issues.json").write_text(
        json.dumps([i.model_dump(mode="json") for i in issues], indent=2), encoding="utf-8"
    )
    (out / "chronology.md").write_text(
        issues_markdown(to_markdown(chron), issues), encoding="utf-8"
    )
    (out / "chronology.csv").write_text(to_csv(chron), encoding="utf-8")
    (out / "run.json").write_text(summary.model_dump_json(indent=2), encoding="utf-8")
    with (out / "calls.jsonl").open("w", encoding="utf-8") as fh:
        for c in calls:
            fh.write(json.dumps(c.model_dump(mode="json")) + "\n")
