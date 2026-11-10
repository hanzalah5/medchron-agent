"""Database roles: the app never connects as the table owner.

Postgres exempts superusers from row-level security, and a table owner can turn
it off. So the services connect as ordinary roles:

| Role | Login | Member of | Used by |
|---|---|---|---|
| ``medchron_app`` | no | - | group: table privileges (no UPDATE/DELETE on the audit log) |
| ``medchron_system`` | no | - | group: may enable the system bypass (``medchron.system``) |
| ``medchron_api`` | yes | ``medchron_app`` | the HTTP API and the MCP server |
| ``medchron_worker`` | yes | ``medchron_app``, ``medchron_system`` | the job worker |

Migrations run as the owner. Migration 0008 creates the group roles and grants.
``medchron db-users`` creates or updates the two login roles from environment
variables. The access check requires membership in ``medchron_system`` for the
bypass, so the API role cannot read across matters even with
``set_config('medchron.system', 'on')``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from sqlalchemy.engine import Engine, make_url

GROUP_ROLES_SQL = """
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'medchron_app') THEN
    CREATE ROLE medchron_app NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'medchron_system') THEN
    CREATE ROLE medchron_system NOLOGIN;
  END IF;
END $$;
"""


@dataclass(frozen=True)
class LoginRole:
    name: str
    password: str
    system: bool  # may use the system bypass (worker only)


def login_roles_from_env() -> list[LoginRole]:
    return [
        LoginRole(
            os.environ.get("MEDCHRON_DB_API_USER", "medchron_api"),
            os.environ.get("MEDCHRON_DB_API_PASSWORD", "medchron_api"),
            system=False,
        ),
        LoginRole(
            os.environ.get("MEDCHRON_DB_WORKER_USER", "medchron_worker"),
            os.environ.get("MEDCHRON_DB_WORKER_PASSWORD", "medchron_worker"),
            system=True,
        ),
    ]


def ensure_login_roles(engine: Engine, roles: list[LoginRole]) -> None:
    """Create or update the login roles and their group memberships (idempotent).

    Needs a connection that may create roles (the migration owner with CREATEROLE).
    """
    from psycopg import sql

    raw = engine.raw_connection()
    try:
        cur = raw.cursor()
        cur.execute(GROUP_ROLES_SQL)
        for role in roles:
            cur.execute(
                "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = %s", (role.name,)
            )
            existing = cur.fetchone()
            if existing is None:
                stmt = "CREATE ROLE {} LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD {}"
            elif any(existing):
                raise RuntimeError(
                    f"role {role.name} can bypass row-level security (superuser or BYPASSRLS); "
                    "the services must not connect as it"
                )
            else:
                stmt = "ALTER ROLE {} LOGIN PASSWORD {}"
            cur.execute(sql.SQL(stmt).format(sql.Identifier(role.name), sql.Literal(role.password)))
            cur.execute(sql.SQL("GRANT medchron_app TO {}").format(sql.Identifier(role.name)))
            grant = "GRANT" if role.system else "REVOKE"
            target = "TO" if role.system else "FROM"
            cur.execute(
                sql.SQL(f"{grant} medchron_system {target} {{}}").format(sql.Identifier(role.name))
            )
        raw.commit()
    finally:
        raw.close()


def url_for(owner_url: str, role: LoginRole) -> str:
    """The owner's connection URL with the login role's credentials."""
    return (
        make_url(owner_url)
        .set(username=role.name, password=role.password)
        .render_as_string(hide_password=False)
    )
