"""Compare two chronology versions: which events were added, removed or changed.

A matter's chronology is rebuilt whenever records arrive (a new production) or
the pipeline is re-run. Reviewers need to see what moved. Events are matched by
ID first: IDs derive from the document's first page, and page numbers are stable
across productions. Unmatched events are then paired when they have the same
type and date and a similar description, which covers events whose document
boundaries shifted. Whatever is left was added or removed.

A "changed" event usually gained sources: the same visit described again in a
newly produced record, merged into the existing event rather than duplicated.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from medchron.chronology.builder import similarity
from medchron.chronology.models import Chronology, ExtractedEvent

PAIR_SIMILARITY = 0.5


class EventChange(BaseModel):
    old: ExtractedEvent
    new: ExtractedEvent
    fields: list[str] = Field(description="date, type, summary and/or sources")
    added_pages: list[int] = Field(default_factory=list, description="pages of new sources")


class ChronologyDiff(BaseModel):
    from_version: int | None = None
    to_version: int | None = None
    added: list[ExtractedEvent] = Field(default_factory=list)
    removed: list[ExtractedEvent] = Field(default_factory=list)
    changed: list[EventChange] = Field(default_factory=list)
    unchanged: int = 0

    @property
    def is_empty(self) -> bool:
        return not (self.added or self.removed or self.changed)


def _changes(old: ExtractedEvent, new: ExtractedEvent) -> EventChange | None:
    fields = [f for f in ("date", "type", "summary") if getattr(old, f) != getattr(new, f)]
    old_spans = {s.span_id for s in old.sources}
    new_spans = {s.span_id for s in new.sources}
    if old_spans != new_spans:
        fields.append("sources")
    if not fields:
        return None
    added_pages = sorted({s.page_no for s in new.sources if s.span_id not in old_spans})
    return EventChange(old=old, new=new, fields=fields, added_pages=added_pages)


def diff_chronologies(
    old: Chronology | None,
    new: Chronology,
    *,
    from_version: int | None = None,
    to_version: int | None = None,
) -> ChronologyDiff:
    out = ChronologyDiff(from_version=from_version, to_version=to_version)
    if old is None:
        out.added = list(new.events)
        return out
    old_by_id = {e.event_id: e for e in old.events}
    new_by_id = {e.event_id: e for e in new.events}
    pairs: list[tuple[ExtractedEvent, ExtractedEvent]] = [
        (old_by_id[i], new_by_id[i]) for i in old_by_id if i in new_by_id
    ]
    rest_old = [e for i, e in old_by_id.items() if i not in new_by_id]
    rest_new = [e for i, e in new_by_id.items() if i not in old_by_id]
    for n in list(rest_new):
        match = next(
            (
                o
                for o in rest_old
                if o.type == n.type
                and o.date == n.date
                and similarity(o.summary, n.summary) >= PAIR_SIMILARITY
            ),
            None,
        )
        if match is not None:
            pairs.append((match, n))
            rest_old.remove(match)
            rest_new.remove(n)
    for o, n in pairs:
        change = _changes(o, n)
        if change is None:
            out.unchanged += 1
        else:
            out.changed.append(change)
    out.added = rest_new
    out.removed = rest_old
    return out
