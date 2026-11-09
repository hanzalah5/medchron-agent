# ADR-0007: An oracle test double and record/replay, with no fabricated model numbers

- **Status:** Accepted
- **Date:** 2026-11-09

## Context
The pipeline has to be testable end to end in CI and demo-able without spending money on every run. It is also tempting to publish impressive-looking numbers that no model produced.

## Decision
- An **oracle** provider answers each pipeline route from the gold labels, through the same tool calls a model would make. It tests plumbing, grounding and metrics, sets a ceiling, and, with injected noise, shows that the metrics respond.
- **Record/replay** providers capture real model calls as fixtures for offline reruns.
- Results files carry `is_model_result`. The report keeps oracle self-checks separate and labeled, and the live scorecard stays "pending" until a real run exists.

## Consequences
CI covers the whole system for free, and the public demo costs nothing per visitor. Live evaluation is a deliberate, budgeted step (`make eval-live`).
