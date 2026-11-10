"""Evaluation harness tests: matching rules, metrics, regression gate, report."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from medchron.chronology.models import (
    CaseFlag,
    Chronology,
    Citation,
    Confidence,
    EventSource,
    ExtractedEvent,
    Issue,
)
from medchron.cli import main
from medchron.evals.metrics import PRF, aggregate, citation_validity, match_events, score_case
from medchron.evals.report import render
from medchron.evals.runner import EvalResult, regressions
from medchron.ingestion import ingest_pdf
from medchron.synth.models import CaseGold, GoldEvent
from medchron.taxonomy import EventType, IssueKind

SMOKE = Path(__file__).resolve().parents[1] / "data" / "synthetic"


def gold_event(eid: str, day: int, page: int, type_: EventType = EventType.PROCEDURE) -> GoldEvent:
    from medchron.synth.models import SourceRef

    return GoldEvent(
        event_id=eid,
        date=dt.date(2024, 1, day),
        type=type_,
        summary="appendectomy performed",
        facility="H",
        provider="P",
        sources=[SourceRef(doc_id="D1", page=page, quote="appendectomy")],
    )


def pred(
    eid: str, day: int | None, page: int, type_: EventType = EventType.PROCEDURE
) -> ExtractedEvent:
    return ExtractedEvent(
        event_id=eid,
        date=dt.date(2024, 1, day) if day else None,
        date_text="",
        type=type_,
        summary="appendectomy",
        facility="H",
        provider="P",
        doc_id="d",
        sources=[
            EventSource(
                span_id=f"c:p{page}:0-5",
                page_no=page,
                quote="appendectomy",
                char_start=0,
                char_end=12,
                match_score=1.0,
            )
        ],
        confidence=Confidence.HIGH,
    )


def test_prf_math() -> None:
    p = PRF(tp=8, fp=2, fn=8)
    assert p.precision == 0.8 and p.recall == 0.5
    assert p.f1 == pytest.approx(2 * 0.8 * 0.5 / 1.3)
    assert PRF(tp=0, fp=0, fn=0).f1 == 0.0


def test_matching_requires_type_date_and_page() -> None:
    g = [gold_event("g1", 10, page=3)]
    assert len(match_events(g, [pred("p1", 10, 3)])) == 1
    assert match_events(g, [pred("p1", 11, 3)]) == []  # wrong date
    assert len(match_events(g, [pred("p1", 11, 3)], day_tolerance=1)) == 1
    assert match_events(g, [pred("p1", 10, 4)]) == []  # wrong page
    assert match_events(g, [pred("p1", 10, 3, EventType.IMAGING)]) == []  # wrong type
    assert match_events(g, [pred("p1", None, 3)]) == []  # undated


def test_matching_is_one_to_one() -> None:
    g = [gold_event("g1", 10, page=3)]
    matches = match_events(g, [pred("p1", 10, 3), pred("p2", 10, 3)])
    assert len(matches) == 1


def test_citation_validity_detects_bad_offsets() -> None:
    case = ingest_pdf(SMOKE / "case_0002.pdf", "case_0002", ocr=False)
    page = next(p for p in case.pages if "Urgent Care" in p.text and len(p.text) > 200)
    start = page.text.index("Urgent Care")
    good = pred("a", 10, page.page_no)
    good.sources[0].quote, good.sources[0].char_start = "Urgent Care", start
    good.sources[0].char_end = start + len("Urgent Care")
    bad = good.model_copy(deep=True)
    bad.event_id = "b"
    bad.sources[0].char_start, bad.sources[0].char_end = 0, 5
    chron = Chronology(case_id="c", documents=[], events=[good, bad])
    validity, checked = citation_validity(chron, case)
    assert checked == 2 and validity == 0.5


def test_empty_prediction_scores_zero_recall() -> None:
    gold = CaseGold.model_validate_json((SMOKE / "case_0001.gold.json").read_text())
    case = ingest_pdf(SMOKE / "case_0001.pdf", "case_0001", ocr=False)
    m = score_case(Chronology(case_id="case_0001", documents=[], events=[]), gold, case)
    assert m.events_strict.recall == 0.0 and m.events_strict.fn == len(gold.events)
    agg = aggregate([m])
    assert agg.event_f1 == 0.0
    assert "not attempted" in agg.issue_recall_by_kind.get("potential_delay", "not attempted")


@pytest.fixture(scope="module")
def selfcheck(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("eval")
    code = main(
        ["eval", "--label", "t", "--provider", "oracle", "--cases", "case_0002,case_0003",
         "--out", str(out / "res"), "--work", str(out / "work")]
    )  # fmt: skip
    assert code == 0
    return next((out / "res").glob("*-t.json"))


def test_oracle_selfcheck_is_perfect_on_events(selfcheck: Path) -> None:
    r = EvalResult.model_validate_json(selfcheck.read_text())
    assert not r.is_model_result
    assert r.aggregate.cases == 2
    assert r.aggregate.event_recall == 1.0 and r.aggregate.event_precision == 1.0
    assert r.aggregate.citation_validity == 1.0
    assert r.prompts and r.models["extract"]
    report = render(r)
    assert "Harness self-check, not model performance" in report


def test_regression_gate(selfcheck: Path, tmp_path: Path) -> None:
    base = EvalResult.model_validate_json(selfcheck.read_text())
    worse = base.model_copy(deep=True)
    worse.aggregate.event_recall = base.aggregate.event_recall - 0.1
    assert regressions(base, base, 0.02) == []
    assert any("event_recall" in r for r in regressions(worse, base, 0.02))
    # CLI exits non-zero on regression: compare a noisy run against the clean baseline.
    code = main(
        ["eval", "--label", "noisy", "--provider", "oracle", "--oracle-noise", "0.5",
         "--cases", "case_0003", "--baseline", str(selfcheck),
         "--out", str(tmp_path / "res"), "--work", str(tmp_path / "work")]
    )  # fmt: skip
    assert code == 1
    data = json.loads(next((tmp_path / "res").glob("*.json")).read_text())
    assert data["aggregate"]["event_recall"] < 1.0


# ---------------------------------------------------------------- v1.1: precision and errors
def _smoke_case_with(*kinds: str) -> tuple[CaseGold, object]:
    """First smoke case whose planted issues include every kind in ``kinds``."""
    manifest = json.loads((SMOKE / "manifest.json").read_text())
    for m in manifest:
        if set(kinds) <= set(m["issues"]):
            gold = CaseGold.model_validate_json((SMOKE / f"{m['case_id']}.gold.json").read_text())
            return gold, ingest_pdf(SMOKE / f"{m['case_id']}.pdf", m["case_id"], ocr=False)
    raise AssertionError(f"no smoke case plants {kinds}")


def _issue(iid: str, kind: str, page: int, description: str = "Is this documented?") -> Issue:
    return Issue(
        issue_id=iid,
        category=kind,
        description=description,
        event_ids=[],
        citations=[Citation(span_id=f"c:p{page}:0-5", page_no=page, quote="x", char_start=0,
                            char_end=5)],
    )  # fmt: skip


def test_issue_precision_counts_unmatched_signals() -> None:
    gold, case = _smoke_case_with("prompt_injection", "potential_delay")
    inj = next(i for i in gold.issues if i.kind.value == "prompt_injection")
    delay = next(i for i in gold.issues if i.kind.value == "potential_delay")
    other_page = next(p.page for p in gold.pages if p.page not in inj.pages)
    absent = next(k.value for k in IssueKind if k.value not in {i.kind.value for i in gold.issues})
    chron = Chronology(
        case_id=gold.case_id,
        documents=[],
        events=[],
        flags=[
            CaseFlag(kind="possible_prompt_injection", page_no=inj.pages[0], note="n"),  # TP
            CaseFlag(kind="possible_prompt_injection", page_no=other_page, note="n"),  # FP
        ],
    )
    analyst = [
        _issue("I1", "potential_delay", delay.pages[0]),  # TP
        _issue("I2", "potential_delay", delay.pages[0]),  # duplicate of I1: matched, not FP
        _issue("I3", absent, delay.pages[0]),  # FP, kind not planted
    ]
    m = score_case(chron, gold, case, analyst)  # type: ignore[arg-type]
    assert (m.issue_predictions, m.issue_predictions_matched) == (5, 3)
    assert m.issue_predictions_duplicate == 1
    assert m.issue_pred_by_kind["prompt_injection"] == [1, 2]
    agg = aggregate([m])
    assert agg.issue_precision == pytest.approx(3 / 5)
    assert agg.issue_precision_by_kind["potential_delay"] == "2/2"
    spurious = [e for e in m.errors if e.item == "spurious_issue"]
    assert {e.category for e in spurious} == {"wrong_span", "other"}


def test_issue_precision_is_none_without_predictions() -> None:
    gold, case = _smoke_case_with("potential_delay")
    m = score_case(Chronology(case_id=gold.case_id, documents=[], events=[]), gold, case)  # type: ignore[arg-type]
    assert m.issue_predictions == 0
    assert aggregate([m]).issue_precision is None


def _with_quote(g: GoldEvent, quote: str) -> GoldEvent:
    g = g.model_copy(deep=True)
    g.sources[0].quote = quote
    return g


def test_error_categories() -> None:
    gold, case = _smoke_case_with("potential_delay")
    scanned = next(p.page for p in gold.pages if not p.text_layer)
    text_pages = [p.page for p in gold.pages if p.text_layer and p.duplicate_of is None]
    a, b, c = text_pages[:3]
    gold = gold.model_copy(
        update={
            "issues": [],
            "events": [
                gold_event("g_date", 10, a),
                gold_event("g_span", 12, b, EventType.IMAGING),
                gold_event("g_dup", 14, c, EventType.DIAGNOSIS),
                gold_event("g_ocr", 16, scanned, EventType.LAB_RESULT),
                _with_quote(gold_event("g_none", 18, c, EventType.REFERRAL), "referred to ortho"),
            ],
        }
    )
    preds = [
        pred("p_date", 11, a),  # one day off on the right page
        pred("p_span", 12, c, EventType.IMAGING),  # right date, wrong page
        pred("p_dup1", 14, c, EventType.DIAGNOSIS),  # matches g_dup
        pred("p_dup2", 14, c, EventType.DIAGNOSIS),  # unmerged duplicate
    ]
    chron = Chronology(case_id=gold.case_id, documents=[], events=preds)
    analyst = [_issue("I1", "potential_delay", a, "Was the surgeon negligent here?")]
    m = score_case(chron, gold, case, analyst)  # type: ignore[arg-type]
    cats = {(e.item, e.category) for e in m.errors}
    assert ("missed_event", "date_error") in cats and ("spurious_event", "date_error") in cats
    assert ("missed_event", "wrong_span") in cats and ("spurious_event", "wrong_span") in cats
    assert ("spurious_event", "merge_error") in cats
    assert ("missed_event", "ocr_miss") in cats
    assert ("missed_event", "other") in cats
    assert ("output", "scope_violation") in cats
    agg = aggregate([m])
    assert set(agg.error_counts) >= {"date_error", "wrong_span", "merge_error", "ocr_miss"}


def test_gate_flags_precision_drop_and_tolerates_old_results(selfcheck: Path) -> None:
    base = EvalResult.model_validate_json(selfcheck.read_text())
    assert base.aggregate.issue_precision is not None
    worse = base.model_copy(deep=True)
    worse.aggregate.issue_precision = base.aggregate.issue_precision - 0.2
    assert any("issue_precision" in r for r in regressions(worse, base, 0.02))
    # A results file written before v1.1 has no precision or error fields.
    old = json.loads(selfcheck.read_text())
    for key in (
        "issue_precision",
        "issue_precision_by_kind",
        "issue_predictions",
        "issue_duplicates",
        "error_counts",
    ):
        old["aggregate"].pop(key)
    for c in old["cases"]:
        for key in (
            "issue_predictions",
            "issue_predictions_matched",
            "issue_predictions_duplicate",
            "issue_pred_by_kind",
            "errors",
        ):
            c.pop(key)
    legacy = EvalResult.model_validate(old)  # fmt: skip
    assert legacy.aggregate.issue_precision is None
    assert regressions(base, legacy, 0.02) == []
    assert "Planted-issue precision" not in render(legacy)


def test_report_shows_precision_and_error_analysis(tmp_path: Path) -> None:
    code = main(
        ["eval", "--label", "noisy", "--provider", "oracle", "--oracle-noise", "0.4",
         "--cases", "case_0004", "--out", str(tmp_path / "res"), "--work", str(tmp_path / "w")]
    )  # fmt: skip
    assert code == 0
    r = EvalResult.model_validate_json(next((tmp_path / "res").glob("*.json")).read_text())
    text = render(r)
    assert "Planted-issue precision (all raised signals)" in text
    assert "| Issue kind | Recall" in text
    assert "**Error analysis.**" in text
    assert r.aggregate.issue_precision is not None and r.aggregate.issue_precision < 1.0
