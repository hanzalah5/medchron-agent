"""API and job-queue tests (Postgres). The pipeline runs in oracle demo mode."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine

from medchron.api.app import create_app
from medchron.db.models import Base
from medchron.db.store import make_engine
from medchron.jobs import work
from medchron.storage import LocalStorage

ROOT = Path(__file__).resolve().parents[1]
URL = os.environ.get("MEDCHRON_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="MEDCHRON_TEST_DATABASE_URL not set")


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    eng = make_engine(URL)
    with eng.begin() as conn:
        Base.metadata.drop_all(conn)
        conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
        cfg = Config(str(ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(ROOT / "migrations"))
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")
    yield eng
    eng.dispose()


@pytest.fixture(scope="module")
def client(engine: Engine, tmp_path_factory: pytest.TempPathFactory) -> Iterator[TestClient]:
    tmp = tmp_path_factory.mktemp("api")
    os.environ["MEDCHRON_GOLD_DIR"] = str(ROOT / "data" / "synthetic")
    os.environ["MEDCHRON_RUNS_DIR"] = str(tmp / "runs")
    app = create_app(engine, LocalStorage(tmp / "storage"))
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def processed(client: TestClient, engine: Engine) -> dict[str, str]:
    pdf = (ROOT / "data/synthetic/case_0003.pdf").read_bytes()
    r = client.post(
        "/api/matters",
        files={"file": ("case_0003.pdf", pdf, "application/pdf")},
        data={"matter_id": "demo-0003", "title": "Synthetic case 3", "provider": "oracle"},
    )
    assert r.status_code == 202, r.text
    body = r.json()
    storage_root = client.app.state.storage.root  # type: ignore[attr-defined]
    os.environ["MEDCHRON_STORAGE_DIR"] = str(storage_root)
    assert work(engine, once=True) == 1
    return dict(body)


def test_healthz(client: TestClient) -> None:
    assert client.get("/healthz").json()["status"] == "ok"


def test_job_completes_and_streams_progress(client: TestClient, processed: dict[str, str]) -> None:
    job = client.get(f"/api/jobs/{processed['job_id']}").json()
    assert job["status"] == "completed", job["error"]
    assert job["summary"]["events"] > 10
    with client.stream("GET", f"/api/jobs/{processed['job_id']}/events") as r:
        lines = [json.loads(line[6:]) for line in r.iter_lines() if line.startswith("data: ")]
    assert lines[0]["message"] == "queued"
    assert any("extract" in m.get("message", "") for m in lines)
    assert lines[-1] == {"status": "completed", "done": True}


def test_chronology_and_pages(client: TestClient, processed: dict[str, str]) -> None:
    chron = client.get("/api/matters/demo-0003/chronology").json()
    assert len(chron["events"]) > 10 and chron["documents"]
    assert any(g for g in chron["gaps"])  # planted missing follow-up record
    src = chron["events"][0]["sources"][0]
    page = client.get(f"/api/matters/demo-0003/pages/{src['page_no']}").json()
    assert page["text"][src["char_start"] : src["char_end"]].strip()
    assert page["words"] and page["width"] > 0
    img = client.get(f"/api/matters/demo-0003/pages/{src['page_no']}/image")
    assert img.status_code == 200 and img.content.startswith(b"\x89PNG")


def test_hybrid_search(client: TestClient, processed: dict[str, str]) -> None:
    hits = client.get("/api/matters/demo-0003/search", params={"q": "perforated appendicitis"})
    assert hits.status_code == 200
    assert "appendicitis" in hits.json()[0]["text"].lower()


def test_list_and_detail(client: TestClient, processed: dict[str, str]) -> None:
    matters = client.get("/api/matters").json()
    assert any(m["matter_id"] == "demo-0003" and m["job"]["status"] == "completed" for m in matters)
    detail = client.get("/api/matters/demo-0003").json()
    assert detail["pages"] > 0 and detail["jobs"]


@pytest.mark.parametrize(
    ("data", "content", "status"),
    [
        ({"matter_id": "x1"}, b"hello", 415),
        ({"matter_id": "bad id!"}, b"%PDF-1.4", 422),
        ({"matter_id": "x2", "provider": "gpt"}, b"%PDF-1.4", 422),
        ({"matter_id": "demo-0003"}, b"%PDF-1.4", 409),
    ],
)
def test_upload_validation(
    client: TestClient, processed: dict[str, str], data: dict[str, str], content: bytes, status: int
) -> None:
    r = client.post("/api/matters", files={"file": ("f.pdf", content)}, data=data)
    assert r.status_code == status


def test_not_found(client: TestClient) -> None:
    assert client.get("/api/matters/nope/chronology").status_code == 404
    assert client.get("/api/jobs/nope").status_code == 404
    assert client.get("/api/matters/demo-0003/pages/999").status_code == 404


def test_failed_job_is_recorded(client: TestClient, engine: Engine) -> None:
    r = client.post(
        "/api/matters",
        files={"file": ("x.pdf", b"%PDF-1.4 not really a pdf", "application/pdf")},
        data={"matter_id": "broken", "provider": "oracle"},
    )
    assert r.status_code == 202
    assert work(engine, once=True) == 1
    job = client.get(f"/api/jobs/{r.json()['job_id']}").json()
    assert job["status"] == "failed" and job["error"]
