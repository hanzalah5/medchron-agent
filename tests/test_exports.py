"""Chronology exports, provider index and treatment gaps (no database)."""

from __future__ import annotations

import datetime as dt
import io
from pathlib import Path

import pytest

from medchron.chronology.analysis import page_ranges, provider_index, treatment_gaps
from medchron.chronology.export import COLUMNS, ExportMeta, export_rows, to_docx, to_xlsx
from medchron.chronology.models import Chronology
from medchron.evals.oracle import OracleProvider
from medchron.ingestion.pipeline import bates_id, bates_prefix_for
from medchron.orchestrator.pipeline import RunConfig, RunResult, run_case
from medchron.synth import CaseGold

DEV = Path(__file__).resolve().parents[1] / "data" / "synthetic" / "dev"


@pytest.fixture(scope="module")
def result(tmp_path_factory: pytest.TempPathFactory) -> RunResult:
    gold = CaseGold.model_validate_json((DEV / "case_0004.gold.json").read_text())
    return run_case(
        DEV / "case_0004.pdf",
        "case_0004",
        OracleProvider(gold),
        RunConfig(out_dir=tmp_path_factory.mktemp("x"), bates_prefix="SMITH"),
    )


def test_bates_ids_are_assigned_at_ingestion(result: RunResult) -> None:
    assert [p.bates for p in result.case.pages[:2]] == ["SMITH-000001", "SMITH-000002"]
    assert bates_prefix_for("demo-0003") == "DEMO0003" and bates_prefix_for("--") == "MC"
    assert bates_id("X", 42) == "X-000042"


def test_export_rows_follow_the_conventional_layout(result: RunResult) -> None:
    bates = {p.page_no: p.bates for p in result.case.pages}
    rows = export_rows(result.chronology, bates)
    assert len(rows) == len(result.chronology.events)
    for row, event in zip(rows, result.chronology.events, strict=True):
        assert row[0] == (event.date.isoformat() if event.date else "(undated)")
        assert event.summary in row[2]
        assert f"p{event.sources[0].page_no}, SMITH-" in row[3]
        assert row[4] == "pending"


def test_docx_and_xlsx_open_and_carry_the_banner(result: RunResult) -> None:
    from docx import Document
    from openpyxl import load_workbook

    meta = ExportMeta(matter_id="m1", title="Synthetic", version=2, approved_by="alice",
                      approved_at=dt.datetime(2026, 1, 2, 3, 4, tzinfo=dt.UTC))  # fmt: skip
    bates = {p.page_no: p.bates for p in result.case.pages}
    doc = Document(io.BytesIO(to_docx(result.chronology, meta, bates)))
    assert [c.text for c in doc.tables[0].rows[0].cells] == COLUMNS
    text = "\n".join(p.text for p in doc.paragraphs)
    assert "SYNTHETIC" in text and "Chronology version: 2" in text and "alice" in text
    wb = load_workbook(io.BytesIO(to_xlsx(result.chronology, meta, bates)))
    assert [c.value for c in wb["Chronology"][1]] == COLUMNS
    assert "SYNTHETIC" in str(wb["About"]["A1"].value)


def test_page_ranges() -> None:
    assert page_ranges([7, 1, 2, 3, 3]) == [(1, 3), (7, 7)]
    assert page_ranges([]) == []


def test_provider_index_and_gaps(result: RunResult) -> None:
    chron: Chronology = result.chronology
    index = provider_index(chron, {p.page_no: p.bates for p in result.case.pages})
    assert index and sum(e.events for e in index) == len(chron.events)
    assert all(e.bates_ranges and e.pages == sorted(e.pages) for e in index)
    dated = [e.first_date for e in index if e.first_date]
    assert dated == sorted(dated)
    gaps = treatment_gaps(chron, min_days=1)
    assert gaps.intervals and all(i.days >= 1 for i in gaps.intervals)
    assert all(i.from_date < i.to_date for i in gaps.intervals)
    assert treatment_gaps(chron, min_days=10_000).intervals == []
