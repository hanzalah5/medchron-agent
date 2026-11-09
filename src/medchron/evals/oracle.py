"""Gold-label oracle: a test double that answers like an ideal model.

The oracle reads the gold labels of a synthetic case and answers the
pipeline's ``classify`` and ``extract`` requests exactly as a perfect model
would, through the same tool calls. Use it to:

- test the orchestrator, grounding validators and exporters end to end without
  an API key;
- sanity-check the evaluation harness (oracle scores near 1.0, set by the
  limits of OCR and the merge rules, show the metric code works);
- inject controlled noise (``noise > 0``) and confirm the validators and
  metrics catch it.

**Oracle runs are not model results and are never reported as such.**
"""

from __future__ import annotations

import datetime as dt
import itertools
import random
import re
from typing import Any

from medchron.grounding.quotes import find_quote, normalize
from medchron.llm.types import LLMRequest, LLMResponse, StopReason, TextPart, ToolCall, Usage
from medchron.synth.models import CaseGold, GoldDocument
from medchron.taxonomy import IssueKind

_PAGE = re.compile(r'<page number="(\d+)">')
_SPAN = re.compile(r'<span id="([^"]+)">\n(.*?)\n</span>', re.S)


class OracleProvider:
    name = "oracle"

    def __init__(self, gold: CaseGold, *, noise: float = 0.0, seed: int = 0) -> None:
        self.gold = gold
        self.noise = noise
        self._rng = random.Random(seed)
        self._ids = itertools.count(1)
        self._page_doc: dict[int, GoldDocument] = {}
        for d in gold.documents:
            for p in range(d.start_page, d.end_page + 1):
                self._page_doc[p] = d
        self._injection_pages = {
            p for i in gold.issues if i.kind == IssueKind.PROMPT_INJECTION for p in i.pages
        }

    # ------------------------------------------------------------------ protocol
    def complete(self, req: LLMRequest) -> LLMResponse:
        last = req.messages[-1]
        if last["role"] == "user" and isinstance(last["content"], list):
            return self._text("Recorded.")  # tool results came back: finish the turn
        content = str(last["content"])
        if req.route == "classify":
            return self._tool("label_pages", {"pages": self._labels(content)})
        if req.route == "extract":
            events, flags = self._events(content)
            return self._tool("record_events", {"events": events, "flags": flags})
        raise ValueError(f"oracle has no answer for route {req.route!r}")

    # ------------------------------------------------------------------ answers
    def _labels(self, content: str) -> list[dict[str, Any]]:
        out = []
        for page in map(int, _PAGE.findall(content)):
            d = self._page_doc[page]
            out.append(
                {
                    "page_number": page,
                    "doc_type": d.doc_type.value,
                    "title": d.title,
                    "date_iso": d.date.isoformat(),
                    "facility": d.facility,
                    "provider": d.provider,
                    "starts_new_document": page == d.start_page,
                }
            )
        return out

    def _events(self, content: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        spans = _SPAN.findall(content)
        events: list[dict[str, Any]] = []
        for ev in self.gold.events:
            for src in ev.sources:
                hit = self._locate(spans, src.page, src.quote)
                if hit is None:
                    continue
                span_id, quote = hit
                if self.noise and self._rng.random() < self.noise:
                    continue  # simulated miss
                date: dt.date = ev.date
                if self.noise and self._rng.random() < self.noise:
                    date = date + dt.timedelta(days=self._rng.choice([-1, 1]))
                if self.noise and self._rng.random() < self.noise:
                    quote = quote + " (paraphrased)"  # should be rejected by grounding
                events.append(
                    {
                        "date_text": date.isoformat(),
                        "date_iso": date.isoformat(),
                        "type": ev.type.value,
                        "summary": ev.summary,
                        "source_span_id": span_id,
                        "quote": quote,
                        "confidence": "high",
                    }
                )
        flags = [
            {
                "kind": "possible_prompt_injection",
                "source_span_id": span_id,
                "note": "Text addresses automated systems with instructions; not followed.",
            }
            for span_id, text in spans
            if self._page_of(span_id) in self._injection_pages
            and "automated review systems" in normalize(text)
        ]
        return events, flags

    @staticmethod
    def _page_of(span_id: str) -> int:
        return int(span_id.split(":p")[1].split(":")[0])

    def _locate(
        self, spans: list[tuple[str, str]], page: int, quote: str
    ) -> tuple[str, str] | None:
        for span_id, text in spans:
            if self._page_of(span_id) != page:
                continue
            m = find_quote(text, quote, threshold=0.8)
            if m is not None:
                return span_id, text[m.start : m.end]  # copy what the page actually says
        return None

    # ------------------------------------------------------------------ responses
    def _tool(self, name: str, args: dict[str, Any]) -> LLMResponse:
        call_id = f"toolu_oracle_{next(self._ids)}"
        return LLMResponse(
            model="oracle",
            stop_reason=StopReason.TOOL_USE,
            content=[{"type": "tool_use", "id": call_id, "name": name, "input": args}],
            tool_calls=[ToolCall(id=call_id, name=name, input=args)],
            usage=Usage(),
        )

    @staticmethod
    def _text(text: str) -> LLMResponse:
        return LLMResponse(
            model="oracle",
            stop_reason=StopReason.END_TURN,
            content=[{"type": "text", "text": text}],
            text_parts=[TextPart(text=text)],
            usage=Usage(),
        )
