# MedChron Agent: Design Plan

> Design document for this reference implementation. It is a portfolio project built on synthetic data only, not work delivered to any client.

## Context

This project is a reference implementation of a **production-grade agent system for a law firm that works on healthcare matters**. It covers use-case selection, architecture, production concerns, evaluation, deployment, repository presentation and a phased roadmap.

---

## 0. Assumptions (stated, not invented)

You have not told me your stack, experience level, time or budget. I assumed the following. Change them and the plan adapts.

| Area | Assumption | Effect if it is wrong |
|---|---|---|
| Experience | You can write backend Python and use Docker and Git. Frontend skills are moderate. | If frontend is weak, use the Streamlit alternative for the UI. |
| Time | Unknown. The roadmap uses relative sizes (S/M/L) instead of weeks, and **each phase ends in something you can show**. | You can stop after any phase. |
| Budget | Personal: small LLM API spend during development and a near-zero hosting bill when idle. | All cost decisions favor scale-to-zero hosting and a replay demo that costs nothing to view. |
| Data | **Only synthetic or public data.** No real patient or client data, ever. | None. This is a hard rule. |

### Recommended default stack and why

| Layer | Default | Why | Alternatives |
|---|---|---|---|
| Language | **Python 3.12** | It dominates AI and ML hiring. It has strong PDF, OCR and evaluation tooling and a first-party Anthropic SDK. | TypeScript end to end, if you are targeting full-stack roles. |
| LLM | **Claude via the Anthropic API** (`claude-opus-5-5` as the default; model and `effort` configurable per agent) | Native **Citations** on PDF document blocks (page-level locations), strict tool use, structured outputs, prompt caching and a Message Batches API. Together these cover grounding, reliable extraction and cost control. The same models are also offered through Bedrock and Vertex for a real rollout inside the firm's cloud. | Run the same design on Bedrock or Vertex. Keep the model behind a small interface so the provider can be swapped. |
| Orchestration | **Your own code-controlled orchestrator** (an explicit state machine or DAG) with **bounded agent loops** inside steps, built on the Anthropic SDK (manual loop or Tool Runner) | It shows you understand when to use an agent and when to use a workflow. It is auditable and deterministic where it needs to be. There is no framework magic for reviewers to discount. | **LangGraph** (well known, built-in checkpointing). **Claude Agent SDK** or **Managed Agents** (hosted loop and sandbox). |
| API | **FastAPI**, background jobs with **Postgres-backed queue** (e.g. `procrastinate`/`pgqueuer`) | A single datastore keeps hosting costs low. | Celery + Redis; Temporal for durable workflows (excellent story, heavier). |
| Data | **Postgres 16 + pgvector** (row-level security for matter isolation) | One database holds relational data, vectors and the audit log. | SQLite + sqlite-vec for a laptop-only version; Qdrant for vectors. |
| Ingestion | PyMuPDF for text and layout, **Tesseract OCR** for scanned pages, with page and bounding-box anchors kept | Free, local and deterministic. | A cloud OCR service such as Textract or Document AI, under a BAA in real production. |
| UI | **React + Vite + PDF.js** reviewer UI (click a citation to see the highlighted source) | The click-to-source demo is the strongest visual in the project. | **Streamlit** (faster to build, less impressive). |
| Observability | **OpenTelemetry**, plus **Langfuse** (open source, self-hostable) for LLM traces | It shows production instincts without paid tools. | Grafana Cloud free tier, Arize Phoenix. |
| Infra | Docker, **Terraform**, GitHub Actions, **Google Cloud Run + Neon Postgres** (both scale to zero) | Cost when idle is close to zero, and the Terraform story is clean. | Fly.io; AWS App Runner + RDS; a single small VM with docker-compose. |

---

## 1. Use-case selection

### Candidates

**A. Medical record review and chronology for malpractice and personal-injury cases.**
The system ingests hundreds to thousands of pages of medical records and produces:
- a cited medical chronology
- a list of missing records
- flagged inconsistencies and *potential* standard-of-care issues for expert review
- a draft case-summary memo

**B. Healthcare contract and compliance review.**
The system reviews vendor contracts, BAAs and provider agreements against a firm playbook. It flags missing or non-standard clauses and proposes redlines with explanations.

**C. Medical-malpractice client intake triage.**
The system screens inbound inquiries and intake forms. It extracts the facts, flags conflicts-check entities and deadline-sensitive dates for attorney review, scores matter fit and routes the inquiry.

### Comparison (1 = low, 5 = high)

| Criterion | A. Record review and chronology | B. Contract and compliance | C. Intake triage |
|---|---|---|---|
| Business value | **5.** Paralegals and nurse consultants spend many hours per case on this. It is a recognized pain point and a common outsourced service. | 4. Valuable but crowded: many legal-tech products already do it. | 3. Useful, but the value per item is small and it depends on the firm's process. |
| Agentic depth | **5.** Fan-out over many documents, tool use (search, page lookup, timeline queries), cross-document reconciliation, verification loops and multiple human review points. | 3. Mostly clause extraction plus comparison, closer to a workflow. | 2–3. Classification and routing. Shallow loops. |
| Portfolio impact | **5.** Click-to-source citations over long, messy PDFs are visually compelling, and the healthcare plus legal angle is clear. | 3. It looks like many existing demos. | 2. Hard to demo impressively. |
| Feasibility with synthetic data | **4.** Synthea gives realistic synthetic patient histories. You turn them into messy PDFs and plant ground-truth issues. Building the data takes real effort, but it is also a strength. | 4. Public templates exist, but a realistic playbook and gold labels are harder to justify. | 5. Easy to synthesize. |
| Risk of overclaiming | Medium. Mitigated by "flags for expert review, never conclusions". | Medium. | Low. |

### Recommendation: **A. Medical record review and chronology**

It scores highest on the two criteria that matter most for a portfolio, agentic depth and impact. It directly shows the hardest production skill in legal AI: **outputs grounded in long, noisy source documents, verifiably cited, with humans in control**. It also uses data you can generate legally and label precisely, which makes rigorous evaluation possible. Most portfolio projects lack that.

*Stretch tie-in:* C (intake triage) can later be added as a small upstream agent that feeds A. That gives a second talking point without diluting the main project.

---

## 2. Chosen use case in detail

### Users
| Role | What they need |
|---|---|
| **Paralegal / legal nurse consultant** (primary) | Turn a large record production into an accurate, cited chronology quickly. Spot missing records. |
| **Attorney** (decision-maker) | A trustworthy summary of the case's medical facts, plus flagged issues to take to a medical expert. Verify any statement in one click. |
| **Admin / compliance** | Access control, audit trail, retention and deletion. |
| **Medical expert (external)** | Out of scope as a user. Receives an exported, attorney-approved package. |

### Problem
Medical records arrive as large, inconsistent PDFs: scanned pages, duplicates, out-of-order pages, many providers and handwriting-like OCR noise. Building a chronology by hand is slow and error-prone. Missed events or missed gaps can change case strategy. Generic chat-with-PDF tools are not trustworthy here, because they paraphrase without verifiable citations and they hallucinate dates.

### Workflow before and after
**Before (manual):**
1. A paralegal receives the record set and indexes it by hand.
2. They read every page and type events into a spreadsheet.
3. They cross-check dates and providers and note missing records.
4. An attorney reviews the spreadsheet, asks questions and the paralegal re-reads pages.
5. The attorney drafts a memo.

**After (agent-assisted, with humans in control):**
1. Upload the records. The system OCRs, splits, de-duplicates and classifies them.
2. **Checkpoint 1:** the paralegal confirms the document index.
3. Extraction agents build a candidate chronology. Every event carries a page citation.
4. A verifier agent audits every citation automatically.
5. **Checkpoint 2:** the paralegal accepts, edits or rejects events in the UI, with the source highlighted.
6. The issue analyst flags gaps, contradictions and *potential* care issues as questions for expert review.
7. **Checkpoint 3:** the attorney triages the flags.
8. A drafting agent writes a cited memo.
9. **Checkpoint 4:** the attorney signs off before any export.

### Success criteria (targets to validate on the synthetic evaluation set, not claims)
| Metric | How it is measured | Initial target (a goal, adjust after the baseline) |
|---|---|---|
| Event recall | Matched to gold events (date ±0 days, same type, same page) | ≥ 0.90 |
| Event precision | Same matching rule | ≥ 0.90 |
| Citation validity | The cited page or span exists and contains the supporting text (deterministic check) | ≥ 0.99 |
| Faithfulness | Share of claims in the memo or chronology supported by their citation (LLM judge, calibrated against human labels) | ≥ 0.97 |
| Planted-issue recall | Share of deliberately planted gaps or contradictions that get flagged | ≥ 0.80 |
| Unsupported-claim rate | Claims with no valid citation that reach the reviewer | ≈ 0 (blocked by the verifier) |
| Cost per 100 pages | Token accounting from traces | Measure, publish and reduce over phases |
| Processing time per case | p50 and p95 for an asynchronous job | Measure and publish |
| Reviewer effort (proxy) | Edits per 100 events; share of events accepted unchanged | Track the trend |

Be honest about the limits. You cannot measure real paralegal time savings without real users. Present reviewer effort as a proxy. Optionally run a small timed exercise with volunteers on synthetic cases, and label it clearly as such.

---

## 3. Architecture

### 3.1 High-level diagram

```
                          ┌──────────────────────────────────────────────┐
  Reviewer UI (React +    │                 FastAPI gateway              │
  PDF.js, click-to-source)│  OIDC auth · RBAC · matter ACL · rate limits │
  ───────────────────────►│  audit middleware · request IDs              │
                          └───────────────┬──────────────────────────────┘
                                          │ enqueue job / read state
                     ┌────────────────────▼────────────────────┐
                     │      ORCHESTRATOR (code-controlled DAG) │
                     │ case state machine · checkpoints · retry│
                     │ budgets · HITL gates · resume from fail │
                     └─┬──────────┬───────────┬──────────┬─────┘
          deterministic│          │fan-out    │          │
 ┌─────────────────────▼──┐ ┌─────▼────────┐ ┌▼─────────┐┌▼───────────────┐
 │ 1 INGESTION PIPELINE   │ │2 CLASSIFIER  │ │3 EXTRACT ││ 4 CHRONOLOGY   │
 │ OCR · split · dedupe   │ │ agent (cheap │ │ agents   ││ builder: merge │
 │ page/bbox anchors      │ │ effort) doc  │ │ per doc  ││ dedupe · order │
 │ chunk + embed (pgvector│ │ type/date/   │ │ → events ││ conflict detect│
 │ PHI tagger (metadata)  │ │ provider     │ │ +span IDs││ (code + LLM)   │
 └──────────┬─────────────┘ └──────────────┘ └──────────┘└───────┬────────┘
            │      ◆ HITL-1: confirm doc index                    │
            │                                     ◆ HITL-2: review events
            │                         ┌───────────────────────────▼───────┐
            │                         │ 5 ISSUE ANALYST (agent loop)      │
            │                         │ tools: search_records, get_page,  │
            │                         │ query_timeline, find_gaps,        │
            │                         │ lookup_reference (curated corpus) │
            │                         └──────────────┬────────────────────┘
            │                                        │
            │                         ┌──────────────▼────────────────────┐
            │                         │ 6 VERIFIER / CITATION AUDITOR     │
            │                         │ deterministic span match + LLM    │
            │                         │ entailment check → pass / revise /│
            │                         │ drop / escalate                   │
            │                         └──────────────┬────────────────────┘
            │                         ◆ HITL-3: attorney triages flags
            │                         ┌──────────────▼────────────────────┐
            │                         │ 7 DRAFTING AGENT (Citations API   │
            │                         │ on PDF pages) → cited memo        │
            │                         └──────────────┬────────────────────┘
            │                         ◆ HITL-4: attorney sign-off → export
            ▼                                        ▼
 ┌────────────────────────────────────────────────────────────────────────┐
 │ Postgres: matters · documents · pages · spans · events · issues ·      │
 │ reviews · runs/checkpoints · hash-chained audit_log · pgvector         │
 │ Object storage (encrypted): original PDFs, page images                 │
 └────────────────────────────────────────────────────────────────────────┘
   Cross-cutting: OpenTelemetry traces → Langfuse · metrics · PHI-safe logs
                  per-case token budget · prompt cache · secrets manager
```

Also commit a **Mermaid** version in `docs/architecture.md` so GitHub renders it, and export a PNG for the README.

### 3.2 Agents and roles

| # | Component | Agent or code? | Role | Output contract |
|---|---|---|---|---|
| 1 | Ingestion pipeline | **Code** (deliberately not an agent) | OCR, page images, layout text with char offsets and bounding boxes, near-duplicate page detection, chunking and embedding, PHI entity tagging as metadata | `Page{id, doc_id, page_no, text, ocr_conf, bboxes}` |
| 2 | Classifier | Single LLM call per segment, low effort | Splits the bundle into logical documents and labels type (ED note, op report, lab, imaging, discharge…), date, provider and facility | Structured output (JSON schema) |
| 3 | Extraction agents | Bounded tool loop, **fan-out per document**, concurrency-limited | Extracts clinical events (encounter, diagnosis, procedure, medication change, vitals of interest, complaint, provider instruction) | Strict tool call `record_event{date, type, summary, provider, source_span_ids[], verbatim_quote, confidence}` |
| 4 | Chronology builder | **Code first**, LLM only for ambiguous merges | Normalizes dates, merges duplicate events across sources, orders them, detects conflicts (e.g. different dates for the same procedure) and gaps (a referral with no follow-up record) | `Timeline`, `Conflict[]`, `Gap[]` |
| 5 | Issue analyst | **Agent loop** (the most agentic component) with a tool budget | Investigates the timeline: missing records, contradictions, delays between symptom and action, medication interactions noted in the record. Phrases every result as a *question for expert review*. | `Issue{category, description, supporting_event_ids, citations, severity_hint, rationale}` |
| 6 | Verifier / citation auditor | Code + LLM judge | For every claim: (a) a deterministic check that the quote exists in the cited span (fuzzy match tolerant of OCR noise); (b) an LLM entailment check of claim against span. Then pass, revise (one retry with feedback), drop, or escalate to a human. | `Verdict{claim_id, status, reason}` |
| 7 | Drafting agent | Single long call with **Citations enabled** on the relevant PDF pages | Writes a case-summary memo from *approved* events and issues only | Text with native citation blocks (page locations) |

**Orchestration pattern:** an *orchestrator-workers* DAG, owned by code, with *evaluator-optimizer* loops at the verifier. The reasons are worth stating in an ADR:
- In legal work, **auditability and reproducibility beat autonomy**. Every step is a checkpointed, re-runnable node.
- Agents get autonomy only where the path cannot be specified in advance (extraction and the issue analysis loop), and only **within tool and token budgets**.
- No agent can call another agent freely. The orchestrator owns control flow, so you can explain exactly what happened in any run.

### 3.3 Tools (exposed to agents)

All tools are **read-only, scoped to the current matter**, take typed arguments (`strict: true` schemas) and return content that includes stable IDs for citation.

| Tool | Used by | Notes |
|---|---|---|
| `search_records(query, filters)` | Extraction, analyst | Hybrid search: BM25 (Postgres FTS) plus a vector query, reranked. Returns span IDs, page numbers and snippets. |
| `get_page(page_id)` / `get_span(span_id)` | All | Exact text and image reference. Agents cannot cite what they have not retrieved. |
| `query_timeline(range, type, provider)` | Analyst, drafting | Reads the structured timeline. |
| `find_gaps(rule)` | Analyst | Deterministic helpers, e.g. "referral without a follow-up encounter within N days" (N is configurable, not a medical claim). |
| `lookup_reference(term)` | Analyst | **Optional**, curated public corpus only (e.g. public-domain drug labeling text). Clearly separated from record citations. |
| `record_event`, `record_issue` | Extraction, analyst | Output tools. The schema forces source span IDs. |

There is no web access, filesystem access or code execution in agent tool sets. That is a deliberate prompt-injection boundary (see §4).

**Optional MCP server (stretch).** Expose the same tools as an MCP server, so the case tools can be used from Claude Desktop or Claude Code. It is a strong talking point about standard interfaces.

### 3.4 Retrieval and data pipeline

1. **Upload**: hash the file (SHA-256), store it encrypted, and create a `document` row.
2. **Render and OCR**: produce page images and text with per-word bounding boxes. Keep OCR confidence per page and route low-confidence pages to human review.
3. **Normalize**: de-duplicate (exact hash plus near-duplicate via MinHash or page-embedding similarity) and detect page order.
4. **Segment**: the classifier splits the bundle into logical documents.
5. **Chunk**: structure-aware chunks (headings, note sections) that **never cross page boundaries**, each with `span_id = doc:page:char_start-char_end`.
6. **Index**: Postgres FTS plus pgvector embeddings. The embedding provider is pluggable: use a local open model such as `bge` or `e5` through sentence-transformers so PHI does not leave the system boundary in the demo.
7. **Retrieve**: hybrid search, then a reranker (local cross-encoder), returning results that carry span IDs.
8. **Cache**: LLM results are cached by content hash plus prompt version, so re-runs after small changes are cheap.

### 3.5 Memory and state

| Kind | Where | Policy |
|---|---|---|
| Workflow state | `runs`, `steps`, `checkpoints` tables | Every node persists its inputs and outputs, so a run can resume after failure and can be replayed for audit. |
| Case working memory | Structured tables (events, issues, reviews) | Agents read *structured state*, not long chat transcripts. This keeps context small and cache-friendly. |
| Agent scratch context | In-request only | Bounded by a token budget. Old tool results are cleared within long loops. |
| Cross-case memory | **None by design** | This prevents leaking confidential facts between matters (confidentiality and ethical walls). |
| Firm knowledge | Versioned prompt and playbook files in the repo (event taxonomy, memo style guide) | Curated, reviewed and versioned like code. It is not learned from client data. |
| Reviewer feedback | `reviews` table | Feeds evaluation and regression datasets (synthetic only). Shows a learning loop without training on client data. |

### 3.6 Human review points
- **HITL-1, document index.** Confirm document splits and types, and resolve pages with low OCR confidence.
- **HITL-2, chronology.** Accept, edit or reject each event, with the source highlighted side by side. Low-confidence events are sorted to the top.
- **HITL-3, issues.** The attorney triages each flag as pursue, dismiss or ask an expert. A reason is required on dismissal.
- **HITL-4, sign-off.** Nothing is exported until an attorney approves it. Exports are watermarked "DRAFT – attorney work product" in the demo.
- Every decision is written to the audit log with the user, the time and the before and after values.

### 3.7 Grounding and citations (core differentiator)
- **Stable anchors**: every page and span has an ID. Every claim in every output must carry at least one anchor.
- **Extraction**: strict tool schemas *require* `source_span_ids` and a `verbatim_quote`. The orchestrator rejects any event whose quote cannot be fuzzy-matched inside the cited span.
- **Drafting**: uses the API's native **Citations** feature on the PDF page documents, which returns page-level locations. *Design note:* citations cannot be combined with structured-output formatting in the same request. Extraction therefore uses strict tools and drafting uses citations. Document this trade-off in an ADR.
- **Verifier gate**: claims without support never reach the reviewer silently. They are revised, dropped or escalated, and every drop is logged.
- **UI**: clicking a citation opens the PDF page with the quoted text highlighted from the stored bounding boxes.
- **No unsourced medical knowledge**: anything from `lookup_reference` is labeled "reference, not from record" and kept visually separate.

---

## 4. Production concerns

### Security and access control
- **Authentication**: OIDC. Use Auth0, Clerk or Keycloak; for the demo, a small Keycloak container or a hosted free tier.
- **Authorization**: RBAC roles `paralegal`, `attorney`, `admin` and `auditor`, plus **matter-level ACLs** (ethical walls), enforced in the API *and* by Postgres **row-level security**. This is defense in depth.
- **Least privilege** for agents: tools are read-only and matter-scoped. The service account cannot read other matters.
- **Encryption**: TLS everywhere. Data at rest is encrypted (managed disk and database encryption, and an encrypted bucket). In production, use customer-managed keys.
- **Prompt-injection defense**: treat record text as untrusted data. Wrap documents in clearly delimited blocks, give agents no tools that can exfiltrate (no web, email or file writes), validate every output against its schema, and let the verifier reject claims that are unsupported or out of scope. Add an **injection test set** to the evaluations, e.g. a synthetic clinical note containing "ignore previous instructions…".
- **Dependencies and supply chain**: Dependabot or Renovate, `pip-audit`, Trivy container scans, CodeQL, pinned base images and SBOM generation in CI.

### Audit logging
- An append-only `audit_log` table. Each row is **hash-chained** to the previous row (tamper-evident) and records the actor, action, matter, object and request ID.
- What is logged: logins, document views (page-level), exports, review decisions, configuration changes, and every LLM call's **metadata** (model, prompt version, token counts, latency, tool calls, input and output *hashes*). The text itself is not logged.
- An `auditor` role can view the log and export it.

### Sensitive health and legal data (HIPAA-style handling)
Frame these as *design controls aligned with HIPAA-style requirements*, never as "HIPAA compliant". No certification exists, and compliance depends on the organization operating the system.
- **Synthetic data only**, enforced in code: an upload banner, plus a CI check that only allows fixtures from the `data/synthetic/` generator.
- **Minimum necessary**: agents receive only the pages and spans they need. Logs and traces hold IDs, not PHI text. Langfuse traces use redaction or masking hooks.
- **De-identification utility**: an optional redaction pass that tags the HIPAA Safe Harbor identifier categories, used for exports to external parties.
- **Technical safeguards** mapped in `docs/security.md`: access control, audit controls, integrity (hash chain and document hashes), authentication and transmission security.
- **Retention and deletion**: a per-matter retention setting and a hard-delete job that removes the database rows, blobs, embeddings and cache entries for a matter. Deletion events are themselves audited.
- **Vendor note (in the README)**: a real deployment needs a **Business Associate Agreement** with every vendor that handles PHI (cloud, LLM provider, OCR, observability), plus an appropriate data-retention arrangement with the LLM provider. The demo does not process PHI, so it does not have these.
- **Legal confidentiality**: no cross-matter memory, matter ACLs and work-product watermarking, plus a disclaimer that the output is not legal or medical advice.

### Guardrails against hallucination
1. Structural: every claim needs an anchor, enforced by the schema.
2. Deterministic quote-in-span verification.
3. An LLM entailment check by the verifier.
4. Abstention: prompts and schemas allow `insufficient_evidence` and low confidence, and these route to a human instead of being guessed.
5. Date normalization in code (a parser plus a cross-check against the document date), never left to the model alone.
6. Scope guard: the analyst must phrase findings as questions for expert review. A classifier or regex check flags conclusory legal or medical language such as "negligent" or "malpractice occurred".
7. Evaluations track the hallucination rate on every change (§5).

### Error handling
- SDK retries with exponential backoff for 429, 5xx and connection errors. Typed exception handling separates retryable from non-retryable errors.
- Every step is **idempotent** (keyed by run, step and content hash) and checkpointed, so a run can **resume from the failed node**.
- Schema validation failure: one retry with the validation error fed back, then the item goes to a human review queue.
- Handle every `stop_reason`: `max_tokens` (continue or split the input), `refusal` (log and route to a human), `pause_turn` and `tool_use`.
- Per-page dead-letter queue: one bad page never fails the whole case. The UI shows partial results with clear "incomplete" markers.
- Circuit breaker and per-case budget: the run stops cleanly and alerts when a token or cost budget is exceeded.

### Observability
- OpenTelemetry spans for every API request, orchestrator step, LLM call and tool call, correlated by `run_id` and `matter_id`.
- Langfuse for LLM traces: prompt version, tokens, latency, cost, tool calls and evaluation scores attached to traces.
- Metrics: job duration, step failure rates, verifier drop rate, cache hit rate, cost per case and the human edit rate.
- Dashboards plus alerts for error-rate spikes, budget breaches and queue backlog.
- **PHI-safe by default**: content capture is off in production traces. A dev-only flag enables it for synthetic data.

### Cost control
- **Prompt caching**: stable system prompts and tool definitions go first. A case's document prefix is reused across the analyst and drafting turns. Verify the hit rate from `usage.cache_read_input_tokens`.
- **Message Batches API** for non-urgent bulk extraction in "overnight mode", which is discounted compared with synchronous calls (check current pricing).
- **Per-route `effort` tuning** (low for classification, higher for the analyst), chosen by evaluation. Measure "one strong model at lower effort" before adding a multi-model cascade. Model per agent stays configurable so evaluations can decide whether a cheaper model (e.g. `claude-haiku-5-5` for page classification) holds quality.
- Skip duplicate pages before any LLM call; content-hash result cache; per-case and per-day budget caps; a hard monthly cap on the API key.
- Publish **cost per 100 pages** in the README from evaluation runs.

### Latency
- A case is an **asynchronous job**. Progress streams to the UI through server-sent events (SSE).
- Parallel fan-out with bounded concurrency, sized to stay within API rate limits.
- Interactive "ask about this case" Q&A, if added, streams tokens and uses retrieval plus the cached prefix.
- Publish measured p50 and p95 per phase. Do not promise numbers before measuring.

---

## 5. Evaluation strategy

### Test dataset (synthetic, versioned, licensed)
- **Generator** (`data/generator/`):
  1. **Synthea** synthetic patients (open source) supply the structured histories: encounters, conditions, medications and procedures.
  2. An LLM turns those histories into realistic clinical note text, seeded and fully reproducible.
  3. Notes are rendered into PDFs with real-world messiness: scanned-look rasterization, rotation, OCR noise, duplicate pages, mixed-up order, fax headers and handwriting-style fonts.
  4. **Planted ground truth**: deliberate gaps (a missing follow-up), contradictions (conflicting dates), delays and prompt-injection notes. Each one is recorded in a gold file.
- **Gold labels**: `gold/{case_id}.json` holds events (date, type, page, quote), planted issues and expected abstentions. Because you generated the data, labels are exact. Hand-verify a sample and document the process.
- **Size**: start with 5 cases for a smoke set and grow to about 30–50 cases of varying length (tens of pages up to several hundred).
- **Splits**: `dev` for tuning prompts and `test` held out and reported. Never tune on `test`.
- Do **not** use credentialed real-patient datasets such as MIMIC. They are real (de-identified) patient data under data-use agreements, which violates the synthetic-only rule and their terms.

### Automated evaluations
| Eval | Method |
|---|---|
| Classification accuracy | Exact match against gold document types and dates |
| Event extraction P/R/F1 | Bipartite matching on (date, type, page) with a fuzzy summary similarity |
| Citation validity | Deterministic: the span exists and the quote fuzzy-matches inside it |
| Faithfulness | LLM-as-judge (claim → span entailment), **calibrated**: hand-label about 100 claims and report judge–human agreement |
| Issue detection | Recall and precision on planted issues |
| Abstention quality | Correct `insufficient_evidence` on deliberately ambiguous items |
| Safety | Prompt-injection resistance (no instruction-following, no tool misuse); scope guard (no conclusory legal or medical language); PHI-in-logs scanner over trace output |
| Operational | Cost per case, tokens, latency p50/p95, cache hit rate |

### Regression testing
- **Every PR (no LLM spend)**: unit tests, schema tests, deterministic verifier tests, and orchestrator tests with **recorded LLM fixtures** in a replay mode.
- **PR label `run-evals` or nightly**: the smoke evaluation (5 cases) runs against the live API with a budget cap. CI fails if a metric drops more than a set tolerance from the stored baseline.
- **On release tags**: the full `test` split runs, and the results are committed.
- Prompts are versioned files (`prompts/extract_event/v3.md`). Every evaluation result records prompt versions, model IDs and the git SHA.

### Reporting in the repository
- `evals/results/<date>-<sha>.json`: raw per-case metrics.
- `evals/REPORT.md` is generated automatically. It holds a current scorecard table, deltas against the previous release, cost and latency, and a **failure analysis** section with three to five real failure examples and what was changed.
- README: a compact scorecard plus a link to the full report. A small trend chart image is regenerated by CI.
- `docs/eval-methodology.md`: dataset construction, matching rules, judge calibration and known limitations.

---

## 6. Deployment

### Containerization
- Multi-stage Dockerfiles for `api`, `worker` and `ui` (static build served by the API or a CDN). Non-root user, slim pinned base images, health checks.
- `docker-compose.yml` for one-command local setup: Postgres+pgvector, API, worker, UI, Langfuse and an optional Keycloak.
- `make demo` seeds synthetic cases and opens the UI.

### Infrastructure as code
- **Terraform** in `infra/terraform/` with modules for networking, Cloud Run services, Neon (or Cloud SQL), a storage bucket, Secret Manager, the service account and IAM, and an Artifact Registry repository.
- Environments are separate Terraform workspaces or directories (`envs/dev`, `envs/prod-demo`) with remote state in a bucket.

### CI/CD (GitHub Actions)
- `ci.yml`: lint (ruff), type-check (mypy or pyright), unit tests, replay-mode orchestrator tests, frontend build, Trivy scan, pip-audit, CodeQL and a `terraform plan` on infra changes.
- `evals.yml`: the label-triggered or nightly smoke evaluation, which posts a metrics delta as a PR comment.
- `deploy.yml`: on merge to `main`, build and push images, deploy to `dev`, run smoke tests, then promote to `prod-demo` through a manual approval environment.
- Cloud auth uses **GitHub OIDC → cloud workload identity**, so there are no long-lived cloud keys in GitHub.

### Environments
- `local` (compose), `dev` (auto-deploy), `prod-demo` (public, manual promotion).
- `prod-demo` runs **replay mode by default**: precomputed results, so it costs nothing per viewer and cannot be abused. A **live mode** sits behind an access code with per-day budget and rate limits, for interviews.

### Secrets management
- Local: a `.env` file (git-ignored) plus `.env.example`.
- Cloud: Secret Manager, mounted at runtime, with a least-privilege service account.
- CI: GitHub environment secrets, used only for the evaluation API key with a spend cap.
- `gitleaks` pre-commit hook and CI check.

### Monitoring
- Cloud Run metrics and logs, Langfuse for LLM traces, uptime checks, alerts to email, and a budget alert on cloud billing and on the LLM API account.

### Keeping hosting cheap
- Scale-to-zero compute (Cloud Run), a serverless Postgres free tier (Neon), a static UI, the replay-mode public demo and tight caps on API spend. **Expected idle cost is close to zero.** Confirm this against current provider free-tier terms; they change.

### What changes for a real production rollout
| Demo choice | Production change |
|---|---|
| Public cloud, free tiers | Deploy in the firm's own cloud account or VPC with private networking, under **BAAs** with every PHI-touching vendor |
| Anthropic API direct | Claude through the firm's cloud provider (Bedrock or Vertex) or the direct API under an appropriate retention or data agreement, with regional data residency where required |
| Local OCR in a container | Scaled OCR workers, or a managed OCR service under a BAA |
| Simple OIDC | Firm SSO (SAML/OIDC), MFA, SCIM provisioning, integration with the matter-management system for ACLs |
| Postgres hash-chained audit | Also stream to immutable WORM storage or a SIEM |
| Managed encryption | Customer-managed keys (KMS) with rotation; per-matter encryption keys for crypto-shredding |
| Single region, no SLA | High availability, backups with tested restore, a disaster-recovery plan, defined SLOs |
| Self-assessed security | Penetration test, formal risk assessment, policies and training, and vendor reviews (e.g. SOC 2 reports) |
| Synthetic eval set | Firm-approved, de-identified internal evaluation set, ongoing human QA sampling, a model-change governance process |
| — | Integrations: DMS (document management), e-discovery exports, matter-management system |

---

## 7. GitHub presentation

### Repository structure
```
medchron-agent/
├── README.md
├── LICENSE                      # Apache-2.0
├── DISCLAIMER.md                # synthetic data only, not legal/medical advice, not a client deliverable
├── SECURITY.md
├── docker-compose.yml  Makefile  .env.example  pyproject.toml
├── src/medchron/
│   ├── api/                     # FastAPI routes, auth, RBAC, audit middleware
│   ├── orchestrator/            # DAG, state machine, checkpoints, budgets
│   ├── agents/                  # classifier, extractor, analyst, verifier, drafter
│   ├── tools/                   # search_records, get_page, query_timeline, ...
│   ├── ingestion/               # OCR, segmentation, dedupe, chunking, embeddings
│   ├── grounding/               # span IDs, quote matching, citation validation
│   ├── llm/                     # provider interface, caching, retries, cost accounting
│   ├── security/                # PHI tagging/redaction, log scrubbing
│   └── db/                      # models, migrations (alembic), RLS policies
├── prompts/                     # versioned prompt files
├── ui/                          # React + Vite + PDF.js reviewer app
├── data/
│   ├── generator/               # Synthea → notes → messy PDFs + gold labels
│   └── synthetic/               # small committed sample cases
├── evals/                       # harness, metrics, judge calibration, results/, REPORT.md
├── tests/                       # unit, integration, replay fixtures
├── infra/terraform/             # modules + envs/dev, envs/prod-demo
├── .github/workflows/           # ci.yml, evals.yml, deploy.yml
└── docs/
    ├── architecture.md          # Mermaid diagrams, data flow, sequence diagram
    ├── security.md              # threat model, safeguards mapping, data handling
    ├── eval-methodology.md
    ├── adr/                     # 0001-code-orchestrated-dag.md, 0002-citations-vs-structured-output.md, ...
    └── production-rollout.md    # the "what would change" table, expanded
```

### README outline
1. **Title, one-sentence pitch and badges** (CI, eval score, license).
2. **Honesty banner**: "A portfolio reference implementation for a realistic legal-healthcare scenario. Built on synthetic data only. Not a product delivered to any client, and not legal or medical advice."
3. **Demo**: a 20–30 second GIF (upload → chronology → click citation → highlighted source), a link to the hosted replay demo and a link to a 3–4 minute narrated video.
4. **The problem and who it is for**: three bullets.
5. **What it does**: before and after workflow.
6. **Architecture**: the diagram plus a short description of each agent.
7. **Grounding and human-in-the-loop**: why you can trust the output.
8. **Evaluation results**: the scorecard table and a link to `REPORT.md`, including known failure modes.
9. **Production concerns**: security, audit, data handling, cost and observability, with links to the docs.
10. **Quickstart**: `git clone`, `cp .env.example .env`, `make demo`. Replay mode works **without an API key**.
11. **Design decisions and trade-offs**: links to the ADRs, with the key trade-offs in one line each.
12. **Roadmap and limitations.**
13. **License and acknowledgements**: Synthea and other libraries, with their licenses.

### Architecture diagram
Use Mermaid in `docs/architecture.md`, rendered natively by GitHub, plus one polished PNG or SVG for the README (Excalidraw or draw.io; commit the source file). Also add a **sequence diagram** of one claim's path: extract → verify → review → memo citation.

### Demo
- **GIF** at the top of the README (most important).
- **Hosted replay demo** (zero marginal cost) with three or four precomputed synthetic cases.
- **Narrated video** on YouTube (unlisted or public) covering the problem, the demo, the architecture, the evaluations and the trade-offs.
- The live mode is behind an access code you hand out in interviews.

### Design decisions to document as ADRs
1. A code-orchestrated DAG instead of autonomous multi-agent chat.
2. Strict tool schemas for extraction, Citations API for drafting (they cannot be combined in one call).
3. Postgres for everything (relational, vector, queue, audit) instead of specialized stores.
4. No cross-matter memory.
5. A local embedding model, to keep record text inside the boundary.
6. Deterministic date handling and gap rules in code instead of the LLM.
7. Replay mode as the public demo.
8. LLM-as-judge with human calibration.

### License
**Apache-2.0**. It is permissive, includes a patent grant and is familiar to enterprises. MIT is a fine simpler alternative. Add `DISCLAIMER.md`, and confirm the licenses of Synthea and any reference corpus you include.

---

## 8. Phased build roadmap

Sizes are relative (S < M < L). Every phase ends in something you can show.

| Phase | Scope | Milestone / what you can show if you stop here | Size |
|---|---|---|---|
| **0. Foundations and data** | Repo skeleton, compose, CI lint and test, Synthea → notes → messy PDF generator with **gold labels and planted issues**, 5-case smoke set | "I built a reproducible synthetic medical-record benchmark with exact ground truth." This is already unusual. | M |
| **1. Grounded extraction MVP** (vertical slice) | Ingestion (OCR, anchors, dedupe), classifier, extraction agents with strict schemas, chronology builder, deterministic citation validation, CLI output (Markdown or CSV chronology with page cites), **eval harness v1 with P/R/F1 and citation validity** | **Minimum impressive project**: an agent pipeline producing a cited medical chronology, with measured accuracy and a published scorecard. | L |
| **2. Reviewer UI and human-in-the-loop** | FastAPI, React + PDF.js click-to-source, HITL-1 and HITL-2 queues, review decisions stored, record GIF | **The demo moment**: click any event and see the highlighted source page. Ship the README v1 here. | M–L |
| **3. Deep agents** | Issue analyst loop with tools and budgets, verifier/citation auditor, drafting agent with the Citations API, HITL-3 and HITL-4, LLM-judge faithfulness with human calibration, prompt-injection evals | Shows real agentic depth: an investigation loop, an evaluator-optimizer loop and a verifiably cited memo. | L |
| **4. Production hardening** | OIDC, RBAC, matter ACLs with Postgres RLS, hash-chained audit log, PHI-safe logging, OTel and Langfuse, retries, resume and dead-letter, budgets, prompt caching, batch mode, cost and latency in the eval report | "Built like it would actually run at a firm." `docs/security.md` and the threat model. | M–L |
| **5. Deploy** | Terraform, Cloud Run + Neon, GitHub OIDC deploys, dev → prod-demo with approval, replay-mode public demo, monitoring and budget alerts | Live URL, green deploy pipeline, IaC. | M |
| **6. Polish and publish** | ADRs, eval REPORT with failure analysis, narrated video, architecture visuals, `production-rollout.md`, a blog post or LinkedIn write-up | Interview-ready package. | S–M |
| **Stretch** | MCP server exposing case tools; intake-triage agent (use case C) feeding the pipeline; FHIR bundle ingestion alongside PDFs; a small timed usability study with volunteers on synthetic cases | Extra talking points. | — |

**Recommended publishing cadence**: make the repo public at the end of Phase 1, with an honest "work in progress" roadmap. Tag releases `v0.1` (Phase 1), `v0.2` (Phase 2), and so on. Each tag comes with a committed evaluation report, so the history itself shows disciplined iteration.

---

## 9. Verification (how you will know each phase is done)

- **Phase 0**: `make generate CASES=5` produces PDFs and gold JSON deterministically (same seed gives the same hashes). CI is green.
- **Phase 1**: `make eval SPLIT=smoke` prints P/R/F1 and citation validity and writes `evals/results/*.json`. Every chronology row cites a page that exists and contains the quote.
- **Phase 2**: a Playwright end-to-end test: upload a sample, wait for the job, click a citation, and assert the PDF viewer shows the highlighted span. Record the GIF from this flow.
- **Phase 3**: planted-issue recall is reported. The injection eval shows no instruction-following. The judge–human agreement number is published.
- **Phase 4**: a cross-matter access test returns 403 at both the API and the database level. A log scan finds no synthetic PHI strings in traces. Killing the worker mid-run and resuming completes the case without duplicate events. A budget-exceeded test stops cleanly.
- **Phase 5**: `terraform plan` is clean in CI. Merging to main deploys to dev automatically. The prod-demo replay works with no API key configured.
- **Phase 6**: a friend unfamiliar with the project goes from the README to the running local demo in 15 minutes or less.

