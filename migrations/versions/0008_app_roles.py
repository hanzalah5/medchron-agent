"""database roles: the app connects as a non-owner, and only the worker may bypass

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-10 03:00:00.000000

Row-level security does not apply to superusers, and a table owner can disable
it. The services therefore connect as ordinary login roles (see
``medchron/db/roles.py`` and ``medchron db-users``). This migration:

- creates the group roles ``medchron_app`` (table privileges) and
  ``medchron_system`` (may use the system bypass);
- grants ``medchron_app`` read and write on the tables, but only INSERT and
  SELECT on the audit log;
- requires membership in ``medchron_system`` for the ``medchron.system``
  bypass. Until now any role that could run ``set_config`` could switch it on;
  now the API role cannot, even with direct SQL;
- makes ``medchron_matter_exists`` a SECURITY DEFINER function, since it is the
  one place the API legitimately needs the bypass.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

GROUP_ROLES = """
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'medchron_app') THEN
    CREATE ROLE medchron_app NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'medchron_system') THEN
    CREATE ROLE medchron_system NOLOGIN;
  END IF;
END $$;
"""

# The owner (who runs migrations and owns the SECURITY DEFINER function) keeps the bypass.
OWNER_IS_SYSTEM = """
DO $$ BEGIN
  EXECUTE format('GRANT medchron_system TO %I', current_user);
EXCEPTION WHEN insufficient_privilege THEN
  RAISE WARNING 'could not grant medchron_system to %; grant it manually', current_user;
END $$;
"""

IS_SYSTEM_FN = """
CREATE OR REPLACE FUNCTION medchron_is_system() RETURNS boolean
LANGUAGE sql STABLE AS $$
  SELECT coalesce(current_setting('medchron.system', true), '') = 'on'
     AND pg_has_role(current_user, 'medchron_system', 'MEMBER')
$$;
"""

ACCESS_FN = """
CREATE OR REPLACE FUNCTION medchron_can_access(m text) RETURNS boolean
LANGUAGE sql STABLE AS $$
  SELECT medchron_is_system()
      OR EXISTS (
           SELECT 1 FROM matter_members mm
           WHERE mm.matter_id = m
             AND mm.user_id = coalesce(current_setting('medchron.user', true), '')
         )
$$;
"""

EXISTS_FN = """
CREATE OR REPLACE FUNCTION medchron_matter_exists(m text) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
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

OLD_ACCESS_FN = """
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

OLD_EXISTS_FN = EXISTS_FN.replace(
    "LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS",
    "LANGUAGE plpgsql AS",
)


def upgrade() -> None:
    op.execute(GROUP_ROLES)
    op.execute(OWNER_IS_SYSTEM)
    op.execute("GRANT USAGE ON SCHEMA public TO medchron_app")
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO medchron_app"
    )
    op.execute("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO medchron_app")
    op.execute("REVOKE UPDATE, DELETE, TRUNCATE ON audit_log FROM medchron_app")
    op.execute("REVOKE ALL ON alembic_version FROM medchron_app")
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO medchron_app"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO medchron_app"
    )
    op.execute(IS_SYSTEM_FN)
    op.execute(ACCESS_FN)
    op.execute(EXISTS_FN)
    op.execute("REVOKE ALL ON FUNCTION medchron_matter_exists(text) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION medchron_matter_exists(text) TO medchron_app")
    op.execute("DROP POLICY IF EXISTS matter_create ON matters")
    op.execute(
        "CREATE POLICY matter_create ON matters FOR INSERT WITH CHECK ("
        "coalesce(current_setting('medchron.user', true), '') <> '' OR medchron_is_system())"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS matter_create ON matters")
    op.execute(
        "CREATE POLICY matter_create ON matters FOR INSERT WITH CHECK ("
        "coalesce(current_setting('medchron.user', true), '') <> '' "
        "OR coalesce(current_setting('medchron.system', true), '') = 'on')"
    )
    op.execute(OLD_EXISTS_FN)
    op.execute("GRANT EXECUTE ON FUNCTION medchron_matter_exists(text) TO PUBLIC")
    op.execute(OLD_ACCESS_FN)
    op.execute("DROP FUNCTION IF EXISTS medchron_is_system()")
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        "REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM medchron_app"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE USAGE, SELECT ON SEQUENCES FROM medchron_app"
    )
    op.execute("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM medchron_app")
    op.execute("REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM medchron_app")
    # The roles themselves are cluster-wide and may be in use elsewhere: they are kept.
