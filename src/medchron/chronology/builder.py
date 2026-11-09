"""Deterministic chronology assembly: merge, order, detect conflicts and gaps.

The extraction agents produce one event per mention. This module merges
mentions of the same event across documents (keeping every source), orders
them, and computes *candidate* review signals with transparent rules:

- **Date conflicts**: two procedure mentions that describe the same procedure
  but carry different dates within a short window.
- **Follow-up gaps**: a referral or instruction to follow up, with no later
  encounter from the same facility, or with a clinician named in the
  referral, that could be that follow-up.

These are signals for human and analyst review (step 9), never conclusions.
"""

from __future__ import annotations

import datetime as dt
import re

from medchron.chronology.models import (
    CaseFlag,
    Chronology,
    Conflict,
    DocumentSegment,
    ExtractedEvent,
    Gap,
)
from medchron.taxonomy import EventType

MERGE_SIMILARITY = 0.5
CONFLICT_SIMILARITY = 0.4
CONFLICT_WINDOW_DAYS = 7
FOLLOWUP_WINDOW_DAYS = 60
_STOP = {"the", "a", "an", "of", "and", "with", "for", "to", "in", "on", "at", "by", "was", "is"}


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in _STOP and len(t) > 2}


def similarity(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


def _event_text(e: ExtractedEvent) -> str:
    return e.summary + " " + " ".join(s.quote for s in e.sources)


def merge_events(events: list[ExtractedEvent]) -> list[ExtractedEvent]:
    """Merge mentions with the same type and date that describe the same thing."""
    merged: list[ExtractedEvent] = []
    for ev in sorted(events, key=lambda e: (e.date or dt.date.max, e.event_id)):
        target = next(
            (
                m
                for m in merged
                if m.type == ev.type
                and m.date == ev.date
                and ev.date is not None
                and similarity(_event_text(m), _event_text(ev)) >= MERGE_SIMILARITY
            ),
            None,
        )
        if target is None:
            merged.append(ev.model_copy(deep=True))
            continue
        known = {s.span_id for s in target.sources}
        target.sources.extend(s for s in ev.sources if s.span_id not in known)
        target.flags = sorted(set(target.flags) | set(ev.flags))
    return merged


def find_conflicts(events: list[ExtractedEvent]) -> list[Conflict]:
    procs = [e for e in events if e.type == EventType.PROCEDURE and e.date]
    conflicts: list[Conflict] = []
    for i, a in enumerate(procs):
        for b in procs[i + 1 :]:
            assert a.date and b.date
            days = abs((a.date - b.date).days)
            if 0 < days <= CONFLICT_WINDOW_DAYS and (
                similarity(_event_text(a), _event_text(b)) >= CONFLICT_SIMILARITY
            ):
                conflicts.append(
                    Conflict(
                        description=(
                            f"Records give different dates for what may be the same procedure: "
                            f"{a.date.isoformat()} (p{a.sources[0].page_no}) vs "
                            f"{b.date.isoformat()} (p{b.sources[0].page_no})."
                        ),
                        event_ids=[a.event_id, b.event_id],
                    )
                )
    return conflicts


_FOLLOWUP = re.compile(r"\bfollow[\s-]?up\b|\breturn (?:to|in)\b|\bsee (?:dr|in)\b", re.I)


def find_followup_gaps(events: list[ExtractedEvent]) -> list[Gap]:
    gaps: list[Gap] = []
    encounters = [e for e in events if e.type == EventType.ENCOUNTER and e.date]
    for ev in events:
        if ev.type not in (EventType.REFERRAL, EventType.INSTRUCTION) or not ev.date:
            continue
        if not _FOLLOWUP.search(_event_text(ev)):
            continue
        assert ev.date is not None
        start = ev.date
        named = {n.lower() for n in re.findall(r"\b[A-Z]\. ([A-Z][a-z]+)", _event_text(ev))}
        later = [
            e
            for e in encounters
            if e.date
            and start < e.date <= start + dt.timedelta(days=FOLLOWUP_WINDOW_DAYS)
            and (e.facility == ev.facility or any(n in (e.provider or "").lower() for n in named))
        ]
        if not later:
            gaps.append(
                Gap(
                    description=(
                        f"Follow-up directed on {start.isoformat()} "
                        f"(p{ev.sources[0].page_no}), but no later encounter within "
                        f"{FOLLOWUP_WINDOW_DAYS} days from the same facility or named clinician "
                        "appears in the production. "
                        "Records may be missing."
                    ),
                    event_ids=[ev.event_id],
                )
            )
    return gaps


def build_chronology(
    case_id: str,
    documents: list[DocumentSegment],
    events: list[ExtractedEvent],
    flags: list[CaseFlag],
) -> Chronology:
    merged = merge_events(events)
    merged.sort(key=lambda e: (e.date or dt.date.max, e.sources[0].page_no, e.event_id))
    return Chronology(
        case_id=case_id,
        documents=documents,
        events=merged,
        conflicts=find_conflicts(merged),
        gaps=find_followup_gaps(merged),
        flags=flags,
    )
