"""Ingestion entry point: PDF productions -> pages, duplicates, spans.

A matter can receive several productions over time. Pages are numbered across
productions (the second production starts after the last page of the first),
so page numbers, span IDs and citations stay stable when records arrive later.
Duplicate detection runs across all productions: a page re-produced in a later
batch is marked as a duplicate of the earlier copy.
"""

from __future__ import annotations

import hashlib
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


def assemble_case(case_id: str, pages: list[Page], shas: list[str]) -> IngestedCase:
    """Cross-production duplicate detection and chunking (deterministic and cheap)."""
    pages = [p.model_copy(update={"duplicate_of": None}) for p in pages]
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
    pdfs: Sequence[bytes | Path], case_id: str, *, ocr: bool = True
) -> IngestedCase:
    pages: list[Page] = []
    shas: list[str] = []
    for n, pdf in enumerate(pdfs, start=1):
        data = _bytes(pdf)
        shas.append(hashlib.sha256(data).hexdigest())
        pages += extract_production(data, production=n, page_offset=len(pages), ocr=ocr)
    return assemble_case(case_id, pages, shas)


def ingest_pdf(pdf: bytes | Path, case_id: str, *, ocr: bool = True) -> IngestedCase:
    return ingest_productions([pdf], case_id, ocr=ocr)
