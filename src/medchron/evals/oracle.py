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
import json
import random
import re
from typing import Any

from medchron.grounding.quotes import find_quote, normalize
from medchron.llm.types import (
    Citation,
    LLMRequest,
    LLMResponse,
    StopReason,
    TextPart,
    ToolCall,
    Usage,
)
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
        if req.route == "analyze":
            return self._analyze(req)
        if req.route == "draft":
            return self._draft(req)
        last = req.messages[-1]
        if last["role"] == "user" and isinstance(last["content"], list):
            return self._text("Recorded.")  # tool results came back: finish the turn
        content = str(last["content"])
        if req.route == "classify":
            return self._tool("label_pages", {"pages": self._labels(content)})
        if req.route == "extract":
            events, flags = self._events(content)
            return self._tool("record_events", {"events": events, "flags": flags})
        if req.route == "verify":
            n = len(re.findall(r"^Claim \d+:", content, re.M))
            verdicts = [
                {"claim": k, "verdict": "supported", "reason": "Evidence states the facts."}
                for k in range(1, n + 1)
            ]
            return self._tool("record_verdicts", {"verdicts": verdicts})
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

    # ------------------------------------------------------------------ analyst
    def _analyze(self, req: LLMRequest) -> LLMResponse:
        """Turn 1: read the issue pages. Turn 2: record each gold issue. Turn 3: finish."""
        last = req.messages[-1]
        if isinstance(last["content"], str):
            wanted = sorted({p for i in self.gold.issues for p in i.pages})
            return self._tools([("get_page", {"page_no": p}) for p in wanted])
        results = []
        for r in last["content"]:
            try:
                results.append(json.loads(r["content"]))
            except (json.JSONDecodeError, TypeError):
                continue  # e.g. "recorded I001" from record_issue
        pages = {r["page"]: r for r in results if isinstance(r, dict) and "spans" in r}
        if not pages:
            return self._text("Recorded all supported issues.")
        calls = []
        for issue in self.gold.issues:
            cites = [c for p in issue.pages if p in pages for c in [self._cite(pages[p], issue)]]
            cites = [c for c in cites if c is not None]
            if cites:
                calls.append(
                    (
                        "record_issue",
                        {
                            "category": issue.kind.value,
                            "description": issue.description,
                            "event_ids": [],
                            "citations": cites,
                            "severity_hint": "medium",
                        },
                    )
                )
        return self._tools(calls) if calls else self._text("No supportable issues.")

    def _cite(self, page: dict[str, Any], issue: Any) -> dict[str, str] | None:
        text: str = page["text"]
        lines = text.splitlines()
        body_start = next((i + 1 for i, ln in enumerate(lines) if set(ln.strip()) == {"-"}), 0)
        candidates = [ln.strip() for ln in lines[body_start:] if len(ln.strip()) > 20]
        if issue.kind == IssueKind.PROMPT_INJECTION:
            candidates = [c for c in candidates if "automated review" in c.lower()] or candidates
        if not candidates:
            return None
        quote = candidates[0]
        offset = text.find(quote)
        for span_id in page["spans"]:
            start, end = (int(x) for x in span_id.rsplit(":", 1)[1].split("-"))
            if start <= offset and offset + len(quote) <= end:
                return {"span_id": span_id, "quote": quote}
        return None

    # ------------------------------------------------------------------ drafter
    def _draft(self, req: LLMRequest) -> LLMResponse:
        blocks = req.messages[0]["content"]
        docs = [b for b in blocks if b.get("type") == "document"]
        parts = [
            TextPart(
                text="DRAFT - attorney work product - prepared from synthetic records "
                "for review.\n\n## Summary of medical course\n\n"
            )
        ]
        for i, doc in enumerate(docs):
            data: str = doc["source"]["data"]
            lines = data.splitlines()
            body = next((k + 1 for k, ln in enumerate(lines) if set(ln.strip()) == {"-"}), 0)
            line = next((ln.strip() for ln in lines[body:] if len(ln.strip()) > 25), None)
            if line is None or "automated review" in line.lower():
                continue
            start = data.find(line)
            parts.append(TextPart(text=f"{doc['title']} records: "))
            parts.append(
                TextPart(
                    text=line,
                    citations=[
                        Citation(
                            cited_text=line,
                            document_index=i,
                            document_title=doc["title"],
                            start_char=start,
                            end_char=start + len(line),
                        )
                    ],
                )
            )
            parts.append(TextPart(text="\n\n"))
        return LLMResponse(
            model="oracle",
            stop_reason=StopReason.END_TURN,
            content=[{"type": "text", "text": p.text} for p in parts],
            text_parts=parts,
            usage=Usage(),
        )

    # ------------------------------------------------------------------ responses
    def _tool(self, name: str, args: dict[str, Any]) -> LLMResponse:
        return self._tools([(name, args)])

    def _tools(self, calls: list[tuple[str, dict[str, Any]]]) -> LLMResponse:
        ids = [f"toolu_oracle_{next(self._ids)}" for _ in calls]
        return LLMResponse(
            model="oracle",
            stop_reason=StopReason.TOOL_USE,
            content=[
                {"type": "tool_use", "id": i, "name": n, "input": a}
                for i, (n, a) in zip(ids, calls, strict=True)
            ],
            tool_calls=[
                ToolCall(id=i, name=n, input=a) for i, (n, a) in zip(ids, calls, strict=True)
            ],
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
