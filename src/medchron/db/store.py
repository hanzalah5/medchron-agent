"""Persistence helpers for ingested matters."""

from __future__ import annotations

import os

from sqlalchemy import create_engine, delete, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from medchron.db.models import Matter, PageRow, SpanRow
from medchron.ingestion.models import IngestedCase, Page, Span
from medchron.retrieval.embeddings import Embedder

DEFAULT_URL = "postgresql+psycopg://medchron:medchron@localhost:5432/medchron"


def make_engine(url: str | None = None) -> Engine:
    return create_engine(url or os.environ.get("DATABASE_URL", DEFAULT_URL), pool_pre_ping=True)


def save_ingested(
    session: Session, case: IngestedCase, embedder: Embedder, *, title: str = ""
) -> None:
    """Insert or replace a matter's pages and spans. Idempotent per matter."""
    session.execute(delete(Matter).where(Matter.matter_id == case.case_id))
    session.flush()
    session.add(Matter(matter_id=case.case_id, title=title, source_sha256=case.file_sha256))
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
