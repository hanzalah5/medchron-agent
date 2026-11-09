"""Verifier: a second, independent check that cited evidence supports each claim.

Grounding already guarantees that every quote *exists* in the record. The
verifier asks a different question: does the evidence *support* what the
claim says? Each claim is shown with only its cited evidence, and an LLM
judge returns supported / partial / unsupported with a reason. Unsupported
issues are not dropped silently. They are marked and shown to the reviewer.
"""

from __future__ import annotations

from typing import Any

from medchron.chronology.models import Issue
from medchron.ingestion.models import IngestedCase
from medchron.llm.loop import ToolInputError, run_tool_loop
from medchron.llm.prompts import load_prompt
from medchron.llm.providers import LLMProvider
from medchron.llm.types import LLMRequest, ToolSpec

BATCH = 10
VERDICTS = ("supported", "partial", "unsupported")

RECORD_VERDICTS = ToolSpec(
    name="record_verdicts",
    description="Record a verdict for every claim.",
    input_schema={
        "type": "object",
        "properties": {
            "verdicts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "claim": {"type": "integer"},
                        "verdict": {"type": "string", "enum": list(VERDICTS)},
                        "reason": {"type": "string"},
                    },
                    "required": ["claim", "verdict", "reason"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["verdicts"],
        "additionalProperties": False,
    },
)


def _evidence(case: IngestedCase, issue: Issue) -> str:
    spans = {s.span_id: s for s in case.spans}
    parts = []
    for c in issue.citations:
        span = spans.get(c.span_id)
        parts.append(f"[p{c.page_no}] {span.text if span else c.quote}")
    return "\n".join(parts)


def verify_issues(provider: LLMProvider, case: IngestedCase, issues: list[Issue]) -> list[Issue]:
    if not issues:
        return []
    system = load_prompt("system_base")
    task = load_prompt("verify_claims")
    out = [i.model_copy(deep=True) for i in issues]
    for start in range(0, len(out), BATCH):
        batch = out[start : start + BATCH]
        got: dict[int, dict[str, Any]] = {}

        def record(args: dict[str, Any], n: int = len(batch), got: dict[int, Any] = got) -> str:
            for v in args["verdicts"]:
                if 1 <= v["claim"] <= n:
                    got[v["claim"]] = v
            missing = [k for k in range(1, n + 1) if k not in got]
            if missing:
                raise ToolInputError(f"missing verdicts for claims {missing}")
            return "ok"

        claims = "\n\n".join(
            f"Claim {k}: {issue.description}\n<evidence>\n{_evidence(case, issue)}\n</evidence>"
            for k, issue in enumerate(batch, start=1)
        )
        run_tool_loop(
            provider,
            LLMRequest(
                route="verify",
                system=system.text + "\n\n" + task.text,
                prompt_version=f"{system.id},{task.id}",
                tools=[RECORD_VERDICTS],
                messages=[{"role": "user", "content": claims}],
                max_tokens=4000,
            ),
            {"record_verdicts": record},
            max_turns=3,
            done=lambda got=got, n=len(batch): len(got) >= n,  # type: ignore[misc]  # noqa: B008
        )
        for k, issue in enumerate(batch, start=1):
            if k in got:
                issue.verdict = got[k]["verdict"]
                issue.verdict_reason = got[k]["reason"][:500]
    return out
