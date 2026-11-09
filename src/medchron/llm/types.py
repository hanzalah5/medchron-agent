"""Provider-neutral request and response types for LLM calls.

Messages use the Anthropic Messages wire shape as the common format. Responses
keep the raw content blocks so an agent loop can append them back unchanged
(thinking blocks must round-trip as-is), next to parsed views for text, tool
calls and citations. Every type serializes to JSON, so calls can be recorded
and replayed offline.
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class StopReason(StrEnum):
    END_TURN = "end_turn"
    TOOL_USE = "tool_use"
    MAX_TOKENS = "max_tokens"
    STOP_SEQUENCE = "stop_sequence"
    PAUSE_TURN = "pause_turn"
    REFUSAL = "refusal"
    OTHER = "other"


class ToolSpec(BaseModel):
    name: str
    description: str
    input_schema: dict[str, Any]
    strict: bool = True


class LLMRequest(BaseModel):
    route: str = Field(
        description="Logical step, e.g. 'classify' or 'extract'. Selects model and effort."
    )
    system: str = Field(
        description="Stable system prompt. It is cached, so keep volatile data out."
    )
    messages: list[dict[str, Any]]
    tools: list[ToolSpec] = Field(default_factory=list)
    max_tokens: int = 16000
    model: str | None = None
    effort: str | None = None
    prompt_version: str = ""

    def fingerprint(self) -> str:
        """Stable hash of everything that affects the output (used for replay and caching)."""
        payload = self.model_dump(mode="json")
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()


class ToolCall(BaseModel):
    id: str
    name: str
    input: dict[str, Any]


class Citation(BaseModel):
    cited_text: str
    document_index: int
    document_title: str | None = None
    start_page: int | None = None
    end_page: int | None = None


class TextPart(BaseModel):
    text: str
    citations: list[Citation] = Field(default_factory=list)


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_input_tokens=self.cache_read_input_tokens + other.cache_read_input_tokens,
            cache_creation_input_tokens=self.cache_creation_input_tokens
            + other.cache_creation_input_tokens,
        )


class LLMResponse(BaseModel):
    model: str
    stop_reason: StopReason
    content: list[dict[str, Any]] = Field(description="Raw content blocks, for appending back")
    text_parts: list[TextPart] = Field(default_factory=list)
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    request_id: str | None = None
    refusal_category: str | None = None
    served_by_fallback: bool = False

    @property
    def text(self) -> str:
        return "".join(p.text for p in self.text_parts)


class LLMError(Exception):
    """Base class for LLM-layer failures."""


class LLMTransientError(LLMError):
    """Rate limits, overload, timeouts or network errors after SDK retries are exhausted."""


class LLMPermanentError(LLMError):
    """Bad request, authentication, permission or unknown model. Retrying won't help."""


class LLMRefusalError(LLMError):
    """The model (and any fallback) declined. Route the item to a human."""

    def __init__(self, category: str | None) -> None:
        super().__init__(f"model declined the request (category={category})")
        self.category = category


class LLMTruncatedError(LLMError):
    """Output hit max_tokens. Split the input or raise the limit."""


class BudgetExceededError(LLMError):
    """The per-case cost budget is spent, so the run stops cleanly."""
