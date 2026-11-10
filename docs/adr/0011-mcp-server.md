# ADR-0011: Expose the case tools over MCP, read-only and with the API's access rules

- **Status:** Accepted
- **Date:** 2026-11-10

## Context
Attorneys and paralegals increasingly work with their own AI assistants. Those assistants could use a matter's records, with citations, if they could reach the same read-only tools the issue analyst uses. The Model Context Protocol (MCP) is the standard way to offer tools to such clients.

Exposing record text outside the reviewer UI widens the attack surface. A second entry point must not become a way around matter isolation, role checks or the audit trail. It must also not become a way to change the record or to send it anywhere.

## Decision
- `medchron mcp` serves five **read-only** tools: `list_matters`, `search`, `get_page`, `get_event` and `list_issues`. There are no write tools, and nothing that fetches or sends data elsewhere. Every tool is annotated read-only and closed-world.
- **The same access path as the HTTP API.** The shared module `security/access.py` provides the actor session (Postgres row-level security), the role check (`matter.read`), the membership check (non-members get "not found") and audit logging.
- **Authentication:**
  - Over streamable HTTP, every call is authenticated from its headers. OIDC mode requires a verified bearer token. Dev mode accepts `X-Dev-User`, which is refused outside development and test.
  - Over stdio, the server acts for one local user: a verified token (`MEDCHRON_MCP_TOKEN`) in OIDC mode, or a dev identity.
- **Audit.** Every call is appended to the hash-chained audit log as `mcp.<tool>`. Search queries are stored as SHA-256 hashes, as in the API.
- **Bounded output.** Output uses the agents' caps: page text up to 6,000 characters, eight search hits of up to 700 characters each.
- `mcp` is an optional dependency (`pip install .[mcp]`), so the API container does not carry it.

## Alternatives considered

| Option | Pros | Cons |
|---|---|---|
| No external tool access | Smallest attack surface | Users copy text out by hand, with no citations and no audit |
| A bespoke REST "assistant API" | Full control | Every assistant needs custom integration work, and it duplicates the HTTP API |
| MCP with write tools (review, triage) | More capable assistants | Human checkpoints could be bypassed by an assistant acting on prompt-injected text |

## Consequences
- An MCP client sees exactly what its user could see in the reviewer UI, and every read leaves an audit row.
- Content returned to an external assistant leaves MedChron's control once delivered. Deployments must decide which assistants (and which model providers behind them) may connect. That is a policy question, not something this server can enforce. `docs/security.md` lists it as a deployment requirement.
- Tested in process with a real MCP client against Postgres:
  - members can read
  - non-members get "not found"
  - auditors cannot read content
  - calls are audited with hashed queries

  The HTTP transport's OIDC path reuses `verify_token`, which the API tests cover. It has not been exercised against a live identity provider.
