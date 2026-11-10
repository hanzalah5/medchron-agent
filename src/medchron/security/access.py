"""Matter access checks shared by every entry point (HTTP API, MCP server).

Each request runs in an *actor session*: a database session that declares the
caller to Postgres, so row-level security applies to every query (see
``medchron.db``). On top of RLS, ``open_matter`` checks the caller's role
and matter membership. Non-members get "not found", which does not reveal
whether the matter exists.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from medchron.db.models import Matter, MatterMember
from medchron.security import audit
from medchron.security.auth import Principal, require


class MatterNotFound(LookupError):
    """The matter does not exist or the caller is not a member (indistinguishable)."""


def actor_session(engine: Engine, principal: Principal) -> Session:
    return Session(engine, info={"actor": principal.user_id, "principal": principal})


def principal_of(session: Session) -> Principal:
    principal: Principal = session.info["principal"]
    return principal


def open_matter(session: Session, matter_id: str, permission: str = "matter.read") -> Matter:
    """The matter, if the caller's role allows ``permission`` and they are a member."""
    user = principal_of(session)
    require(user, permission)
    member = session.get(MatterMember, (matter_id, user.user_id))
    matter = session.get(Matter, matter_id) if member else None
    if matter is None:
        raise MatterNotFound(matter_id)
    return matter


def record(session: Session, action: str, matter_id: str | None = None, **kw: Any) -> None:
    """Append to the hash-chained audit log as the session's caller."""
    audit.record(session, principal_of(session).user_id, action, matter_id=matter_id, **kw)
