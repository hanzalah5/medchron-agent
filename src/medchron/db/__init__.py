"""Database schema and persistence (Postgres + pgvector).

Every transaction declares who is acting, so Postgres row-level security can
enforce matter isolation (see migration 0005):

- ``Session(engine, info={"actor": user_id})``: an end user. Only matters the
  user is a member of are visible.
- ``Session(engine)`` with no actor: a trusted backend process (worker,
  migrations, CLI), which sets ``medchron.system``.

API code always passes an actor. Backend code runs only after the API has
authorized the work, for example by enqueueing a job.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import event, text
from sqlalchemy.orm import Session, SessionTransaction


@event.listens_for(Session, "after_begin")
def _declare_actor(session: Session, transaction: SessionTransaction, connection: Any) -> None:
    actor = session.info.get("actor")
    if actor is None:
        connection.execute(
            text(
                "SELECT set_config('medchron.system', 'on', true), "
                "set_config('medchron.user', '', true)"
            )
        )
    else:
        connection.execute(
            text(
                "SELECT set_config('medchron.system', 'off', true), "
                "set_config('medchron.user', :u, true)"
            ),
            {"u": str(actor)},
        )
