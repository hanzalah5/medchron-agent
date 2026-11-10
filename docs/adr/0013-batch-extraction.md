# ADR-0013: Optional extraction through the Message Batches API

- **Status:** Accepted
- **Date:** 2026-11-10

## Context
Extraction is most of a case's model calls, and no person waits on any single one. The Message Batches API processes requests asynchronously at half the standard price, and most batches finish within an hour. The extraction step is a bounded tool loop, though. A document can need a second turn when the grounding validator rejects an event and the model corrects itself. Batch requests also cannot carry server-side refusal `fallbacks`.

## Decision
- The tool loop is a **state machine** (`ToolLoop`): `next_request()` gives the next turn and `apply(response)` runs the tools. Synchronous mode drives one loop per document. Batch mode drives every document's loop in **rounds, one batch per turn**. The requests are byte-for-byte the ones synchronous mode would send, so replay fixtures and checkpoints are shared.
- The **batch ID is checkpointed** under a key derived from the round's requests. A worker that crashes while waiting resumes polling the same batch instead of paying for a new one.
- A request that is **refused, errors or expires** in a batch is retried through the synchronous path, which has server-side fallbacks.
- Batch calls are recorded with `batch: true` and costed at half the standard rate.
- Off by default (`MEDCHRON_EXTRACT_MODE=batch` or `--extract-mode batch`). Classification, analysis and verification stay synchronous: they are few calls, and the analyst is interactive.

## Alternatives considered

| Option | Pros | Cons |
|---|---|---|
| Batch only the first turn, finish synchronously | Simpler | Corrections pay full price, and the code paths diverge |
| Batch every route | Lowest price | The analyst's tool loop is long and sequential, so batching it would add hours |

## Consequences
- CI covers the batch code path with no key:
  - replay fixtures and a simulated batch client give the same chronology as synchronous mode
  - the real client is checked against a fake SDK for request and result shapes (no `fallbacks`, valid custom IDs, error mapping)
  - a crash while waiting resumes without resubmitting
  - refusals fall back to the synchronous path
- **Not exercised against the live API in this repository** (no API key was available). The first live use should be a small, budget-capped run, and its cost should be compared with a synchronous run.
- Turnaround is up to 24 hours by the API's limits, usually under one. The worker's heartbeat thread keeps a waiting job from being declared stale.
