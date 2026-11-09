"""Issue analyst: the most agentic step in the pipeline.

An open-ended investigation loop over read-only, matter-scoped tools, with
hard limits on turns (and on cost, through the metered provider). The
``record_issue`` tool enforces three guardrails before it accepts an issue:

1. **Grounding**: every citation's quote must be found in the cited span.
2. **Scope**: the description must not use conclusory legal or medical
   language. Issues are questions for experts, not findings.
3. **Validity**: the category and event IDs must exist.

Rejections go back to the model with reasons, so it can correct itself.
"""

from __future__ import annotations

import itertools
import re
from typing import Any

from medchron.chronology.models import Chronology, Citation, Issue
from medchron.grounding.quotes import find_quote
from medchron.ingestion.models import IngestedCase
from medchron.llm.loop import ToolInputError, run_tool_loop
from medchron.llm.prompts import load_prompt
from medchron.llm.providers import LLMProvider
from medchron.llm.types import LLMRequest, ToolSpec
from medchron.retrieval.search import SpanSearch
from medchron.taxonomy import IssueKind
from medchron.tools.case_tools import CaseTools

MAX_TURNS = 14

# Wording that turns a question for an expert into a legal or medical conclusion.
CONCLUSORY = re.compile(
    r"\b(negligen\w*|malpractice|breach(?:ed)? (?:of )?the standard|"
    r"(?:fell|falls|was|were) below the standard|deviat\w+ from the standard|"
    r"standard of care (?:was|were) (?:not )?met|"
    r"(?:met|meets|meet|within|exceeded) the (?:applicable )?standard of care|"
    r"caused (?:the|her|his|their) (?:injury|harm|death)|"
    r"\bat fault\b|liab(?:le|ility))\b",
    re.I,
)

RECORD_ISSUE = ToolSpec(
    name="record_issue",
    description="Record one issue for expert review, with verbatim citations to record spans.",
    input_schema={
        "type": "object",
        "properties": {
            "category": {"type": "string", "enum": [k.value for k in IssueKind]},
            "description": {"type": "string"},
            "event_ids": {"type": "array", "items": {"type": "string"}},
            "citations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"span_id": {"type": "string"}, "quote": {"type": "string"}},
                    "required": ["span_id", "quote"],
                    "additionalProperties": False,
                },
            },
            "severity_hint": {"type": "string", "enum": ["low", "medium", "high"]},
        },
        "required": ["category", "description", "event_ids", "citations", "severity_hint"],
        "additionalProperties": False,
    },
)


def scope_violation(text: str) -> str | None:
    m = CONCLUSORY.search(text)
    return m.group(0) if m else None


class _IssueRecorder:
    def __init__(self, case: IngestedCase, chron: Chronology) -> None:
        self.spans = {s.span_id: s for s in case.spans}
        self.event_ids = {e.event_id for e in chron.events}
        self.issues: list[Issue] = []
        self.rejected = 0
        self._ids = itertools.count(1)

    def __call__(self, args: dict[str, Any]) -> str:
        try:
            issue = self._validate(args)
        except ToolInputError:
            self.rejected += 1
            raise
        self.issues.append(issue)
        return f"recorded {issue.issue_id}"

    def _validate(self, args: dict[str, Any]) -> Issue:
        bad = scope_violation(args["description"])
        if bad:
            raise ToolInputError(
                f"description uses conclusory language ({bad!r}). Rephrase it as a neutral "
                "question for expert review."
            )
        unknown = [e for e in args.get("event_ids", []) if e not in self.event_ids]
        if unknown:
            raise ToolInputError(f"unknown event_ids {unknown}; use query_timeline to find IDs")
        if not args.get("citations"):
            raise ToolInputError("at least one citation is required")
        cites: list[Citation] = []
        for c in args["citations"]:
            span = self.spans.get(c["span_id"])
            if span is None:
                raise ToolInputError(f"span_id {c['span_id']!r} does not exist")
            m = find_quote(span.text, c["quote"])
            if m is None:
                raise ToolInputError(f"quote not found in {c['span_id']}; copy it verbatim")
            cites.append(
                Citation(
                    span_id=span.span_id,
                    page_no=span.page_no,
                    quote=c["quote"],
                    char_start=span.char_start + m.start,
                    char_end=span.char_start + m.end,
                )
            )
        return Issue(
            issue_id=f"I{next(self._ids):03d}",
            category=IssueKind(args["category"]).value,
            description=args["description"].strip()[:1200],
            event_ids=list(args.get("event_ids", [])),
            citations=cites,
            severity_hint=args.get("severity_hint", "medium"),
        )


def analyze(
    provider: LLMProvider, case: IngestedCase, chron: Chronology, search: SpanSearch
) -> tuple[list[Issue], int]:
    """Run the analyst loop. Returns (issues, rejected submissions)."""
    tools = CaseTools(case, chron, search)
    recorder = _IssueRecorder(case, chron)
    system = load_prompt("system_base")
    task = load_prompt("analyze_issues")
    overview = (
        f"Matter {case.case_id}: {len(case.pages)} pages, {len(chron.documents)} documents, "
        f"{len(chron.events)} chronology events "
        f"({chron.events[0].date} to {chron.events[-1].date})."
        if chron.events
        else f"Matter {case.case_id}: no chronology events were extracted."
    )
    signals = [f"- candidate date conflict: {c.description}" for c in chron.conflicts] + [
        f"- candidate missing record: {g.description}" for g in chron.gaps
    ]
    flags = [f"- reviewer flag ({f.kind}) on p{f.page_no}: {f.note}" for f in chron.flags]
    content = "\n".join(
        [
            overview,
            "Deterministic signals already found (verify before recording):",
            *signals,
            *flags,
        ]
        if signals or flags
        else [overview]
    )
    request = LLMRequest(
        route="analyze",
        system=system.text + "\n\n" + task.text,
        prompt_version=f"{system.id},{task.id}",
        tools=[*tools.specs(), RECORD_ISSUE],
        messages=[{"role": "user", "content": content}],
    )
    run_tool_loop(
        provider, request, {**tools.functions(), "record_issue": recorder}, max_turns=MAX_TURNS
    )
    return recorder.issues, recorder.rejected
