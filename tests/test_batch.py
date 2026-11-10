"""Batch extraction mode (Message Batches API), tested without the network."""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from medchron.evals.oracle import OracleProvider
from medchron.llm.batch import AnthropicBatchClient, SimulatedBatchClient, custom_id
from medchron.llm.providers import MeteredProvider, ReplayProvider
from medchron.llm.types import (
    LLMError,
    LLMPermanentError,
    LLMRequest,
    LLMResponse,
    LLMTransientError,
    StopReason,
    Usage,
)
from medchron.orchestrator.pipeline import RunConfig, run_case
from medchron.synth import CaseGold

ROOT = Path(__file__).resolve().parents[1]
DEV = ROOT / "data" / "synthetic" / "dev"
FIXTURES = ROOT / "tests" / "fixtures" / "llm"
CASE = "case_0005"  # the case the committed replay fixtures were recorded for


def gold() -> CaseGold:
    return CaseGold.model_validate_json((DEV / f"{CASE}.gold.json").read_text())


def test_replay_fixtures_give_identical_chronologies_in_sync_and_batch(tmp_path: Path) -> None:
    sync = run_case(DEV / f"{CASE}.pdf", CASE, ReplayProvider(FIXTURES),
                    RunConfig(out_dir=tmp_path / "sync", extract_mode="sync"))  # fmt: skip
    batch = run_case(DEV / f"{CASE}.pdf", CASE, ReplayProvider(FIXTURES),
                     RunConfig(out_dir=tmp_path / "batch", extract_mode="batch"))  # fmt: skip
    assert sync.summary.status == batch.summary.status == "completed"
    assert sync.chronology == batch.chronology
    extract_calls = [c for c in batch.calls if c.route == "extract"]
    assert extract_calls and all(c.batch for c in extract_calls)
    assert not any(c.batch for c in sync.calls)


class _Server:
    """Stands in for the Batches API across worker restarts."""

    def __init__(self, provider: OracleProvider) -> None:
        self.provider = provider
        self.batches: dict[str, list[tuple[str, LLMRequest]]] = {}


class _Client(SimulatedBatchClient):
    name = "fake-batch"

    def __init__(self, server: _Server, crash_on_results: bool = False) -> None:
        super().__init__(server.provider)
        self.server, self.crash = server, crash_on_results
        self._batches = server.batches

    def results(self, batch_id: str) -> dict[str, LLMResponse | LLMError]:
        if self.crash:
            raise RuntimeError("worker killed while waiting for the batch")
        return super().results(batch_id)


def test_a_crash_while_waiting_resumes_the_same_batch(tmp_path: Path) -> None:
    server = _Server(OracleProvider(gold()))
    first = _Client(server, crash_on_results=True)
    with pytest.raises(RuntimeError):
        run_case(DEV / f"{CASE}.pdf", CASE, server.provider,
                 RunConfig(out_dir=tmp_path, extract_mode="batch", batch_client=first))  # fmt: skip
    assert first.submitted == 1 and len(server.batches) == 1
    messages: list[str] = []
    second = _Client(server)
    result = run_case(DEV / f"{CASE}.pdf", CASE, server.provider,
                      RunConfig(out_dir=tmp_path, extract_mode="batch", batch_client=second),
                      progress=messages.append)  # fmt: skip
    assert second.submitted == 0  # nothing new was paid for
    assert any("resuming batch" in m for m in messages)
    assert result.summary.status == "completed" and result.summary.events > 5


def test_refused_batch_requests_fall_back_to_the_synchronous_path(tmp_path: Path) -> None:
    oracle = OracleProvider(gold())

    class Refusing(SimulatedBatchClient):
        def results(self, batch_id: str) -> dict[str, LLMResponse | LLMError]:
            out = super().results(batch_id)
            first = sorted(out)[0]
            out[first] = LLMResponse(model="claude-opus-5-5", stop_reason=StopReason.REFUSAL,
                                     content=[], refusal_category="other")  # fmt: skip
            return out

    result = run_case(DEV / f"{CASE}.pdf", CASE, oracle,
                      RunConfig(out_dir=tmp_path, extract_mode="batch",
                                batch_client=Refusing(oracle)))  # fmt: skip
    extract = [c for c in result.calls if c.route == "extract"]
    assert any(not c.batch for c in extract) and any(c.batch for c in extract)
    assert result.summary.status == "completed" and not result.summary.failed_documents


def test_batch_usage_is_billed_at_half_price() -> None:
    meter = MeteredProvider(OracleProvider(gold()))
    req = LLMRequest(route="extract", system="s", messages=[{"role": "user", "content": "x"}])
    resp = LLMResponse(model="claude-opus-5-5", stop_reason=StopReason.END_TURN, content=[],
                       usage=Usage(input_tokens=1_000_000, output_tokens=100_000))  # fmt: skip
    full = meter.record(req, resp).cost_usd
    half = meter.record(req, resp, batch=True).cost_usd
    assert full > 0 and half == pytest.approx(full / 2)


def test_custom_ids_fit_the_api_limits() -> None:
    cid = custom_id("a-very-long-matter-id" * 5, "doc-0001", "3")
    assert re.fullmatch(r"[A-Za-z0-9_-]{1,64}", cid)
    assert cid != custom_id("a-very-long-matter-id" * 5, "doc-0001", "4")


# ------------------------------------------------------------- the real client, fake SDK
def _message(tool_input: dict[str, Any]) -> Any:
    from anthropic.types import Message

    return Message.model_validate(
        {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": "claude-opus-5-5",
            "content": [
                {"type": "tool_use", "id": "toolu_1", "name": "record_events", "input": tool_input}
            ],
            "stop_reason": "tool_use",
            "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
    )


class _FakeBatches:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []

    def create(self, *, requests: list[dict[str, Any]]) -> Any:
        self.created = requests
        return SimpleNamespace(id="msgbatch_1")

    def retrieve(self, batch_id: str) -> Any:
        return SimpleNamespace(processing_status="ended")

    def results(self, batch_id: str) -> list[Any]:
        ids = [r["custom_id"] for r in self.created]
        invalid = SimpleNamespace(error=SimpleNamespace(type="invalid_request_error"))
        ok = SimpleNamespace(type="succeeded", message=_message({"events": [], "flags": []}))
        return [
            SimpleNamespace(custom_id=ids[0], result=ok),
            SimpleNamespace(
                custom_id=ids[1], result=SimpleNamespace(type="errored", error=invalid)
            ),
            SimpleNamespace(custom_id=ids[2], result=SimpleNamespace(type="expired")),
        ]


def test_anthropic_batch_client_request_and_result_shapes() -> None:
    fake = _FakeBatches()
    client = AnthropicBatchClient(SimpleNamespace(messages=SimpleNamespace(batches=fake)))
    req = LLMRequest(route="extract", system="sys", messages=[{"role": "user", "content": "doc"}])
    items = [(custom_id("m", str(i)), req) for i in range(3)]
    batch_id = client.submit(items)
    assert batch_id == "msgbatch_1" and client.ended(batch_id)
    sent = fake.created[0]
    assert set(sent) == {"custom_id", "params"}
    params = sent["params"]
    assert params["model"] and params["max_tokens"] and params["messages"]
    assert "fallbacks" not in params and "stream" not in params  # not allowed in batches
    assert params["output_config"]["effort"]
    json.dumps(fake.created)  # the payload is plain JSON
    out = client.results(batch_id)
    ok, invalid, expired = (out[cid] for cid, _ in items)
    assert isinstance(ok, LLMResponse) and ok.tool_calls[0].name == "record_events"
    assert isinstance(invalid, LLMPermanentError)
    assert isinstance(expired, LLMTransientError)
