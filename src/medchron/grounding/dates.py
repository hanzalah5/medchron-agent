"""Deterministic date handling. Dates are never left to the model alone.

Records mix formats (``03/14/2024``, ``March 14, 2024``, ``14-Mar-2024``,
``2024-03-14``, ``03-14-24``). The model reports both the date as written and
an ISO date. This code checks that the written date appears in the cited text
and parses to the same ISO value. If either check fails, the event is
flagged ``date_unverified``.
"""

from __future__ import annotations

import datetime as dt
import re

_MONTHS = "jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec"
DATE_PATTERNS = [
    (re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"), "ymd"),
    (re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b"), "mdy"),
    (re.compile(r"\b(\d{1,2})-(\d{1,2})-(\d{2})\b"), "mdy2"),
    (re.compile(rf"\b({_MONTHS})[a-z]*\.? (\d{{1,2}}),? (\d{{4}})\b", re.I), "Mdy"),
    (re.compile(rf"\b(\d{{1,2}})-({_MONTHS})[a-z]*-(\d{{4}})\b", re.I), "dMy"),
]
_MONTH_NUM = {m: i for i, m in enumerate(_MONTHS.split("|")[:9], start=1)} | {
    "sept": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}


def _month(name: str) -> int:
    return _MONTH_NUM[name.lower()[:4] if name.lower().startswith("sept") else name.lower()[:3]]


def _build(kind: str, g: tuple[str, ...]) -> dt.date | None:
    try:
        if kind == "ymd":
            return dt.date(int(g[0]), int(g[1]), int(g[2]))
        if kind == "mdy":
            return dt.date(int(g[2]), int(g[0]), int(g[1]))
        if kind == "mdy2":
            year = int(g[2])
            return dt.date(2000 + year if year < 70 else 1900 + year, int(g[0]), int(g[1]))
        if kind == "Mdy":
            return dt.date(int(g[2]), _month(g[0]), int(g[1]))
        if kind == "dMy":
            return dt.date(int(g[2]), _month(g[1]), int(g[0]))
    except (ValueError, KeyError):
        return None
    return None


def find_dates(text: str) -> list[tuple[dt.date, str]]:
    """All parseable dates in ``text`` as (date, original string), in order of appearance."""
    hits: list[tuple[int, dt.date, str]] = []
    for pattern, kind in DATE_PATTERNS:
        for m in pattern.finditer(text):
            d = _build(kind, m.groups())
            if d is not None:
                hits.append((m.start(), d, m.group(0)))
    hits.sort(key=lambda h: h[0])
    return [(d, s) for _, d, s in hits]


def parse_date(text: str) -> dt.date | None:
    found = find_dates(text)
    return found[0][0] if found else None


def parse_iso(value: str) -> dt.date | None:
    try:
        return dt.date.fromisoformat(value.strip())
    except ValueError:
        return None
