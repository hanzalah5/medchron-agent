"""Ingestion tests against the committed synthetic smoke set."""

from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path

import pytest

from medchron.ingestion import IngestedCase, Page, TextSource, ingest_productions
from medchron.ingestion.chunking import chunk_page
from medchron.ingestion.dedupe import comparable_text
from medchron.ingestion.identity import find_misfiled, page_identity
from medchron.ingestion.pdf import ocr_available
from medchron.retrieval.embeddings import HashingEmbedder
from medchron.synth import CaseGold

DATA = Path(__file__).resolve().parents[1] / "data" / "synthetic"
# Ingestion is not tuned on any split, so it is checked on both committed splits.
MANIFEST = {
    m["case_id"]: (DATA / split, m)
    for split in ("dev", "test")
    for m in json.loads((DATA / split / "manifest.json").read_text())
}
CASES = list(MANIFEST)


def _files(cid: str) -> list[Path]:
    folder, m = MANIFEST[cid]
    return [folder / name for name in m["pdfs"]]


def _gold(cid: str) -> CaseGold:
    folder, _ = MANIFEST[cid]
    return CaseGold.model_validate_json((folder / f"{cid}.gold.json").read_text())


def _norm(t: str) -> str:
    return re.sub(r"\s+", " ", t).strip()


@pytest.fixture(scope="module", params=CASES)
def pair(request: pytest.FixtureRequest) -> tuple[IngestedCase, CaseGold]:
    cid = request.param
    return ingest_productions(_files(cid), cid), _gold(cid)


def test_page_count_matches(pair: tuple[IngestedCase, CaseGold]) -> None:
    ic, gold = pair
    assert len(ic.pages) == len(gold.pages)


def test_text_layer_pages_extract_exactly(pair: tuple[IngestedCase, CaseGold]) -> None:
    ic, gold = pair
    for page, gp in zip(ic.pages, gold.pages, strict=True):
        if gp.text_layer:
            assert page.source == TextSource.TEXT_LAYER
            assert _norm(page.text) == _norm(gp.text), gp.page


def test_word_offsets_match_text(pair: tuple[IngestedCase, CaseGold]) -> None:
    ic, _ = pair
    for page in ic.pages:
        for w in page.words:
            assert page.text[w.char_start : w.char_end] == w.text
            assert 0 <= w.bbox.x0 < w.bbox.x1 <= page.width + 1
            assert 0 <= w.bbox.y0 < w.bbox.y1 <= page.height + 1


def test_gold_duplicates_detected(pair: tuple[IngestedCase, CaseGold]) -> None:
    ic, gold = pair
    predicted = {p.page_no: p.duplicate_of for p in ic.pages if p.duplicate_of}
    expected = {p.page: p.duplicate_of for p in gold.pages if p.duplicate_of}
    if not ocr_available():  # a scanned original or copy can't be compared without OCR
        scanned = {p.page for p in gold.pages if not p.text_layer}
        expected = {k: v for k, v in expected.items() if k not in scanned and v not in scanned}
    assert predicted == expected


def test_spans_round_trip_and_stay_on_one_page(pair: tuple[IngestedCase, CaseGold]) -> None:
    ic, _ = pair
    assert ic.spans
    dup_pages = {p.page_no for p in ic.pages if p.duplicate_of}
    for span in ic.spans:
        assert ic.resolve(span.span_id) == span.text
        assert span.page_no not in dup_pages
        assert span.span_id.startswith(f"{ic.case_id}:p{span.page_no}:")


def test_every_text_layer_quote_is_inside_one_span(pair: tuple[IngestedCase, CaseGold]) -> None:
    ic, gold = pair
    scanned = {p.page for p in gold.pages if not p.text_layer}
    for ev in gold.events:
        for src in ev.sources:
            if src.page in scanned:
                continue
            assert any(
                s.page_no == src.page and _norm(src.quote) in _norm(s.text) for s in ic.spans
            ), (ev.event_id, src.page)


@pytest.mark.skipif(not ocr_available(), reason="tesseract not installed")
def test_scanned_pages_are_ocred(pair: tuple[IngestedCase, CaseGold]) -> None:
    ic, gold = pair
    for gp in gold.pages:
        if not gp.text_layer:
            page = ic.page(gp.page)
            assert page.source == TextSource.OCR
            assert page.ocr_confidence is not None
            expected = set(re.findall(r"[a-z]{4,}", gp.text.lower()))
            got = set(re.findall(r"[a-z]{4,}", page.text.lower()))
            assert len(expected & got) / len(expected) > 0.8


def test_without_ocr_scans_are_flagged_for_review() -> None:
    cid = CASES[0]
    ic = ingest_productions(_files(cid), cid, ocr=False)
    gold = _gold(cid)
    for gp in gold.pages:
        if not gp.text_layer:
            assert ic.page(gp.page).source == TextSource.NONE
            assert ic.page(gp.page).needs_review


def test_resolve_rejects_bad_ids(pair: tuple[IngestedCase, CaseGold]) -> None:
    ic, _ = pair
    for bad in ["nope", "other:p1:0-5", f"{ic.case_id}:p999:0-5", f"{ic.case_id}:p1:5-99999"]:
        with pytest.raises(KeyError):
            ic.resolve(bad)


def test_comparable_text_ignores_fax_headers() -> None:
    a = "FAX 01/02/2024 10:11 FROM HIM DEPT    P.003\nProgress note body text here"
    b = "FAX 01/02/2024 10:11 FROM HIM DEPT    P.009\nProgress note  body text here"
    assert comparable_text(a) == comparable_text(b)


def test_long_paragraph_is_split_within_page(pair: tuple[IngestedCase, CaseGold]) -> None:
    ic, _ = pair
    page = ic.pages[1]
    for span in chunk_page(ic.case_id, page, max_chars=120):
        assert len(span.text) <= 120
        assert page.text[span.char_start : span.char_end] == span.text


def test_hashing_embedder_is_normalized_and_deterministic() -> None:
    e = HashingEmbedder()
    a, b, c = e.embed(["white blood cell count elevated", "white blood cell count elevated", "x"])
    assert a == b and len(a) == e.dim
    assert abs(sum(v * v for v in a) - 1.0) < 1e-9
    assert a != c


def test_misfiled_pages_are_detected(pair: tuple[IngestedCase, CaseGold]) -> None:
    ic, gold = pair
    assert find_misfiled(ic.pages) == [p.page for p in gold.pages if p.misfiled]


def test_patient_header_parsing() -> None:
    h = page_identity("CLINIC\nPatient: Riley Samplefield   DOB: 18-Jul-1951   MRN: P56419")
    assert h is not None and h.name == "riley samplefield" and h.dob == dt.date(1951, 7, 18)
    assert page_identity("No header on this page") is None


def test_orientation_and_deskew_helpers() -> None:
    from PIL import Image, ImageDraw

    from medchron.ingestion.pdf import _unrotate_quarter, estimate_skew

    img = Image.new("L", (400, 300), 255)
    draw = ImageDraw.Draw(img)
    for y in range(40, 260, 20):
        draw.line((40, y, 360, y), fill=0, width=3)  # stand-in for lines of text
    assert abs(estimate_skew(img)) < 0.3
    assert abs(estimate_skew(img.rotate(3, fillcolor=255)) + 3) < 0.5
    # A point in an upright copy maps back to the same place on the rotated scan.
    w, h = 400, 300
    for rot in (90, 180, 270):
        upright = img.rotate(-rot, expand=True)
        x, y = 30.0, 50.0  # a point in the upright image
        ox, oy = _unrotate_quarter(x, y, w, h, rot)
        back = upright.rotate(rot, expand=True)
        assert back.size == (w, h) and 0 <= ox <= w and 0 <= oy <= h


def _page(n: int, text: str) -> Page:
    from medchron.ingestion.models import BBox, Word

    words, pos = [], 0
    for token in text.split():
        start = text.index(token, pos)
        words.append(Word(text=token, bbox=BBox(x0=0, y0=0, x1=1, y1=1), char_start=start,
                          char_end=start + len(token)))  # fmt: skip
        pos = start + len(token)
    return Page(page_no=n, text=text, words=words, source=TextSource.TEXT_LAYER, width=612,
                height=792, content_hash=str(n))  # fmt: skip


BODY = (
    "POD 3. Incision with erythema and purulent drainage consistent with superficial surgical "
    "site infection. Wound opened at bedside and packed. Cultures sent. Started cephalexin. "
    "Afebrile. Tolerating diet. Plan: daily dressing changes and wound check in clinic."
)


def test_templated_notes_with_different_headers_are_not_duplicates() -> None:
    from medchron.ingestion.dedupe import mark_duplicates

    def note(n: int, mrn: str, author: str, fax: str = "") -> Page:
        head = f"{fax}\n" if fax else ""
        return _page(n, f"{head}GENERAL HOSPITAL\nSurgery Progress Note\nPatient: A Person   "
                        f"DOB: 01/02/1980   MRN: {mrn}\nAuthor: {author}\n{BODY}")  # fmt: skip

    pages = [
        note(1, "T2592612", "W. Mbeki, MD"),
        note(2, "T2592612", "W. Mbeki, MD", fax="FAX 03/04/2024 10:12 FROM HIM DEPT    P.002"),
        note(3, "E3919860", "W. Mbeki, MD"),  # another encounter's chart number
        note(4, "T2592612", "L. Abernathy, MD"),  # same template, another author
    ]
    marked = {p.page_no: p.duplicate_of for p in mark_duplicates(pages)}
    assert marked == {1: None, 2: 1, 3: None, 4: None}
