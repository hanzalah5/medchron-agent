"""Security tests: RBAC, matter isolation (API and Postgres RLS), audit chain, auth, logging."""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from medchron.security import auth
from medchron.security.logs import JsonFormatter, ScrubFilter, scrub

ROOT = Path(__file__).resolve().parents[1]
URL = os.environ.get("MEDCHRON_TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not URL, reason="MEDCHRON_TEST_DATABASE_URL not set")


# --------------------------------------------------------------------------- no database
def test_role_permissions() -> None:
    para = auth.Principal("p", frozenset({auth.Role.PARALEGAL}))
    atty = auth.Principal("a", frozenset({auth.Role.ATTORNEY}))
    admin = auth.Principal("x", frozenset({auth.Role.ADMIN}))
    auditor = auth.Principal("u", frozenset({auth.Role.AUDITOR}))
    assert para.can("events.review") and not para.can("issues.triage")
    assert atty.can("memo.approve") and atty.can("issues.triage")
    assert not admin.can("matter.read")  # admins manage access, they don't read records
    assert auditor.can("audit.read") and not auditor.can("matter.read")
    assert not auth.Principal("n", frozenset()).can("matter.read")


def test_dev_mode_refused_outside_development(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEDCHRON_AUTH_MODE", "dev")
    monkeypatch.setenv("MEDCHRON_ENV", "production")
    with pytest.raises(RuntimeError):
        auth.auth_mode()


class _Key:
    def __init__(self, key: Any) -> None:
        self.key = key


@pytest.fixture
def oidc(monkeypatch: pytest.MonkeyPatch) -> Any:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setenv("MEDCHRON_AUTH_MODE", "oidc")
    monkeypatch.setenv("MEDCHRON_OIDC_ISSUER", "https://idp.example/realms/firm")
    monkeypatch.setenv("MEDCHRON_OIDC_AUDIENCE", "medchron")
    client = type("C", (), {"get_signing_key_from_jwt": lambda self, t: _Key(private.public_key())})
    monkeypatch.setattr(auth, "_jwks", lambda url: client())

    def token(**over: Any) -> str:
        now = dt.datetime.now(dt.UTC)
        claims: dict[str, Any] = {
            "iss": "https://idp.example/realms/firm",
            "aud": "medchron",
            "sub": "123",
            "preferred_username": "jdoe",
            "iat": now,
            "exp": now + dt.timedelta(minutes=5),
            "realm_access": {"roles": ["attorney", "offline_access"]},
        }
        claims.update(over)
        return jwt.encode(claims, private, algorithm="RS256")

    return token


def test_oidc_valid_token(oidc: Any) -> None:
    p = auth.verify_token(oidc())
    assert p.user_id == "jdoe" and p.roles == frozenset({auth.Role.ATTORNEY})


@pytest.mark.parametrize(
    "bad",
    [
        {"aud": "other-app"},
        {"iss": "https://evil.example"},
        {"exp": dt.datetime(2020, 1, 1, tzinfo=dt.UTC)},
    ],
)
def test_oidc_rejects_bad_tokens(oidc: Any, bad: dict[str, Any]) -> None:
    with pytest.raises(auth.AuthError) as e:
        auth.verify_token(oidc(**bad))
    assert e.value.status_code == 401


def test_log_scrubbing() -> None:
    line = "Patient DOB: 03/14/1980 MRN: X1234567 phone 555-123-4567 email a.b@example.com"
    out = scrub(line)
    for leaked in ("03/14/1980", "X1234567", "555-123-4567", "a.b@example.com"):
        assert leaked not in out
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "ssn %s", ("123-45-6789",), None)
    ScrubFilter().filter(record)
    assert "123-45-6789" not in json.loads(JsonFormatter().format(record))["msg"]


def test_pipeline_logs_contain_no_record_text(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    from medchron.evals.oracle import OracleProvider
    from medchron.orchestrator.pipeline import RunConfig, run_case
    from medchron.synth import CaseGold

    gold = CaseGold.model_validate_json(
        (ROOT / "data/synthetic/dev/case_0004.gold.json").read_text()
    )
    with caplog.at_level(logging.DEBUG):
        run_case(ROOT / "data/synthetic/dev/case_0004.pdf", "c3", OracleProvider(gold),
                 RunConfig(out_dir=tmp_path))  # fmt: skip
    logged = "\n".join(r.getMessage() + json.dumps(getattr(r, "llm", {})) for r in caplog.records)
    assert gold.patient.name not in logged
    for ev in gold.events:
        assert ev.sources[0].quote not in logged


# --------------------------------------------------------------------------- database
@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    from alembic import command
    from alembic.config import Config

    from medchron.db.models import Base
    from medchron.db.store import make_engine

    eng = make_engine(URL)
    with eng.begin() as conn:
        Base.metadata.drop_all(conn)
        for t in ("alembic_version", "audit_log", "matter_members"):
            conn.execute(text(f"DROP TABLE IF EXISTS {t} CASCADE"))
        cfg = Config(str(ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(ROOT / "migrations"))
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")
    yield eng
    eng.dispose()


def _client(engine: Engine, tmp: Path, user: str, roles: str) -> TestClient:
    from medchron.api.app import create_app
    from medchron.storage import LocalStorage

    app = create_app(engine, LocalStorage(tmp / "storage"))
    return TestClient(app, headers={"X-Dev-User": user, "X-Dev-Roles": roles})


@pytest.fixture(scope="module")
def matter(engine: Engine, tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    from medchron.jobs import work

    tmp = tmp_path_factory.mktemp("sec")
    os.environ["MEDCHRON_GOLD_DIR"] = str(ROOT / "data" / "synthetic")
    os.environ["MEDCHRON_RUNS_DIR"] = str(tmp / "runs")
    os.environ["MEDCHRON_STORAGE_DIR"] = str(tmp / "storage")
    alice = _client(engine, tmp, "alice", "attorney")
    pdf = (ROOT / "data/synthetic/dev/case_0001.pdf").read_bytes()
    r = alice.post("/api/matters", files={"file": ("c1.pdf", pdf)},
                   data={"matter_id": "wall-1", "provider": "oracle"})  # fmt: skip
    assert r.status_code == 202, r.text
    assert work(engine, once=True) == 1
    return {"tmp": tmp, "alice": alice}


@needs_db
def test_non_member_cannot_see_matter(engine: Engine, matter: dict[str, Any]) -> None:
    mallory = _client(engine, matter["tmp"], "mallory", "attorney")
    for url in [
        "/api/matters/wall-1",
        "/api/matters/wall-1/chronology",
        "/api/matters/wall-1/pages/1",
        "/api/matters/wall-1/pages/1/image",
        "/api/matters/wall-1/issues",
    ]:
        assert mallory.get(url).status_code == 404, url  # 404 hides that the matter exists
    assert mallory.get("/api/matters").json() == []
    # A matter ID can't be claimed twice, even by someone who can't see the first one.
    r = mallory.post("/api/matters", files={"file": ("x.pdf", b"%PDF-1.4")},
                     data={"matter_id": "wall-1"})  # fmt: skip
    assert r.status_code == 409


@needs_db
def test_roles_limit_actions(engine: Engine, matter: dict[str, Any]) -> None:
    alice: TestClient = matter["alice"]
    assert alice.post("/api/matters/wall-1/members", json={"user_id": "pat"}).status_code == 201
    pat = _client(engine, matter["tmp"], "pat", "paralegal")
    assert pat.get("/api/matters/wall-1/chronology").status_code == 200
    issue = pat.get("/api/matters/wall-1/issues").json()[0]["issue_id"]
    r = pat.patch(f"/api/matters/wall-1/issues/{issue}", json={"status": "expert"})
    assert r.status_code == 403  # triage is an attorney decision
    assert pat.post("/api/matters/wall-1/memo", json={"provider": "oracle"}).status_code == 403
    assert pat.get("/api/audit").status_code == 403
    nobody = _client(engine, matter["tmp"], "anon", "")
    assert nobody.get("/api/matters/wall-1").status_code == 403


@needs_db
def test_admin_manages_walls_without_reading(engine: Engine, matter: dict[str, Any]) -> None:
    admin = _client(engine, matter["tmp"], "root", "admin")
    assert admin.get("/api/matters/wall-1/chronology").status_code == 403
    assert admin.post("/api/matters/wall-1/members", json={"user_id": "bob"}).status_code == 201
    bob = _client(engine, matter["tmp"], "bob", "attorney")
    assert bob.get("/api/matters/wall-1/chronology").status_code == 200


@needs_db
def test_unauthenticated_requests_rejected(engine: Engine, matter: dict[str, Any]) -> None:
    from medchron.api.app import create_app

    client = TestClient(create_app(engine))
    assert client.get("/api/matters").status_code == 401
    assert client.get("/healthz").status_code == 200


@needs_db
def test_row_level_security_in_postgres(engine: Engine, matter: dict[str, Any]) -> None:
    """Even raw SQL, without the API's checks, only sees the actor's matters."""
    with engine.begin() as conn:
        is_super = conn.execute(
            text("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
        ).scalar()
        if is_super:  # superusers bypass RLS, so probe through an unprivileged role
            conn.execute(text(
                "DO $$ BEGIN CREATE ROLE medchron_rls_probe NOLOGIN; "
                "EXCEPTION WHEN duplicate_object THEN NULL; END $$"
            ))  # fmt: skip
            conn.execute(text("GRANT SELECT ON ALL TABLES IN SCHEMA public TO medchron_rls_probe"))
    probe = "SET LOCAL ROLE medchron_rls_probe; " if is_super else ""

    def visible(actor: str | None) -> int:
        with Session(engine, info={"actor": actor} if actor else {}) as s:
            if probe:
                s.execute(text(probe))
            return int(
                s.execute(text("SELECT count(*) FROM pages WHERE matter_id = 'wall-1'")).scalar()
                or 0
            )

    assert visible("alice") > 0
    assert visible("mallory") == 0
    assert visible(None) > 0  # trusted backend process (worker)


@needs_db
def test_audit_chain_records_and_detects_tampering(engine: Engine, matter: dict[str, Any]) -> None:
    from medchron.security import audit

    assert matter["alice"].get("/api/matters/wall-1/pages/2").status_code == 200
    auditor = _client(engine, matter["tmp"], "audrey", "auditor")
    entries = auditor.get("/api/audit", params={"matter_id": "wall-1"}).json()
    actions = {e["action"] for e in entries}
    assert {"matter.create", "llm.call", "page.view", "matter.member_add"} <= actions
    assert "SYNTHETIC RECORD" not in json.dumps(entries)  # metadata only, never record text
    assert auditor.get("/api/audit/verify").json()["ok"] is True

    with engine.connect() as conn, conn.begin() as tx:
        with pytest.raises(Exception, match="append-only"), conn.begin_nested():
            conn.execute(text("UPDATE audit_log SET actor = 'someone-else' WHERE id = 1"))
        # A privileged attacker who disables the trigger is still caught by the hash chain.
        conn.execute(text("ALTER TABLE audit_log DISABLE TRIGGER audit_log_immutable"))
        conn.execute(text("UPDATE audit_log SET actor = 'someone-else' WHERE id = 2"))
        ok, _, bad = audit.verify_chain(Session(bind=conn))
        assert not ok and bad == 2
        tx.rollback()


@needs_db
def test_stale_jobs_are_recovered(engine: Engine, matter: dict[str, Any]) -> None:
    from medchron.db.models import Job
    from medchron.jobs import MAX_ATTEMPTS, requeue_stale

    alice: TestClient = matter["alice"]
    job_id = alice.get("/api/matters/wall-1").json()["jobs"][0]["job_id"]
    old = dt.datetime.now(dt.UTC) - dt.timedelta(hours=1)
    with Session(engine) as s, s.begin():
        job = s.get(Job, job_id)
        assert job is not None
        job.status, job.heartbeat_at, job.attempts = "running", old, 1
    assert requeue_stale(engine) == 1
    with Session(engine) as s, s.begin():
        job = s.get(Job, job_id)
        assert job is not None and job.status == "queued"
        job.status, job.heartbeat_at, job.attempts = "running", old, MAX_ATTEMPTS
    requeue_stale(engine)
    with Session(engine) as s:
        job = s.get(Job, job_id)
        assert job is not None and job.status == "failed"
