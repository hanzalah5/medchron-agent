"""HTTP API: upload productions, follow jobs, read chronologies and pages.

There is no authentication yet. OIDC, RBAC and matter ACLs arrive in the
hardening step, so run this only on localhost with synthetic data.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from collections.abc import AsyncIterator, Iterator
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any

import pymupdf
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from medchron import __version__
from medchron.chronology.models import Chronology
from medchron.db.models import Job, JobEvent, Matter, PageRow
from medchron.db.store import load_chronology, make_engine
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

    return app


def request_storage(app: FastAPI) -> LocalStorage:
    storage: LocalStorage = app.state.storage
    return storage


def _job_json(j: Job) -> dict[str, Any]:
    return {
        "job_id": j.job_id,
        "matter_id": j.matter_id,
        "status": j.status,
        "provider": j.provider,
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
