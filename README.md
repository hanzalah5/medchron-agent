# MedChron Agent

**Grounded, human-in-the-loop AI agents that turn large medical record productions into a cited medical chronology for legal teams.**

[![CI](https://github.com/hanzalah5/medchron-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/hanzalah5/medchron-agent/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

> [!IMPORTANT]
> **This is a portfolio reference implementation**, built for a realistic legal and healthcare scenario: medical record review for malpractice and personal-injury matters.
> It runs **only on synthetic data** that this repository generates. It is **not** a product delivered to any client, it has **not** been validated for real-world use, and it is **not legal or medical advice**. See [DISCLAIMER.md](DISCLAIMER.md).

> [!NOTE]
> **Status: work in progress.** The project is being built in public, phase by phase. See the [roadmap](#roadmap).

## The problem

Firms handling medical malpractice and personal-injury matters receive record productions that run to hundreds or thousands of pages: scanned, duplicated, out of order and spread across many providers. Paralegals and nurse consultants build a **medical chronology** by hand. Generic "chat with your PDF" tools are not trustworthy here, because they paraphrase without verifiable citations and can invent dates.

## What MedChron does (target design)

1. **Ingests** messy PDFs: it runs OCR, de-duplicates pages, segments the bundle into documents and anchors every span of text to a page and position.
2. **Extracts** clinical events with agents that must cite the exact source span for every claim.
3. **Builds a chronology**: it normalizes dates, merges duplicates and detects conflicts and missing records.
4. **Investigates**: an issue-analyst agent flags gaps, contradictions and delays *as questions for expert review*, never as legal or medical conclusions.
5. **Verifies**: a citation-auditor agent rejects any claim its source does not support.
6. **Drafts** a case memo with native page-level citations.
7. Keeps **humans in control** at four review checkpoints. Nothing is exported without attorney sign-off.

Every statement in the output can be clicked through to the highlighted source page.

## Architecture (summary)

```
Upload → Ingestion (code) → Classifier → Extraction agents (fan-out) → Chronology builder
      → [HITL review] → Issue analyst (agent loop) → Verifier → [HITL triage]
      → Drafting agent (citations) → [Attorney sign-off] → Export
```

Control flow belongs to a **code-orchestrated DAG**. Agents get autonomy only inside bounded, budgeted steps. The full design covers agents, tools, retrieval, memory, security, evaluation and deployment, in [docs/plan.md](docs/plan.md).

## Evaluation

The project ships its own **synthetic benchmark**: generated patient histories, rendered as messy PDFs, with exact gold labels and deliberately planted issues. Planned metrics are event precision, recall and F1, citation validity, faithfulness (an LLM judge calibrated against human labels), planted-issue recall, prompt-injection resistance, and cost and latency per case.

The generator is deterministic, and five smoke cases are committed in [`data/synthetic/`](data/synthetic/). Each is a messy multi-provider PDF with scanned pages, duplicates and fax covers, plus gold labels.

```bash
medchron synth --cases 5 --start-seed 1 --out data/generated
```

*Scorecard pending the first live evaluation run. No numbers are published until they have been measured.*

## Roadmap

| Phase | Milestone | Status |
|---|---|---|
| 0 | Repo foundations, synthetic benchmark generator with gold labels | ✅ Done |
| 1 | Grounded extraction MVP and eval harness v1 (`v0.1`) | 🚧 In progress |
| 2 | Reviewer UI with click-to-source, human-in-the-loop queues (`v0.2`) | ⏳ |
| 3 | Issue analyst, verifier and cited memo drafting (`v0.3`) | ⏳ |
| 4 | Production hardening: auth, RBAC, audit, observability, cost controls (`v0.4`) | ⏳ |
| 5 | Infrastructure as code and deployment, hosted replay demo (`v0.5`) | ⏳ |
| 6 | Architecture decision records (ADRs), eval report, demo video (`v1.0`) | ⏳ |

## Quickstart (developers)

Requirements: Python 3.12+, [uv](https://docs.astral.sh/uv/), Docker (for Postgres + pgvector) and, optionally, [Tesseract](https://github.com/tesseract-ocr/tesseract) for OCR of scanned pages.

```bash
git clone https://github.com/hanzalah5/medchron-agent.git
cd medchron-agent
make install          # .venv with dev dependencies
make check            # lint, type-check, tests
make db-up            # Postgres + pgvector via docker compose, then migrations

# Ingest a synthetic production: text layer + OCR, duplicate detection, page-anchored spans
.venv/bin/medchron ingest data/synthetic/case_0003.pdf --matter case_0003 --save
```

## What's built so far

| Component | Where | Notes |
|---|---|---|
| Synthetic benchmark generator | `src/medchron/synth/` | Seeded, deterministic, gold labels with exact quote and page sources |
| Ingestion | `src/medchron/ingestion/` | PyMuPDF text with word bounding boxes, Tesseract OCR fallback with confidence, duplicate-page detection that ignores fax headers, page-bounded chunks with resolvable span IDs (`case:p12:340-918`) |
| Storage | `src/medchron/db/`, `migrations/` | Postgres + pgvector: matters, pages, spans with generated `tsvector` (GIN) and HNSW cosine index; Alembic migrations |
| Embeddings | `src/medchron/retrieval/` | Local `bge-small` (optional extra) or a deterministic hashing embedder for tests and CI, so record text is never sent out to be embedded |

## License

[Apache-2.0](LICENSE). See [DISCLAIMER.md](DISCLAIMER.md) for the limits on use.
