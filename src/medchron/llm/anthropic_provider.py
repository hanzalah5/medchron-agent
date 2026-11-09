"""Claude implementation of the LLM provider.

Production behaviors handled here:
- **Retries**: the SDK retries 408/409/429/5xx and connection errors with
  exponential backoff (``max_retries``). After that, errors map to
  ``LLMTransientError`` (worth retrying later) or ``LLMPermanentError``.
- **Refusals**: the server-side ``fallbacks: "default"`` option re-runs a declined
  request on Anthropic's recommended fallback model. A refusal that survives
  becomes ``stop_reason=refusal``, which callers route to a human. Disable it
  with ``MEDCHRON_REFUSAL_FALLBACK=off``.
- **Prompt caching**: the system prompt and the last tool definition carry
  ``cache_control`` breakpoints, so the stable prefix (tools, then system) is
  reused across the many per-document calls in a case.
- **Strict tools**: tool inputs are guaranteed to validate against the schema.
- **Thinking**: on the default model thinking is always on, and depth is
  controlled per route with ``output_config.effort``.
"""

from __future__ import annotations

import os
from typing import Any

import anthropic

from medchron.llm.config import effort_for, model_for
from medchron.llm.types import (
    Citation,
    LLMPermanentError,
    LLMRequest,
    LLMResponse,
    LLMTransientError,
    StopReason,
    TextPart,
    ToolCall,
    Usage,
)

FALLBACK_BETA = "server-side-fallback-2026-07-01"
FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}

_TRANSIENT = (
    anthropic.RateLimitError,
    anthropic.InternalServerError,
    anthropic.APIConnectionError,  # includes APITimeoutError
)
_PERMANENT = (
    anthropic.BadRequestError,
    anthropic.AuthenticationError,
    anthropic.PermissionDeniedError,
    anthropic.NotFoundError,
    anthropic.UnprocessableEntityError,
)


def build_params(req: LLMRequest) -> dict[str, Any]:
    """Translate an ``LLMRequest`` into Messages API keyword arguments."""
    model = req.model or model_for(req.route)
    params: dict[str, Any] = {
        "model": model,
        "max_tokens": req.max_tokens,
        "system": [{"type": "text", "text": req.system, "cache_control": {"type": "ephemeral"}}],
        "messages": req.messages,
        "output_config": {"effort": req.effort or effort_for(req.route)},
    }
    if req.tools:
        tools: list[dict[str, Any]] = [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.input_schema,
                "strict": t.strict,
            }
            for t in req.tools
        ]
        tools[-1]["cache_control"] = {"type": "ephemeral"}
        params["tools"] = tools
    return params


def parse_message(message: Any) -> LLMResponse:
    """Normalize an SDK ``Message`` / ``BetaMessage`` into an ``LLMResponse``."""
    text_parts: list[TextPart] = []
    tool_calls: list[ToolCall] = []
    content: list[dict[str, Any]] = []
    fallback = False
    for block in message.content:
        content.append(block.model_dump(mode="json", exclude_none=True, by_alias=True))
        if block.type == "text":
            cites = [
                Citation(
                    cited_text=c.cited_text,
                    document_index=c.document_index,
                    document_title=getattr(c, "document_title", None),
                    start_page=getattr(c, "start_page_number", None),
                    end_page=getattr(c, "end_page_number", None),
                    start_char=getattr(c, "start_char_index", None),
                    end_char=getattr(c, "end_char_index", None),
                )
                for c in (block.citations or [])
            ]
            text_parts.append(TextPart(text=block.text, citations=cites))
        elif block.type == "tool_use":
            tool_calls.append(ToolCall(id=block.id, name=block.name, input=dict(block.input)))
        elif block.type == "fallback":
            fallback = True
    try:
        stop = StopReason(message.stop_reason)
    except ValueError:
        stop = StopReason.OTHER
    details = getattr(message, "stop_details", None)
    u = message.usage
    return LLMResponse(
        model=message.model,
        stop_reason=stop,
        content=content,
        text_parts=text_parts,
        tool_calls=tool_calls,
        usage=Usage(
            input_tokens=u.input_tokens or 0,
            output_tokens=u.output_tokens or 0,
            cache_read_input_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
            cache_creation_input_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
        ),
        request_id=getattr(message, "_request_id", None),
        refusal_category=getattr(details, "category", None) if stop == StopReason.REFUSAL else None,
        served_by_fallback=fallback,
    )


class AnthropicProvider:
    name = "anthropic"

    def __init__(
        self,
        client: anthropic.Anthropic | None = None,
        *,
        max_retries: int = 4,
        timeout_s: float = 300.0,
        refusal_fallback: bool | None = None,
    ) -> None:
        self._client = client or anthropic.Anthropic(max_retries=max_retries, timeout=timeout_s)
        if refusal_fallback is None:
            refusal_fallback = os.environ.get("MEDCHRON_REFUSAL_FALLBACK", "default") != "off"
        self._fallback = refusal_fallback

    def complete(self, req: LLMRequest) -> LLMResponse:
        params = build_params(req)
        try:
            if self._fallback and params["model"] in FALLBACK_MODELS:
                message = self._client.beta.messages.create(
                    betas=[FALLBACK_BETA], fallbacks="default", **params
                )
            else:
                message = self._client.messages.create(**params)
        except _TRANSIENT as e:
            raise LLMTransientError(str(e)) from e
        except _PERMANENT as e:
            raise LLMPermanentError(str(e)) from e
        except anthropic.APIStatusError as e:
            if e.status_code >= 500 or e.status_code == 429:
                raise LLMTransientError(str(e)) from e
            raise LLMPermanentError(str(e)) from e
        return parse_message(message)
