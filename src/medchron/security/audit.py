"""Tamper-evident audit log.

Every row stores the SHA-256 of its own canonical content plus the previous
row's hash, forming a chain. Editing or deleting any past row breaks every
later hash, and ``verify_chain`` reports where. Writers are serialized with a
transaction-scoped advisory lock, so the chain stays linear under
concurrency.

The log records **metadata only**: who, what, which matter and which object,
plus content hashes, never record text. In production it is also shipped to
WORM storage or a SIEM, so a database administrator cannot rewrite history
undetected.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from medchron.db.models import AuditRow

GENESIS = "0" * 64
_LOCK_KEY = 7_311_017  # arbitrary constant for pg_advisory_xact_lock


def _digest(prev: str, at: str, actor: str, action: str, matter: str | None,
            obj_type: str | None, obj_id: str | None, details: dict[str, Any]) -> str:  # fmt: skip
    canonical = json.dumps(
        [prev, at, actor, action, matter, obj_type, obj_id, details],
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def record(
    session: Session,
    actor: str,
    action: str,
    *,
    matter_id: str | None = None,
    object_type: str | None = None,
    object_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> AuditRow:
    """Append one audit entry inside the caller's transaction."""
    session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _LOCK_KEY})
    prev = session.scalar(select(AuditRow.hash).order_by(AuditRow.id.desc()).limit(1)) or GENESIS
    at = dt.datetime.now(dt.UTC).isoformat(timespec="microseconds")
    det = details or {}
    row = AuditRow(
        at=at,
        actor=actor,
        action=action,
        matter_id=matter_id,
        object_type=object_type,
        object_id=object_id,
        details=det,
        prev_hash=prev,
        hash=_digest(prev, at, actor, action, matter_id, object_type, object_id, det),
    )
    session.add(row)
    session.flush()
    return row


def verify_chain(session: Session) -> tuple[bool, int, int | None]:
    """Returns (ok, rows checked, id of the first bad row or None)."""
    prev = GENESIS
    n = 0
    for row in session.scalars(select(AuditRow).order_by(AuditRow.id)):
        n += 1
        expected = _digest(prev, row.at, row.actor, row.action, row.matter_id,
                           row.object_type, row.object_id, row.details)  # fmt: skip
        if row.prev_hash != prev or row.hash != expected:
            return False, n, row.id
        prev = row.hash
    return True, n, None
