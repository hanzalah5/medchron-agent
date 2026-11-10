# ADR-0008: An independent LLM verifier, calibrated before it is trusted

- **Status:** Accepted (calibration pending)
- **Date:** 2026-11-09

## Context
Grounding proves a quote *exists*. It doesn't prove the quote *supports* the claim built on it.

## Decision
A verifier sees each claim with only its cited evidence and returns supported, partial or unsupported, with a reason. Unsupported issues are shown to the reviewer, not dropped. Memo approval is blocked while any citation fails deterministic validation.

## Consequences
The verifier is itself a model, so its score can't be taken at face value. Before relying on it: hand-label about 100 claims from live runs and report judge–human agreement in the evaluation report. Until then it is a triage aid, not a gate.

## Update (v1.1): calibration tooling exists; human labels do not
`medchron calib` samples judged claims from live runs into a labelling file with blank labels. It runs the verifier over a file, and scores judge-label agreement: accuracy, per-class precision and recall, a confusion matrix and Cohen's kappa, reported separately for each label source. A seed set derived from the gold labels (a planted issue with its own evidence is `supported`, with unrelated evidence `unsupported`) is committed and labelled `gold-derived`. It is never presented as human judgement. The hand-labelling protocol is in `evals/calibration/README.md`. **Calibration is still pending**: no human labels exist, and the live judge has not been run.
