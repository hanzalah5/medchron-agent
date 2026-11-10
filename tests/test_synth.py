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
DATA = Path(__file__).resolve().parents[1] / "data" / "synthetic"
SPLIT_DIRS = {"dev": DATA / "dev", "test": DATA / "test"}


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
    reached = {generate_case(s).gold.scenario for s in range(len(SCENARIOS))}
    assert reached == set(SCENARIOS) and len(SCENARIOS) >= 6


def test_unknown_scenario_rejected() -> None:
    with pytest.raises(ValueError):
        generate_case(1, scenario="nope")


def test_pdf_page_count_matches_gold() -> None:
    for seed in (3, 8):  # seed 8 is delivered in two productions
        case = generate_case(seed)
        assert all(pdf.startswith(b"%PDF") for pdf in case.pdfs)
        pages = sum(len(re.findall(rb"/Type /Page[^s]", pdf)) for pdf in case.pdfs)
        assert pages == len(case.gold.pages)
        assert len(case.pdfs) == case.gold.productions


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
            def body(text: str) -> list[str]:
                return [ln for ln in text.splitlines() if not ln.startswith("FAX ")]

            assert body(p.text) == body(orig.text)


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
    for n in range(2, gold.productions + 1):
        first = min(p.page for p in gold.pages if p.production == n)
        assert any(
            d.start_page == first and d.doc_type == DocType.FAX_COVER for d in gold.documents
        )


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
    args = ["synth", "--split", "dev", "--cases", "2", "--start-seed", "7", "--out", str(tmp_path)]
    assert main(args) == 0
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert [m["case_id"] for m in manifest] == ["case_0007", "case_0008"]
    for m in manifest:
        assert m["split"] == "dev" and m["pdfs"]
        assert all((tmp_path / name).exists() for name in m["pdfs"])
        CaseGold.model_validate_json((tmp_path / f"{m['case_id']}.gold.json").read_text())


def test_split_seed_ranges_never_overlap(tmp_path: Path) -> None:
    from medchron.synth.generate import SPLITS

    assert not set(SPLITS["dev"]) & set(range(SPLITS["test"].start, SPLITS["test"].start + 10))
    with pytest.raises(SystemExit):
        main(["synth", "--split", "test", "--start-seed", "5", "--out", str(tmp_path)])
    with pytest.raises(SystemExit):
        main(["synth", "--split", "dev", "--start-seed", "998", "--cases", "3", "--out",
              str(tmp_path)])  # fmt: skip


@pytest.mark.parametrize("split", ["dev", "test"])
def test_committed_smoke_set_matches_generator(split: str) -> None:
    """Gold labels in data/synthetic/<split> must be reproducible from their seeds."""
    from medchron.synth.generate import SPLITS

    folder = SPLIT_DIRS[split]
    manifest = json.loads((folder / "manifest.json").read_text())
    assert len(manifest) >= 6
    for m in manifest:
        assert m["seed"] in SPLITS[split] and m["split"] == split
        committed = CaseGold.model_validate_json((folder / f"{m['case_id']}.gold.json").read_text())
        assert committed == generate_case(m["seed"]).gold


@pytest.mark.parametrize("split", ["dev", "test"])
def test_committed_splits_cover_every_scenario_and_hazard(split: str) -> None:
    manifest = json.loads((SPLIT_DIRS[split] / "manifest.json").read_text())
    assert {m["scenario"] for m in manifest} == set(SCENARIOS)
    hazards = [m["hazards"] for m in manifest]
    for name in ("tables", "copy_forward", "misfiled_page"):
        assert any(h[name] for h in hazards), name
    assert any(h["productions"] > 1 for h in hazards)
    assert {k for h in hazards for k in h["scans"]} == {"tilt", "skew", "rot90", "rot180"}
    kinds = {k for m in manifest for k in m["issues"]}
    assert kinds == {k.value for k in IssueKind}


def test_copy_forward_cites_the_original_event() -> None:
    case = next(c for s in SEEDS if (c := generate_case(s)).hazards["copy_forward"])
    by_page = {p.page: p.text for p in case.gold.pages}
    marker = "copied forward from prior note):"

    def in_copy(page: int, quote: str) -> bool:
        text = by_page[page]
        at = text.find(marker)
        end = text.find("\n\n", at)
        return at >= 0 and _norm(quote) in _norm(text[at : end if end > 0 else None])

    copied = [
        (ev, src) for ev in case.gold.events for src in ev.sources if in_copy(src.page, src.quote)
    ]
    assert copied
    for ev, src in copied:  # the copy is a second source of the earlier event
        assert len(ev.sources) >= 2 and src != ev.sources[0]


def test_misfiled_page_belongs_to_someone_else() -> None:
    case = next(c for s in SEEDS if (c := generate_case(s)).hazards["misfiled_page"])
    g = case.gold
    misfiled = [p for p in g.pages if p.misfiled]
    assert len(misfiled) == 1
    page = misfiled[0]
    assert g.patient.name not in page.text
    assert not any(src.page == page.page for ev in g.events for src in ev.sources)
    issue = next(i for i in g.issues if i.kind == IssueKind.MISFILED_PAGE)
    assert issue.pages == [page.page]


def test_sideways_scans_are_landscape_pages() -> None:
    import pymupdf

    case = next(
        c
        for s in range(1, 30)
        if "rot90" in (c := generate_case(s)).hazards["scans"]  # type: ignore[operator]
    )
    sideways = {p.page for p in case.gold.pages if p.scan == "rot90"}
    offset = 0
    for pdf in case.pdfs:
        with pymupdf.open(stream=pdf, filetype="pdf") as doc:
            for i, page in enumerate(doc, start=offset + 1):
                landscape = page.rect.width > page.rect.height
                assert landscape == (i in sideways)
            offset += doc.page_count
