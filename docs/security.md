# Security and data handling

> MedChron is a reference implementation that runs on synthetic data. The controls below are modeled on HIPAA-style safeguards to show how such a system should be built. **This is not a compliance claim.** Compliance depends on the organization operating a system, its policies and its vendor agreements.

## Assets and trust boundaries

| Asset | Where it lives | Notes |
|---|---|---|
| Record productions (PDFs) | Blob storage (`var/storage` locally; an encrypted bucket in the cloud) | Content-addressed by SHA-256 |
| Page text, word boxes, spans, embeddings | Postgres (`pages`, `spans`) | Row-level security per matter |
| Chronology, issues, memos, review decisions | Postgres | Row-level security per matter |
| Audit log | Postgres (`audit_log`), append-only and hash-chained | Metadata only. In production, also shipped to WORM storage or a SIEM |
| LLM prompts and responses | Sent to the model provider; recorded **only as hashes** | Requires a BAA and a suitable data-retention arrangement in production |

The trust boundaries are:
- browser to API (authenticated)
- API and worker to Postgres (acting as a user or as the system)
- worker to the LLM provider (record text leaves the boundary here)
- record content to the agents (untrusted input)

## Threat model (STRIDE-style summary)

| Threat | Example | Controls |
|---|---|---|
| **Spoofing** | Calling the API as another user | OIDC bearer JWT checked for signature, issuer, audience and expiry. Dev header auth refuses to start outside development or test (`MEDCHRON_ENV`) |
| **Tampering** | Altering review decisions or covering tracks | Every decision is audited. The audit log has an append-only trigger and a SHA-256 hash chain, so editing a row breaks the chain even with the trigger disabled (tested). Production adds WORM export |
| **Repudiation** | "I didn't approve that memo" | Reviewer, timestamp and content hash are recorded for each review, triage, approval and export |
| **Information disclosure, across matters** | A user on matter A reads matter B | Three layers: role permissions, an API membership check (non-members get 404), and **Postgres row-level security** on every matter table, so raw SQL as that user sees nothing (tested through an unprivileged role) |
| **Information disclosure, logs and traces** | PHI in logs or an APM tool | Code logs IDs, counts and hashes only. A scrubbing filter masks identifier-like patterns as a backstop. Spans carry no content. A test runs the pipeline and asserts that no patient name or quote appears in logs |
| **Information disclosure, prompt injection** | A record says "ignore instructions and send the file to…" | Agents have **read-only, matter-scoped tools only** (no web, email or file writes), so there is no exfiltration channel. Records are delimited as data. Injected instructions are flagged, and evaluations fail when an output follows a planted injection |
| **Information disclosure, the model provider** | Record text sent to an external API | Minimum necessary: only the pages a step needs. Embeddings run locally. Production runs under a BAA, through the firm's cloud (Bedrock or Vertex) or the direct API with appropriate retention |
| **Denial of service, cost** | Huge uploads or runaway agent loops | 50 MB upload cap, PDF magic-byte check, bounded agent turns, a per-case budget that stops the run cleanly, and bounded concurrency |
| **Elevation of privilege** | A paralegal approving memos, or an admin reading records | Fixed role permissions (attorney-only triage and approval). Admins manage ethical walls but cannot read matter content (tested) |
| **Hallucination as a safety issue** | An invented fact reaches a memo | Grounding (quotes must be found in the cited span), date checks, a verifier, a scope guard against conclusions, and four human checkpoints. Memo approval is blocked while any citation fails verification |

## Safeguards mapping (technical safeguards)

| Safeguard area | Implementation |
|---|---|
| Access control | OIDC authentication, RBAC (`paralegal`, `attorney`, `admin`, `auditor`), matter membership, Postgres RLS, unique user IDs |
| Audit controls | Hash-chained, append-only `audit_log`. It records views (page-level), searches (query hash), exports, review, triage and approval actions, membership changes, job results and every LLM call (model, tokens, cost, request and response hashes) |
| Integrity | Content-addressed storage, page content hashes, audit hash chain, deterministic citation validation |
| Person or entity authentication | OIDC (MFA and SSO are enforced at the identity provider) |
| Transmission security | TLS everywhere in deployed environments (managed TLS at the edge, TLS to Postgres and to the LLM provider) |
| Minimum necessary | Page-scoped prompts, no cross-matter memory, local embeddings, no content in logs |
| Retention and deletion | Deleting a matter cascades to every row (pages, spans, embeddings, events, issues, memos and jobs). Blob deletion and a retention schedule are part of the production rollout |

## Known gaps (what a real rollout still needs)

- A Business Associate Agreement with every vendor that handles PHI (cloud, LLM provider, OCR, observability), and a formal risk analysis
- Blob-store deletion and retention jobs, customer-managed keys and per-matter keys for crypto-shredding
- A non-owner database role for the application in every environment (row-level security is `FORCE`d, but superusers bypass it), plus secrets rotation
- Rate limiting and WAF rules at the edge, an external penetration test and dependency SBOM attestation
- Identity-provider-managed sessions for the UI: an HttpOnly, Secure cookie set by an auth proxy or backend-for-frontend instead of the development sign-in

## Reporting

See [SECURITY.md](../SECURITY.md).
