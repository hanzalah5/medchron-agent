# ADR-0012: Incremental productions, content-keyed checkpoints and chronology versions

- **Status:** Accepted
- **Date:** 2026-11-10

## Context
Records rarely arrive at once. A firm requests records, receives a first production, starts review, and later receives supplemental records. Treating each upload as a new matter would split the chronology. Re-running everything from scratch would:
- repeat paid model work
- discard reviewers' decisions, since the store replaced events and issues on every run
- give attorneys no view of what the new records changed

## Decision
- A matter has **productions**, numbered in arrival order (table `productions`). **Page numbers run across productions**: the second production's first page follows the first's last page. Page numbers, span IDs, Bates IDs and event IDs from the first production therefore stay valid.
- **De-duplication runs across productions**, so pages re-sent in a later production are marked as duplicates of the originals. Pages are also never treated as duplicates when their dates contradict each other: two cover sheets from the same facility differ only in their dates.
- The **extraction checkpoint key is the document's own content** plus the matter ID and model settings, instead of the hash of the whole upload. When a second production arrives, every first-production document is reused without a model call. The key includes the matter ID, so nothing is shared across matters (ADR-0004).
- Every run that changes the chronology stores a **version** (`chronology_versions`, a JSON snapshot). A **diff** lists added, removed and changed events. Events are matched by ID, then by type, date and description. A changed event usually gained a source: the same visit restated in a newly produced record and merged rather than duplicated.
- Reviewer decisions survive re-runs. Event review state is kept by event ID. Issue triage is kept by category plus cited spans.
- Chronology **export approval** is tied to the current version and to a digest of the reviewed chronology. Any later change requires approval again.

## Alternatives considered

| Option | Pros | Cons |
|---|---|---|
| A new matter per upload | Simple | Splits the chronology, and reviewers redo work |
| Re-run everything, with per-production page numbering (p1-1, p2-1) | Fewer schema changes | Breaks every page-based citation and the RLS-protected page key; no reuse of model work |
| Event-sourced chronology (store every extraction, derive views) | Full history | Much more machinery than needed; snapshots plus diffs answer the reviewer's question |

## Consequences
- On the committed dev and test splits (oracle self-check), every first-production document is reused when the second production arrives, and no event is re-added instead of merged. The report states these as plumbing checks, not model results.
- Event IDs depend on document segmentation. If a later production changes how earlier pages are segmented (it should not, since each production is classified on its own), IDs can change. The diff then falls back to matching by content.
- Snapshots grow with the number of versions. That is acceptable at matter scale. Pruning old snapshots is a retention decision.
