"""access control, audit log, job heartbeat

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-09 05:19:11.013787

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Tables holding matter data. Rows are visible only to members of the matter,
# or to trusted backend processes (workers, migrations) that set medchron.system.
MATTER_TABLES = [
    "pages", "spans", "documents", "events", "case_flags", "issues", "memos", "jobs",
]  # fmt: skip

ACCESS_FN = """
CREATE OR REPLACE FUNCTION medchron_can_access(m text) RETURNS boolean
LANGUAGE sql STABLE AS $$
  SELECT coalesce(current_setting('medchron.system', true), '') = 'on'
      OR EXISTS (
           SELECT 1 FROM matter_members mm
           WHERE mm.matter_id = m
             AND mm.user_id = coalesce(current_setting('medchron.user', true), '')
         )
$$;
"""


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("at", sa.String(length=40), nullable=False),
        sa.Column("actor", sa.String(length=128), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("matter_id", sa.String(length=64), nullable=True),
        sa.Column("object_type", sa.String(length=32), nullable=True),
        sa.Column("object_id", sa.String(length=128), nullable=True),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("prev_hash", sa.String(length=64), nullable=False),
        sa.Column("hash", sa.String(length=64), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("hash"),
    )
    op.create_index("ix_audit_matter", "audit_log", ["matter_id"], unique=False)
    op.create_table(
        "matter_members",
        sa.Column("matter_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=128), nullable=False),
        sa.Column("added_by", sa.String(length=128), nullable=False),
        sa.Column(
            "added_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["matter_id"], ["matters.matter_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("matter_id", "user_id"),
    )
    op.add_column("jobs", sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("jobs", sa.Column("attempts", sa.Integer(), server_default="0", nullable=False))
    op.add_column(
        "matters",
        sa.Column("created_by", sa.String(length=128), server_default="", nullable=False),
    )

    _enable_rls()


EXISTS_FN = """
CREATE OR REPLACE FUNCTION medchron_matter_exists(m text) RETURNS boolean
LANGUAGE plpgsql AS $$
DECLARE
  prev text := coalesce(current_setting('medchron.system', true), '');
  found boolean;
BEGIN
  PERFORM set_config('medchron.system', 'on', true);
  SELECT EXISTS (SELECT 1 FROM matters WHERE matter_id = m) INTO found;
  PERFORM set_config('medchron.system', prev, true);  -- restore before returning
  RETURN found;
END
$$;
"""


def _enable_rls() -> None:
    op.execute(ACCESS_FN)
    # Lets the API answer "does this ID exist?" (for 409 on create) without exposing rows.
    op.execute(EXISTS_FN)
    for t in MATTER_TABLES:
        op.execute(f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY matter_isolation ON {t} "
            "USING (medchron_can_access(matter_id)) WITH CHECK (medchron_can_access(matter_id))"
        )
    # matters: any authenticated user may create one (they become its first member);
    # reading, updating and deleting require membership.
    op.execute("ALTER TABLE matters ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE matters FORCE ROW LEVEL SECURITY")
    # The creator can read the row it just inserted (INSERT ... RETURNING) before the
    # membership row exists in the same transaction.
    op.execute(
        "CREATE POLICY matter_read ON matters FOR SELECT USING (medchron_can_access(matter_id) "
        "OR created_by = coalesce(current_setting('medchron.user', true), ''))"
    )
    op.execute(
        "CREATE POLICY matter_create ON matters FOR INSERT WITH CHECK ("
        "coalesce(current_setting('medchron.user', true), '') <> '' "
        "OR coalesce(current_setting('medchron.system', true), '') = 'on')"
    )
    op.execute(
        "CREATE POLICY matter_update ON matters FOR UPDATE USING (medchron_can_access(matter_id))"
    )
    op.execute(
        "CREATE POLICY matter_delete ON matters FOR DELETE USING (medchron_can_access(matter_id))"
    )
    # The audit log is append-only: no UPDATE or DELETE through the application role.
    op.execute(
        "CREATE OR REPLACE FUNCTION medchron_audit_immutable() RETURNS trigger LANGUAGE plpgsql AS $$ "
        "BEGIN RAISE EXCEPTION 'audit_log is append-only'; END $$"
    )
    op.execute(
        "CREATE TRIGGER audit_log_immutable BEFORE UPDATE OR DELETE ON audit_log "
        "FOR EACH ROW EXECUTE FUNCTION medchron_audit_immutable()"
    )


def _disable_rls() -> None:
    op.execute("DROP TRIGGER IF EXISTS audit_log_immutable ON audit_log")
    op.execute("DROP FUNCTION IF EXISTS medchron_audit_immutable()")
    for name in ("matter_read", "matter_create", "matter_update", "matter_delete"):
        op.execute(f"DROP POLICY IF EXISTS {name} ON matters")
    op.execute("ALTER TABLE matters NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE matters DISABLE ROW LEVEL SECURITY")
    for t in MATTER_TABLES:
        op.execute(f"DROP POLICY IF EXISTS matter_isolation ON {t}")
        op.execute(f"ALTER TABLE {t} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {t} DISABLE ROW LEVEL SECURITY")
    op.execute("DROP FUNCTION IF EXISTS medchron_can_access(text)")
    op.execute("DROP FUNCTION IF EXISTS medchron_matter_exists(text)")


def downgrade() -> None:
    """Downgrade schema."""
    _disable_rls()
    op.drop_column("matters", "created_by")
    op.drop_column("jobs", "attempts")
    op.drop_column("jobs", "heartbeat_at")
    op.drop_table("matter_members")
    op.drop_index("ix_audit_matter", table_name="audit_log")
    op.drop_table("audit_log")
