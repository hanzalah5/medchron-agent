"""Persistence helpers: ingested pages and spans, and chronology results."""

from __future__ import annotations

import os

from sqlalchemy import create_engine, delete, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from medchron.chronology.builder import find_conflicts, find_followup_gaps
from medchron.chronology.models import (
    CaseFlag,
    Chronology,
    Citation,
    Confidence,
    DocumentSegment,
    EventSource,
    ExtractedEvent,
    Issue,
)
from medchron.db.models import (
    CaseFlagRow,
    DocumentRow,
    EventRow,
    IssueRow,
    Matter,
    PageRow,
    SpanRow,
)
from medchron.ingestion.models import IngestedCase, Page, Span
from medchron.retrieval.embeddings import Embedder
from medchron.taxonomy import DocType, EventType

DEFAULT_URL = "postgresql+psycopg://medchron:medchron@localhost:5432/medchron"


def make_engine(url: str | None = None) -> Engine:
    return create_engine(url or os.environ.get("DATABASE_URL", DEFAULT_URL), pool_pre_ping=True)


def save_ingested(
    session: Session, case: IngestedCase, embedder: Embedder, *, title: str = ""
) -> None:
    """Insert or replace a matter's pages and spans. Idempotent per matter.

    The matter row itself is upserted, not deleted, so jobs and other rows that
    reference it survive a re-ingest.
    """
    matter = session.get(Matter, case.case_id)
    if matter is None:
        session.add(Matter(matter_id=case.case_id, title=title, source_sha256=case.file_sha256))
    else:
        matter.source_sha256 = case.file_sha256
        matter.title = title or matter.title
        session.execute(delete(PageRow).where(PageRow.matter_id == case.case_id))
    session.flush()
    session.add_all(
        PageRow(
            matter_id=case.case_id,
            page_no=p.page_no,
            text=p.text,
            source=p.source.value,
            ocr_confidence=p.ocr_confidence,
            width=p.width,
            height=p.height,
            content_hash=p.content_hash,
            duplicate_of=p.duplicate_of,
            words=[w.model_dump() for w in p.words],
        )
        for p in case.pages
    )
    session.flush()
    vectors = embedder.embed([s.text for s in case.spans]) if case.spans else []
    session.add_all(
        SpanRow(
            span_id=s.span_id,
            matter_id=s.case_id,
            page_no=s.page_no,
            char_start=s.char_start,
            char_end=s.char_end,
            text=s.text,
            embedding=v,
            embedder=embedder.name,
        )
        for s, v in zip(case.spans, vectors, strict=True)
    )
    session.flush()


def load_ingested(session: Session, matter_id: str) -> IngestedCase:
    matter = session.get(Matter, matter_id)
    if matter is None:
        raise KeyError(matter_id)
    pages = session.scalars(
        select(PageRow).where(PageRow.matter_id == matter_id).order_by(PageRow.page_no)
    ).all()
    spans = session.scalars(
        select(SpanRow)
        .where(SpanRow.matter_id == matter_id)
        .order_by(SpanRow.page_no, SpanRow.char_start)
    ).all()
    return IngestedCase(
        case_id=matter_id,
        file_sha256=matter.source_sha256,
        pages=[
            Page.model_validate(
                {
                    "page_no": p.page_no,
                    "text": p.text,
                    "words": p.words,
                    "source": p.source,
                    "ocr_confidence": p.ocr_confidence,
                    "width": p.width,
                    "height": p.height,
                    "content_hash": p.content_hash,
                    "duplicate_of": p.duplicate_of,
                }
            )
            for p in pages
        ],
        spans=[
            Span(
                span_id=s.span_id,
                case_id=s.matter_id,
                page_no=s.page_no,
                char_start=s.char_start,
                char_end=s.char_end,
                text=s.text,
            )
            for s in spans
        ],
    )


def save_chronology(session: Session, chron: Chronology) -> None:
    """Replace a matter's documents, events and flags with a new pipeline result."""
    m = chron.case_id
    for table in (DocumentRow, EventRow, CaseFlagRow):
        session.execute(delete(table).where(table.matter_id == m))
    session.add_all(
        DocumentRow(matter_id=m, **d.model_dump(mode="python")) for d in chron.documents
    )
    session.add_all(
        EventRow(
            matter_id=m,
            event_id=e.event_id,
            date=e.date,
            date_text=e.date_text,
            type=e.type.value,
            summary=e.summary,
            facility=e.facility,
            provider=e.provider,
            doc_id=e.doc_id,
            confidence=e.confidence.value,
            flags=e.flags,
            sources=[s.model_dump() for s in e.sources],
        )
        for e in chron.events
    )
    session.add_all(CaseFlagRow(matter_id=m, **f.model_dump()) for f in chron.flags)
    session.flush()


def load_chronology(session: Session, matter_id: str) -> Chronology:
    docs = session.scalars(
        select(DocumentRow)
        .where(DocumentRow.matter_id == matter_id)
        .order_by(DocumentRow.start_page)
    ).all()
    events = session.scalars(
        select(EventRow)
        .where(EventRow.matter_id == matter_id)
        .order_by(EventRow.date, EventRow.event_id)
    ).all()
    flags = session.scalars(select(CaseFlagRow).where(CaseFlagRow.matter_id == matter_id)).all()
    chron = Chronology(
        case_id=matter_id,
        documents=[
            DocumentSegment(
                doc_id=d.doc_id,
                doc_type=DocType(d.doc_type),
                title=d.title,
                date=d.date,
                facility=d.facility,
                provider=d.provider,
                start_page=d.start_page,
                end_page=d.end_page,
            )
            for d in docs
        ],
        events=[
            ExtractedEvent(
                event_id=e.event_id,
                date=e.date,
                date_text=e.date_text,
                type=EventType(e.type),
                summary=e.summary,
                facility=e.facility,
                provider=e.provider,
                doc_id=e.doc_id,
                sources=[EventSource.model_validate(s) for s in e.sources],
                confidence=Confidence(e.confidence),
                flags=list(e.flags),
            )
            for e in events
        ],
        flags=[
            CaseFlag(kind=f.kind, page_no=f.page_no, span_id=f.span_id, note=f.note) for f in flags
        ],
    )
    # Conflicts and gaps are derived signals, so recompute them instead of storing them.
    chron.conflicts = find_conflicts(chron.events)
    chron.gaps = find_followup_gaps(chron.events)
    return chron


def save_issues(session: Session, matter_id: str, issues: list[Issue]) -> None:
    """Replace the analyst's issues for a matter (triage starts again from pending)."""
    session.execute(delete(IssueRow).where(IssueRow.matter_id == matter_id))
    session.add_all(
        IssueRow(
            matter_id=matter_id,
            issue_id=i.issue_id,
            category=i.category,
            description=i.description,
            event_ids=i.event_ids,
            citations=[c.model_dump() for c in i.citations],
            severity_hint=i.severity_hint,
            verdict=i.verdict,
            verdict_reason=i.verdict_reason,
        )
        for i in issues
    )
    session.flush()


def issue_from_row(r: IssueRow) -> Issue:
    return Issue(
        issue_id=r.issue_id,
        category=r.category,
        description=r.description,
        event_ids=list(r.event_ids),
        citations=[Citation.model_validate(c) for c in r.citations],
        severity_hint=r.severity_hint,
        verdict=r.verdict,
        verdict_reason=r.verdict_reason,
    )


def reviewed_chronology(session: Session, matter_id: str) -> Chronology:
    """The chronology as the reviewer left it: edits applied, rejected events removed."""
    chron = load_chronology(session, matter_id)
    reviews = {
        r.event_id: r
        for r in session.scalars(select(EventRow).where(EventRow.matter_id == matter_id))
    }
    kept = []
    for e in chron.events:
        r = reviews.get(e.event_id)
        if r is None or r.review_status == "rejected":
            continue
        update: dict[str, object] = {"flags": [*e.flags, f"review:{r.review_status}"]}
        if r.edited_summary:
            update["summary"] = r.edited_summary
        if r.edited_date:
            update["date"] = r.edited_date
        kept.append(e.model_copy(update=update))
    chron.events = kept
    return chron
