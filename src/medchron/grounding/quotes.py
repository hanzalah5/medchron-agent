"""Locate a model-supplied quote inside source text, tolerating OCR noise.

This is the deterministic half of grounding: an extracted claim is only kept
if its verbatim quote can be found inside the span it cites. Exact matching
runs first (after whitespace and case normalization). If that fails, a fuzzy
match over character windows handles OCR errors such as ``WBC 16.4`` read as
``WBC 16,4``. The result carries page-level character offsets, so the UI can
highlight exactly what was cited.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

FUZZY_THRESHOLD = 0.88
# OCR noise produces scattered single-character errors. A contiguous run of
# quote text that is absent from the source means the quote was altered or
# fabricated, however similar the rest of it is.
MAX_UNMATCHED_RUN = 4


@dataclass(frozen=True)
class QuoteMatch:
    start: int  # offsets in the searched text
    end: int
    score: float  # 1.0 = exact (normalized) match

    @property
    def exact(self) -> bool:
        return self.score >= 1.0


def _normalize_with_map(text: str) -> tuple[str, list[int]]:
    """Lowercase and collapse whitespace, keeping a map back to original offsets."""
    out: list[str] = []
    index: list[int] = []
    prev_space = True
    for i, ch in enumerate(text):
        if ch.isspace():
            if not prev_space:
                out.append(" ")
                index.append(i)
            prev_space = True
        else:
            out.append(ch.lower())
            index.append(i)
            prev_space = False
    while out and out[-1] == " ":
        out.pop()
        index.pop()
    return "".join(out), index


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def find_quote(text: str, quote: str, threshold: float = FUZZY_THRESHOLD) -> QuoteMatch | None:
    q = normalize(quote)
    if len(q) < 4:
        return None
    norm, index = _normalize_with_map(text)
    pos = norm.find(q)
    if pos >= 0:
        return QuoteMatch(index[pos], index[pos + len(q) - 1] + 1, 1.0)

    # Fuzzy: slide a window of the quote's length (with slack) and keep the best ratio.
    n = len(q)
    if n > len(norm) + 10:
        return None
    best = (0.0, 0, 0)
    step = max(1, n // 10)
    matcher = SequenceMatcher(autojunk=False)
    matcher.set_seq2(q)
    for start in range(0, max(1, len(norm) - n + 1), step):
        for length in (n - n // 10, n, n + n // 10):
            window = norm[start : start + length]
            if not window:
                continue
            matcher.set_seq1(window)
            if matcher.real_quick_ratio() < best[0] or matcher.quick_ratio() < best[0]:
                continue
            ratio = matcher.ratio()
            if ratio > best[0]:
                best = (ratio, start, min(start + length, len(norm)))
    score, s, e = best
    if score < threshold:
        return None
    # Refine the start position around the coarse hit (step granularity).
    for refined in range(max(0, s - step), min(len(norm) - 1, s + step) + 1):
        window = norm[refined : refined + (e - s)]
        matcher.set_seq1(window)
        r = matcher.ratio()
        if r > score:
            score, s, e = r, refined, min(refined + (e - s), len(norm))
    matcher.set_seq1(norm[s:e])
    if _max_unmatched_run(matcher, len(q)) > MAX_UNMATCHED_RUN:
        return None
    return QuoteMatch(index[s], index[e - 1] + 1, round(min(score, 0.999), 3))


def _max_unmatched_run(matcher: SequenceMatcher[str], quote_len: int) -> int:
    """Longest stretch of the quote (seq2) not covered by any matching block."""
    longest, pos = 0, 0
    for block in matcher.get_matching_blocks():
        longest = max(longest, block.b - pos)
        pos = block.b + block.size
    return max(longest, quote_len - pos)
