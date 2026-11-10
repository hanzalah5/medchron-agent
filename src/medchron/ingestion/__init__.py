"""Deterministic ingestion: text and OCR extraction, de-duplication, chunking."""

from medchron.ingestion.models import IngestedCase, Page, Span, TextSource
from medchron.ingestion.pipeline import ingest_pdf, ingest_productions

__all__ = ["IngestedCase", "Page", "Span", "TextSource", "ingest_pdf", "ingest_productions"]
