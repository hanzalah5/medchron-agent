# ADR-0004: No cross-matter memory

- **Status:** Accepted
- **Date:** 2026-11-09

## Context
Agent "memory" that learns across cases would let facts from one client's matter influence another's. That conflicts with confidentiality and ethical walls.

## Decision
Agents work only from the current matter's structured state (pages, spans, chronology, issues). Firm knowledge (taxonomy, style, rules) lives in versioned prompt files and code, reviewed like code. Reviewer feedback is stored per matter and used for evaluation, not to train models on client data.

## Consequences
Every case starts from the same baseline, which makes evaluations comparable. Improvements ship as reviewed prompt or code changes with evaluation deltas, not as silent drift.

## Update (v1.1): reuse within a matter, never across
Incremental productions (ADR-0012) reuse extraction work. The reuse is scoped to one matter:
- checkpoints live under that matter's run directory
- the checkpoint key includes the matter ID as well as the document's content

Two matters whose records contain identical pages still extract them separately. The MCP server (ADR-0011) gives assistants per-call, membership-checked access to one matter at a time. It holds no memory between calls.
