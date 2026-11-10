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


# When set (as in CI), the API and the worker connect as the non-owner roles, so these
# tests run with Postgres row-level security enforced. Otherwise they use the owner.
APP_URL = os.environ.get("MEDCHRON_TEST_APP_DATABASE_URL")
WORKER_URL = os.environ.get("MEDCHRON_TEST_WORKER_DATABASE_URL")


@pytest.fixture(scope="module")
def app_engine(engine: Engine) -> Iterator[Engine]:
    eng = make_engine(APP_URL) if APP_URL else engine
    yield eng
    if APP_URL:
        eng.dispose()


@pytest.fixture(scope="module")
def worker_engine(engine: Engine) -> Iterator[Engine]:
    eng = make_engine(WORKER_URL) if WORKER_URL else engine
    yield eng
    if WORKER_URL:
        eng.dispose()


@pytest.fixture(scope="module")
def client(app_engine: Engine, tmp_path_factory: pytest.TempPathFactory) -> Iterator[TestClient]:
    tmp = tmp_path_factory.mktemp("api")
    os.environ["MEDCHRON_GOLD_DIR"] = str(ROOT / "data" / "synthetic")
    os.environ["MEDCHRON_RUNS_DIR"] = str(tmp / "runs")
    app = create_app(app_engine, LocalStorage(tmp / "storage"))
    with TestClient(app, headers={"X-Dev-User": "alice", "X-Dev-Roles": "attorney"}) as c:
        yield c


@pytest.fixture(scope="module")
def processed(worker_engine: Engine, client: TestClient, engine: Engine) -> dict[str, str]:
    pdf = (ROOT / "data/synthetic/dev/case_0004.pdf").read_bytes()
    r = client.post(
        "/api/matters",
        files={"file": ("case_0004.pdf", pdf, "application/pdf")},
        data={"matter_id": "demo-0003", "title": "Synthetic case 3", "provider": "oracle"},
    )
    assert r.status_code == 202, r.text
    body = r.json()
    storage_root = client.app.state.storage.root  # type: ignore[attr-defined]
    os.environ["MEDCHRON_STORAGE_DIR"] = str(storage_root)
    assert work(worker_engine, once=True) == 1
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
    hits = client.get("/api/matters/demo-0003/search", params={"q": "bile leak cystic duct stump"})
    assert hits.status_code == 200
    assert "bile leak" in hits.json()[0]["text"].lower()


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


def test_failed_job_is_recorded(worker_engine: Engine, client: TestClient, engine: Engine) -> None:
    r = client.post(
        "/api/matters",
        files={"file": ("x.pdf", b"%PDF-1.4 not really a pdf", "application/pdf")},
        data={"matter_id": "broken", "provider": "oracle"},
    )
    assert r.status_code == 202
    assert work(worker_engine, once=True) == 1
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

    gated = client.get("/api/matters/demo-0003/export.md")
    assert gated.status_code == 409 and "approval" in gated.json()["detail"]
    assert client.post("/api/matters/demo-0003/chronology/approve").status_code == 200
    md = client.get("/api/matters/demo-0003/export.md")
    assert md.status_code == 200 and "attachment" in md.headers["content-disposition"]
    text_md = md.text
    assert "Corrected summary" in text_md and "2024-01-02" in text_md
    assert "review:accepted" in text_md
    assert "review:rejected" not in text_md  # rejected events are excluded


def test_issue_triage_and_memo_signoff(
    worker_engine: Engine, client: TestClient, processed: dict[str, str], engine: Engine
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
    assert work(worker_engine, once=True) == 1
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


def test_second_production_creates_a_version_and_keeps_review_state(
    worker_engine: Engine, client: TestClient, engine: Engine
) -> None:
    """Upload production 1, review an event, upload production 2: the chronology gets a
    new version with a diff, earlier work is reused, and the review decision survives."""
    dev = ROOT / "data/synthetic/dev"
    manifest = json.loads((dev / "manifest.json").read_text())
    case = next(m for m in manifest if m["productions"] == 2)
    p1, p2 = ((dev / name).read_bytes() for name in case["pdfs"])
    mid = "incremental-1"
    r = client.post(
        "/api/matters",
        files={"file": ("p1.pdf", p1, "application/pdf")},
        data={"matter_id": mid, "provider": "oracle"},
    )
    assert r.status_code == 202, r.text
    assert work(worker_engine, once=True) == 1
    versions = client.get(f"/api/matters/{mid}/chronology/versions").json()
    assert [v["version"] for v in versions] == [1] and versions[0]["diff"] is None
    first_events = client.get(f"/api/matters/{mid}/chronology").json()["events"]
    reviewed = first_events[0]["event_id"]
    client.patch(f"/api/matters/{mid}/events/{reviewed}", json={"status": "accepted"})

    dup = client.post(
        f"/api/matters/{mid}/productions",
        files={"file": ("again.pdf", p1, "application/pdf")},
        data={"provider": "oracle"},
    )
    assert dup.status_code == 409  # the same file twice is refused
    r = client.post(
        f"/api/matters/{mid}/productions",
        files={"file": ("p2.pdf", p2, "application/pdf")},
        data={"provider": "oracle"},
    )
    assert r.status_code == 202 and r.json()["production_no"] == 2
    assert work(worker_engine, once=True) == 1
    job = client.get(f"/api/jobs/{r.json()['job_id']}").json()
    assert job["status"] == "completed", job["error"]
    assert job["summary"]["reused_documents"] > 0 and job["summary"]["chronology_version"] == 2

    detail = client.get(f"/api/matters/{mid}").json()
    prods = detail["productions"]
    assert [p["production_no"] for p in prods] == [1, 2]
    assert prods[1]["page_offset"] == prods[0]["page_count"] > 0

    versions = client.get(f"/api/matters/{mid}/chronology/versions").json()
    assert [v["version"] for v in versions] == [1, 2] and versions[1]["diff"]["added"] > 0
    diff = client.get(f"/api/matters/{mid}/chronology/diff").json()
    assert diff["from_version"] == 1 and diff["to_version"] == 2
    assert diff["added"] and not diff["removed"]
    assert client.get(f"/api/matters/{mid}/chronology/diff", params={"to": 9}).status_code == 404
    review = client.get(f"/api/matters/{mid}/review").json()
    assert review["events"][reviewed]["status"] == "accepted"  # survived the re-run

    audit_rows = client.get("/api/audit", params={"matter_id": mid},
                            headers={"X-Dev-Roles": "auditor"}).json()  # fmt: skip
    actions = {a["action"] for a in audit_rows}
    assert {"production.add", "chronology.diff_view"} <= actions


def test_productions_respect_matter_access(client: TestClient) -> None:
    pdf = (ROOT / "data/synthetic/dev/case_0005.pdf").read_bytes()
    other = {"X-Dev-User": "mallory", "X-Dev-Roles": "attorney"}
    r = client.post(
        "/api/matters/incremental-1/productions",
        files={"file": ("x.pdf", pdf, "application/pdf")},
        data={"provider": "oracle"},
        headers=other,
    )
    assert r.status_code == 404  # not a member: the matter's existence is not revealed
    assert (
        client.get("/api/matters/incremental-1/chronology/versions", headers=other).status_code
        == 404
    )
    auditor = {"X-Dev-User": "audrey", "X-Dev-Roles": "auditor"}
    r = client.post(
        "/api/matters/incremental-1/productions",
        files={"file": ("x.pdf", pdf, "application/pdf")},
        headers=auditor,
    )
    assert r.status_code == 403


def test_chronology_export_is_gated_audited_and_bates_numbered(
    client: TestClient, processed: dict[str, str]
) -> None:
    import io

    from docx import Document
    from openpyxl import load_workbook

    base = "/api/matters/demo-0003"
    paralegal = {"X-Dev-User": "alice", "X-Dev-Roles": "paralegal"}
    assert client.post(f"{base}/chronology/approve", headers=paralegal).status_code == 403
    client.post(f"{base}/index/confirm")
    approved = client.post(f"{base}/chronology/approve").json()
    assert approved["current"] and approved["approved_by"] == "alice" and approved["version"]

    page = client.get(f"{base}/pages/1").json()
    assert page["bates"] == "DEMO0003-000001"

    docx = client.get(f"{base}/chronology/export.docx")
    assert docx.status_code == 200
    assert docx.headers["content-type"].startswith("application/vnd.openxmlformats-officedocument")
    doc = Document(io.BytesIO(docx.content))
    header = [c.text for c in doc.tables[0].rows[0].cells]
    assert header == ["Date", "Provider / Facility", "Event", "Source (document, page, Bates)",
                      "Review"]  # fmt: skip
    assert any("DEMO0003-0000" in r.cells[3].text for r in doc.tables[0].rows[1:])
    assert any("SYNTHETIC" in para.text for para in doc.paragraphs)

    xlsx = client.get(f"{base}/chronology/export.xlsx")
    assert xlsx.status_code == 200
    wb = load_workbook(io.BytesIO(xlsx.content))
    ws = wb["Chronology"]
    assert [c.value for c in ws[1]] == header
    assert ws.freeze_panes == "A2" and ws.auto_filter.ref
    assert ws.max_row > 2 and "DEMO0003-" in str(ws.cell(row=2, column=4).value)
    assert client.get(f"{base}/chronology/export.pdf").status_code == 404

    # A review edit after approval makes the approval stale until it is renewed.
    chron = client.get(f"{base}/chronology").json()
    client.patch(f"{base}/events/{chron['events'][-1]['event_id']}", json={"status": "accepted"})
    stale = client.get(f"{base}/chronology/export.xlsx")
    assert stale.status_code == 409 and "approve it again" in stale.json()["detail"]
    assert client.get(f"{base}/review").json()["chronology_approval"]["current"] is False
    client.post(f"{base}/chronology/approve")
    assert client.get(f"{base}/chronology/export.xlsx").status_code == 200

    audit_rows = client.get("/api/audit", params={"matter_id": "demo-0003"},
                            headers={"X-Dev-Roles": "auditor"}).json()  # fmt: skip
    exports = [a for a in audit_rows if a["action"] == "chronology.export"]
    assert {e["details"]["format"] for e in exports} >= {"md", "docx", "xlsx"}
    assert all(len(e["details"]["sha256"]) == 64 for e in exports)
    assert any(a["action"] == "chronology.approve" for a in audit_rows)


def test_provider_index_and_treatment_gaps(client: TestClient, processed: dict[str, str]) -> None:
    base = "/api/matters/demo-0003"
    providers = client.get(f"{base}/providers").json()
    assert providers and all(p["facility"] and p["pages"] for p in providers)
    assert all(p["bates_ranges"] for p in providers)
    assert not any(p["provider"] == "Health Information Management" for p in providers)
    gaps = client.get(f"{base}/gaps", params={"min_days": 1}).json()
    assert gaps["encounters"] > 1 and gaps["intervals"]
    assert all(i["days"] >= 1 for i in gaps["intervals"])
    assert gaps["followup"]  # the planted missing post-operative visit
    wide = client.get(f"{base}/gaps", params={"min_days": 3650}).json()
    assert wide["intervals"] == []
    assert client.get(f"{base}/gaps", params={"min_days": 0}).status_code == 422
