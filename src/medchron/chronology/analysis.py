"""Derived views of a chronology: provider and facility index, gaps in treatment.

These are deterministic summaries for the reviewer. They compute intervals and
groupings and make no judgement: a long interval between encounters is not a
finding, it is a place to look.
"""

from __future__ import annotations

import datetime as dt
import itertools
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from medchron.chronology.models import Chronology, ExtractedEvent
from medchron.taxonomy import EventType

DEFAULT_GAP_DAYS = 30


class ProviderEntry(BaseModel):
    facility: str
    provider: str
    first_date: dt.date | None
    last_date: dt.date | None
    documents: int
    events: int
    pages: list[int]
    bates_ranges: list[str] = Field(default_factory=list)


class Interval(BaseModel):
    from_event: str
    to_event: str
    from_date: dt.date
    to_date: dt.date
    days: int
    from_summary: str
    to_summary: str
    from_facility: str
    to_facility: str


class TreatmentGaps(BaseModel):
    min_days: int
    encounters: int
    intervals: list[Interval] = Field(description="consecutive encounters at least min_days apart")
    followup: list[dict[str, object]] = Field(
        default_factory=list, description="directed follow-ups with no later encounter"
    )


def page_ranges(pages: list[int]) -> list[tuple[int, int]]:
    """Collapse sorted page numbers into inclusive runs: [1, 2, 3, 7] -> [(1, 3), (7, 7)]."""
    runs: list[tuple[int, int]] = []
    for p in sorted(set(pages)):
        if runs and p == runs[-1][1] + 1:
            runs[-1] = (runs[-1][0], p)
        else:
            runs.append((p, p))
    return runs


def bates_label(bates: dict[int, str], start: int, end: int) -> str:
    a, b = bates.get(start, f"p{start}"), bates.get(end, f"p{end}")
    return a if start == end else f"{a} - {b}"


@dataclass
class _Acc:
    dates: list[dt.date] = field(default_factory=list)
    documents: int = 0
    events: int = 0
    pages: set[int] = field(default_factory=set)


def provider_index(chron: Chronology, bates: dict[int, str] | None = None) -> list[ProviderEntry]:
    """Every facility and clinician in the records, with dates, counts and page ranges."""
    bates = bates or {}
    groups: dict[tuple[str, str], _Acc] = {}

    def acc(facility: str, provider: str) -> _Acc:
        key = (facility.strip() or "(unknown facility)", provider.strip() or "(unknown)")
        return groups.setdefault(key, _Acc())

    for d in chron.documents:
        if d.doc_type.value == "fax_cover":
            continue  # transmittals name the records department, not a treating provider
        a = acc(d.facility, d.provider)
        a.documents += 1
        a.pages.update(range(d.start_page, d.end_page + 1))
        if d.date:
            a.dates.append(d.date)
    for e in chron.events:
        a = acc(e.facility, e.provider)
        a.events += 1
        a.pages.update(s.page_no for s in e.sources)
        if e.date:
            a.dates.append(e.date)
    out = [
        ProviderEntry(
            facility=facility,
            provider=provider,
            first_date=min(a.dates) if a.dates else None,
            last_date=max(a.dates) if a.dates else None,
            documents=a.documents,
            events=a.events,
            pages=sorted(a.pages),
            bates_ranges=[bates_label(bates, x, y) for x, y in page_ranges(sorted(a.pages))],
        )
        for (facility, provider), a in groups.items()
    ]
    out.sort(key=lambda p: (p.first_date or dt.date.max, p.facility, p.provider))
    return out


def intervals(events: list[ExtractedEvent]) -> list[Interval]:
    """Intervals between consecutive dated events (same-day pairs are skipped)."""
    dated = sorted((e for e in events if e.date), key=lambda e: (e.date, e.event_id))
    out = []
    for a, b in itertools.pairwise(dated):
        assert a.date is not None and b.date is not None
        if a.date == b.date:
            continue
        out.append(
            Interval(
                from_event=a.event_id,
                to_event=b.event_id,
                from_date=a.date,
                to_date=b.date,
                days=(b.date - a.date).days,
                from_summary=a.summary,
                to_summary=b.summary,
                from_facility=a.facility,
                to_facility=b.facility,
            )
        )
    return out


def treatment_gaps(chron: Chronology, min_days: int = DEFAULT_GAP_DAYS) -> TreatmentGaps:
    """Intervals of at least ``min_days`` between encounters, plus unfulfilled follow-ups."""
    encounters = [e for e in chron.events if e.type == EventType.ENCOUNTER]
    return TreatmentGaps(
        min_days=min_days,
        encounters=len(encounters),
        intervals=[i for i in intervals(encounters) if i.days >= min_days],
        followup=[g.model_dump() for g in chron.gaps],
    )
