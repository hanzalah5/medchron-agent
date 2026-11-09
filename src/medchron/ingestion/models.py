"""Data model for ingested documents.

Every character of page text is addressable. ``Word`` carries a bounding box
and its offsets in ``Page.text``, so any span of text can be highlighted on
the page image. A ``Span`` is the unit agents retrieve and cite. Its ID
encodes the case, page and character range, so a citation can always be
resolved back to the exact text.
"""

from __future__ import annotations

import re
from enum import StrEnum

from pydantic import BaseModel, Field

SPAN_ID_RE = re.compile(r"^(?P<case>[\w.-]+):p(?P<page>\d+):(?P<start>\d+)-(?P<end>\d+)$")


class TextSource(StrEnum):
    TEXT_LAYER = "text_layer"
    OCR = "ocr"
    NONE = "none"  # image-only page and OCR unavailable or empty


class BBox(BaseModel):
    """Bounding box in PDF points, origin top-left (PyMuPDF convention)."""

    x0: float
    y0: float
    x1: float
    y1: float


class Word(BaseModel):
    text: str
    bbox: BBox
    char_start: int
    char_end: int


class Page(BaseModel):
    page_no: int = Field(ge=1)
    text: str
    words: list[Word]
    source: TextSource
    ocr_confidence: float | None = Field(
        default=None, description="Mean Tesseract word confidence, 0-100"
    )
    width: float
    height: float
    content_hash: str = Field(description="SHA-256 of whitespace-normalized text")
    duplicate_of: int | None = Field(default=None, description="Earlier page this duplicates")

    @property
    def needs_review(self) -> bool:
        """Low-confidence OCR or empty pages go to human review (HITL-1)."""
        if self.source == TextSource.NONE:
            return True
        return self.source == TextSource.OCR and (self.ocr_confidence or 0) < 70


class Span(BaseModel):
    span_id: str
    case_id: str
    page_no: int
    char_start: int
    char_end: int
    text: str

    @staticmethod
    def make_id(case_id: str, page_no: int, start: int, end: int) -> str:
        return f"{case_id}:p{page_no}:{start}-{end}"


class IngestedCase(BaseModel):
    case_id: str
    file_sha256: str
    pages: list[Page]
    spans: list[Span]

    def page(self, page_no: int) -> Page:
        return self.pages[page_no - 1]

    def resolve(self, span_id: str) -> str:
        """Return the exact text a span ID points at (raises on invalid IDs)."""
        m = SPAN_ID_RE.match(span_id)
        if not m or m["case"] != self.case_id:
            raise KeyError(f"invalid span id for case {self.case_id}: {span_id!r}")
        page_no, start, end = int(m["page"]), int(m["start"]), int(m["end"])
        if not 1 <= page_no <= len(self.pages):
            raise KeyError(f"page out of range: {span_id!r}")
        text = self.page(page_no).text
        if not 0 <= start < end <= len(text):
            raise KeyError(f"offsets out of range: {span_id!r}")
        return text[start:end]
