"""MCP server: read-only, matter-scoped case tools with RBAC and audit (Postgres)."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from medchron.db.models import AuditRow, Base, MatterMember
from medchron.db.store import make_engine, save_chronology, save_ingested, save_issues
from medchron.evals.oracle import OracleProvider
from medchron.orchestrator.pipeline import RunConfig, run_case
from medchron.retrieval.embeddings import HashingEmbedder
from medchron.security.auth import Principal, Role
from medchron.synth import CaseGold

mcp = pytest.importorskip("mcp")
from mcp import Client  # noqa: E402

from medchron.mcp_server import build_server, fixed_principal  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
URL = os.environ.get("MEDCHRON_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="MEDCHRON_TEST_DATABASE_URL not set")
MATTER = "mcp-case"


@pytest.fixture(scope="module")
def engine(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Engine]:
    eng = make_engine(URL)
    with eng.begin() as conn:
        Base.metadata.drop_all(conn)
        conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
        cfg = Config(str(ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(ROOT / "migrations"))
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")
    dev = ROOT / "data" / "synthetic" / "dev"
    gold = CaseGold.model_validate_json((dev / "case_0004.gold.json").read_text())
    result = run_case(dev / "case_0004.pdf", MATTER, OracleProvider(gold),
                      RunConfig(out_dir=tmp_path_factory.mktemp("mcp")))  # fmt: skip
    with Session(eng) as s, s.begin():  # a system session, as the worker would use
        save_ingested(s, result.case, HashingEmbedder(), title="MCP test matter")
        save_chronology(s, result.chronology)
        save_issues(s, MATTER, result.issues)
        s.add(MatterMember(matter_id=MATTER, user_id="alice", added_by="test"))
    yield eng
    eng.dispose()


def _user(name: str, *roles: Role) -> Principal:
    return Principal(user_id=name, roles=frozenset(roles))


def call(engine: Engine, who: Principal, tool: str, args: dict[str, Any]) -> tuple[bool, Any]:
    """Call one tool through a real MCP client connected in-process."""

    async def go() -> tuple[bool, Any]:
        async with Client(build_server(engine, fixed_principal(who))) as client:
            result = await client.call_tool(tool, args)
            if result.is_error:
                return True, result.content[0].text  # type: ignore[union-attr]
            if result.structured_content is not None:
                data = result.structured_content
                return False, data.get("result", data)
            return False, json.loads(result.content[0].text)  # type: ignore[union-attr]

    return asyncio.run(go())


def test_tools_are_listed_as_read_only(engine: Engine) -> None:
    async def go() -> list[Any]:
        async with Client(
            build_server(engine, fixed_principal(_user("alice", Role.ATTORNEY)))
        ) as c:
            return (await c.list_tools()).tools

    tools = asyncio.run(go())
    assert {t.name for t in tools} == {"list_matters", "search", "get_page", "get_event",
                                       "list_issues"}  # fmt: skip
    assert all(t.annotations and t.annotations.read_only_hint for t in tools)


def test_member_can_read_the_matter(engine: Engine) -> None:
    alice = _user("alice", Role.ATTORNEY)
    err, matters = call(engine, alice, "list_matters", {})
    assert not err and [m["matter_id"] for m in matters] == [MATTER]
    err, hits = call(engine, alice, "search", {"matter_id": MATTER, "query": "bile leak"})
    assert not err and hits and "bile leak" in hits[0]["text"].lower()
    assert hits[0]["bates"].startswith("MCPCASE-") and hits[0]["span_id"].startswith(MATTER)
    err, page = call(engine, alice, "get_page", {"matter_id": MATTER, "page_no": 2})
    assert not err and page["page"] == 2 and page["bates"] == "MCPCASE-000002" and page["text"]
    err, issues = call(engine, alice, "list_issues", {"matter_id": MATTER})
    assert not err and issues and all(i["citations"] for i in issues)
    with Session(engine) as s:
        event_id = s.execute(
            text("SELECT event_id FROM events WHERE matter_id = :m LIMIT 1"), {"m": MATTER}
        ).scalar_one()
    err, event = call(engine, alice, "get_event", {"matter_id": MATTER, "event_id": event_id})
    assert not err and event["event_id"] == event_id and event["sources"][0]["bates"]


def test_non_members_and_wrong_roles_are_refused(engine: Engine) -> None:
    mallory = _user("mallory", Role.ATTORNEY)  # attorney, but not on this matter
    err, msg = call(engine, mallory, "get_page", {"matter_id": MATTER, "page_no": 1})
    assert err and "not found" in msg
    err, matters = call(engine, mallory, "list_matters", {})
    assert not err and matters == []
    auditor = _user("alice", Role.AUDITOR)  # auditors may not read record content
    err, msg = call(engine, auditor, "search", {"matter_id": MATTER, "query": "bile"})
    assert err and "matter.read" in msg
    err, msg = call(engine, _user("alice", Role.ATTORNEY), "get_page",
                    {"matter_id": MATTER, "page_no": 999})  # fmt: skip
    assert err and "not found" in msg


def test_every_call_is_audited_with_hashed_queries(engine: Engine) -> None:
    alice = _user("alice", Role.PARALEGAL)
    call(engine, alice, "search", {"matter_id": MATTER, "query": "secret phrase about bile"})
    with Session(engine) as s:
        rows = s.scalars(select(AuditRow).where(AuditRow.action.like("mcp.%"))).all()
    actions = {r.action for r in rows}
    assert {"mcp.list_matters", "mcp.search", "mcp.get_page", "mcp.get_event",
            "mcp.list_issues"} <= actions  # fmt: skip
    searches = [r for r in rows if r.action == "mcp.search"]
    assert all(r.actor in {"alice"} and len(r.details["query_sha256"]) == 64 for r in searches)
    assert not any("secret phrase" in json.dumps(r.details) for r in rows)
