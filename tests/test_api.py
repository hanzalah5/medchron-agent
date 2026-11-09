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


def test_review_workflow(client: TestClient, processed: dict[str, str]) -> None:
    chron = client.get("/api/matters/demo-0003/chronology").json()
    first, second, third = (e["event_id"] for e in chron["events"][:3])
    base = "/api/matters/demo-0003/events"

    ok = client.patch(f"{base}/{first}", json={"status": "accepted"})
    assert ok.status_code == 200 and ok.json()["status"] == "accepted"
    assert ok.json()["reviewed_by"] and ok.json()["reviewed_at"]

    assert client.patch(f"{base}/{second}", json={"status": "rejected"}).status_code == 422
    rej = client.patch(f"{base}/{second}", json={"status": "rejected", "note": "duplicate entry"})
    assert rej.status_code == 200

    assert client.patch(f"{base}/{third}", json={"status": "edited"}).status_code == 422
    edit = client.patch(
        f"{base}/{third}",
        json={"status": "edited", "summary": "Corrected summary", "date": "2024-01-02"},
    )
    assert edit.json()["edited_summary"] == "Corrected summary"
    assert client.patch(f"{base}/nope", json={"status": "accepted"}).status_code == 404
    assert client.patch(f"{base}/{first}", json={"status": "approved"}).status_code == 422

    state = client.get("/api/matters/demo-0003/review").json()
    assert state["events"][first]["status"] == "accepted"
    assert state["index_confirmed_at"] is None
    confirmed = client.post("/api/matters/demo-0003/index/confirm").json()
    assert confirmed["index_confirmed_by"]

    md = client.get("/api/matters/demo-0003/export.md")
    assert md.status_code == 200 and "attachment" in md.headers["content-disposition"]
    text_md = md.text
    assert "Corrected summary" in text_md and "2024-01-02" in text_md
    assert "review:accepted" in text_md
    assert "review:rejected" not in text_md  # rejected events are excluded


def test_issue_triage_and_memo_signoff(
    client: TestClient, processed: dict[str, str], engine: Engine
) -> None:
    base = "/api/matters/demo-0003"
    issues = client.get(f"{base}/issues").json()
    kinds = {i["category"] for i in issues}
    assert {"potential_delay", "missing_record", "prompt_injection"} <= kinds
    assert all(i["citations"] and i["triage_status"] == "pending" for i in issues)

    # Checkpoint 4 is gated on checkpoints 1 and 3.
    client.post(f"{base}/index/confirm")
    assert client.post(f"{base}/memo", json={"provider": "oracle"}).status_code == 409

    first = issues[0]["issue_id"]
    assert client.patch(f"{base}/issues/{first}", json={"status": "dismiss"}).status_code == 422
    for i in issues:
        status = "dismiss" if i["category"] == "prompt_injection" else "expert"
        r = client.patch(
            f"{base}/issues/{i['issue_id']}",
            json={"status": status, "note": "Injection text, flagged for the file."},
        )
        assert r.status_code == 200 and r.json()["triaged_by"]

    assert client.get(f"{base}/memo").status_code == 404
    r = client.post(f"{base}/memo", json={"provider": "oracle"})
    assert r.status_code == 202
    assert work(engine, once=True) == 1
    job = client.get(f"/api/jobs/{r.json()['job_id']}").json()
    assert job["status"] == "completed" and job["kind"] == "memo", job["error"]
    assert job["summary"]["invalid_citations"] == 0

    memo = client.get(f"{base}/memo").json()
    assert memo["status"] == "draft" and memo["markdown"].startswith("DRAFT")
    assert any(c["citations"] for c in memo["claims"])
    assert "automated review" not in memo["markdown"].lower()  # injection text kept out

    url = f"{base}/memo/{memo['memo_id']}"
    assert client.get(f"{url}/export.md").status_code == 409  # not approved yet
    approved = client.post(f"{url}/approve").json()
    assert approved["status"] == "approved" and approved["approved_by"]
    exported = client.get(f"{url}/export.md")
    assert exported.status_code == 200 and "Approved by" in exported.text
