"""Memo drafting with the Claude Citations feature.

Each relevant page is sent as its own plain-text document with citations
enabled, so every returned citation is a character range within one page.
That range is checked deterministically against the stored page text and
maps straight onto word boxes for click-to-source highlighting.

Only events the attorney has not rejected, and issues the attorney chose to
pursue or send to an expert, go into the memo (human checkpoint 3 comes
first). Citations cannot be combined with structured-output formatting, so
this step returns prose with native citations, while extraction uses strict
tools. See ADR 0002.
"""

from __future__ import annotations

from medchron.agents.analyst import scope_violation
from medchron.chronology.models import Chronology, Citation, Issue, Memo, MemoClaim
from medchron.grounding.quotes import normalize
from medchron.ingestion.models import IngestedCase, TextSource
from medchron.llm.prompts import load_prompt
from medchron.llm.providers import LLMProvider
from medchron.llm.types import LLMRefusalError, LLMRequest, LLMTruncatedError, StopReason

MAX_PAGES = 40


def _pages_for(chron: Chronology, issues: list[Issue]) -> list[int]:
    pages: list[int] = []
    for e in chron.events:
        pages += [s.page_no for s in e.sources]
    for i in issues:
        pages += [c.page_no for c in i.citations]
    seen: list[int] = []
    for p in pages:
        if p not in seen:
            seen.append(p)
    return sorted(seen[:MAX_PAGES])


def draft_memo(
    provider: LLMProvider, case: IngestedCase, chron: Chronology, issues: list[Issue]
) -> Memo:
    pages = [p for p in _pages_for(chron, issues) if case.page(p).source != TextSource.NONE]
    documents = [
        {
            "type": "document",
            "source": {"type": "text", "media_type": "text/plain", "data": case.page(p).text},
            "title": f"Page {p}",
            "citations": {"enabled": True},
        }
        for p in pages
    ]
    chronology = "\n".join(
        f"- {e.date or 'undated'} [{e.type.value}] {e.summary} (p{e.sources[0].page_no})"
        for e in chron.events
    )
    questions = "\n".join(f"- [{i.category}] {i.description}" for i in issues) or "- (none)"
    system = load_prompt("system_base")
    task = load_prompt("draft_memo")
    request = LLMRequest(
        route="draft",
        system=system.text + "\n\n" + task.text,
        prompt_version=f"{system.id},{task.id}",
        messages=[
            {
                "role": "user",
                "content": [
                    *documents,
                    {
                        "type": "text",
                        "text": f"Approved chronology:\n{chronology}\n\n"
                        f"Approved questions for expert review:\n{questions}\n\n"
                        "Write the memo now.",
                    },
                ],
            }
        ],
        max_tokens=8000,
    )
    resp = provider.complete(request)
    if resp.stop_reason == StopReason.REFUSAL:
        raise LLMRefusalError(resp.refusal_category)
    if resp.stop_reason == StopReason.MAX_TOKENS:
        raise LLMTruncatedError("memo exceeded max_tokens")

    claims: list[MemoClaim] = []
    markdown: list[str] = []
    for part in resp.text_parts:
        cites: list[Citation] = []
        valid = True
        for c in part.citations:
            if not 0 <= c.document_index < len(pages):
                valid = False
                continue
            page_no = pages[c.document_index]
            text = case.page(page_no).text
            start, end = c.start_char or 0, c.end_char or 0
            ok = 0 <= start < end <= len(text) and normalize(text[start:end]) == normalize(
                c.cited_text
            )
            valid &= ok
            cites.append(
                Citation(
                    span_id=f"{case.case_id}:p{page_no}:{start}-{end}",
                    page_no=page_no,
                    quote=c.cited_text,
                    char_start=start,
                    char_end=end,
                )
            )
        claims.append(MemoClaim(text=part.text, citations=cites, citation_valid=valid))
        marks = "".join(sorted({f" [p{c.page_no}]" for c in cites}))
        markdown.append(part.text + marks)
    body = "".join(markdown).strip()
    warning = scope_violation(body)
    if warning:
        body += (
            f"\n\n> **Reviewer warning:** the draft contains conclusory language "
            f"({warning!r}). Revise before use."
        )
    return Memo(case_id=case.case_id, markdown=body, claims=claims)
