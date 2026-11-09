"""Provider protocol plus offline and wrapping providers.

- ``ScriptedProvider``: test double that returns canned or computed responses.
- ``RecordingProvider`` / ``ReplayProvider``: record real calls to JSON fixtures
  and replay them offline, so CI and the public demo cost nothing.
- ``MeteredProvider``: cost accounting, per-case budget enforcement and
  PHI-safe call logs (metadata and hashes only, never prompt or response text).
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel

from medchron.llm.config import cost_usd, model_for
from medchron.llm.types import (
    BudgetExceededError,
    LLMRequest,
    LLMResponse,
    Usage,
)

log = logging.getLogger("medchron.llm")


class LLMProvider(Protocol):
    name: str

    def complete(self, req: LLMRequest) -> LLMResponse: ...


class ScriptedProvider:
    """Returns responses from a function or a fixed sequence (tests and oracle runs)."""

    name = "scripted"

    def __init__(
        self, responder: Callable[[LLMRequest], LLMResponse] | Iterable[LLMResponse]
    ) -> None:
        if callable(responder):
            self._fn = responder
        else:
            queue = list(responder)
            self._fn = lambda _req: queue.pop(0)
        self.requests: list[LLMRequest] = []

    def complete(self, req: LLMRequest) -> LLMResponse:
        self.requests.append(req)
        return self._fn(req)


class ReplayMissError(LookupError):
    """No recorded fixture exists for this request (prompt, model or input changed)."""


def _fixture_path(root: Path, req: LLMRequest) -> Path:
    return root / req.route / f"{req.fingerprint()[:32]}.json"


class RecordingProvider:
    name = "recording"

    def __init__(self, inner: LLMProvider, root: Path) -> None:
        self._inner, self._root = inner, root

    def complete(self, req: LLMRequest) -> LLMResponse:
        resp = self._inner.complete(req)
        path = _fixture_path(self._root, req)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(resp.model_dump_json(indent=1), encoding="utf-8")
        return resp


class ReplayProvider:
    name = "replay"

    def __init__(self, root: Path) -> None:
        self._root = root

    def complete(self, req: LLMRequest) -> LLMResponse:
        path = _fixture_path(self._root, req)
        if not path.exists():
            raise ReplayMissError(f"no fixture for route={req.route} at {path}")
        return LLMResponse.model_validate_json(path.read_text(encoding="utf-8"))


class CallRecord(BaseModel):
    """One LLM call's metadata. Content is stored only as hashes (PHI-safe)."""

    route: str
    model: str
    prompt_version: str
    request_sha256: str
    response_sha256: str
    stop_reason: str
    usage: Usage
    cost_usd: float
    latency_ms: int
    request_id: str | None = None
    served_by_fallback: bool = False


class MeteredProvider:
    """Wraps a provider with cost tracking, a hard budget and structured call logs.

    Thread-safe: parallel workers share one budget. The budget check happens
    before each call, so concurrent in-flight calls can overshoot it by at most
    the cost of those calls.
    """

    name = "metered"

    def __init__(self, inner: LLMProvider, budget_usd: float | None = None) -> None:
        self._inner = inner
        self.budget_usd = budget_usd
        self.records: list[CallRecord] = []
        self._lock = threading.Lock()

    @property
    def spent_usd(self) -> float:
        return sum(r.cost_usd for r in self.records)

    @property
    def usage(self) -> Usage:
        total = Usage()
        for r in self.records:
            total = total + r.usage
        return total

    def complete(self, req: LLMRequest) -> LLMResponse:
        with self._lock:
            if self.budget_usd is not None and self.spent_usd >= self.budget_usd:
                raise BudgetExceededError(
                    f"case budget ${self.budget_usd:.2f} spent (${self.spent_usd:.4f})"
                )
        start = time.perf_counter()
        resp = self._inner.complete(req)
        latency = int((time.perf_counter() - start) * 1000)
        model = resp.model or req.model or model_for(req.route)
        record = CallRecord(
            route=req.route,
            model=model,
            prompt_version=req.prompt_version,
            request_sha256=req.fingerprint(),
            response_sha256=hashlib.sha256(
                json.dumps(resp.content, sort_keys=True).encode()
            ).hexdigest(),
            stop_reason=resp.stop_reason.value,
            usage=resp.usage,
            cost_usd=cost_usd(model, resp.usage),
            latency_ms=latency,
            request_id=resp.request_id,
            served_by_fallback=resp.served_by_fallback,
        )
        with self._lock:
            self.records.append(record)
        log.info("llm_call", extra={"llm": record.model_dump(mode="json")})
        return resp
