"""Page classifier and document segmentation.

The classifier makes one structured LLM call per batch of pages (strict tool
schema). Code then turns the page labels into document segments, so the
segmentation logic stays deterministic and testable.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from medchron.chronology.models import DocumentSegment
from medchron.grounding.dates import parse_iso
from medchron.ingestion.models import IngestedCase, Page, TextSource
from medchron.llm.loop import ToolInputError, run_tool_loop
from medchron.llm.prompts import load_prompt
from medchron.llm.providers import LLMProvider
from medchron.llm.types import LLMRequest, ToolSpec
from medchron.taxonomy import DocType

BATCH_PAGES = 20
PAGE_CHARS = 1500

LABEL_TOOL = ToolSpec(
    name="label_pages",
    description="Record the document labels for every page in this batch, in page order.",
    input_schema={
        "type": "object",
        "properties": {
            "pages": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "page_number": {"type": "integer"},
                        "doc_type": {"type": "string", "enum": [d.value for d in DocType]},
                        "title": {"type": "string"},
                        "date_iso": {"type": "string"},
                        "facility": {"type": "string"},
                        "provider": {"type": "string"},
                        "starts_new_document": {"type": "boolean"},
                    },
                    "required": [
                        "page_number",
                        "doc_type",
                        "title",
                        "date_iso",
                        "facility",
                        "provider",
                        "starts_new_document",
                    ],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["pages"],
        "additionalProperties": False,
    },
)


class PageLabel(BaseModel):
    page_number: int
    doc_type: DocType
    title: str
    date_iso: str
    facility: str
    provider: str
    starts_new_document: bool


def _render(page: Page) -> str:
    body = page.text[:PAGE_CHARS] if page.source != TextSource.NONE else "[no text available]"
    return f'<page number="{page.page_no}">\n{body}\n</page>'


def classify_pages(provider: LLMProvider, case: IngestedCase) -> list[PageLabel]:
    system = load_prompt("system_base")
    task = load_prompt("classify_pages")
    pages = [p for p in case.pages if p.duplicate_of is None]
    labels: dict[int, PageLabel] = {}
    for i in range(0, len(pages), BATCH_PAGES):
        batch = pages[i : i + BATCH_PAGES]
        expected = {p.page_no for p in batch}

        def record(args: dict[str, Any], expected: set[int] = expected) -> str:
            got = [PageLabel.model_validate(p) for p in args["pages"]]
            unknown = {g.page_number for g in got} - expected
            if unknown:
                raise ToolInputError(f"pages {sorted(unknown)} are not in this batch")
            for g in got:
                labels[g.page_number] = g
            missing = sorted(expected - set(labels))
            if missing:
                raise ToolInputError(f"missing labels for pages {missing}; label them too")
            return "ok"

        request = LLMRequest(
            route="classify",
            system=system.text + "\n\n" + task.text,
            prompt_version=f"{system.id},{task.id}",
            tools=[LABEL_TOOL],
            messages=[
                {
                    "role": "user",
                    "content": "\n\n".join(_render(p) for p in batch),
                }
            ],
            max_tokens=8000,
        )
        run_tool_loop(
            provider,
            request,
            {"label_pages": record},
            max_turns=3,
            done=lambda expected=expected: expected <= set(labels),  # type: ignore[misc]
        )
        for page_no in expected - set(labels):  # model never labeled it: keep it reviewable
            labels[page_no] = PageLabel(
                page_number=page_no,
                doc_type=DocType.OTHER,
                title="",
                date_iso="",
                facility="",
                provider="",
                starts_new_document=False,
            )
    return [labels[p.page_no] for p in pages]


def segment(labels: list[PageLabel]) -> list[DocumentSegment]:
    """Group consecutive page labels into documents."""
    segments: list[DocumentSegment] = []
    for label in labels:
        prev = segments[-1] if segments else None
        new = (
            prev is None
            or label.starts_new_document
            or label.doc_type != prev.doc_type
            or (label.facility and prev.facility and label.facility != prev.facility)
        )
        if new:
            segments.append(
                DocumentSegment(
                    doc_id=f"doc-{label.page_number:04d}",
                    doc_type=label.doc_type,
                    title=label.title,
                    date=parse_iso(label.date_iso),
                    facility=label.facility,
                    provider=label.provider,
                    start_page=label.page_number,
                    end_page=label.page_number,
                )
            )
        else:
            assert prev is not None
            prev.end_page = label.page_number
    return segments
