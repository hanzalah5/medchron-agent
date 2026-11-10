"""HTTP API: upload productions, follow jobs, review chronologies, issues and memos.

Every endpoint except ``/healthz`` requires an authenticated principal (OIDC
in production, dev headers locally; see ``medchron.security.auth``).
Access is enforced in three layers:

1. **Role permissions**: what the user may do (for example, only attorneys
   triage issues or approve memos).
2. **Matter membership**: which matters the user may touch (ethical walls),
   checked in the API.
3. **Postgres row-level security**: the same membership rule enforced in the
   database for every query (migration 0005), so a bug in layer 2 still
   can't leak another matter's rows.

Every read of record content, every review decision and every export is
written to the hash-chained audit log, with metadata only.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import hmac
import json
import os
import re
import uuid
from collections.abc import AsyncIterator, Iterator
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal

import pymupdf
from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from medchron import __version__
from medchron.chronology.diff import diff_chronologies
from medchron.chronology.export import to_markdown
from medchron.chronology.models import Chronology
from medchron.db.models import (
    AuditRow,
    EventRow,
    IssueRow,
    Job,
    JobEvent,
    Matter,
    MatterMember,
    MemoRow,
    PageRow,
)
from medchron.db.store import (
    add_production,
    list_productions,
    list_versions,
    load_chronology,
    load_version,
    make_engine,
    reviewed_chronology,
)
from medchron.ingestion.models import Page
from medchron.jobs import PROVIDERS, enqueue, notify_worker
from medchron.observability import setup_tracing
from medchron.retrieval.embeddings import get_embedder
from medchron.retrieval.search import PostgresSearch, SearchHit
from medchron.security import audit
from medchron.security.auth import Principal, Role, authenticate, require
from medchron.storage import Storage, get_storage

MAX_UPLOAD_BYTES = 50 * 1024 * 1024
MATTER_ID = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
USER_ID = re.compile(r"^[A-Za-z0-9_.@+-]{1,128}$")


def current_principal(request: Request) -> Principal:
    return authenticate(request)


User = Annotated[Principal, Depends(current_principal)]


def db(request: Request, user: User) -> Iterator[Session]:
    """A session that acts as the user, so Postgres row-level security applies."""
    with Session(request.app.state.engine, info={"actor": user.user_id, "principal": user}) as s:
        yield s


DB = Annotated[Session, Depends(db)]


def who(session: Session) -> Principal:
    principal: Principal = session.info["principal"]
    return principal


def get_matter(session: Session, matter_id: str, permission: str = "matter.read") -> Matter:
    """Load a matter the caller may access. Non-members get 404, which doesn't reveal existence."""
    user = who(session)
    require(user, permission)
    member = session.get(MatterMember, (matter_id, user.user_id))
    matter = session.get(Matter, matter_id) if member else None
    if matter is None:
        raise HTTPException(404, "matter not found")
    return matter


def log(session: Session, action: str, matter_id: str | None = None, **kw: Any) -> None:
    audit.record(session, who(session).user_id, action, matter_id=matter_id, **kw)


def create_app(engine: Engine | None = None, storage: Storage | None = None) -> FastAPI:
    setup_tracing("medchron-api")
    app = FastAPI(
        title="MedChron API",
        version=__version__,
        description="Grounded medical-record review (synthetic data only).",
    )
    app.state.engine = engine or make_engine()
    app.state.storage = storage or get_storage()
    _STORAGES[str(app.state.storage.root)] = app.state.storage

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/api/me")
    def me(user: User) -> dict[str, Any]:
        return {"user_id": user.user_id, "roles": sorted(r.value for r in user.roles)}

    # ------------------------------------------------------------------ matters
    @app.post("/api/matters", status_code=202)
    async def create_matter(
        session: DB,
        file: Annotated[UploadFile, File()],
        matter_id: Annotated[str | None, Form()] = None,
        title: Annotated[str, Form()] = "",
        provider: Annotated[str, Form()] = "anthropic",
        budget_usd: Annotated[float | None, Form()] = None,
        access_code: Annotated[str | None, Form()] = None,
    ) -> dict[str, str]:
        user = who(session)
        require(user, "matter.create")
        data = await file.read(MAX_UPLOAD_BYTES + 1)
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "file too large")
        if not data.startswith(b"%PDF"):
            raise HTTPException(415, "only PDF uploads are accepted")
        if provider not in PROVIDERS:
            raise HTTPException(422, f"provider must be one of {sorted(PROVIDERS)}")
        budget_usd = demo_policy(provider, budget_usd, access_code)
        mid = matter_id or f"m-{uuid.uuid4().hex[:10]}"
        if not MATTER_ID.match(mid):
            raise HTTPException(422, "matter_id may contain letters, digits, '.', '_' and '-'")
        exists = session.execute(
            select(func.medchron_matter_exists(mid))  # sees past RLS without revealing rows
        ).scalar()
        if exists:
            raise HTTPException(409, "matter already exists")
        sha = hashlib.sha256(data).hexdigest()
        key = request_storage(app).put(data)
        session.add(
            Matter(
                matter_id=mid,
                title=title[:255],
                source_sha256=sha,
                source_path=key,
                created_by=user.user_id,
            )
        )
        session.flush()
        session.add(MatterMember(matter_id=mid, user_id=user.user_id, added_by=user.user_id))
        session.flush()
        add_production(session, mid, sha, key, user.user_id)
        job = enqueue(session, mid, provider, budget_usd)
        log(session, "matter.create", mid, object_type="file", object_id=sha,
            details={"bytes": len(data), "provider": provider})  # fmt: skip
        session.commit()
        notify_worker()
        return {"matter_id": mid, "job_id": job.job_id}

    @app.get("/api/matters")
    def list_matters(session: DB) -> list[dict[str, Any]]:
        user = who(session)
        mine = select(MatterMember.matter_id).where(MatterMember.user_id == user.user_id)
        latest = (
            select(Job.matter_id, func.max(Job.created_at).label("at"))
            .group_by(Job.matter_id)
            .subquery()
        )
        rows = session.execute(
            select(Matter, Job)
            .where(Matter.matter_id.in_(mine))
            .outerjoin(latest, latest.c.matter_id == Matter.matter_id)
            .outerjoin(Job, (Job.matter_id == Matter.matter_id) & (Job.created_at == latest.c.at))
            .order_by(Matter.created_at.desc())
        ).all()
        return [
            {
                "matter_id": m.matter_id,
                "title": m.title,
                "created_at": m.created_at.isoformat() if m.created_at else None,
                "job": _job_json(j) if j else None,
            }
            for m, j in rows
        ]

    @app.get("/api/matters/{matter_id}")
    def matter_detail(session: DB, matter_id: str) -> dict[str, Any]:
        m = get_matter(session, matter_id)
        pages = session.scalar(select(func.count()).where(PageRow.matter_id == matter_id))
        jobs = session.scalars(
            select(Job).where(Job.matter_id == matter_id).order_by(Job.created_at.desc())
        ).all()
        return {
            "matter_id": m.matter_id,
            "title": m.title,
            "pages": pages or 0,
            "jobs": [_job_json(j) for j in jobs],
            "productions": [
                {
                    "production_no": r.production_no,
                    "sha256": r.sha256,
                    "page_offset": r.page_offset,
                    "page_count": r.page_count,
                    "created_by": r.created_by,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                }
                for r in list_productions(session, matter_id)
            ],
        }

    @app.post("/api/matters/{matter_id}/productions", status_code=202)
    async def add_matter_production(
        session: DB,
        matter_id: str,
        file: Annotated[UploadFile, File()],
        provider: Annotated[str, Form()] = "anthropic",
        budget_usd: Annotated[float | None, Form()] = None,
        access_code: Annotated[str | None, Form()] = None,
    ) -> dict[str, Any]:
        """A later production for an existing matter. The whole matter is re-run; work on
        earlier productions is reused, and the chronology gets a new version and a diff."""
        user = who(session)
        require(user, "matter.create")
        get_matter(session, matter_id)
        data = await file.read(MAX_UPLOAD_BYTES + 1)
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "file too large")
        if not data.startswith(b"%PDF"):
            raise HTTPException(415, "only PDF uploads are accepted")
        if provider not in PROVIDERS:
            raise HTTPException(422, f"provider must be one of {sorted(PROVIDERS)}")
        budget_usd = demo_policy(provider, budget_usd, access_code)
        sha = hashlib.sha256(data).hexdigest()
        if any(r.sha256 == sha for r in list_productions(session, matter_id)):
            raise HTTPException(409, "this file was already produced for the matter")
        key = request_storage(app).put(data)
        row = add_production(session, matter_id, sha, key, user.user_id)
        job = enqueue(session, matter_id, provider, budget_usd)
        log(session, "production.add", matter_id, object_type="file", object_id=sha,
            details={"production_no": row.production_no, "bytes": len(data)})  # fmt: skip
        session.commit()
        notify_worker()
        return {"matter_id": matter_id, "production_no": row.production_no, "job_id": job.job_id}

    @app.get("/api/matters/{matter_id}/chronology/versions")
    def chronology_versions(session: DB, matter_id: str) -> list[dict[str, Any]]:
        get_matter(session, matter_id)
        return [
            {
                "version": v.version,
                "created_at": v.created_at.isoformat() if v.created_at else None,
                "job_id": v.job_id,
                "productions": v.productions,
                "events": v.events,
                "diff": v.diff_summary,
            }
            for v in list_versions(session, matter_id)
        ]

    @app.get("/api/matters/{matter_id}/chronology/diff")
    def chronology_diff(
        session: DB, matter_id: str, from_: Annotated[int | None, Query(alias="from")] = None,
        to: int | None = None,
    ) -> dict[str, Any]:  # fmt: skip
        get_matter(session, matter_id)
        versions = [v.version for v in list_versions(session, matter_id)]
        if not versions:
            raise HTTPException(404, "no chronology versions yet")
        to_v = to if to is not None else versions[-1]
        from_v = from_ if from_ is not None else (to_v - 1 if to_v > 1 else None)
        new = load_version(session, matter_id, to_v)
        old = load_version(session, matter_id, from_v) if from_v is not None else None
        if new is None or (from_v is not None and old is None):
            raise HTTPException(404, "version not found")
        diff = diff_chronologies(old, new, from_version=from_v, to_version=to_v)
        log(session, "chronology.diff_view", matter_id, object_type="version",
            object_id=f"{from_v}->{to_v}")  # fmt: skip
        session.commit()
        return diff.model_dump(mode="json")

    @app.get("/api/matters/{matter_id}/members")
    def members(session: DB, matter_id: str) -> list[dict[str, Any]]:
        get_matter(session, matter_id)
        rows = session.scalars(select(MatterMember).where(MatterMember.matter_id == matter_id))
        return [{"user_id": r.user_id, "added_by": r.added_by} for r in rows]

    @app.post("/api/matters/{matter_id}/members", status_code=201)
    def add_member(session: DB, matter_id: str, body: NewMember) -> dict[str, str]:
        user = who(session)
        require(user, "matter.members")
        # Admins manage ethical walls without being members (they can't read content).
        if Role.ADMIN not in user.roles:
            get_matter(session, matter_id)
        elif not session.execute(select(func.medchron_matter_exists(matter_id))).scalar():
            raise HTTPException(404, "matter not found")
        if session.get(MatterMember, (matter_id, body.user_id)) is None:
            session.add(
                MatterMember(matter_id=matter_id, user_id=body.user_id, added_by=user.user_id)
            )
        log(session, "matter.member_add", matter_id, object_type="user", object_id=body.user_id)
        session.commit()
        return {"matter_id": matter_id, "user_id": body.user_id}

    # ------------------------------------------------------------------ records
    @app.get("/api/matters/{matter_id}/chronology", response_model=Chronology)
    def chronology(session: DB, matter_id: str) -> Chronology:
        get_matter(session, matter_id)
        log(session, "chronology.view", matter_id)
        session.commit()
        return load_chronology(session, matter_id)

    @app.get("/api/matters/{matter_id}/pages/{page_no}", response_model=Page)
    def page(session: DB, matter_id: str, page_no: int) -> Page:
        get_matter(session, matter_id)
        row = session.get(PageRow, (matter_id, page_no))
        if row is None:
            raise HTTPException(404, "page not found")
        log(session, "page.view", matter_id, object_type="page", object_id=str(page_no))
        session.commit()
        return Page.model_validate(
            {
                "page_no": row.page_no,
                "text": row.text,
                "words": row.words,
                "source": row.source,
                "ocr_confidence": row.ocr_confidence,
                "width": row.width,
                "height": row.height,
                "content_hash": row.content_hash,
                "duplicate_of": row.duplicate_of,
            }
        )

    @app.get("/api/matters/{matter_id}/pages/{page_no}/image")
    def page_image(session: DB, matter_id: str, page_no: int, dpi: int = 110) -> Response:
        m = get_matter(session, matter_id)
        png = _render_page(request_storage(app), m.source_path, page_no, min(max(dpi, 50), 200))
        if png is None:
            raise HTTPException(404, "page not found")
        log(session, "page.image", matter_id, object_type="page", object_id=str(page_no))
        session.commit()
        return Response(
            png, media_type="image/png", headers={"Cache-Control": "private, max-age=3600"}
        )

    @app.get("/api/matters/{matter_id}/search", response_model=list[SearchHit])
    def search(session: DB, matter_id: str, q: str, limit: int = 8) -> list[SearchHit]:
        get_matter(session, matter_id)
        # The query may itself contain PHI, so only its hash is audited.
        log(session, "records.search", matter_id,
            details={"query_sha256": hashlib.sha256(q.encode()).hexdigest()})  # fmt: skip
        session.commit()
        return PostgresSearch(session, matter_id, get_embedder()).search(q, limit=min(limit, 25))

    # ------------------------------------------------------------------ jobs
    @app.get("/api/jobs/{job_id}")
    def job(session: DB, job_id: str) -> dict[str, Any]:
        j = session.get(Job, job_id)
        if j is None:
            raise HTTPException(404, "job not found")
        get_matter(session, j.matter_id)
        return _job_json(j)

    @app.get("/api/jobs/{job_id}/events")
    async def job_events(request: Request, user: User, job_id: str) -> StreamingResponse:
        engine: Engine = request.app.state.engine
        with Session(engine, info={"actor": user.user_id, "principal": user}) as s:
            j = s.get(Job, job_id)
            if j is None:
                raise HTTPException(404, "job not found")
            get_matter(s, j.matter_id)

        async def stream() -> AsyncIterator[str]:
            last = 0
            while True:
                with Session(engine, info={"actor": user.user_id}) as s:
                    j = s.get(Job, job_id)
                    if j is None:
                        return
                    events = s.scalars(
                        select(JobEvent)
                        .where(JobEvent.job_id == job_id, JobEvent.id > last)
                        .order_by(JobEvent.id)
                    ).all()
                    for e in events:
                        last = e.id
                        yield _sse({"message": e.message, "at": e.at.isoformat()})
                    done = j.status not in ("queued", "running")
                    status = j.status
                if done:
                    yield _sse({"status": status, "done": True})
                    return
                if await request.is_disconnected():
                    return
                await asyncio.sleep(0.5)

        return StreamingResponse(stream(), media_type="text/event-stream")

    # ------------------------------------------------------------------ review (HITL-1/2)
    @app.get("/api/matters/{matter_id}/review")
    def review_state(session: DB, matter_id: str) -> dict[str, Any]:
        m = get_matter(session, matter_id)
        rows = session.scalars(select(EventRow).where(EventRow.matter_id == matter_id)).all()
        return {
            "index_confirmed_by": m.index_confirmed_by,
            "index_confirmed_at": m.index_confirmed_at.isoformat()
            if m.index_confirmed_at
            else None,
            "events": {r.event_id: _review_json(r) for r in rows},
        }

    @app.patch("/api/matters/{matter_id}/events/{event_id}")
    def review_event(
        session: DB, matter_id: str, event_id: str, body: EventReview
    ) -> dict[str, Any]:
        get_matter(session, matter_id, "events.review")
        row = session.get(EventRow, (matter_id, event_id))
        if row is None:
            raise HTTPException(404, "event not found")
        if body.status == "edited" and not (body.summary or body.date):
            raise HTTPException(422, "an edit needs a new summary or date")
        if body.status == "rejected" and not body.note.strip():
            raise HTTPException(422, "rejecting an event requires a reason")
        row.review_status = body.status
        row.reviewer_note = body.note[:2000]
        row.edited_summary = (body.summary or None) if body.status == "edited" else None
        row.edited_date = body.date if body.status == "edited" else None
        row.reviewed_by = who(session).user_id
        row.reviewed_at = dt.datetime.now(dt.UTC)
        log(session, "event.review", matter_id, object_type="event", object_id=event_id,
            details={"status": body.status})  # fmt: skip
        session.commit()
        return _review_json(row)

    @app.post("/api/matters/{matter_id}/index/confirm")
    def confirm_index(session: DB, matter_id: str) -> dict[str, str]:
        m = get_matter(session, matter_id, "index.confirm")
        m.index_confirmed_by = who(session).user_id
        m.index_confirmed_at = dt.datetime.now(dt.UTC)
        log(session, "index.confirm", matter_id)
        session.commit()
        return {
            "index_confirmed_by": m.index_confirmed_by,
            "index_confirmed_at": m.index_confirmed_at.isoformat(),
        }

    @app.get("/api/matters/{matter_id}/export.md")
    def export_markdown(session: DB, matter_id: str) -> Response:
        """Draft chronology with reviewer edits applied and rejected events left out."""
        get_matter(session, matter_id)
        chron = reviewed_chronology(session, matter_id)
        body = to_markdown(chron)
        log(session, "chronology.export", matter_id,
            details={"sha256": hashlib.sha256(body.encode()).hexdigest()})  # fmt: skip
        session.commit()
        return Response(
            body,
            media_type="text/markdown",
            headers={"Content-Disposition": f'attachment; filename="{matter_id}-chronology.md"'},
        )

    # ------------------------------------------------------------------ issues (HITL-3)
    @app.get("/api/matters/{matter_id}/issues")
    def list_issues(session: DB, matter_id: str) -> list[dict[str, Any]]:
        get_matter(session, matter_id)
        rows = session.scalars(
            select(IssueRow).where(IssueRow.matter_id == matter_id).order_by(IssueRow.issue_id)
        ).all()
        return [_issue_json(r) for r in rows]

    @app.patch("/api/matters/{matter_id}/issues/{issue_id}")
    def triage_issue(
        session: DB, matter_id: str, issue_id: str, body: IssueTriage
    ) -> dict[str, Any]:
        get_matter(session, matter_id, "issues.triage")
        row = session.get(IssueRow, (matter_id, issue_id))
        if row is None:
            raise HTTPException(404, "issue not found")
        if body.status == "dismiss" and not body.note.strip():
            raise HTTPException(422, "dismissing an issue requires a reason")
        row.triage_status, row.triage_note = body.status, body.note[:2000]
        row.triaged_by, row.triaged_at = who(session).user_id, dt.datetime.now(dt.UTC)
        log(session, "issue.triage", matter_id, object_type="issue", object_id=issue_id,
            details={"status": body.status})  # fmt: skip
        session.commit()
        return _issue_json(row)

    # ------------------------------------------------------------------ memo (HITL-4)
    @app.post("/api/matters/{matter_id}/memo", status_code=202)
    def request_memo(session: DB, matter_id: str, body: MemoRequest) -> dict[str, str]:
        m = get_matter(session, matter_id, "memo.draft")
        if m.index_confirmed_at is None:
            raise HTTPException(409, "confirm the document index before drafting a memo")
        pending = session.scalar(
            select(func.count()).where(
                IssueRow.matter_id == matter_id, IssueRow.triage_status == "pending"
            )
        )
        if pending:
            raise HTTPException(409, f"{pending} issue(s) still need triage")
        budget = demo_policy(body.provider, body.budget_usd, body.access_code)
        job = enqueue(session, matter_id, body.provider, budget, kind="memo")
        log(session, "memo.request", matter_id, object_type="job", object_id=job.job_id)
        session.commit()
        notify_worker()
        return {"job_id": job.job_id}

    @app.get("/api/matters/{matter_id}/memo")
    def latest_memo(session: DB, matter_id: str) -> dict[str, Any]:
        get_matter(session, matter_id)
        row = session.scalars(
            select(MemoRow)
            .where(MemoRow.matter_id == matter_id)
            .order_by(MemoRow.created_at.desc())
            .limit(1)
        ).first()
        if row is None:
            raise HTTPException(404, "no memo drafted yet")
        return _memo_json(row)

    @app.post("/api/matters/{matter_id}/memo/{memo_id}/approve")
    def approve_memo(session: DB, matter_id: str, memo_id: str) -> dict[str, Any]:
        get_matter(session, matter_id, "memo.approve")
        row = session.get(MemoRow, memo_id)
        if row is None or row.matter_id != matter_id:
            raise HTTPException(404, "memo not found")
        if any(not c.get("citation_valid", True) for c in row.claims):
            raise HTTPException(409, "memo has citations that failed verification")
        row.status, row.approved_by = "approved", who(session).user_id
        row.approved_at = dt.datetime.now(dt.UTC)
        log(session, "memo.approve", matter_id, object_type="memo", object_id=memo_id,
            details={"sha256": hashlib.sha256(row.markdown.encode()).hexdigest()})  # fmt: skip
        session.commit()
        return _memo_json(row)

    @app.get("/api/matters/{matter_id}/memo/{memo_id}/export.md")
    def export_memo(session: DB, matter_id: str, memo_id: str) -> Response:
        get_matter(session, matter_id)
        row = session.get(MemoRow, memo_id)
        if row is None or row.matter_id != matter_id:
            raise HTTPException(404, "memo not found")
        if row.status != "approved":
            raise HTTPException(409, "only an attorney-approved memo can be exported")
        footer = (
            f"\n\n---\nApproved by {row.approved_by} on "
            f"{row.approved_at.isoformat() if row.approved_at else ''}. Synthetic records.\n"
        )
        log(session, "memo.export", matter_id, object_type="memo", object_id=memo_id)
        session.commit()
        return Response(
            row.markdown + footer,
            media_type="text/markdown",
            headers={"Content-Disposition": f'attachment; filename="{matter_id}-memo.md"'},
        )

    # ------------------------------------------------------------------ audit
    @app.get("/api/audit")
    def audit_log(
        session: DB, matter_id: str | None = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        require(who(session), "audit.read")
        q = select(AuditRow).order_by(AuditRow.id.desc()).limit(min(limit, 1000))
        if matter_id:
            q = q.where(AuditRow.matter_id == matter_id)
        return [
            {
                "id": r.id,
                "at": r.at,
                "actor": r.actor,
                "action": r.action,
                "matter_id": r.matter_id,
                "object_type": r.object_type,
                "object_id": r.object_id,
                "details": r.details,
                "hash": r.hash,
            }
            for r in session.scalars(q)
        ]

    @app.get("/api/audit/verify")
    def audit_verify(session: DB) -> dict[str, Any]:
        require(who(session), "audit.read")
        ok, n, bad = audit.verify_chain(session)
        return {"ok": ok, "rows": n, "first_bad_id": bad}

    _mount_ui(app)
    return app


class NewMember(BaseModel):
    user_id: str = Field(pattern=USER_ID.pattern)


class IssueTriage(BaseModel):
    status: Literal["pending", "pursue", "expert", "dismiss"]
    note: str = ""


class MemoRequest(BaseModel):
    provider: Literal["anthropic", "replay", "oracle"] = "anthropic"
    budget_usd: float | None = None
    access_code: str | None = None


def demo_policy(provider: str, budget_usd: float | None, access_code: str | None) -> float | None:
    """On the public demo, live model runs need an access code and get a hard budget cap."""
    if os.environ.get("MEDCHRON_ENV") != "demo" or provider != "anthropic":
        return budget_usd
    expected = os.environ.get("MEDCHRON_DEMO_ACCESS_CODE", "")
    if not expected or not hmac.compare_digest(expected, access_code or ""):
        raise HTTPException(403, "live model runs on the demo need an access code")
    cap = float(os.environ.get("MEDCHRON_DEMO_MAX_BUDGET_USD", "1.00"))
    return min(budget_usd or cap, cap)


class EventReview(BaseModel):
    status: Literal["pending", "accepted", "edited", "rejected"]
    note: str = ""
    summary: str | None = Field(default=None, max_length=300)
    date: dt.date | None = None


def _issue_json(r: IssueRow) -> dict[str, Any]:
    return {
        "issue_id": r.issue_id,
        "category": r.category,
        "description": r.description,
        "event_ids": r.event_ids,
        "citations": r.citations,
        "severity_hint": r.severity_hint,
        "verdict": r.verdict,
        "verdict_reason": r.verdict_reason,
        "triage_status": r.triage_status,
        "triage_note": r.triage_note,
        "triaged_by": r.triaged_by,
    }


def _memo_json(r: MemoRow) -> dict[str, Any]:
    return {
        "memo_id": r.memo_id,
        "status": r.status,
        "markdown": r.markdown,
        "claims": r.claims,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "approved_by": r.approved_by,
        "approved_at": r.approved_at.isoformat() if r.approved_at else None,
    }


def _review_json(r: EventRow) -> dict[str, Any]:
    return {
        "event_id": r.event_id,
        "status": r.review_status,
        "note": r.reviewer_note,
        "edited_summary": r.edited_summary,
        "edited_date": r.edited_date.isoformat() if r.edited_date else None,
        "reviewed_by": r.reviewed_by,
        "reviewed_at": r.reviewed_at.isoformat() if r.reviewed_at else None,
    }


def _mount_ui(app: FastAPI) -> None:
    """Serve the built reviewer UI (ui/dist) at / when it exists."""
    dist = Path(os.environ.get("MEDCHRON_UI_DIST", "ui/dist"))
    if (dist / "index.html").exists():
        app.mount("/", StaticFiles(directory=dist, html=True), name="ui")


def request_storage(app: FastAPI) -> Storage:
    storage: Storage = app.state.storage
    return storage


def _job_json(j: Job) -> dict[str, Any]:
    return {
        "job_id": j.job_id,
        "matter_id": j.matter_id,
        "status": j.status,
        "provider": j.provider,
        "kind": j.kind,
        "error": j.error,
        "summary": j.summary,
        "created_at": j.created_at.isoformat() if j.created_at else None,
        "finished_at": j.finished_at.isoformat() if j.finished_at else None,
    }


def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload)}\n\n"


_STORAGES: dict[str, Storage] = {}


@lru_cache(maxsize=256)
def _render_cached(root: str, key: str, page_no: int, dpi: int) -> bytes | None:
    data = _STORAGES[root].get(key)
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        if not 1 <= page_no <= doc.page_count:
            return None
        pix = doc[page_no - 1].get_pixmap(dpi=dpi)
        png: bytes = pix.tobytes("png")
        return png


def _render_page(storage: Storage, key: str, page_no: int, dpi: int) -> bytes | None:
    # Content-addressed keys never change, so caching by key is safe.
    return _render_cached(str(storage.root), key, page_no, dpi)
