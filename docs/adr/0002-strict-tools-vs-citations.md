# ADR-0002: Strict tool schemas for extraction, the Citations API for drafting

- **Status:** Accepted
- **Date:** 2026-11-09

## Context
Extraction needs machine-checkable structure: types, dates and span IDs. The memo needs fluent prose in which every sentence is traceable to a source. In the Claude API, document citations can't be combined with structured-output formatting in the same request.

## Decision
- **Extraction, analysis and verification** use `strict: true` tool schemas. Our tool functions validate grounding as they record and return rejections, so the model corrects itself.
- **Drafting** sends each relevant page as a plain-text document with citations enabled. The returned character ranges are verified against the stored page text and map directly onto word boxes for highlighting.

## Alternatives considered
| Option | Cons |
|---|---|
| Structured JSON memo with a citation field per sentence | Self-reported citations aren't the model's native grounding, and they read worse |
| Citations with PDF documents (page-level locations) | Page-level only, so there are no character offsets to highlight |

## Consequences
Two output styles to maintain. In exchange, every claim in every artifact carries a verifiable, clickable pointer to the record.
