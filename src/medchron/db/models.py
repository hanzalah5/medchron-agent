"""Relational schema (Postgres + pgvector).

The schema is kept deliberately small at this step. Later steps add events,
issues, reviews, workflow runs and the audit log. A *matter* is the unit of
access control, so every row carries ``matter_id``. Row-level security is
added in the hardening step.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Computed,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from medchron.retrieval.embeddings import DIM


class Base(DeclarativeBase):
    pass


class Matter(Base):
    __tablename__ = "matters"

    matter_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(String(255), default="")
    source_sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class PageRow(Base):
    __tablename__ = "pages"

    matter_id: Mapped[str] = mapped_column(
        ForeignKey("matters.matter_id", ondelete="CASCADE"), primary_key=True
    )
    page_no: Mapped[int] = mapped_column(Integer, primary_key=True)
    text: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(16))
    ocr_confidence: Mapped[float | None] = mapped_column(Float)
    width: Mapped[float] = mapped_column(Float)
    height: Mapped[float] = mapped_column(Float)
    content_hash: Mapped[str] = mapped_column(String(64))
    duplicate_of: Mapped[int | None] = mapped_column(Integer)
    words: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)


class SpanRow(Base):
    __tablename__ = "spans"
    __table_args__ = (
        ForeignKeyConstraint(
            ["matter_id", "page_no"],
            ["pages.matter_id", "pages.page_no"],
            ondelete="CASCADE",
        ),
        Index("ix_spans_matter_page", "matter_id", "page_no"),
        Index("ix_spans_tsv", "tsv", postgresql_using="gin"),
        Index(
            "ix_spans_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    span_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    matter_id: Mapped[str] = mapped_column(String(64))
    page_no: Mapped[int] = mapped_column(Integer)
    char_start: Mapped[int] = mapped_column(Integer)
    char_end: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    tsv: Mapped[str] = mapped_column(
        TSVECTOR, Computed("to_tsvector('english', text)", persisted=True)
    )
    embedding: Mapped[list[float]] = mapped_column(Vector(DIM))
    embedder: Mapped[str] = mapped_column(String(64))
