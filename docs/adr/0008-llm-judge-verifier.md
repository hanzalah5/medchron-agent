# ADR-0008: An independent LLM verifier, calibrated before it is trusted

- **Status:** Accepted (calibration pending)
- **Date:** 2026-11-09

## Context
Grounding proves a quote *exists*. It doesn't prove the quote *supports* the claim built on it.

## Decision
A verifier sees each claim with only its cited evidence and returns supported, partial or unsupported, with a reason. Unsupported issues are shown to the reviewer, not dropped. Memo approval is blocked while any citation fails deterministic validation.

## Consequences
The verifier is itself a model, so its score can't be taken at face value. Before relying on it: hand-label about 100 claims from live runs and report judge–human agreement in the evaluation report. Until then it is a triage aid, not a gate.
