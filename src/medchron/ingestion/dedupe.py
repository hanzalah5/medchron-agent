"""Exact and near-duplicate page detection.

Record productions often repeat pages, and re-sent pages differ only in fax
headers or OCR noise. Transmission header lines are removed first. Exact
duplicates are then found by hash. Near-duplicates are found by Jaccard
similarity of word shingles. The pairwise
comparison is fine for productions of a few thousand pages. At larger scale,
swap in MinHash with locality-sensitive hashing behind the same function.
"""

from __future__ import annotations

import hashlib
import re
from difflib import SequenceMatcher

from medchron.grounding.dates import find_dates
from medchron.ingestion.models import Page, TextSource

SHINGLE = 4
NEAR_DUP_THRESHOLD = 0.85
# OCR errors break word shingles (one misread character spoils four shingles), so a
# pair involving OCR text is compared differently: a word-overlap prefilter, then a
# character-level similarity that tolerates scattered misreads.
# On the synthetic benchmark, true OCR duplicates score 0.96 or more on letters and
# digits alone, and the closest non-duplicate pages score about 0.93.
OCR_TOKEN_PREFILTER = 0.6  # a cheap filter only; the character check decides
OCR_CHAR_SIMILARITY = 0.95
MIN_WORDS = 20  # don't call short or near-empty pages duplicates

# Transmission artifacts that differ between otherwise identical re-sent pages.
_TRANSMISSION_LINE = re.compile(r"^\s*(FAX\b|.*\bP\.\s?\d{2,4}\s*$)", re.IGNORECASE)


def comparable_text(text: str) -> str:
    """Page text without fax/transmission header lines, whitespace-normalized."""
    lines = [ln for ln in text.splitlines() if not _TRANSMISSION_LINE.match(ln)]
    return re.sub(r"\s+", " ", " ".join(lines)).strip().lower()


def _shingles(text: str) -> set[tuple[str, ...]]:
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    return {tuple(tokens[i : i + SHINGLE]) for i in range(max(0, len(tokens) - SHINGLE + 1))}


def jaccard(a: set[tuple[str, ...]], b: set[tuple[str, ...]]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# Header fields that identify a document. A re-sent copy of a page repeats them
# exactly (apart from OCR noise); two different notes built from the same
# template usually do not (another author, another encounter, another MRN).
_FIELD = re.compile(
    r"\b(mrn|author|date of service|visit date|dos)\s*[:#]\s*(.{2,48}?)(?=\s{2,}|$)",
    re.IGNORECASE | re.MULTILINE,
)
FIELD_SIMILARITY = 0.8


def identity_fields(text: str) -> dict[str, str]:
    """MRN, author and service date from a page header, letters and digits only."""
    fields: dict[str, str] = {}
    for name, value in _FIELD.findall(text):
        key = name.lower()
        if key not in fields:
            fields[key] = "".join(re.findall(r"[a-z0-9]+", value.lower()))
    return fields


def _fields_conflict(a: dict[str, str], b: dict[str, str]) -> bool:
    return any(
        k in b and a[k] and b[k] and SequenceMatcher(None, a[k], b[k]).ratio() < FIELD_SIMILARITY
        for k in a
    )


def _dates_conflict(a: set[object], b: set[object]) -> bool:
    """Each page has a date the other lacks: two different documents (e.g. two cover sheets).

    An OCR misread usually makes a date unparseable rather than different, so a
    scanned copy's dates are a subset of the original's and do not conflict.
    """
    return bool(a - b) and bool(b - a)


def _ocr_duplicate(ta: set[str], tb: set[str], a: str, b: str) -> bool:
    """``a``/``b`` are letters and digits only, so OCR punctuation noise is ignored."""
    if not ta or not tb or len(ta & tb) / len(ta | tb) < OCR_TOKEN_PREFILTER:
        return False
    m = SequenceMatcher(None, a, b, autojunk=False)
    return m.quick_ratio() >= OCR_CHAR_SIMILARITY and m.ratio() >= OCR_CHAR_SIMILARITY


def mark_duplicates(pages: list[Page], threshold: float = NEAR_DUP_THRESHOLD) -> list[Page]:
    """Set ``duplicate_of`` on every page that repeats an earlier page."""
    seen_hash: dict[str, int] = {}
    originals: list[
        tuple[int, set[tuple[str, ...]], bool, str, set[str], set[object], dict[str, str]]
    ] = []
    for page in pages:
        if page.source == TextSource.NONE or len(page.words) < MIN_WORDS:
            continue
        comparable = comparable_text(page.text)
        digest = hashlib.sha256(comparable.encode()).hexdigest()
        if digest in seen_hash:
            page.duplicate_of = seen_hash[digest]
            continue
        sh = _shingles(comparable)
        dates: set[object] = {d for d, _ in find_dates(comparable)}
        fields = identity_fields(page.text)
        words = re.findall(r"[a-z0-9]+", comparable)
        tokens, alnum = set(words), " ".join(words)
        is_ocr = page.source == TextSource.OCR
        match = next(
            (
                n
                for n, osh, o_ocr, otext, otok, odates, ofields in originals
                if not _dates_conflict(dates, odates)
                and not _fields_conflict(fields, ofields)
                and (
                    _ocr_duplicate(tokens, otok, alnum, otext)
                    if is_ocr or o_ocr
                    else jaccard(sh, osh) >= threshold
                )
            ),
            None,
        )
        if match is not None:
            page.duplicate_of = match
            continue
        seen_hash[digest] = page.page_no
        originals.append((page.page_no, sh, is_ocr, alnum, tokens, dates, fields))
    return pages
