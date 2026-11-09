"""jobs, documents, events, flags

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-09 04:58:05.259385

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "case_flags",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("matter_id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=48), nullable=False),
        sa.Column("page_no", sa.Integer(), nullable=True),
        sa.Column("span_id", sa.String(length=128), nullable=True),
        sa.Column("note", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["matter_id"], ["matters.matter_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_case_flags_matter_id"), "case_flags", ["matter_id"], unique=False)
    op.create_table(
        "documents",
        sa.Column("matter_id", sa.String(length=64), nullable=False),
        sa.Column("doc_id", sa.String(length=32), nullable=False),
        sa.Column("doc_type", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("date", sa.Date(), nullable=True),
        sa.Column("facility", sa.String(length=255), nullable=False),
        sa.Column("provider", sa.String(length=255), nullable=False),
        sa.Column("start_page", sa.Integer(), nullable=False),
        sa.Column("end_page", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["matter_id"], ["matters.matter_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("matter_id", "doc_id"),
    )
    op.create_table(
        "events",
        sa.Column("matter_id", sa.String(length=64), nullable=False),
        sa.Column("event_id", sa.String(length=48), nullable=False),
        sa.Column("date", sa.Date(), nullable=True),
        sa.Column("date_text", sa.String(length=64), nullable=False),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("facility", sa.String(length=255), nullable=False),
        sa.Column("provider", sa.String(length=255), nullable=False),
        sa.Column("doc_id", sa.String(length=32), nullable=False),
        sa.Column("confidence", sa.String(length=8), nullable=False),
        sa.Column("flags", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("sources", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("review_status", sa.String(length=16), nullable=False),
        sa.Column("reviewer_note", sa.Text(), nullable=False),
        sa.Column("reviewed_by", sa.String(length=128), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["matter_id"], ["matters.matter_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("matter_id", "event_id"),
    )
    op.create_index("ix_events_matter_date", "events", ["matter_id", "date"], unique=False)
    op.create_table(
        "jobs",
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column("matter_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("provider", sa.String(length=24), nullable=False),
        sa.Column("budget_usd", sa.Float(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("summary", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["matter_id"], ["matters.matter_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("job_id"),
    )
    op.create_index("ix_jobs_status_created", "jobs", ["status", "created_at"], unique=False)
    op.create_table(
        "job_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column(
            "at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("message", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.job_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_job_events_job_id"), "job_events", ["job_id"], unique=False)
    op.add_column(
        "matters",
        sa.Column("source_path", sa.String(length=512), server_default="", nullable=False),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("matters", "source_path")
    op.drop_index(op.f("ix_job_events_job_id"), table_name="job_events")
    op.drop_table("job_events")
    op.drop_index("ix_jobs_status_created", table_name="jobs")
    op.drop_table("jobs")
    op.drop_index("ix_events_matter_date", table_name="events")
    op.drop_table("events")
    op.drop_table("documents")
    op.drop_index(op.f("ix_case_flags_matter_id"), table_name="case_flags")
    op.drop_table("case_flags")
