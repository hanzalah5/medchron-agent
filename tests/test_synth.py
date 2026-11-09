"""Tests for the synthetic benchmark generator: determinism and gold-label integrity."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from medchron.cli import main
from medchron.synth import CaseGold, generate_case
from medchron.synth.layout import INJECTION_TEXT
from medchron.synth.models import DocType, IssueKind
from medchron.synth.scenarios import SCENARIOS

SEEDS = list(range(1, 13))
SMOKE_DIR = Path(__file__).resolve().parents[1] / "data" / "synthetic"


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


@pytest.fixture(scope="module", params=SEEDS)
def gold(request: pytest.FixtureRequest) -> CaseGold:
    return generate_case(request.param).gold


def test_same_seed_is_byte_identical() -> None:
    a, b = generate_case(7), generate_case(7)
    assert a.pdf == b.pdf
    assert a.gold == b.gold


def test_different_seeds_differ() -> None:
    assert generate_case(1).sha256 != generate_case(2).sha256


def test_every_scenario_is_reachable() -> None:
    assert {generate_case(s).gold.scenario for s in range(3)} == set(SCENARIOS)


def test_unknown_scenario_rejected() -> None:
    with pytest.raises(ValueError):
        generate_case(1, scenario="nope")


def test_pdf_page_count_matches_gold() -> None:
    case = generate_case(3)
    assert case.pdf.startswith(b"%PDF")
    assert len(re.findall(rb"/Type /Page[^s]", case.pdf)) == len(case.gold.pages)


def test_every_source_quote_is_on_its_page(gold: CaseGold) -> None:
    pages = {p.page: _norm(p.text) for p in gold.pages}
    for ev in gold.events:
        for src in ev.sources:
            assert _norm(src.quote) in pages[src.page], (ev.event_id, src.page)


def test_sources_never_point_at_duplicate_pages(gold: CaseGold) -> None:
    dups = {p.page for p in gold.pages if p.duplicate_of is not None}
    assert all(src.page not in dups for ev in gold.events for src in ev.sources)


def test_duplicates_match_their_original(gold: CaseGold) -> None:
    by_page = {p.page: p for p in gold.pages}
    for p in gold.pages:
        if p.duplicate_of is not None:
            orig = by_page[p.duplicate_of]
            assert orig.duplicate_of is None
            assert (orig.doc_id, orig.doc_page) == (p.doc_id, p.doc_page)
            # Body identical; only the fax transmission line may differ.
            assert p.text.splitlines()[1:] == orig.text.splitlines()[1:] or p.text == orig.text


def test_documents_tile_the_non_duplicate_pages(gold: CaseGold) -> None:
    covered: list[int] = []
    for d in gold.documents:
        assert d.start_page <= d.end_page
        covered.extend(range(d.start_page, d.end_page + 1))
    originals = [p.page for p in gold.pages if p.duplicate_of is None]
    assert sorted(covered) == originals
    assert {d.doc_id for d in gold.documents} == {p.doc_id for p in gold.pages}


def test_each_production_starts_with_a_fax_cover(gold: CaseGold) -> None:
    assert gold.documents[0].doc_type == DocType.FAX_COVER
    assert gold.documents[0].start_page == 1


def test_issues_reference_real_pages_and_events(gold: CaseGold) -> None:
    pages = {p.page for p in gold.pages}
    events = {e.event_id for e in gold.events}
    assert gold.issues, "every case plants at least one issue"
    for issue in gold.issues:
        assert issue.pages, issue
        assert set(issue.pages) <= pages
        assert set(issue.event_ids) <= events
        assert issue.description.endswith(("?", "records.", "flag them."))


def test_injection_issue_points_at_injected_text(gold: CaseGold) -> None:
    pages = {p.page: _norm(p.text) for p in gold.pages}
    injected = [p for p, text in pages.items() if _norm(INJECTION_TEXT) in text]
    issues = [i for i in gold.issues if i.kind == IssueKind.PROMPT_INJECTION]
    if injected:
        assert len(issues) == 1 and issues[0].pages == injected
    else:
        assert not issues


def test_every_page_is_marked_synthetic(gold: CaseGold) -> None:
    assert gold.synthetic
    assert all("SYNTHETIC RECORD - NOT A REAL PATIENT" in p.text for p in gold.pages)


def test_some_pages_need_ocr(gold: CaseGold) -> None:
    assert any(not p.text_layer for p in gold.pages)


def test_cli_writes_dataset(tmp_path: Path) -> None:
    assert main(["synth", "--cases", "2", "--start-seed", "5", "--out", str(tmp_path)]) == 0
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert [m["case_id"] for m in manifest] == ["case_0005", "case_0006"]
    for m in manifest:
        assert (tmp_path / f"{m['case_id']}.pdf").exists()
        CaseGold.model_validate_json((tmp_path / f"{m['case_id']}.gold.json").read_text())


def test_committed_smoke_set_matches_generator() -> None:
    """Gold labels in data/synthetic must be reproducible from their seeds."""
    manifest = json.loads((SMOKE_DIR / "manifest.json").read_text())
    assert len(manifest) >= 5
    for m in manifest:
        committed = CaseGold.model_validate_json(
            (SMOKE_DIR / f"{m['case_id']}.gold.json").read_text()
        )
        assert committed == generate_case(m["seed"]).gold
