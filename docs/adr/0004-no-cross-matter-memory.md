# ADR-0004: No cross-matter memory

- **Status:** Accepted
- **Date:** 2026-11-09

## Context
Agent "memory" that learns across cases would let facts from one client's matter influence another's. That conflicts with confidentiality and ethical walls.

## Decision
Agents work only from the current matter's structured state (pages, spans, chronology, issues). Firm knowledge (taxonomy, style, rules) lives in versioned prompt files and code, reviewed like code. Reviewer feedback is stored per matter and used for evaluation, not to train models on client data.

## Consequences
Every case starts from the same baseline, which makes evaluations comparable. Improvements ship as reviewed prompt or code changes with evaluation deltas, not as silent drift.
