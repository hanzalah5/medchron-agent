"""issues, memos, job kind

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-09 05:11:08.121751

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "issues",
        sa.Column("matter_id", sa.String(length=64), nullable=False),
        sa.Column("issue_id", sa.String(length=16), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("event_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("citations", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("severity_hint", sa.String(length=8), nullable=False),
        sa.Column("verdict", sa.String(length=16), nullable=False),
        sa.Column("verdict_reason", sa.Text(), nullable=False),
        sa.Column("triage_status", sa.String(length=16), nullable=False),
        sa.Column("triage_note", sa.Text(), nullable=False),
        sa.Column("triaged_by", sa.String(length=128), nullable=True),
        sa.Column("triaged_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["matter_id"], ["matters.matter_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("matter_id", "issue_id"),
    )
    op.create_table(
        "memos",
        sa.Column("memo_id", sa.String(length=36), nullable=False),
        sa.Column("matter_id", sa.String(length=64), nullable=False),
        sa.Column("markdown", sa.Text(), nullable=False),
        sa.Column("claims", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("approved_by", sa.String(length=128), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["matter_id"], ["matters.matter_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("memo_id"),
    )
    op.create_index(op.f("ix_memos_matter_id"), "memos", ["matter_id"], unique=False)
    op.add_column(
        "jobs", sa.Column("kind", sa.String(length=16), server_default="pipeline", nullable=False)
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("jobs", "kind")
    op.drop_index(op.f("ix_memos_matter_id"), table_name="memos")
    op.drop_table("memos")
    op.drop_table("issues")
