"""initial schema: matters, pages, spans

Revision ID: 0001
Revises:
Create Date: 2026-11-09 04:37:09.863531

"""

from collections.abc import Sequence

import pgvector.sqlalchemy
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "matters",
        sa.Column("matter_id", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("source_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("matter_id"),
    )
    op.create_table(
        "pages",
        sa.Column("matter_id", sa.String(length=64), nullable=False),
        sa.Column("page_no", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("ocr_confidence", sa.Float(), nullable=True),
        sa.Column("width", sa.Float(), nullable=False),
        sa.Column("height", sa.Float(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("duplicate_of", sa.Integer(), nullable=True),
        sa.Column("words", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(["matter_id"], ["matters.matter_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("matter_id", "page_no"),
    )
    op.create_table(
        "spans",
        sa.Column("span_id", sa.String(length=128), nullable=False),
        sa.Column("matter_id", sa.String(length=64), nullable=False),
        sa.Column("page_no", sa.Integer(), nullable=False),
        sa.Column("char_start", sa.Integer(), nullable=False),
        sa.Column("char_end", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column(
            "tsv",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('english', text)", persisted=True),
            nullable=False,
        ),
        sa.Column("embedding", pgvector.sqlalchemy.vector.VECTOR(dim=384), nullable=False),
        sa.Column("embedder", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(
            ["matter_id", "page_no"], ["pages.matter_id", "pages.page_no"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("span_id"),
    )
    op.create_index(
        "ix_spans_embedding_hnsw",
        "spans",
        ["embedding"],
        unique=False,
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
    op.create_index("ix_spans_matter_page", "spans", ["matter_id", "page_no"], unique=False)
    op.create_index("ix_spans_tsv", "spans", ["tsv"], unique=False, postgresql_using="gin")


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_spans_tsv", table_name="spans", postgresql_using="gin")
    op.drop_index("ix_spans_matter_page", table_name="spans")
    op.drop_index(
        "ix_spans_embedding_hnsw",
        table_name="spans",
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
    op.drop_table("spans")
    op.drop_table("pages")
    op.drop_table("matters")
