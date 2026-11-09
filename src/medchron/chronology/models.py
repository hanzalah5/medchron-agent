"""Pipeline output types: document segments, grounded events, chronology."""

from __future__ import annotations

import datetime as dt
from enum import StrEnum

from pydantic import BaseModel, Field

from medchron.taxonomy import DocType, EventType


class Confidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class DocumentSegment(BaseModel):
    """A logical document found by the classifier inside the PDF bundle."""

    doc_id: str
    doc_type: DocType
    title: str
    date: dt.date | None
    facility: str
    provider: str
    start_page: int
    end_page: int


class EventSource(BaseModel):
    span_id: str
    page_no: int
    quote: str
    char_start: int = Field(description="Offset of the matched quote in the page text")
    char_end: int
    match_score: float = Field(description="1.0 for an exact match, lower for fuzzy (OCR)")


class ExtractedEvent(BaseModel):
    event_id: str
    date: dt.date | None
    date_text: str
    type: EventType
    summary: str
    facility: str
    provider: str
    doc_id: str
    sources: list[EventSource] = Field(min_length=1)
    confidence: Confidence
    flags: list[str] = Field(default_factory=list)


class CaseFlag(BaseModel):
    kind: str  # possible_prompt_injection | illegible_or_missing_text | extraction_failed | ...
    page_no: int | None = None
    span_id: str | None = None
    note: str


class Conflict(BaseModel):
    description: str
    event_ids: list[str]


class Gap(BaseModel):
    description: str
    event_ids: list[str]


class Chronology(BaseModel):
    case_id: str
    documents: list[DocumentSegment]
    events: list[ExtractedEvent]
    conflicts: list[Conflict] = Field(default_factory=list)
    gaps: list[Gap] = Field(default_factory=list)
    flags: list[CaseFlag] = Field(default_factory=list)
