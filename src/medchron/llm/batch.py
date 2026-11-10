"""Message Batches for extraction: half-price, asynchronous model calls.

Extraction is the bulk of a case's model calls and nobody waits on any single
one, so it can go through the Message Batches API, which processes requests
asynchronously at half the price. A ``BatchClient`` submits many requests at
once, reports when the batch has ended, and returns one response or error per
request (results can arrive in any order, so they are keyed by ``custom_id``).

- ``AnthropicBatchClient`` uses ``client.messages.batches``. Batch requests
  cannot carry server-side ``fallbacks``, so a request that is refused or
  errors in the batch is retried through the normal synchronous path, which
  has them.
- ``SimulatedBatchClient`` answers each request with an ordinary provider,
  such as replay fixtures or the oracle, so CI exercises the batch code path
  with no API key.

The orchestrator (``orchestrator/pipeline.py``) runs extraction as rounds: one
batch per tool-loop turn, with exactly the requests the synchronous loop
would send.
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Callable
from typing import Any, Protocol

from medchron.llm.anthropic_provider import build_params, parse_message
from medchron.llm.providers import LLMProvider
from medchron.llm.types import (
    LLMError,
    LLMPermanentError,
    LLMRequest,
    LLMResponse,
    LLMTransientError,
)

log = logging.getLogger(__name__)

CUSTOM_ID_MAX = 64


def custom_id(*parts: str) -> str:
    """A batch custom_id (letters, digits, '_' and '-', at most 64 characters)."""
    return "x" + hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[: CUSTOM_ID_MAX - 1]


class BatchClient(Protocol):
    name: str

    def submit(self, items: list[tuple[str, LLMRequest]]) -> str:
        """Submit ``(custom_id, request)`` pairs; returns the batch ID."""
        ...

    def ended(self, batch_id: str) -> bool: ...

    def results(self, batch_id: str) -> dict[str, LLMResponse | LLMError]: ...


PERMANENT_ERRORS = {
    "invalid_request_error",
    "authentication_error",
    "permission_error",
    "not_found_error",
    "billing_error",
}


class AnthropicBatchClient:
    name = "anthropic-batch"

    def __init__(self, client: Any | None = None) -> None:
        if client is None:
            import anthropic

            client = anthropic.Anthropic()
        self._client: Any = client

    def submit(self, items: list[tuple[str, LLMRequest]]) -> str:
        requests = [{"custom_id": cid, "params": build_params(req)} for cid, req in items]
        batch = self._client.messages.batches.create(requests=requests)
        log.info("batch submitted", extra={"batch_id": batch.id, "requests": len(items)})
        return str(batch.id)

    def ended(self, batch_id: str) -> bool:
        return bool(self._client.messages.batches.retrieve(batch_id).processing_status == "ended")

    def results(self, batch_id: str) -> dict[str, LLMResponse | LLMError]:
        out: dict[str, LLMResponse | LLMError] = {}
        for entry in self._client.messages.batches.results(batch_id):
            result: Any = entry.result
            cid = str(entry.custom_id)
            if result.type == "succeeded":
                out[cid] = parse_message(result.message)
                continue
            error = getattr(getattr(result, "error", None), "error", None)
            kind = getattr(error, "type", result.type)
            if kind in PERMANENT_ERRORS:
                out[cid] = LLMPermanentError(f"batch request failed ({kind}): {cid}")
            else:  # server error, canceled or expired: safe to retry
                out[cid] = LLMTransientError(f"batch request {kind}: {cid}")
        return out


class SimulatedBatchClient:
    """A batch client backed by an ordinary provider (replay fixtures, oracle, tests).

    It keeps the batch code path honest without the network: requests are the
    same as the real client would send, and each result is whatever the
    provider answers for that request.
    """

    name = "simulated-batch"

    def __init__(self, provider: LLMProvider) -> None:
        self._provider = provider
        self._batches: dict[str, list[tuple[str, LLMRequest]]] = {}
        self.submitted = 0

    def submit(self, items: list[tuple[str, LLMRequest]]) -> str:
        batch_id = "sim_" + custom_id(*(cid for cid, _ in items))[1:25]
        self._batches[batch_id] = list(items)
        self.submitted += 1
        return batch_id

    def ended(self, batch_id: str) -> bool:
        return True

    def results(self, batch_id: str) -> dict[str, LLMResponse | LLMError]:
        out: dict[str, LLMResponse | LLMError] = {}
        for cid, req in self._batches.get(batch_id, []):
            try:
                out[cid] = self._provider.complete(req)
            except LLMError as e:
                out[cid] = e
            except LookupError as e:  # replay miss: no fixture recorded for this request
                out[cid] = LLMPermanentError(str(e))
        return out


def wait_for(
    client: BatchClient,
    batch_id: str,
    *,
    poll_seconds: float = 30.0,
    timeout_seconds: float = 24 * 3600,
    progress: Callable[[str], None] | None = None,
    progress_every: float = 300.0,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Poll until the batch has ended. Most batches end within an hour; the limit is 24h."""
    start = last = time.monotonic()
    while not client.ended(batch_id):
        now = time.monotonic()
        if now - start > timeout_seconds:
            raise LLMTransientError(f"batch {batch_id} did not finish in time")
        if progress and now - last >= progress_every:
            progress(f"extract: still waiting for batch {batch_id}")
            last = now
        sleep(poll_seconds)


def default_batch_client(provider: LLMProvider) -> BatchClient:
    """The real Batches API for the live provider; a simulation for anything else."""
    if provider.name == "anthropic":
        return AnthropicBatchClient()
    return SimulatedBatchClient(provider)
