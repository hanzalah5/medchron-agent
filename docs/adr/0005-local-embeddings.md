# ADR-0005: Local embedding model

- **Status:** Accepted
- **Date:** 2026-11-09

## Context
Embedding every span through a hosted API sends all record text to another vendor, which is another BAA and another data flow, mostly for retrieval quality that a small local model already covers on clinical notes.

## Decision
Embeddings are computed locally (`bge-small`, 384 dimensions, optional extra). A deterministic hashing embedder of the same dimension serves tests, CI and the demo, so nothing has to be downloaded there. Hybrid lexical-plus-vector fusion offsets the smaller model.

## Consequences
Less PHI egress and no per-token embedding cost. The model is swappable behind the `Embedder` protocol if evaluations show retrieval is the bottleneck.
