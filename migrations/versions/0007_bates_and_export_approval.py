"""Bates-style page IDs and attorney approval of chronology exports

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-10 02:00:00.000000

Pages get a Bates-style ID (matter prefix plus page number, stable across
productions). A chronology version can be approved by an attorney for export;
the approval records a digest of the reviewed chronology, so any later review
edit requires approval again.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "matters", sa.Column("bates_prefix", sa.String(16), nullable=False, server_default="")
    )
    op.add_column("pages", sa.Column("bates", sa.String(32), nullable=False, server_default=""))
    op.add_column("chronology_versions", sa.Column("approved_by", sa.String(128)))
    op.add_column("chronology_versions", sa.Column("approved_at", sa.DateTime(timezone=True)))
    op.add_column("chronology_versions", sa.Column("approved_digest", sa.String(64)))
    # Existing pages: derive the default prefix (letters and digits of the matter ID).
    op.execute("SELECT set_config('medchron.system', 'on', true)")
    op.execute(
        """
        UPDATE matters
        SET bates_prefix = coalesce(
            nullif(left(regexp_replace(upper(matter_id), '[^A-Z0-9]', '', 'g'), 12), ''), 'MC')
        WHERE bates_prefix = ''
        """
    )
    op.execute(
        """
        UPDATE pages p
        SET bates = m.bates_prefix || '-' || lpad(p.page_no::text, 6, '0')
        FROM matters m
        WHERE m.matter_id = p.matter_id AND p.bates = ''
        """
    )


def downgrade() -> None:
    for col in ("approved_digest", "approved_at", "approved_by"):
        op.drop_column("chronology_versions", col)
    op.drop_column("pages", "bates")
    op.drop_column("matters", "bates_prefix")
