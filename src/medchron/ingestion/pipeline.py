"""Ingestion entry point: PDF bytes -> pages, duplicates, spans."""

from __future__ import annotations

import hashlib
from pathlib import Path

from medchron.ingestion.chunking import chunk_page
from medchron.ingestion.dedupe import mark_duplicates
from medchron.ingestion.models import IngestedCase
from medchron.ingestion.pdf import extract_pages


def ingest_pdf(pdf: bytes | Path, case_id: str, *, ocr: bool = True) -> IngestedCase:
    data = pdf.read_bytes() if isinstance(pdf, Path) else pdf
    pages = mark_duplicates(extract_pages(data, ocr=ocr))
    spans = [s for p in pages if p.duplicate_of is None for s in chunk_page(case_id, p)]
    return IngestedCase(
        case_id=case_id,
        file_sha256=hashlib.sha256(data).hexdigest(),
        pages=pages,
        spans=spans,
    )
