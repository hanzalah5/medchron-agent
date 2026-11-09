# ADR-0006: Deterministic dates, grounding and review rules live in code

- **Status:** Accepted
- **Date:** 2026-11-09

## Context
LLMs are good at reading messy text and unreliable at exact bookkeeping, such as normalizing dates, checking that a quote really exists, or comparing intervals.

## Decision
Code owns:
- date parsing across formats
- quote location, tolerant of OCR errors but rejecting fabricated runs of text
- cross-document merging
- conflict and follow-up gap rules
- citation-offset validation
- the conclusory-language scope guard

Models propose; code verifies. The analyst gets the deterministic rules as a tool (`find_gaps`) instead of re-deriving them.

## Consequences
These checks are unit-tested and explainable ("rejected: quote not found in span"). Two real defects were caught this way during development: the fuzzy matcher accepting appended fabricated text, and the scope guard missing "met the standard of care". Both are now regression tests.
