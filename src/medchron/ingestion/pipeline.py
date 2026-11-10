"""Ingestion entry point: PDF productions -> pages, duplicates, spans.

A matter can receive several productions over time. Pages are numbered across
productions (the second production starts after the last page of the first),
so page numbers, span IDs and citations stay stable when records arrive later.
Duplicate detection runs across all productions: a page re-produced in a later
batch is marked as a duplicate of the earlier copy.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from pathlib import Path

from medchron.ingestion.chunking import chunk_page
from medchron.ingestion.dedupe import mark_duplicates
from medchron.ingestion.models import IngestedCase, Page
from medchron.ingestion.pdf import extract_pages


def _bytes(pdf: bytes | Path) -> bytes:
    return pdf.read_bytes() if isinstance(pdf, Path) else pdf


def extract_production(
    pdf: bytes | Path, *, production: int, page_offset: int, ocr: bool = True
) -> list[Page]:
    """Text, OCR and word boxes for one production (the expensive, cacheable step)."""
    return extract_pages(_bytes(pdf), ocr=ocr, first_page_no=page_offset + 1, production=production)


BATES_DIGITS = 6


def bates_prefix_for(matter_id: str) -> str:
    """A default Bates prefix: the matter ID's letters and digits, upper-case, at most 12."""
    return re.sub(r"[^A-Z0-9]", "", matter_id.upper())[:12] or "MC"


def bates_id(prefix: str, page_no: int) -> str:
    return f"{prefix}-{page_no:0{BATES_DIGITS}d}"


def assemble_case(
    case_id: str, pages: list[Page], shas: list[str], *, bates_prefix: str | None = None
) -> IngestedCase:
    """Cross-production duplicate detection, Bates numbering and chunking.

    Every produced page gets a Bates-style ID from the matter prefix and its page
    number. Page numbers run across productions, so IDs never collide and stay
    stable when later productions arrive.
    """
    prefix = bates_prefix or bates_prefix_for(case_id)
    pages = [
        p.model_copy(update={"duplicate_of": None, "bates": bates_id(prefix, p.page_no)})
        for p in pages
    ]
    pages = mark_duplicates(pages)
    spans = [s for p in pages if p.duplicate_of is None for s in chunk_page(case_id, p)]
    return IngestedCase(
        case_id=case_id,
        file_sha256=shas[0] if shas else "",
        pages=pages,
        spans=spans,
        productions=shas,
    )


def ingest_productions(
    pdfs: Sequence[bytes | Path], case_id: str, *, ocr: bool = True, bates_prefix: str | None = None
) -> IngestedCase:
    pages: list[Page] = []
    shas: list[str] = []
    for n, pdf in enumerate(pdfs, start=1):
        data = _bytes(pdf)
        shas.append(hashlib.sha256(data).hexdigest())
        pages += extract_production(data, production=n, page_offset=len(pages), ocr=ocr)
    return assemble_case(case_id, pages, shas, bates_prefix=bates_prefix)


def ingest_pdf(pdf: bytes | Path, case_id: str, *, ocr: bool = True) -> IngestedCase:
    return ingest_productions([pdf], case_id, ocr=ocr)
