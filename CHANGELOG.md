# Changelog

This is a portfolio reference implementation that runs on synthetic data only. "Measured" numbers below come from the harness self-check (the gold-label oracle, a test double) or from the load-test script. None of them is a model result. The live model scorecard is still pending.

## [1.1.0] - 2026-11-10

### Measurement
- **Planted-issue precision** alongside recall. Every raised signal is a prediction: analyst issues, date conflicts, follow-up gaps, injection and misfiled-page flags. It is matched to planted issues by kind and page overlap, and is part of the regression gate. It is a lower bound, because planted labels are not exhaustive.
- **Error analysis** in the report. Misses and unmatched predictions are sorted into date error, OCR miss, merge error, wrong span, scope violation or other, with up to three examples each. It found two real weaknesses in the deterministic rules:
  - the follow-up-gap rule over-fires
  - the merge rule leaves some restated procedures as duplicates
- **Verifier calibration tooling** (`medchron calib sample|seed|judge|score`): accuracy, per-class precision and recall, a confusion matrix and Cohen's kappa for each label source. A seed set derived from the gold labels is included and labelled as such. **No human labels exist yet**, and the live judge has not been run.
- The noisy oracle is now deterministic regardless of thread order.

### Benchmark (generator v0.2)
- Three new scenarios:
  - an ED headache treated as migraine, with a return visit for subarachnoid hemorrhage
  - a bile leak after laparoscopic cholecystectomy
  - warfarin with an interacting antibiotic and no INR check
- New hazards: skewed, sideways and upside-down scans; table pages; copy-forward notes with stale dates; a page from another fictional patient's chart; cases delivered in two productions.
- **Dev and held-out test splits.** Seeds 1–999 are dev, used for tuning. Seeds 1000 and up are test, used for reporting. Smoke sets of 8 dev and 9 test cases are committed, and both cover every scenario and hazard.

### Ingestion and pipeline
- OCR turns pages upright with Tesseract orientation detection, deskews them with a projection profile, and maps word boxes back to the original page.
- Duplicate detection works across productions. OCR pairs are compared character by character, and pages with contradicting dates are never treated as duplicates.
- Misfiled pages (a different patient in the header) are flagged and left out of extraction.
- **Incremental productions.** Pages are numbered across productions, and extraction checkpoints are keyed by document content, so a second production reuses earlier work. Chronology versions are stored with diffs. Reviewer decisions and triage survive re-runs.
- **Bates-style page IDs** are assigned at ingestion.
- **Batch extraction mode** through the Message Batches API: half price, resumable after a crash, with a synchronous fallback for refused requests. It is not exercised against the live API.
- Tesseract is limited to one thread per call. The load test found that concurrent workers oversubscribed the CPU through its OpenMP threads.
- Pages are never treated as duplicates when their header identity (MRN, author, date of service) differs. The load test found templated notes from different encounters being merged: 76 of 2,029 pages, down to 5 after the fix.

### Product
- **DOCX and XLSX chronology export** in the conventional layout: date, provider / facility, event, and source (document, page, Bates). Export is gated by attorney approval of the current version, and any later review edit requires approval again. Approvals and exports are audited.
- Chronology **version diff** view, **provider and facility index**, and **gaps-in-treatment** view.
- **MCP server** (`medchron mcp`) with read-only, matter-scoped tools that reuse RBAC, row-level security and the audit log (ADR-0011).

### Engineering
- Hypothesis **property tests** for quote matching (OCR noise tolerated, fabricated additions rejected) and date parsing.
- Worker **heartbeat thread**, a configurable stale-job timeout, worker IDs, and a guard against a reassigned job being overwritten.
- **Load test** script and measured results ([docs/loadtest.md](docs/loadtest.md); oracle provider, 4 vCPUs):
  - 2,029 pages: an 84.8 s uninterrupted reference, and a 132.6 s run on 3 workers in which the worker holding the big job was SIGKILLed after 69.7 s
  - the job resumed on another worker, reusing all 618 checkpointed documents
  - the chronology was identical to the reference, with no duplicated rows and a verified audit chain
- **Non-owner database roles.** The API and worker connect as `medchron_api` and `medchron_worker`. The system bypass requires membership in `medchron_system`, so the API role cannot enable it. Compose, CI and the end-to-end tests run this way (migration 0008, ADR-0009 update).
- Playwright feature tests, and **axe** accessibility checks on every matter tab. A colour-contrast failure that axe found is fixed.

## [1.0.0] - 2026-11-09

First complete version. The tags `v0.1`–`v1.0` exist locally only; see the README roadmap.
- Synthetic benchmark with exact gold labels and planted issues (3 scenarios).
- Grounded extraction with strict tools, deterministic quote and date validation, and a bounded tool loop.
- Chronology with merge, date-conflict and follow-up-gap rules.
- Issue analyst with read-only tools, an independent verifier, and a memo drafted with the Citations API.
- Reviewer UI with click-to-source highlighting and four human checkpoints.
- OIDC with RBAC, Postgres row-level security, a hash-chained audit log, OpenTelemetry, and PHI-scrubbing logs.
- Docker, Terraform for Cloud Run, GitHub Actions CI/CD, Trivy and gitleaks.
- Evaluation harness with a regression gate, and an oracle self-check. The live scorecard is pending.
