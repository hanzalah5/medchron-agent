"""HTTP API: upload productions, follow jobs, read chronologies and pages.

There is no authentication yet. OIDC, RBAC and matter ACLs arrive in the
hardening step, so run this only on localhost with synthetic data.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import os
import re
import uuid
from collections.abc import AsyncIterator, Iterator
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal

import pymupdf
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from medchron import __version__
from medchron.chronology.export import to_markdown
from medchron.chronology.models import Chronology
from medchron.db.models import EventRow, IssueRow, Job, JobEvent, Matter, MemoRow, PageRow
from medchron.db.store import load_chronology, make_engine, reviewed_chronology
from medchron.ingestion.models import Page
from medchron.jobs import PROVIDERS, enqueue
from medchron.retrieval.embeddings import get_embedder
from medchron.retrieval.search import PostgresSearch, SearchHit
from medchron.storage import LocalStorage

MAX_UPLOAD_BYTES = 50 * 1024 * 1024
MATTER_ID = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def db(request: Request) -> Iterator[Session]:
    with Session(request.app.state.engine) as session:
        yield session


DB = Annotated[Session, Depends(db)]


def create_app(engine: Engine | None = None, storage: LocalStorage | None = None) -> FastAPI:
    app = FastAPI(
        title="MedChron API",
        version=__version__,
        description="Grounded medical-record review (synthetic data only).",
    )
    app.state.engine = engine or make_engine()
    app.state.storage = storage or LocalStorage()

    def get_matter(session: Session, matter_id: str) -> Matter:
        matter = session.get(Matter, matter_id)
        if matter is None:
            raise HTTPException(404, "matter not found")
        return matter

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.post("/api/matters", status_code=202)
    async def create_matter(
        session: DB,
        file: Annotated[UploadFile, File()],
        matter_id: Annotated[str | None, Form()] = None,
        title: Annotated[str, Form()] = "",
        provider: Annotated[str, Form()] = "anthropic",
        budget_usd: Annotated[float | None, Form()] = None,
    ) -> dict[str, str]:
        data = await file.read(MAX_UPLOAD_BYTES + 1)
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "file too large")
        if not data.startswith(b"%PDF"):
            raise HTTPException(415, "only PDF uploads are accepted")
        if provider not in PROVIDERS:
            raise HTTPException(422, f"provider must be one of {sorted(PROVIDERS)}")
        mid = matter_id or f"m-{uuid.uuid4().hex[:10]}"
        if not MATTER_ID.match(mid):
            raise HTTPException(422, "matter_id may contain letters, digits, '.', '_' and '-'")
        if session.get(Matter, mid) is not None:
            raise HTTPException(409, "matter already exists")
        key = request_storage(app).put(data)
        session.add(
            Matter(
                matter_id=mid,
                title=title[:255],
                source_sha256=hashlib.sha256(data).hexdigest(),
                source_path=key,
            )
        )
        session.flush()
        job = enqueue(session, mid, provider, budget_usd)
        session.commit()
        return {"matter_id": mid, "job_id": job.job_id}

    @app.get("/api/matters")
    def list_matters(session: DB) -> list[dict[str, Any]]:
        latest = (
            select(Job.matter_id, func.max(Job.created_at).label("at"))
            .group_by(Job.matter_id)
            .subquery()
        )
        rows = session.execute(
            select(Matter, Job)
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
        }

    @app.get("/api/matters/{matter_id}/chronology", response_model=Chronology)
    def chronology(session: DB, matter_id: str) -> Chronology:
        get_matter(session, matter_id)
        return load_chronology(session, matter_id)

    @app.get("/api/matters/{matter_id}/pages/{page_no}", response_model=Page)
    def page(session: DB, matter_id: str, page_no: int) -> Page:
        row = session.get(PageRow, (matter_id, page_no))
        if row is None:
            raise HTTPException(404, "page not found")
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
        return Response(
            png, media_type="image/png", headers={"Cache-Control": "private, max-age=3600"}
        )

    @app.get("/api/matters/{matter_id}/search", response_model=list[SearchHit])
    def search(session: DB, matter_id: str, q: str, limit: int = 8) -> list[SearchHit]:
        get_matter(session, matter_id)
        return PostgresSearch(session, matter_id, get_embedder()).search(q, limit=min(limit, 25))

    @app.get("/api/jobs/{job_id}")
    def job(session: DB, job_id: str) -> dict[str, Any]:
        j = session.get(Job, job_id)
        if j is None:
            raise HTTPException(404, "job not found")
        return _job_json(j)

    @app.get("/api/jobs/{job_id}/events")
    async def job_events(request: Request, job_id: str) -> StreamingResponse:
        engine: Engine = request.app.state.engine

        async def stream() -> AsyncIterator[str]:
            last = 0
            while True:
                with Session(engine) as s:
                    j = s.get(Job, job_id)
                    if j is None:
                        yield _sse({"error": "job not found"})
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

    # ------------------------------------------------------------------ review (HITL)
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
        row.reviewed_by = CURRENT_USER  # replaced by the authenticated user in the hardening step
        row.reviewed_at = dt.datetime.now(dt.UTC)
        session.commit()
        return _review_json(row)

    @app.post("/api/matters/{matter_id}/index/confirm")
    def confirm_index(session: DB, matter_id: str) -> dict[str, str]:
        m = get_matter(session, matter_id)
        m.index_confirmed_by = CURRENT_USER
        m.index_confirmed_at = dt.datetime.now(dt.UTC)
        session.commit()
        return {
            "index_confirmed_by": CURRENT_USER,
            "index_confirmed_at": m.index_confirmed_at.isoformat(),
        }

    @app.get("/api/matters/{matter_id}/export.md")
    def export_markdown(session: DB, matter_id: str) -> Response:
        """Draft chronology with reviewer edits applied and rejected events left out."""
        get_matter(session, matter_id)
        chron = reviewed_chronology(session, matter_id)
        return Response(
            to_markdown(chron),
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
        row = session.get(IssueRow, (matter_id, issue_id))
        if row is None:
            raise HTTPException(404, "issue not found")
        if body.status == "dismiss" and not body.note.strip():
            raise HTTPException(422, "dismissing an issue requires a reason")
        row.triage_status, row.triage_note = body.status, body.note[:2000]
        row.triaged_by, row.triaged_at = CURRENT_USER, dt.datetime.now(dt.UTC)
        session.commit()
        return _issue_json(row)

    # ------------------------------------------------------------------ memo (HITL-4)
    @app.post("/api/matters/{matter_id}/memo", status_code=202)
    def request_memo(session: DB, matter_id: str, body: MemoRequest) -> dict[str, str]:
        m = get_matter(session, matter_id)
        if m.index_confirmed_at is None:
            raise HTTPException(409, "confirm the document index before drafting a memo")
        pending = session.scalar(
            select(func.count()).where(
                IssueRow.matter_id == matter_id, IssueRow.triage_status == "pending"
            )
        )
        if pending:
            raise HTTPException(409, f"{pending} issue(s) still need triage")
        job = enqueue(session, matter_id, body.provider, body.budget_usd, kind="memo")
        session.commit()
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
        row = session.get(MemoRow, memo_id)
        if row is None or row.matter_id != matter_id:
            raise HTTPException(404, "memo not found")
        if any(not c.get("citation_valid", True) for c in row.claims):
            raise HTTPException(409, "memo has citations that failed verification")
        row.status, row.approved_by = "approved", CURRENT_USER
        row.approved_at = dt.datetime.now(dt.UTC)
        session.commit()
        return _memo_json(row)

    @app.get("/api/matters/{matter_id}/memo/{memo_id}/export.md")
    def export_memo(session: DB, matter_id: str, memo_id: str) -> Response:
        row = session.get(MemoRow, memo_id)
        if row is None or row.matter_id != matter_id:
            raise HTTPException(404, "memo not found")
        if row.status != "approved":
            raise HTTPException(409, "only an attorney-approved memo can be exported")
        footer = (
            f"\n\n---\nApproved by {row.approved_by} on "
            f"{row.approved_at.isoformat() if row.approved_at else ''}. Synthetic records.\n"
        )
        return Response(
            row.markdown + footer,
            media_type="text/markdown",
            headers={"Content-Disposition": f'attachment; filename="{matter_id}-memo.md"'},
        )

    _mount_ui(app)
    return app


CURRENT_USER = "local-reviewer"


class IssueTriage(BaseModel):
    status: Literal["pending", "pursue", "expert", "dismiss"]
    note: str = ""


class MemoRequest(BaseModel):
    provider: Literal["anthropic", "replay", "oracle"] = "anthropic"
    budget_usd: float | None = None


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


class EventReview(BaseModel):
    status: Literal["pending", "accepted", "edited", "rejected"]
    note: str = ""
    summary: str | None = Field(default=None, max_length=300)
    date: dt.date | None = None


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


def request_storage(app: FastAPI) -> LocalStorage:
    storage: LocalStorage = app.state.storage
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


@lru_cache(maxsize=256)
def _render_cached(root: str, key: str, page_no: int, dpi: int) -> bytes | None:
    data = LocalStorage(Path(root)).get(key)
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        if not 1 <= page_no <= doc.page_count:
            return None
        pix = doc[page_no - 1].get_pixmap(dpi=dpi)
        png: bytes = pix.tobytes("png")
        return png


def _render_page(storage: LocalStorage, key: str, page_no: int, dpi: int) -> bytes | None:
    # Content-addressed keys never change, so caching by key is safe.
    return _render_cached(str(storage.root), key, page_no, dpi)
