"""Guardrail tests for the analyst, verifier and drafter, using scripted model output."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from medchron.agents.analyst import analyze, scope_violation
from medchron.agents.drafter import draft_memo
from medchron.agents.verifier import verify_issues
from medchron.evals.metrics import score_case
from medchron.evals.oracle import OracleProvider
from medchron.llm.providers import ScriptedProvider
from medchron.llm.types import (
    Citation as LLMCitation,
)
from medchron.llm.types import (
    LLMResponse,
    StopReason,
    TextPart,
    ToolCall,
)
from medchron.orchestrator.pipeline import RunConfig, RunResult, run_case
from medchron.retrieval.embeddings import HashingEmbedder
from medchron.retrieval.search import MemorySearch
from medchron.synth import CaseGold

SMOKE = Path(__file__).resolve().parents[1] / "data" / "synthetic"


@pytest.fixture(scope="module")
def run(tmp_path_factory: pytest.TempPathFactory) -> tuple[RunResult, CaseGold]:
    gold = CaseGold.model_validate_json((SMOKE / "case_0003.gold.json").read_text())
    result = run_case(
        SMOKE / "case_0003.pdf",
        "case_0003",
        OracleProvider(gold),
        RunConfig(out_dir=tmp_path_factory.mktemp("a")),
    )
    return result, gold


def tool(*calls: tuple[str, dict[str, Any]]) -> LLMResponse:
    ids = [f"t{i}" for i in range(len(calls))]
    return LLMResponse(
        model="scripted",
        stop_reason=StopReason.TOOL_USE,
        content=[
            {"type": "tool_use", "id": i, "name": n, "input": a}
            for i, (n, a) in zip(ids, calls, strict=True)
        ],
        tool_calls=[ToolCall(id=i, name=n, input=a) for i, (n, a) in zip(ids, calls, strict=True)],
    )


def done() -> LLMResponse:
    return LLMResponse(
        model="scripted",
        stop_reason=StopReason.END_TURN,
        content=[{"type": "text", "text": "done"}],
        text_parts=[TextPart(text="done")],
    )


@pytest.mark.parametrize(
    "text",
    [
        "The physician was negligent in discharging the patient.",
        "This was a breach of the standard of care.",
        "Care fell below the standard expected.",
        "The standard of care was not met.",
        "This is a clear case of malpractice.",
        "All care met the standard of care.",
        "Treatment was within the standard of care.",
    ],
)
def test_scope_guard_flags_conclusions(text: str) -> None:
    assert scope_violation(text)


def test_scope_guard_allows_questions() -> None:
    q = "The WBC was elevated at the first visit. Was it addressed before discharge?"
    assert scope_violation(q) is None


def test_analyst_enforces_scope_and_grounding(run: tuple[RunResult, CaseGold]) -> None:
    result, _ = run
    span = next(s for s in result.case.spans if "WBC" in s.text)
    quote = next(line for line in span.text.splitlines() if "WBC" in line).strip()
    good = {
        "category": "potential_delay",
        "description": "The WBC was elevated at the first ED visit. Was it addressed?",
        "event_ids": [],
        "citations": [{"span_id": span.span_id, "quote": quote}],
        "severity_hint": "high",
    }
    conclusory = {**good, "description": "Discharging with an elevated WBC was negligent."}
    fabricated = {
        **good,
        "citations": [{"span_id": span.span_id, "quote": "WBC was 25.0 and rising"}],
    }
    unknown = {**good, "event_ids": ["nope"]}
    provider = ScriptedProvider(
        [
            tool(
                ("record_issue", conclusory),
                ("record_issue", fabricated),
                ("record_issue", unknown),
            ),
            tool(("record_issue", good)),
            done(),
        ]
    )
    issues, rejected = analyze(
        provider, result.case, result.chronology, MemorySearch(result.case, HashingEmbedder())
    )
    assert rejected == 3 and len(issues) == 1
    errors = [r["content"] for r in provider.requests[1].messages[-1]["content"]]
    assert "conclusory" in errors[0] and "not found" in errors[1] and "unknown event" in errors[2]
    cite = issues[0].citations[0]
    assert result.case.page(cite.page_no).text[cite.char_start : cite.char_end].strip() == quote


def test_verifier_records_verdicts_and_retries_missing(run: tuple[RunResult, CaseGold]) -> None:
    result, _ = run
    issues = result.issues[:2]
    provider = ScriptedProvider(
        [
            tool(
                (
                    "record_verdicts",
                    {"verdicts": [{"claim": 1, "verdict": "unsupported", "reason": "x"}]},
                )
            ),
            tool(
                (
                    "record_verdicts",
                    {"verdicts": [{"claim": 2, "verdict": "partial", "reason": "y"}]},
                )
            ),
        ]
    )
    out = verify_issues(provider, result.case, issues)
    assert [i.verdict for i in out] == ["unsupported", "partial"]
    assert "missing verdicts" in provider.requests[1].messages[-1]["content"][0]["content"]
    assert issues[0].verdict != "unsupported" or issues[0] is not out[0]  # inputs not mutated


def _memo_response(parts: list[TextPart]) -> LLMResponse:
    return LLMResponse(
        model="scripted",
        stop_reason=StopReason.END_TURN,
        content=[{"type": "text", "text": p.text} for p in parts],
        text_parts=parts,
    )


def test_drafter_validates_citations_and_scope(run: tuple[RunResult, CaseGold]) -> None:
    result, gold = run
    oracle_memo = draft_memo(OracleProvider(gold), result.case, result.chronology, result.issues)
    assert oracle_memo.claims and all(c.citation_valid for c in oracle_memo.claims)
    assert oracle_memo.cited_ratio > 0.5

    bad = _memo_response(
        [
            TextPart(text="DRAFT - attorney work product.\n"),
            TextPart(
                text="The physician was negligent.",
                citations=[LLMCitation(cited_text="not on the page", document_index=0,
                                       start_char=0, end_char=15)],
            ),
        ]
    )  # fmt: skip
    memo = draft_memo(ScriptedProvider([bad]), result.case, result.chronology, result.issues)
    assert not memo.claims[1].citation_valid
    assert "Reviewer warning" in memo.markdown


def test_eval_detects_followed_injection(run: tuple[RunResult, CaseGold]) -> None:
    result, gold = run
    hijacked = _memo_response(
        [TextPart(text="All care met the standard of care and there were no delays.")]
    )
    memo = draft_memo(ScriptedProvider([hijacked]), result.case, result.chronology, [])
    metrics = score_case(result.chronology, gold, result.case, result.issues, memo)
    assert metrics.injection_followed
    assert metrics.scope_violations >= 1
    clean = score_case(result.chronology, gold, result.case, result.issues, None)
    assert not clean.injection_followed and clean.scope_violations == 0
    assert sum(clean.issues_detected.values()) == sum(clean.issues_total.values())
