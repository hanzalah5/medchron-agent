"""Page-bounded, paragraph-aware chunking.

Chunks never cross a page boundary, so every retrieved span has exactly one
page to cite. Paragraphs are merged until ``max_chars``. A paragraph longer
than that is split on line boundaries.
"""

from __future__ import annotations

import re

from medchron.ingestion.models import Page, Span

MAX_CHARS = 900


def _paragraph_ranges(text: str) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    start = 0
    for m in re.finditer(r"\n\s*\n", text):
        if m.start() > start:
            ranges.append((start, m.start()))
        start = m.end()
    if start < len(text):
        ranges.append((start, len(text)))
    return [(s, e) for s, e in ranges if text[s:e].strip()]


def _split_long(text: str, start: int, end: int, max_chars: int) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    cur = start
    while end - cur > max_chars:
        cut = text.rfind("\n", cur, cur + max_chars)
        if cut <= cur:
            cut = text.rfind(" ", cur, cur + max_chars)
        if cut <= cur:
            cut = cur + max_chars
        out.append((cur, cut))
        cur = cut + 1 if text[cut : cut + 1] in ("\n", " ") else cut
    out.append((cur, end))
    return out


def chunk_page(case_id: str, page: Page, max_chars: int = MAX_CHARS) -> list[Span]:
    text = page.text
    pieces: list[tuple[int, int]] = []
    for s, e in _paragraph_ranges(text):
        pieces.extend(_split_long(text, s, e, max_chars) if e - s > max_chars else [(s, e)])

    spans: list[Span] = []
    cur: tuple[int, int] | None = None
    for s, e in pieces:
        if cur is None:
            cur = (s, e)
        elif e - cur[0] <= max_chars:
            cur = (cur[0], e)
        else:
            spans.append(_span(case_id, page, *cur))
            cur = (s, e)
    if cur is not None:
        spans.append(_span(case_id, page, *cur))
    return spans


def _span(case_id: str, page: Page, start: int, end: int) -> Span:
    return Span(
        span_id=Span.make_id(case_id, page.page_no, start, end),
        case_id=case_id,
        page_no=page.page_no,
        char_start=start,
        char_end=end,
        text=page.text[start:end],
    )
