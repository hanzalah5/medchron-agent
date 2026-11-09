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

from medchron.ingestion.models import Page, TextSource

SHINGLE = 4
NEAR_DUP_THRESHOLD = 0.85
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


def mark_duplicates(pages: list[Page], threshold: float = NEAR_DUP_THRESHOLD) -> list[Page]:
    """Set ``duplicate_of`` on every page that repeats an earlier page."""
    seen_hash: dict[str, int] = {}
    originals: list[tuple[int, set[tuple[str, ...]]]] = []
    for page in pages:
        if page.source == TextSource.NONE or len(page.words) < MIN_WORDS:
            continue
        comparable = comparable_text(page.text)
        digest = hashlib.sha256(comparable.encode()).hexdigest()
        if digest in seen_hash:
            page.duplicate_of = seen_hash[digest]
            continue
        sh = _shingles(comparable)
        match = next((n for n, osh in originals if jaccard(sh, osh) >= threshold), None)
        if match is not None:
            page.duplicate_of = match
            continue
        seen_hash[digest] = page.page_no
        originals.append((page.page_no, sh))
    return pages
