"""Database tests. Run when MEDCHRON_TEST_DATABASE_URL points at Postgres + pgvector."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from medchron.db.models import Base
from medchron.db.store import load_ingested, make_engine, save_ingested
from medchron.ingestion import ingest_pdf
from medchron.retrieval.embeddings import HashingEmbedder

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


def test_save_load_round_trip_and_search(engine: Engine) -> None:
    case = ingest_pdf(ROOT / "data/synthetic/dev/case_0004.pdf", "case_0003", ocr=False)
    embedder = HashingEmbedder()
    with Session(engine) as s, s.begin():
        save_ingested(s, case, embedder, title="Synthetic case 3")
        save_ingested(s, case, embedder, title="Synthetic case 3")  # idempotent
    query = "plainto_tsquery('english', 'bile leak cystic duct stump')"
    with Session(engine) as s:
        loaded = load_ingested(s, "case_0003")
        assert loaded == case

        # Full-text search hits the right span.
        hit = s.execute(
            text(
                f"SELECT span_id FROM spans WHERE matter_id = :m AND tsv @@ {query} "
                f"ORDER BY ts_rank(tsv, {query}) DESC LIMIT 1"
            ),
            {"m": "case_0003"},
        ).scalar_one()
        assert "bile leak" in loaded.resolve(hit).lower()

        # Vector search returns the exact span for its own text.
        target = loaded.spans[3]
        vec = embedder.embed([target.text])[0]
        nearest = s.execute(
            text(
                "SELECT span_id FROM spans WHERE matter_id = :m "
                "ORDER BY embedding <=> CAST(:v AS vector) LIMIT 1"
            ),
            {"m": "case_0003", "v": str(vec)},
        ).scalar_one()
        assert nearest == target.span_id


def test_deleting_matter_cascades(engine: Engine) -> None:
    case = ingest_pdf(ROOT / "data/synthetic/dev/case_0005.pdf", "case_0005", ocr=False)
    with Session(engine) as s, s.begin():
        save_ingested(s, case, HashingEmbedder())
    with Session(engine) as s, s.begin():
        s.execute(text("DELETE FROM matters WHERE matter_id = 'case_0005'"))
    with Session(engine) as s:
        n = s.execute(text("SELECT count(*) FROM spans WHERE matter_id = 'case_0005'")).scalar()
        assert n == 0


def test_issue_triage_survives_a_rerun(engine: Engine) -> None:
    from medchron.chronology.models import Citation, Issue
    from medchron.db.models import IssueRow
    from medchron.db.store import save_issues

    case = ingest_pdf(ROOT / "data/synthetic/dev/case_0005.pdf", "triage-case", ocr=False)
    span = case.spans[0]

    def issue(iid: str, span_id: str) -> Issue:
        return Issue(
            issue_id=iid,
            category="potential_delay",
            description="Was the interval explained?",
            event_ids=[],
            citations=[Citation(span_id=span_id, page_no=span.page_no, quote="x",
                                char_start=0, char_end=1)],
        )  # fmt: skip

    with Session(engine) as s, s.begin():
        save_ingested(s, case, HashingEmbedder())
        save_issues(s, "triage-case", [issue("I001", span.span_id)])
        row = s.get(IssueRow, ("triage-case", "I001"))
        assert row is not None
        row.triage_status, row.triaged_by = "expert", "alice"
    with Session(engine) as s, s.begin():  # same issue found again, under a new ID
        save_issues(s, "triage-case", [issue("I007", span.span_id), issue("I008", "other")])
    with Session(engine) as s:
        kept = s.get(IssueRow, ("triage-case", "I007"))
        fresh = s.get(IssueRow, ("triage-case", "I008"))
        assert kept is not None and kept.triage_status == "expert" and kept.triaged_by == "alice"
        assert fresh is not None and fresh.triage_status == "pending"
