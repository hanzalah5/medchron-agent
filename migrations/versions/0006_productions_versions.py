"""incremental productions and chronology versions

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-10 01:00:00.000000

A matter can now receive several record productions over time. Each is a row
in ``productions``. Pages carry the production they arrived in, and page
numbers run across productions. Every pipeline run that changes the chronology
stores a snapshot in ``chronology_versions``, so reviewers can see what changed.
Both new tables hold matter data and get the same row-level security as the rest.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW_TABLES = ["productions", "chronology_versions"]


def upgrade() -> None:
    op.create_table(
        "productions",
        sa.Column(
            "matter_id",
            sa.String(64),
            sa.ForeignKey("matters.matter_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("production_no", sa.Integer, primary_key=True),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("storage_key", sa.String(512), nullable=False, server_default=""),
        sa.Column("page_offset", sa.Integer, nullable=False, server_default="0"),
        sa.Column("page_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_by", sa.String(128), nullable=False, server_default=""),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("matter_id", "sha256", name="uq_productions_matter_sha"),
    )
    op.create_table(
        "chronology_versions",
        sa.Column(
            "matter_id",
            sa.String(64),
            sa.ForeignKey("matters.matter_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("version", sa.Integer, primary_key=True),
        sa.Column("job_id", sa.String(36)),
        sa.Column("productions", sa.Integer, nullable=False, server_default="1"),
        sa.Column("events", sa.Integer, nullable=False, server_default="0"),
        sa.Column("snapshot", postgresql.JSONB, nullable=False),
        sa.Column("diff_summary", postgresql.JSONB),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.add_column("pages", sa.Column("production", sa.Integer, nullable=False, server_default="1"))
    for table in NEW_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY matter_isolation ON {table} "
            "USING (medchron_can_access(matter_id)) WITH CHECK (medchron_can_access(matter_id))"
        )
    # Every existing matter had exactly one production: its original upload.
    op.execute("SELECT set_config('medchron.system', 'on', true)")
    op.execute(
        """
        INSERT INTO productions (matter_id, production_no, sha256, storage_key, page_offset,
                                 page_count, created_by, created_at)
        SELECT m.matter_id, 1, m.source_sha256, m.source_path, 0,
               (SELECT count(*) FROM pages p WHERE p.matter_id = m.matter_id),
               m.created_by, m.created_at
        FROM matters m
        """
    )


def downgrade() -> None:
    op.drop_column("pages", "production")
    for table in reversed(NEW_TABLES):
        op.drop_table(table)
