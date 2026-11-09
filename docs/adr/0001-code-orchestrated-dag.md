# ADR-0001: A code-orchestrated DAG with bounded agent loops

- **Status:** Accepted
- **Date:** 2026-11-09

## Context
Legal work needs to explain and reproduce how a result was produced. Fully autonomous multi-agent conversations are hard to audit, hard to resume, expensive and nondeterministic. Some steps, though, can't be specified in advance: extracting events from free text, and investigating a chronology for issues.

## Decision
The pipeline is a fixed DAG owned by code: ingest → classify → segment → extract (fan-out) → chronology → analyze → verify, with drafting run after human triage. Model autonomy exists only *inside* steps, as bounded tool loops with turn limits, a shared cost budget and read-only tools. Agents never call other agents.

## Alternatives considered
| Option | Pros | Cons |
|---|---|---|
| Supervisor agent delegating to sub-agents | Flexible; impressive demos | Opaque control flow, cost blowups, hard to checkpoint or explain |
| LangGraph | Checkpointing built in, well known | Extra abstraction over what is a fixed DAG; harder to keep every step typed and testable |
| Pure workflow (no tool loops) | Fully deterministic | Can't self-correct on grounding errors or investigate open-ended questions |

## Consequences
Every step is checkpointed and idempotent, so runs resume after crashes and re-run only what changed. Each LLM call maps to one logical step, which keeps cost, evaluation and audit per step. Adding a new agent means adding a node, not renegotiating a conversation.
