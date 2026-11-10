"""Relational schema (Postgres + pgvector).

A *matter* is the unit of access control, so every row carries
``matter_id``. Row-level security and the audit log are added in the
hardening step.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Computed,
    Date,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
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
    source_path: Mapped[str] = mapped_column(String(512), default="", server_default="")
    created_by: Mapped[str] = mapped_column(String(128), default="", server_default="")
    bates_prefix: Mapped[str] = mapped_column(String(16), default="", server_default="")
    index_confirmed_by: Mapped[str | None] = mapped_column(String(128))
    index_confirmed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Job(Base):
    """A pipeline run for a matter. Workers claim queued jobs with SKIP LOCKED."""

    __tablename__ = "jobs"
    __table_args__ = (Index("ix_jobs_status_created", "status", "created_at"),)

    job_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    matter_id: Mapped[str] = mapped_column(ForeignKey("matters.matter_id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(24), default="queued")  # queued|running|...
    provider: Mapped[str] = mapped_column(String(24))
    kind: Mapped[str] = mapped_column(String(16), default="pipeline", server_default="pipeline")
    budget_usd: Mapped[float | None] = mapped_column(Float)
    error: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    started_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class JobEvent(Base):
    """Progress messages streamed to the UI over server-sent events."""

    __tablename__ = "job_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.job_id", ondelete="CASCADE"), index=True)
    at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    message: Mapped[str] = mapped_column(Text)


class DocumentRow(Base):
    __tablename__ = "documents"

    matter_id: Mapped[str] = mapped_column(
        ForeignKey("matters.matter_id", ondelete="CASCADE"), primary_key=True
    )
    doc_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    doc_type: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(255))
    date: Mapped[dt.date | None] = mapped_column(Date)
    facility: Mapped[str] = mapped_column(String(255))
    provider: Mapped[str] = mapped_column(String(255))
    start_page: Mapped[int] = mapped_column(Integer)
    end_page: Mapped[int] = mapped_column(Integer)


class EventRow(Base):
    """A chronology event plus its human review state (HITL-2)."""

    __tablename__ = "events"
    __table_args__ = (Index("ix_events_matter_date", "matter_id", "date"),)

    matter_id: Mapped[str] = mapped_column(
        ForeignKey("matters.matter_id", ondelete="CASCADE"), primary_key=True
    )
    event_id: Mapped[str] = mapped_column(String(48), primary_key=True)
    date: Mapped[dt.date | None] = mapped_column(Date)
    date_text: Mapped[str] = mapped_column(String(64))
    type: Mapped[str] = mapped_column(String(32))
    summary: Mapped[str] = mapped_column(Text)
    facility: Mapped[str] = mapped_column(String(255))
    provider: Mapped[str] = mapped_column(String(255))
    doc_id: Mapped[str] = mapped_column(String(32))
    confidence: Mapped[str] = mapped_column(String(8))
    flags: Mapped[list[str]] = mapped_column(JSONB)
    sources: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    review_status: Mapped[str] = mapped_column(String(16), default="pending")
    reviewer_note: Mapped[str] = mapped_column(Text, default="")
    edited_summary: Mapped[str | None] = mapped_column(Text)
    edited_date: Mapped[dt.date | None] = mapped_column(Date)
    reviewed_by: Mapped[str | None] = mapped_column(String(128))
    reviewed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class CaseFlagRow(Base):
    __tablename__ = "case_flags"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    matter_id: Mapped[str] = mapped_column(
        ForeignKey("matters.matter_id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(48))
    page_no: Mapped[int | None] = mapped_column(Integer)
    span_id: Mapped[str | None] = mapped_column(String(128))
    note: Mapped[str] = mapped_column(Text)


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
    production: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    bates: Mapped[str] = mapped_column(String(32), default="", server_default="")


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


class IssueRow(Base):
    """An analyst issue plus its attorney triage (human checkpoint 3)."""

    __tablename__ = "issues"

    matter_id: Mapped[str] = mapped_column(
        ForeignKey("matters.matter_id", ondelete="CASCADE"), primary_key=True
    )
    issue_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    category: Mapped[str] = mapped_column(String(32))
    description: Mapped[str] = mapped_column(Text)
    event_ids: Mapped[list[str]] = mapped_column(JSONB)
    citations: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    severity_hint: Mapped[str] = mapped_column(String(8))
    verdict: Mapped[str] = mapped_column(String(16))
    verdict_reason: Mapped[str] = mapped_column(Text, default="")
    triage_status: Mapped[str] = mapped_column(String(16), default="pending")
    triage_note: Mapped[str] = mapped_column(Text, default="")
    triaged_by: Mapped[str | None] = mapped_column(String(128))
    triaged_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class MemoRow(Base):
    """Drafted memos. A memo cannot leave the system until approved (checkpoint 4)."""

    __tablename__ = "memos"

    memo_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    matter_id: Mapped[str] = mapped_column(
        ForeignKey("matters.matter_id", ondelete="CASCADE"), index=True
    )
    markdown: Mapped[str] = mapped_column(Text)
    claims: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(16), default="draft")  # draft | approved
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    approved_by: Mapped[str | None] = mapped_column(String(128))
    approved_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class ProductionRow(Base):
    """One record production (upload) for a matter. Page numbers run across productions."""

    __tablename__ = "productions"
    __table_args__ = (UniqueConstraint("matter_id", "sha256", name="uq_productions_matter_sha"),)

    matter_id: Mapped[str] = mapped_column(
        ForeignKey("matters.matter_id", ondelete="CASCADE"), primary_key=True
    )
    production_no: Mapped[int] = mapped_column(Integer, primary_key=True)
    sha256: Mapped[str] = mapped_column(String(64))
    storage_key: Mapped[str] = mapped_column(String(512), default="", server_default="")
    page_offset: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    page_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_by: Mapped[str] = mapped_column(String(128), default="", server_default="")
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ChronologyVersionRow(Base):
    """A snapshot of the chronology after a pipeline run that changed it."""

    __tablename__ = "chronology_versions"

    matter_id: Mapped[str] = mapped_column(
        ForeignKey("matters.matter_id", ondelete="CASCADE"), primary_key=True
    )
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[str | None] = mapped_column(String(36))
    productions: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    events: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)
    diff_summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # Attorney approval for export (covers this version as reviewed at approval time).
    approved_by: Mapped[str | None] = mapped_column(String(128))
    approved_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    approved_digest: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class MatterMember(Base):
    """Matter-level access (ethical walls). Only members can read a matter's records."""

    __tablename__ = "matter_members"

    matter_id: Mapped[str] = mapped_column(
        ForeignKey("matters.matter_id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    added_by: Mapped[str] = mapped_column(String(128))
    added_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class AuditRow(Base):
    """Append-only, hash-chained audit log (see medchron.security.audit)."""

    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_matter", "matter_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    at: Mapped[str] = mapped_column(String(40))
    actor: Mapped[str] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(String(64))
    matter_id: Mapped[str | None] = mapped_column(String(64))
    object_type: Mapped[str | None] = mapped_column(String(32))
    object_id: Mapped[str | None] = mapped_column(String(128))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB)
    prev_hash: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64), unique=True)
