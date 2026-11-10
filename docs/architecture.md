# Architecture

## System overview

```mermaid
flowchart LR
    subgraph Client
        UI["Reviewer UI<br/>React + TypeScript<br/>review · providers · gaps · versions · export"]
        MCPC["An attorney's MCP client"]
    end
    subgraph API["API (FastAPI) on Cloud Run · role medchron_api"]
        AUTH["OIDC / dev auth<br/>RBAC + matter membership"]
        ROUTES["matters · productions · pages · chronology<br/>versions · diff · providers · gaps · review<br/>issues · memo · gated export · audit"]
    end
    MCP["MCP server (read-only tools)<br/>same access path · role medchron_api"]
    subgraph Worker["Worker (Cloud Run job) · role medchron_worker"]
        ORCH["Orchestrator<br/>(code-owned DAG)<br/>heartbeat · resumable checkpoints"]
    end
    subgraph Data["Postgres + pgvector"]
        T[("matters · productions · pages · spans<br/>events · issues · memos · versions<br/>jobs · audit_log")]
        RLS{{"Row-level security per matter<br/>bypass only for medchron_system"}}
    end
    GCS[("Blob storage<br/>record PDFs")]
    LLM["Claude<br/>(tools · citations · caching · batches)"]

    UI -->|HTTPS| AUTH --> ROUTES
    MCPC -->|stdio / HTTP| MCP --> T
    ROUTES -->|enqueue| T
    ROUTES -->|start execution| ORCH
    ROUTES --- GCS
    ORCH --- GCS
    ORCH --> LLM
    ORCH -->|results| T
    T --- RLS
```

## Pipeline (one case)

```mermaid
flowchart TD
    A["PDF productions<br/>(in arrival order)"] --> B["1 · Ingest (code, per production)<br/>text layer + OCR (orientation, deskew)<br/>dedupe across productions · Bates IDs<br/>misfiled-page check · page-bounded spans"]
    B --> C["2 · Classify pages (LLM, strict tool)"]
    C --> D["3 · Segment into documents (code)"]
    D --> E["4 · Extract events per document<br/>(bounded tool loop: threads, or one Batches round per turn)<br/>checkpoint keyed by the document's content"]
    E -->|"record_events validates:<br/>span exists · quote found · date matches"| E
    E --> F["5 · Chronology (code)<br/>merge · order · conflicts · follow-up gaps<br/>→ new version + diff when it changed"]
    F --> H1{{"Human checkpoint 1-2<br/>confirm index · accept / edit / reject events"}}
    F --> G["6 · Issue analyst (agent loop)<br/>search_records · get_page · query_timeline · find_gaps"]
    G -->|"record_issue validates:<br/>grounding · scope guard · event IDs"| G
    G --> V["7 · Verifier (LLM judge on cited evidence)"]
    V --> H3{{"Human checkpoint 3<br/>triage: pursue · ask expert · dismiss"}}
    H1 --> M
    H3 --> M["8 · Draft memo (Citations API, per-page documents)<br/>offsets checked against page text"]
    M --> H4{{"Human checkpoint 4<br/>attorney approval → memo export"}}
    H1 --> X{{"Attorney approves the current version<br/>→ DOCX / XLSX / MD chronology export"}}
```

Every LLM call goes through a metered provider: per-case budget, cost (batch calls at half price), tokens, and audit records holding hashes only. Every step is checkpointed by a key built from its inputs, prompt versions and model settings. Extraction is keyed by each document's own content, so a second production reuses the first's work ([ADR-0012](adr/0012-incremental-productions-and-versions.md)).

## Incremental productions

```mermaid
sequenceDiagram
    autonumber
    participant P as Paralegal
    participant API
    participant W as Worker
    participant DB as Postgres
    P->>API: upload production 1
    W->>DB: pages 1..N (Bates PFX-000001..), events, version 1
    P->>DB: review events, confirm index
    P->>API: add production 2
    W->>W: ingest p2 as pages N+1..M, dedupe against 1..N
    W->>W: production-1 documents: checkpoint hits (no model calls)
    W->>DB: merged chronology, version 2, review state kept
    P->>API: diff v1 → v2: added, changed (new sources), removed
```

## Database roles

| Role | Connects | Can |
|---|---|---|
| owner | migrations only | everything |
| `medchron_api` | API, MCP server | rows of matters the caller belongs to (RLS); append to the audit log |
| `medchron_worker` | worker | the same, plus the system bypass for background processing |

The system bypass needs both `medchron.system = on` and membership in `medchron_system` ([ADR-0009](adr/0009-defense-in-depth-isolation.md)).

## The path of one claim

```mermaid
sequenceDiagram
    autonumber
    participant X as Extractor (Claude)
    participant T as record_events tool
    participant G as Grounding (code)
    participant DB as Postgres
    participant R as Reviewer
    participant D as Drafter (Claude, citations)
    X->>T: event {type, date_text, date_iso, span_id, quote}
    T->>G: span exists? quote in span? date parses and matches?
    alt rejected
        G-->>T: reason
        T-->>X: tool_result is_error ("quote not found…")
        X->>T: corrected event
    end
    G-->>T: offsets in page text
    T->>DB: event + source (page, char range)
    R->>DB: click p8 → highlight word boxes → accept
    D->>DB: reviewed events + triaged issues
    D-->>DB: memo text + citations (document index, char range)
    DB->>DB: verify cited_text == page.text[start:end]
    R->>DB: approve memo (blocked if any citation is invalid)
```

## Where to look in the code

| Concern | Module |
|---|---|
| Ingestion: OCR (orientation, deskew), word boxes, dedupe, Bates, misfiled pages, spans | `src/medchron/ingestion/` |
| Agents | `src/medchron/agents/` (`classifier`, `extractor`, `analyst`, `verifier`, `drafter`) |
| Tool loop (state machine), providers, cost, replay, batches | `src/medchron/llm/` |
| Grounding: quotes, dates | `src/medchron/grounding/` |
| Orchestration and checkpoints | `src/medchron/orchestrator/pipeline.py` |
| Read-only agent tools, hybrid search | `src/medchron/tools/`, `src/medchron/retrieval/` |
| Chronology versions, diff, exports, provider index, gaps | `src/medchron/chronology/` |
| API, jobs, storage | `src/medchron/api/`, `src/medchron/jobs.py`, `src/medchron/storage.py` |
| MCP server | `src/medchron/mcp_server.py`, [ADR-0011](adr/0011-mcp-server.md) |
| Security: auth, access, audit, logs, DB roles | `src/medchron/security/`, `src/medchron/db/roles.py`, `migrations/versions/0005_*`, `0008_*` |
| Load test | `scripts/loadtest.py`, [loadtest.md](loadtest.md) |
| Evaluation | `src/medchron/evals/`, [eval-methodology.md](eval-methodology.md) |
| Infrastructure | `infra/terraform/`, [deployment.md](deployment.md) |

Design decisions are recorded in [`docs/adr/`](adr/).
