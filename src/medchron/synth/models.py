"""Gold-label schema for the synthetic benchmark.

Every case consists of one PDF "record production" plus a gold JSON file
described by :class:`CaseGold`. Page numbers are 1-based positions in the PDF.
Quotes are verbatim text from the page; matching is whitespace-normalized
because line wrapping changes whitespace.
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum

from pydantic import BaseModel, Field

GENERATOR_VERSION = "0.1.0"


class DocType(StrEnum):
    FAX_COVER = "fax_cover"
    OFFICE_VISIT = "office_visit"
    ED_NOTE = "ed_note"
    LAB_REPORT = "lab_report"
    RADIOLOGY_REPORT = "radiology_report"
    OPERATIVE_REPORT = "operative_report"
    PROGRESS_NOTE = "progress_note"
    DISCHARGE_SUMMARY = "discharge_summary"
    THERAPY_NOTE = "therapy_note"
    TELEPHONE_NOTE = "telephone_note"


class EventType(StrEnum):
    ENCOUNTER = "encounter"
    COMPLAINT = "complaint"
    DIAGNOSIS = "diagnosis"
    PROCEDURE = "procedure"
    MEDICATION = "medication"
    LAB_RESULT = "lab_result"
    IMAGING = "imaging"
    REFERRAL = "referral"
    INSTRUCTION = "instruction"
    COMMUNICATION = "communication"


class IssueKind(StrEnum):
    MISSING_RECORD = "missing_record"
    DATE_CONFLICT = "date_conflict"
    RECORD_INCONSISTENCY = "record_inconsistency"
    POTENTIAL_DELAY = "potential_delay"
    PROMPT_INJECTION = "prompt_injection"


class SourceRef(BaseModel):
    doc_id: str
    page: int = Field(ge=1, description="1-based page in the bundle PDF")
    quote: str = Field(description="Verbatim supporting text on that page")


class SyntheticPatient(BaseModel):
    name: str
    dob: dt.date
    sex: str


class GoldDocument(BaseModel):
    doc_id: str
    doc_type: DocType
    title: str
    date: dt.date
    facility: str
    provider: str
    start_page: int = Field(ge=1)
    end_page: int = Field(ge=1)


class GoldEvent(BaseModel):
    event_id: str
    date: dt.date
    type: EventType
    summary: str
    facility: str
    provider: str
    sources: list[SourceRef] = Field(min_length=1)


class PlantedIssue(BaseModel):
    issue_id: str
    kind: IssueKind
    description: str = Field(description="Phrased as a question for expert review")
    event_ids: list[str] = Field(default_factory=list)
    pages: list[int] = Field(default_factory=list)


class GoldPage(BaseModel):
    page: int = Field(ge=1)
    doc_id: str
    doc_page: int = Field(ge=1)
    text_layer: bool = Field(description="False when rendered as a scanned image (needs OCR)")
    duplicate_of: int | None = None
    text: str = Field(description="Ground-truth page text, before any rendering noise")


class CaseGold(BaseModel):
    case_id: str
    seed: int
    scenario: str
    generator_version: str = GENERATOR_VERSION
    synthetic: bool = True
    patient: SyntheticPatient
    documents: list[GoldDocument]
    events: list[GoldEvent]
    issues: list[PlantedIssue]
    pages: list[GoldPage]
