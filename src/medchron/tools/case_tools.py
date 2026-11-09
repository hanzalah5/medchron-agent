"""Read-only, matter-scoped tools that agents may call.

Security properties (see docs/plan.md, "Tools"):
- **Read-only.** No tool writes, sends or fetches anything external, so a
  prompt-injected document cannot exfiltrate data through a tool.
- **Matter-scoped.** A ``CaseTools`` instance is bound to one matter, and
  agents have no tool argument that can reach another one.
- **Citable.** Every result carries span IDs or page numbers, so anything an
  agent learns from a tool can be cited.
"""

from __future__ import annotations

import datetime as dt
import itertools
from typing import Any

from medchron.chronology.builder import find_conflicts, find_followup_gaps
from medchron.chronology.models import Chronology
from medchron.ingestion.models import IngestedCase, TextSource
from medchron.llm.loop import ToolFn, ToolInputError
from medchron.llm.types import ToolSpec
from medchron.retrieval.search import SpanSearch
from medchron.taxonomy import EventType

MAX_PAGE_CHARS = 6000


def _obj(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": props,
        "required": required,
        "additionalProperties": False,
    }


SPECS: dict[str, ToolSpec] = {
    "search_records": ToolSpec(
        name="search_records",
        description=(
            "Hybrid keyword and semantic search over this matter's records. Returns matching "
            "spans with span_id and page number. Use specific clinical terms. Set page_from and "
            "page_to to 0 to search all pages."
        ),
        input_schema=_obj(
            {
                "query": {"type": "string"},
                "page_from": {"type": "integer"},
                "page_to": {"type": "integer"},
            },
            ["query", "page_from", "page_to"],
        ),
    ),
    "get_page": ToolSpec(
        name="get_page",
        description="Return the full text of one page, with its text source and review flags.",
        input_schema=_obj({"page_no": {"type": "integer"}}, ["page_no"]),
    ),
    "query_timeline": ToolSpec(
        name="query_timeline",
        description=(
            "Filter the extracted chronology. Dates are YYYY-MM-DD; use an empty string for no "
            "bound. event_type is an event type or 'any'. contains is a case-insensitive text "
            "filter (empty for none)."
        ),
        input_schema=_obj(
            {
                "date_from": {"type": "string"},
                "date_to": {"type": "string"},
                "event_type": {"type": "string", "enum": ["any", *[e.value for e in EventType]]},
                "contains": {"type": "string"},
            },
            ["date_from", "date_to", "event_type", "contains"],
        ),
    ),
    "find_gaps": ToolSpec(
        name="find_gaps",
        description=(
            "Run deterministic review rules over the chronology. 'followup' finds directed "
            "follow-ups with no later matching encounter. 'date_conflicts' finds procedures "
            "with conflicting dates. 'intervals' lists the days between consecutive events."
        ),
        input_schema=_obj(
            {"rule": {"type": "string", "enum": ["followup", "date_conflicts", "intervals"]}},
            ["rule"],
        ),
    ),
}


class CaseTools:
    def __init__(self, case: IngestedCase, chronology: Chronology, search: SpanSearch) -> None:
        self.case, self.chron, self._search = case, chronology, search

    def specs(self, names: list[str] | None = None) -> list[ToolSpec]:
        return [SPECS[n] for n in (names or list(SPECS))]

    def functions(self) -> dict[str, ToolFn]:
        return {
            "search_records": self.search_records,
            "get_page": self.get_page,
            "query_timeline": self.query_timeline,
            "find_gaps": self.find_gaps,
        }

    # ------------------------------------------------------------------ tools
    def search_records(self, args: dict[str, Any]) -> list[dict[str, Any]]:
        query = str(args.get("query", "")).strip()
        if not query:
            raise ToolInputError("query must not be empty")
        hits = self._search.search(
            query,
            limit=8,
            page_from=int(args.get("page_from") or 0) or None,
            page_to=int(args.get("page_to") or 0) or None,
        )
        return [
            {"span_id": h.span_id, "page": h.page_no, "text": h.text[:700], "score": h.score}
            for h in hits
        ]

    def get_page(self, args: dict[str, Any]) -> dict[str, Any]:
        n = int(args["page_no"])
        if not 1 <= n <= len(self.case.pages):
            raise ToolInputError(f"page_no must be between 1 and {len(self.case.pages)}")
        page = self.case.page(n)
        return {
            "page": n,
            "text_source": page.source.value,
            "ocr_confidence": page.ocr_confidence,
            "duplicate_of": page.duplicate_of,
            "needs_review": page.needs_review,
            "text": page.text[:MAX_PAGE_CHARS]
            if page.source != TextSource.NONE
            else "[no text available]",
            "spans": [s.span_id for s in self.case.spans if s.page_no == n],
        }

    def query_timeline(self, args: dict[str, Any]) -> list[dict[str, Any]]:
        def parse(v: str) -> dt.date | None:
            if not v:
                return None
            try:
                return dt.date.fromisoformat(v)
            except ValueError as e:
                raise ToolInputError(f"invalid date {v!r}; use YYYY-MM-DD") from e

        lo, hi = parse(args.get("date_from", "")), parse(args.get("date_to", ""))
        etype = args.get("event_type", "any")
        needle = str(args.get("contains", "")).lower()
        out = []
        for e in self.chron.events:
            if etype != "any" and e.type.value != etype:
                continue
            if lo and (e.date is None or e.date < lo):
                continue
            if hi and (e.date is None or e.date > hi):
                continue
            blob = (e.summary + " " + " ".join(s.quote for s in e.sources)).lower()
            if needle and needle not in blob:
                continue
            out.append(
                {
                    "event_id": e.event_id,
                    "date": e.date.isoformat() if e.date else None,
                    "type": e.type.value,
                    "summary": e.summary,
                    "facility": e.facility,
                    "sources": [
                        {"span_id": s.span_id, "page": s.page_no, "quote": s.quote}
                        for s in e.sources
                    ],
                }
            )
        return out[:60]

    def find_gaps(self, args: dict[str, Any]) -> list[dict[str, Any]]:
        rule = args.get("rule")
        if rule == "followup":
            return [g.model_dump() for g in find_followup_gaps(self.chron.events)]
        if rule == "date_conflicts":
            return [c.model_dump() for c in find_conflicts(self.chron.events)]
        if rule == "intervals":
            dated = [e for e in self.chron.events if e.date]
            return [
                {
                    "from_event": a.event_id,
                    "to_event": b.event_id,
                    "from": f"{a.date} {a.type.value}: {a.summary}",
                    "to": f"{b.date} {b.type.value}: {b.summary}",
                    "days": (b.date - a.date).days,  # type: ignore[operator]
                }
                for a, b in itertools.pairwise(dated)
                if b.date != a.date
            ]
        raise ToolInputError("rule must be followup, date_conflicts or intervals")
