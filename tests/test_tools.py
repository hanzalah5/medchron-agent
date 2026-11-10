"""Retrieval and agent-tool tests (in-memory, no database)."""

from __future__ import annotations

from pathlib import Path

import pytest

from medchron.evals.oracle import OracleProvider
from medchron.ingestion import ingest_pdf
from medchron.llm.loop import ToolInputError
from medchron.orchestrator.pipeline import RunConfig, run_case
from medchron.retrieval.embeddings import HashingEmbedder
from medchron.retrieval.search import MemorySearch, rrf
from medchron.synth import CaseGold
from medchron.tools.case_tools import SPECS, CaseTools

SMOKE = Path(__file__).resolve().parents[1] / "data" / "synthetic" / "dev"
CASE = "case_0004"  # laparoscopic cholecystectomy: tables, a misfiled page, a missing follow-up


@pytest.fixture(scope="module")
def tools(tmp_path_factory: pytest.TempPathFactory) -> CaseTools:
    gold = CaseGold.model_validate_json((SMOKE / f"{CASE}.gold.json").read_text())
    result = run_case(
        SMOKE / f"{CASE}.pdf",
        CASE,
        OracleProvider(gold),
        RunConfig(out_dir=tmp_path_factory.mktemp("t")),
    )
    return CaseTools(result.case, result.chronology, MemorySearch(result.case, HashingEmbedder()))


def test_rrf_rewards_agreement() -> None:
    scores = rrf([["a", "b", "c"], ["b", "a", "d"]])
    assert max(scores, key=lambda k: scores[k]) in {"a", "b"}
    assert scores["a"] > scores["c"] and scores["b"] > scores["d"]


def test_memory_search_finds_relevant_span(tools: CaseTools) -> None:
    hits = tools.search_records({"query": "hepatobiliary scan bile leak cystic duct stump",
                                 "page_from": 0, "page_to": 0})  # fmt: skip
    assert "bile leak" in hits[0]["text"].lower()
    assert hits[0]["span_id"].startswith(f"{CASE}:p")


def test_search_respects_page_range(tools: CaseTools) -> None:
    hits = tools.search_records({"query": "patient", "page_from": 5, "page_to": 6})
    assert hits and all(5 <= h["page"] <= 6 for h in hits)


def test_get_page_and_bounds(tools: CaseTools) -> None:
    page = tools.get_page({"page_no": 2})
    assert page["page"] == 2 and page["spans"] and page["text"]
    with pytest.raises(ToolInputError):
        tools.get_page({"page_no": 0})


def test_query_timeline_filters(tools: CaseTools) -> None:
    labs = tools.query_timeline(
        {"date_from": "", "date_to": "", "event_type": "lab_result", "contains": ""}
    )
    assert labs and all(m["type"] == "lab_result" for m in labs)
    wbc = tools.query_timeline(
        {"date_from": "", "date_to": "", "event_type": "any", "contains": "bilirubin"}
    )
    assert wbc and all(s["quote"] for e in wbc for s in e["sources"])
    with pytest.raises(ToolInputError):
        tools.query_timeline(
            {"date_from": "yesterday", "date_to": "", "event_type": "any", "contains": ""}
        )


def test_find_gaps_rules(tools: CaseTools) -> None:
    assert tools.find_gaps({"rule": "followup"})  # planted missing post-operative visit
    intervals = tools.find_gaps({"rule": "intervals"})
    assert intervals and all(i["days"] > 0 for i in intervals)
    with pytest.raises(ToolInputError):
        tools.find_gaps({"rule": "nope"})


def test_tool_specs_are_strict_and_read_only() -> None:
    for spec in SPECS.values():
        assert spec.strict
        assert spec.input_schema["additionalProperties"] is False
        assert set(spec.input_schema["required"]) == set(spec.input_schema["properties"])
    assert set(SPECS) == {"search_records", "get_page", "query_timeline", "find_gaps"}


def test_ingest_is_reusable_for_search() -> None:
    case = ingest_pdf(SMOKE / "case_0005.pdf", "c5", ocr=False)
    assert MemorySearch(case, HashingEmbedder()).search("scaphoid fracture", limit=3)
