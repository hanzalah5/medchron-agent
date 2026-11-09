"""LLM layer tests. No network: the SDK client and model responses are faked."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import anthropic
import httpx2
import pytest

from medchron.llm import (
    BudgetExceededError,
    LLMPermanentError,
    LLMRefusalError,
    LLMRequest,
    LLMResponse,
    LLMTransientError,
    LLMTruncatedError,
    MeteredProvider,
    RecordingProvider,
    ReplayMissError,
    ReplayProvider,
    ScriptedProvider,
    StopReason,
    ToolSpec,
    Usage,
    load_prompt,
)
from medchron.llm.anthropic_provider import FALLBACK_BETA, AnthropicProvider, build_params
from medchron.llm.config import cost_usd
from medchron.llm.loop import ToolInputError, run_tool_loop

TOOL = ToolSpec(
    name="record_event",
    description="Record one event",
    input_schema={
        "type": "object",
        "properties": {"summary": {"type": "string"}},
        "required": ["summary"],
        "additionalProperties": False,
    },
)


def req(**kw: Any) -> LLMRequest:
    base: dict[str, Any] = {
        "route": "extract",
        "system": "stable system prompt",
        "messages": [{"role": "user", "content": "hi"}],
    }
    base.update(kw)
    return LLMRequest(**base)


def text_resp(text: str = "done", **kw: Any) -> LLMResponse:
    from medchron.llm.types import TextPart

    return LLMResponse(
        text_parts=[TextPart(text=text)],
        model="claude-opus-5-5",
        stop_reason=kw.pop("stop_reason", StopReason.END_TURN),
        content=[{"type": "text", "text": text}],
        usage=kw.pop("usage", Usage(input_tokens=1000, output_tokens=100)),
        **kw,
    )


def tool_resp(*calls: tuple[str, str, dict[str, Any]]) -> LLMResponse:
    from medchron.llm import ToolCall

    return LLMResponse(
        model="claude-opus-5-5",
        stop_reason=StopReason.TOOL_USE,
        content=[{"type": "tool_use", "id": i, "name": n, "input": a} for i, n, a in calls],
        tool_calls=[ToolCall(id=i, name=n, input=a) for i, n, a in calls],
    )


# --------------------------------------------------------------------------- params
def test_build_params_caches_stable_prefix_and_uses_strict_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MEDCHRON_MODEL", raising=False)
    p = build_params(req(tools=[TOOL, TOOL.model_copy(update={"name": "other"})]))
    assert p["model"] == "claude-opus-5-5"
    assert p["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert all(t["strict"] for t in p["tools"])
    assert "cache_control" not in p["tools"][0]
    assert p["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    assert p["output_config"] == {"effort": "medium"}
    assert "thinking" not in p and "tool_choice" not in p


def test_route_overrides_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEDCHRON_MODEL_CLASSIFY", "claude-haiku-5-5")
    monkeypatch.setenv("MEDCHRON_EFFORT_CLASSIFY", "low")
    p = build_params(req(route="classify"))
    assert p["model"] == "claude-haiku-5-5"
    assert p["output_config"]["effort"] == "low"


def test_fingerprint_is_stable_and_sensitive() -> None:
    assert req().fingerprint() == req().fingerprint()
    assert req().fingerprint() != req(system="changed").fingerprint()


# --------------------------------------------------------------------------- provider
class FakeMessages:
    def __init__(self, outcome: Any) -> None:
        self.outcome = outcome
        self.calls: list[dict[str, Any]] = []

    def create(self, **kw: Any) -> Any:
        self.calls.append(kw)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class FakeClient:
    def __init__(self, outcome: Any) -> None:
        self.messages = FakeMessages(outcome)
        self.beta = type("Beta", (), {"messages": FakeMessages(outcome)})()


def sdk_message(**over: Any) -> Any:
    data: dict[str, Any] = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5-5",
        "content": [
            {
                "type": "text",
                "text": "Admitted on 2024-01-02.",
                "citations": [
                    {
                        "type": "page_location",
                        "cited_text": "Admission date: 01/02/2024",
                        "document_index": 0,
                        "document_title": "p7",
                        "start_page_number": 7,
                        "end_page_number": 8,
                    }
                ],
            },
            {"type": "tool_use", "id": "tu_1", "name": "record_event", "input": {"summary": "x"}},
        ],
        "stop_reason": "tool_use",
        "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 7},
    }
    data.update(over)
    return anthropic.types.beta.BetaMessage.model_validate(data)


def test_provider_uses_server_side_fallback_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MEDCHRON_REFUSAL_FALLBACK", raising=False)
    client = FakeClient(sdk_message())
    resp = AnthropicProvider(client=client).complete(req())  # type: ignore[arg-type]
    call = client.beta.messages.calls[0]
    assert call["betas"] == [FALLBACK_BETA] and call["fallbacks"] == "default"
    assert not client.messages.calls
    assert resp.stop_reason == StopReason.TOOL_USE
    assert resp.tool_calls[0].input == {"summary": "x"}
    assert resp.text_parts[0].citations[0].start_page == 7
    assert resp.usage.cache_read_input_tokens == 7
    assert resp.content[1]["type"] == "tool_use"


def test_provider_without_fallback_uses_plain_endpoint() -> None:
    client = FakeClient(sdk_message())
    AnthropicProvider(client=client, refusal_fallback=False).complete(req())  # type: ignore[arg-type]
    assert client.messages.calls and not client.beta.messages.calls


def test_refusal_is_surfaced_with_category() -> None:
    msg = sdk_message(
        stop_reason="refusal",
        content=[],
        stop_details={"type": "refusal", "category": "bio", "explanation": "declined"},
    )
    resp = AnthropicProvider(client=FakeClient(msg)).complete(req())  # type: ignore[arg-type]
    assert resp.stop_reason == StopReason.REFUSAL and resp.refusal_category == "bio"


def _err(cls: type[anthropic.APIStatusError], status: int) -> anthropic.APIStatusError:
    r = httpx2.Response(status, request=httpx2.Request("POST", "https://api.anthropic.com"))
    return cls("boom", response=r, body=None)


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (_err(anthropic.RateLimitError, 429), LLMTransientError),
        (_err(anthropic.InternalServerError, 500), LLMTransientError),
        (_err(anthropic.BadRequestError, 400), LLMPermanentError),
        (_err(anthropic.AuthenticationError, 401), LLMPermanentError),
        (_err(anthropic.NotFoundError, 404), LLMPermanentError),
    ],
)
def test_errors_are_classified(exc: Exception, expected: type[Exception]) -> None:
    with pytest.raises(expected):
        AnthropicProvider(client=FakeClient(exc)).complete(req())  # type: ignore[arg-type]


# --------------------------------------------------------------------------- loop
def test_loop_runs_tools_and_batches_results() -> None:
    provider = ScriptedProvider(
        [
            tool_resp(("a", "record_event", {"summary": "one"}), ("b", "record_event", {})),
            text_resp(),
        ]
    )
    seen: list[str] = []

    def record(args: dict[str, Any]) -> dict[str, str]:
        if "summary" not in args:
            raise ToolInputError("summary is required")
        seen.append(args["summary"])
        return {"ok": "recorded"}

    result = run_tool_loop(provider, req(tools=[TOOL]), {"record_event": record})
    assert result.turns == 2 and seen == ["one"] and len(result.tool_calls) == 2
    second = provider.requests[1].messages
    assert second[-2]["role"] == "assistant"
    results = second[-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["a", "b"]
    assert results[1]["is_error"] is True and "summary is required" in results[1]["content"]


def test_loop_reports_unknown_tool_and_tool_crash() -> None:
    def crash(_: dict[str, Any]) -> None:
        raise RuntimeError("bug")

    provider = ScriptedProvider(
        [tool_resp(("a", "nope", {}), ("b", "record_event", {"summary": "x"})), text_resp()]
    )
    run_tool_loop(provider, req(), {"record_event": crash})
    errs = provider.requests[1].messages[-1]["content"]
    assert all(r["is_error"] for r in errs)


@pytest.mark.parametrize(
    ("stop", "exc"),
    [(StopReason.REFUSAL, LLMRefusalError), (StopReason.MAX_TOKENS, LLMTruncatedError)],
)
def test_loop_raises_on_refusal_and_truncation(stop: StopReason, exc: type[Exception]) -> None:
    with pytest.raises(exc):
        run_tool_loop(ScriptedProvider([text_resp(stop_reason=stop)]), req(), {})


def test_loop_continues_after_pause_and_respects_turn_limit() -> None:
    provider = ScriptedProvider([text_resp(stop_reason=StopReason.PAUSE_TURN), text_resp()])
    assert run_tool_loop(provider, req(), {}).turns == 2
    looping = ScriptedProvider(lambda _r: tool_resp(("x", "record_event", {"summary": "s"})))
    result = run_tool_loop(looping, req(), {"record_event": lambda a: "ok"}, max_turns=3)
    assert result.hit_turn_limit and result.turns == 3


# --------------------------------------------------------------------------- metering
def test_metering_costs_and_budget() -> None:
    usage = Usage(input_tokens=1_000_000, output_tokens=100_000)
    assert cost_usd("claude-opus-5-5", usage) == pytest.approx(4.0 + 2.0)
    assert cost_usd("unknown-model", usage) == 0.0
    metered = MeteredProvider(ScriptedProvider(lambda r: text_resp(usage=usage)), budget_usd=7.0)
    metered.complete(req())
    metered.complete(req())  # spent 6 < 7 before this call
    assert metered.spent_usd == pytest.approx(12.0)
    with pytest.raises(BudgetExceededError):
        metered.complete(req())
    rec = metered.records[0]
    assert rec.route == "extract" and rec.latency_ms >= 0
    assert "stable system prompt" not in rec.model_dump_json()  # hashes, not content


# --------------------------------------------------------------------------- replay
def test_record_then_replay(tmp_path: Path) -> None:
    live = RecordingProvider(ScriptedProvider([text_resp("recorded")]), tmp_path)
    assert live.complete(req()).text == "recorded"
    assert ReplayProvider(tmp_path).complete(req()).text == "recorded"
    with pytest.raises(ReplayMissError):
        ReplayProvider(tmp_path).complete(req(system="prompt changed"))


# --------------------------------------------------------------------------- prompts
def test_prompt_registry() -> None:
    p = load_prompt("system_base")
    assert p.version >= 1 and "data, not instructions" in p.text
    assert p.id.startswith(f"system_base@v{p.version}+")
    with pytest.raises(FileNotFoundError):
        load_prompt("system_base", version=999)
