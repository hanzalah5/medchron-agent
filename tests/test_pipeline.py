"""End-to-end pipeline tests using the gold-label oracle (no API key needed)."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest

from medchron.chronology.builder import find_conflicts, find_followup_gaps, merge_events
from medchron.chronology.models import Confidence, EventSource, ExtractedEvent
from medchron.evals.oracle import OracleProvider
from medchron.grounding.dates import find_dates, parse_date
from medchron.grounding.quotes import find_quote, normalize
from medchron.ingestion import ingest_pdf
from medchron.llm.types import LLMRefusalError, LLMRequest, LLMResponse, Usage
from medchron.orchestrator.pipeline import RunConfig, RunResult, run_case
from medchron.synth import CaseGold
from medchron.taxonomy import EventType, IssueKind

SMOKE = Path(__file__).resolve().parents[1] / "data" / "synthetic"
CASES = [m["case_id"] for m in json.loads((SMOKE / "manifest.json").read_text())]


def gold_of(cid: str) -> CaseGold:
    return CaseGold.model_validate_json((SMOKE / f"{cid}.gold.json").read_text())


class Counting:
    name = "counting"

    def __init__(self, inner: Any) -> None:
        self.inner, self.calls = inner, 0

    def complete(self, req: LLMRequest) -> LLMResponse:
        self.calls += 1
        return self.inner.complete(req)  # type: ignore[no-any-return]


@pytest.fixture(scope="module", params=CASES)
def oracle_run(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> tuple[RunResult, CaseGold]:
    cid = request.param
    gold = gold_of(cid)
    out = tmp_path_factory.mktemp(cid)
    return run_case(SMOKE / f"{cid}.pdf", cid, OracleProvider(gold), RunConfig(out_dir=out)), gold


# --------------------------------------------------------------------------- end to end
def test_segmentation_matches_gold(oracle_run: tuple[RunResult, CaseGold]) -> None:
    result, gold = oracle_run
    got = [(d.start_page, d.doc_type) for d in result.chronology.documents]
    want = [(d.start_page, d.doc_type) for d in gold.documents]
    assert got == want


def test_every_gold_event_is_recovered(oracle_run: tuple[RunResult, CaseGold]) -> None:
    result, gold = oracle_run
    scanned = {p.page for p in gold.pages if not p.text_layer}
    for ev in gold.events:
        pages = {s.page for s in ev.sources}
        found = any(
            e.type == ev.type and e.date == ev.date and pages & {s.page_no for s in e.sources}
            for e in result.chronology.events
        )
        # Only events whose every source is a scan may be missed (OCR limits).
        assert found or pages <= scanned, ev.event_id


def test_every_output_event_is_grounded(oracle_run: tuple[RunResult, CaseGold]) -> None:
    result, _ = oracle_run
    case = ingest_pdf(SMOKE / f"{result.summary.case_id}.pdf", result.summary.case_id)
    for e in result.chronology.events:
        for s in e.sources:
            page_text = case.page(s.page_no).text
            cited = page_text[s.char_start : s.char_end]
            assert find_quote(cited, s.quote, threshold=0.85) is not None, e.event_id


def test_injection_is_flagged_not_followed(oracle_run: tuple[RunResult, CaseGold]) -> None:
    result, gold = oracle_run
    planted = any(i.kind == IssueKind.PROMPT_INJECTION for i in gold.issues)
    flagged = any(f.kind == "possible_prompt_injection" for f in result.chronology.flags)
    assert planted == flagged


def test_outputs_written(oracle_run: tuple[RunResult, CaseGold], tmp_path: Path) -> None:
    result, _ = oracle_run
    assert result.summary.status == "completed"
    assert result.summary.llm_calls == 1 + len(result.chronology.documents)


# --------------------------------------------------------------------------- resilience
def test_rerun_resumes_from_checkpoints(tmp_path: Path) -> None:
    cid = CASES[0]
    first = Counting(OracleProvider(gold_of(cid)))
    run_case(SMOKE / f"{cid}.pdf", cid, first, RunConfig(out_dir=tmp_path))
    second = Counting(OracleProvider(gold_of(cid)))
    again = run_case(SMOKE / f"{cid}.pdf", cid, second, RunConfig(out_dir=tmp_path))
    assert first.calls > 0 and second.calls == 0
    assert again.summary.events > 0


def test_failed_document_is_isolated(tmp_path: Path) -> None:
    cid = "case_0003"
    oracle = OracleProvider(gold_of(cid))

    class Flaky:
        name = "flaky"

        def complete(self, req: LLMRequest) -> LLMResponse:
            if req.route == "extract" and "(operative_report)" in str(req.messages[0]["content"]):
                raise LLMRefusalError("other")
            return oracle.complete(req)

    result = run_case(SMOKE / f"{cid}.pdf", cid, Flaky(), RunConfig(out_dir=tmp_path))
    assert result.summary.status == "completed_with_errors"
    assert len(result.summary.failed_documents) == 1
    assert any(f.kind == "extraction_failed" for f in result.chronology.flags)
    assert result.summary.events > 10


def test_budget_stops_run_cleanly(tmp_path: Path) -> None:
    cid = CASES[1]
    oracle = OracleProvider(gold_of(cid))

    class Priced:
        name = "priced"

        def complete(self, req: LLMRequest) -> LLMResponse:
            resp = oracle.complete(req)
            return resp.model_copy(
                update={
                    "model": "claude-opus-5-5",
                    "usage": Usage(input_tokens=10_000, output_tokens=1_000),
                }
            )

    result = run_case(
        SMOKE / f"{cid}.pdf",
        cid,
        Priced(),
        RunConfig(out_dir=tmp_path, budget_usd=0.01, concurrency=1),
    )
    assert result.summary.status == "budget_exceeded"
    assert result.summary.cost_usd >= 0.01
    assert (tmp_path / "run.json").exists()


def test_noisy_model_output_is_rejected_by_grounding(tmp_path: Path) -> None:
    cid = "case_0003"
    noisy = OracleProvider(gold_of(cid), noise=0.4, seed=7)
    result = run_case(SMOKE / f"{cid}.pdf", cid, noisy, RunConfig(out_dir=tmp_path))
    assert result.summary.rejected_events > 0
    assert all("(paraphrased)" not in s.quote for e in result.chronology.events for s in e.sources)


# --------------------------------------------------------------------------- units
def test_find_quote_exact_fuzzy_and_miss() -> None:
    text = "Labs:  WBC 16.4 x10^3/uL (H).\nLipase within normal limits."
    exact = find_quote(text, "WBC 16.4 x10^3/uL (H). Lipase")
    assert exact is not None and exact.exact
    assert normalize(text[exact.start : exact.end]) == "wbc 16.4 x10^3/ul (h). lipase"
    ocr = find_quote(text, "WBC 16,4 x10*3/uL (H). Lipase within")
    assert ocr is not None and 0.88 <= ocr.score < 1.0
    assert find_quote(text, "Hemoglobin was low and transfusion given") is None
    assert find_quote(text, "WBC 16.4 x10^3/uL (H). Lipase within normal limits, improving") is None


@pytest.mark.parametrize(
    "written",
    ["03/14/2024", "March 14, 2024", "14-Mar-2024", "2024-03-14", "03-14-24", "Mar. 14, 2024"],
)
def test_date_formats(written: str) -> None:
    assert parse_date(f"seen on {written} in clinic") == dt.date(2024, 3, 14)


def test_find_dates_order_and_invalid() -> None:
    assert [d for d, _ in find_dates("02/30/2024 then 2024-01-05")] == [dt.date(2024, 1, 5)]


def _ev(
    eid: str, type_: EventType, day: int, text: str, page: int = 1, **kw: Any
) -> ExtractedEvent:
    return ExtractedEvent(
        event_id=eid,
        date=dt.date(2024, 1, day),
        date_text="",
        type=type_,
        summary=text,
        facility=kw.get("facility", "Hosp"),
        provider=kw.get("provider", ""),
        doc_id="d",
        sources=[
            EventSource(
                span_id=f"c:p{page}:0-1",
                page_no=page,
                quote=text,
                char_start=0,
                char_end=1,
                match_score=1.0,
            )
        ],
        confidence=Confidence.HIGH,
    )


def test_merge_conflict_and_gap_rules() -> None:
    a = _ev("a", EventType.PROCEDURE, 10, "laparoscopic appendectomy performed", page=3)
    b = _ev("b", EventType.PROCEDURE, 10, "underwent appendectomy laparoscopic", page=9)
    c = _ev("c", EventType.PROCEDURE, 12, "appendectomy laparoscopic converted", page=11)
    merged = merge_events([a, b, c])
    assert len(merged) == 2 and len(merged[0].sources) == 2
    assert len(find_conflicts(merged)) == 1

    ref = _ev("r", EventType.REFERRAL, 15, "Follow up in surgery clinic with B. Whitfield")
    other = _ev("o", EventType.ENCOUNTER, 20, "PCP visit", facility="Clinic", provider="T. X")
    assert len(find_followup_gaps([ref, other])) == 1
    seen = _ev("s", EventType.ENCOUNTER, 25, "post-op", facility="Ortho", provider="B. Whitfield")
    assert find_followup_gaps([ref, other, seen]) == []
