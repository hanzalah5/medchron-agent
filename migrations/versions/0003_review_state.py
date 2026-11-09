"""review state

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-09 05:00:47.056324

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("events", sa.Column("edited_summary", sa.Text(), nullable=True))
    op.add_column("events", sa.Column("edited_date", sa.Date(), nullable=True))
    op.add_column("matters", sa.Column("index_confirmed_by", sa.String(length=128), nullable=True))
    op.add_column(
        "matters", sa.Column("index_confirmed_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("matters", "index_confirmed_at")
    op.drop_column("matters", "index_confirmed_by")
    op.drop_column("events", "edited_date")
    op.drop_column("events", "edited_summary")
