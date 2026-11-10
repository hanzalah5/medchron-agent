"""Shared taxonomy: document types, event types and issue kinds.

The pipeline, the evaluation harness and the synthetic generator all use
these enums, so predictions and gold labels are always comparable.
"""

from __future__ import annotations

from enum import StrEnum


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
    MEDICATION_LIST = "medication_list"  # medication administration records and med lists
    OTHER = "other"


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
    MISFILED_PAGE = "misfiled_page"  # a page that belongs to a different patient
