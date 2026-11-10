"""Event extraction agent: one bounded tool loop per document.

The ``record_events`` tool *validates as it records*. Every event must cite
a span from this document, and its quote must be found in that span
(exactly, or fuzzily for OCR text). Its date is checked against the record.
Rejected events go back to the model with reasons, so it can correct them.
This is a small evaluator-optimizer loop. Only grounded events leave this
module.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any

from medchron.chronology.models import (
    CaseFlag,
    Confidence,
    DocumentSegment,
    EventSource,
    ExtractedEvent,
)
from medchron.grounding.dates import find_dates, parse_date, parse_iso
from medchron.grounding.quotes import find_quote
from medchron.ingestion.models import IngestedCase, Span
from medchron.llm.loop import LoopResult, ToolLoop
from medchron.llm.prompts import load_prompt
from medchron.llm.providers import LLMProvider
from medchron.llm.types import LLMRequest, ToolSpec
from medchron.taxonomy import EventType

EVENTS_TOOL = ToolSpec(
    name="record_events",
    description=(
        "Record grounded chronology events and reviewer flags for this document. "
        "Returns which events were accepted and why any were rejected."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "events": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "date_text": {"type": "string"},
                        "date_iso": {"type": "string"},
                        "type": {"type": "string", "enum": [e.value for e in EventType]},
                        "summary": {"type": "string"},
                        "source_span_id": {"type": "string"},
                        "quote": {"type": "string"},
                        "confidence": {"type": "string", "enum": [c.value for c in Confidence]},
                    },
                    "required": [
                        "date_text",
                        "date_iso",
                        "type",
                        "summary",
                        "source_span_id",
                        "quote",
                        "confidence",
                    ],
                    "additionalProperties": False,
                },
            },
            "flags": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "kind": {
                            "type": "string",
                            "enum": [
                                "possible_prompt_injection",
                                "illegible_or_missing_text",
                                "other",
                            ],
                        },
                        "source_span_id": {"type": "string"},
                        "note": {"type": "string"},
                    },
                    "required": ["kind", "source_span_id", "note"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["events", "flags"],
        "additionalProperties": False,
    },
)


MAX_TURNS = 4


@dataclass
class ExtractionResult:
    events: list[ExtractedEvent] = field(default_factory=list)
    flags: list[CaseFlag] = field(default_factory=list)
    rejected: int = 0
    turns: int = 0


def render_document(doc: DocumentSegment, spans: list[Span]) -> str:
    header = (
        f"Document: {doc.title or doc.doc_type.value} ({doc.doc_type.value}); "
        f"facility: {doc.facility or 'unknown'}; author: {doc.provider or 'unknown'}; "
        f"document date: {doc.date.isoformat() if doc.date else 'unknown'}; "
        f"pages {doc.start_page}-{doc.end_page}"
    )
    body = "\n".join(f'<span id="{s.span_id}">\n{s.text}\n</span>' for s in spans)
    return f"{header}\n<record>\n{body}\n</record>"


class _Recorder:
    """Validates and accumulates events submitted through ``record_events``."""

    def __init__(self, case: IngestedCase, doc: DocumentSegment, spans: list[Span]) -> None:
        self.case, self.doc = case, doc
        self.spans = {s.span_id: s for s in spans}
        self.result = ExtractionResult()
        self._ids = itertools.count(1)
        self.last_rejected = 0

    def __call__(self, args: dict[str, Any]) -> dict[str, Any]:
        rejected: list[dict[str, Any]] = []
        accepted = 0
        for i, raw in enumerate(args.get("events", [])):
            reason = self._accept(raw)
            if reason:
                rejected.append({"index": i, "reason": reason})
                self.result.rejected += 1
            else:
                accepted += 1
        for raw in args.get("flags", []):
            span = self.spans.get(raw.get("source_span_id", ""))
            self.result.flags.append(
                CaseFlag(
                    kind=raw["kind"],
                    page_no=span.page_no if span else None,
                    span_id=span.span_id if span else None,
                    note=raw.get("note", "")[:500],
                )
            )
        self.last_rejected = len(rejected)
        return {"accepted": accepted, "rejected": rejected}

    def _accept(self, raw: dict[str, Any]) -> str | None:
        span = self.spans.get(raw["source_span_id"])
        if span is None:
            return f"source_span_id {raw['source_span_id']!r} is not a span of this document"
        match = find_quote(span.text, raw["quote"])
        if match is None:
            return "quote not found verbatim in the cited span; copy the text exactly"
        flags: list[str] = []
        date = parse_iso(raw["date_iso"]) if raw["date_iso"] else None
        if raw["date_iso"] and date is None:
            return f"date_iso {raw['date_iso']!r} is not a valid YYYY-MM-DD date"
        if date is not None:
            written = parse_date(raw["date_text"]) if raw["date_text"] else None
            if written is not None and written != date:
                return f"date_text {raw['date_text']!r} does not match date_iso {raw['date_iso']}"
            span_dates = {d for d, _ in find_dates(span.text)}
            if date not in span_dates and date != self.doc.date:
                flags.append("date_unverified")
        else:
            flags.append("undated")
        confidence = Confidence(raw["confidence"])
        if match.score < 1.0:
            flags.append("fuzzy_quote_match")
        self.result.events.append(
            ExtractedEvent(
                event_id=f"{self.doc.doc_id}-e{next(self._ids):03d}",
                date=date,
                date_text=raw["date_text"],
                type=EventType(raw["type"]),
                summary=raw["summary"].strip()[:300],
                facility=self.doc.facility,
                provider=self.doc.provider,
                doc_id=self.doc.doc_id,
                sources=[
                    EventSource(
                        span_id=span.span_id,
                        page_no=span.page_no,
                        quote=raw["quote"],
                        char_start=span.char_start + match.start,
                        char_end=span.char_start + match.end,
                        match_score=match.score,
                    )
                ],
                confidence=confidence,
                flags=flags,
            )
        )
        return None


def prepare_extraction(
    case: IngestedCase,
    doc: DocumentSegment,
    *,
    exclude_pages: set[int] | frozenset[int] = frozenset(),
) -> tuple[_Recorder, ToolLoop] | ExtractionResult:
    """Build the extraction loop for one document, or the final result if there is
    nothing to send (no extractable text)."""
    spans = [
        s
        for s in case.spans
        if doc.start_page <= s.page_no <= doc.end_page and s.page_no not in exclude_pages
    ]
    if not spans:
        return ExtractionResult(
            flags=[
                CaseFlag(
                    kind="illegible_or_missing_text",
                    page_no=doc.start_page,
                    note=f"{doc.doc_id} has no extractable text (OCR needed or failed)",
                )
            ]
        )
    system = load_prompt("system_base")
    task = load_prompt("extract_events")
    recorder = _Recorder(case, doc, spans)
    request = LLMRequest(
        route="extract",
        system=system.text + "\n\n" + task.text,
        prompt_version=f"{system.id},{task.id}",
        tools=[EVENTS_TOOL],
        messages=[{"role": "user", "content": render_document(doc, spans)}],
    )
    loop = ToolLoop(
        request,
        {"record_events": recorder},
        max_turns=MAX_TURNS,
        done=lambda: recorder.last_rejected == 0,
    )
    return recorder, loop


def finish_extraction(recorder: _Recorder, result: LoopResult) -> ExtractionResult:
    recorder.result.turns = result.turns
    return recorder.result


def extract_document(
    provider: LLMProvider,
    case: IngestedCase,
    doc: DocumentSegment,
    *,
    exclude_pages: set[int] | frozenset[int] = frozenset(),
) -> ExtractionResult:
    """Extract events from one document. ``exclude_pages`` (e.g. misfiled pages) are skipped."""
    prepared = prepare_extraction(case, doc, exclude_pages=exclude_pages)
    if isinstance(prepared, ExtractionResult):
        return prepared
    recorder, loop = prepared
    while (result := loop.apply(provider.complete(loop.next_request()))) is None:
        pass
    return finish_extraction(recorder, result)
