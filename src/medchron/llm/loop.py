"""Bounded tool-use loop shared by the agents.

The orchestrator owns control flow. This loop runs one agent step with tools,
until the model stops calling tools or a turn limit is reached. Every stop
reason is handled explicitly:

- ``end_turn``: done
- ``tool_use``: run every requested tool and return all results in one user
  message. Tool failures go back as ``is_error`` results, so the model can
  recover.
- ``pause_turn``: append the turn and continue
- ``max_tokens``: raise ``LLMTruncatedError``. The caller splits the input.
- ``refusal``: raise ``LLMRefusalError``. The item goes to human review.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from medchron.llm.providers import LLMProvider
from medchron.llm.types import (
    LLMRefusalError,
    LLMRequest,
    LLMResponse,
    LLMTruncatedError,
    StopReason,
    ToolCall,
)

log = logging.getLogger(__name__)

ToolFn = Callable[[dict[str, Any]], Any]


class ToolInputError(ValueError):
    """Raise from a tool to send a corrective error message back to the model."""


@dataclass
class LoopResult:
    final: LLMResponse
    turns: int
    tool_calls: list[ToolCall] = field(default_factory=list)
    hit_turn_limit: bool = False


def _result_content(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, default=str)


def run_tool_loop(
    provider: LLMProvider,
    request: LLMRequest,
    tools: dict[str, ToolFn],
    *,
    max_turns: int = 8,
    done: Callable[[], bool] | None = None,
) -> LoopResult:
    """Run the loop. ``done`` lets the caller end it as soon as the tool results
    show the work is complete (all submissions accepted). That skips a final
    model turn whose only job would be to say "finished", roughly halving the
    calls for single-shot extraction steps."""
    messages = list(request.messages)
    calls: list[ToolCall] = []
    for turn in range(1, max_turns + 1):
        resp = provider.complete(request.model_copy(update={"messages": list(messages)}))
        if resp.stop_reason == StopReason.REFUSAL:
            raise LLMRefusalError(resp.refusal_category)
        if resp.stop_reason == StopReason.MAX_TOKENS:
            raise LLMTruncatedError(f"route {request.route} hit max_tokens on turn {turn}")
        messages.append({"role": "assistant", "content": resp.content})
        if resp.stop_reason == StopReason.PAUSE_TURN:
            continue
        if resp.stop_reason != StopReason.TOOL_USE or not resp.tool_calls:
            return LoopResult(final=resp, turns=turn, tool_calls=calls)

        results: list[dict[str, Any]] = []
        for call in resp.tool_calls:
            calls.append(call)
            fn = tools.get(call.name)
            try:
                if fn is None:
                    raise ToolInputError(f"unknown tool {call.name!r}")
                content, is_error = _result_content(fn(call.input)), False
            except ToolInputError as e:
                content, is_error = f"Error: {e}", True
            except Exception as e:  # tool bug: report it, keep the loop alive
                log.exception("tool %s failed", call.name)
                content, is_error = f"Error: tool failed ({type(e).__name__})", True
            result: dict[str, Any] = {
                "type": "tool_result",
                "tool_use_id": call.id,
                "content": content,
            }
            if is_error:
                result["is_error"] = True
            results.append(result)
        messages.append({"role": "user", "content": results})
        if done is not None and not any(r.get("is_error") for r in results) and done():
            return LoopResult(final=resp, turns=turn, tool_calls=calls)
    return LoopResult(final=resp, turns=max_turns, tool_calls=calls, hit_turn_limit=True)
