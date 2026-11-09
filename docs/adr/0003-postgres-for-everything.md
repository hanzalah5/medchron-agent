# ADR-0003: Postgres for relational data, vectors, full-text search, the queue and the audit log

- **Status:** Accepted
- **Date:** 2026-11-09

## Context
The system needs relational data (matters, reviews), hybrid search (lexical plus vector), a job queue and an audit log. Each specialized store adds cost, operations and another place where PHI lives.

## Decision
Use one Postgres with pgvector:
- a generated `tsvector` with GIN for lexical search, and an HNSW index on embeddings, fused with RRF in SQL
- a `SKIP LOCKED` job queue
- an append-only, hash-chained audit table
- row-level security for matter isolation across all of it

## Alternatives considered
A dedicated vector database (Qdrant, Pinecone), Redis or a cloud queue, a separate audit store. Each is better at scale for one job and worse for cost, operations and a single access-control boundary.

## Consequences
One backup, one access-control model and one bill. If retrieval scale grows beyond a few million spans, revisit the vector index (or partition by matter).
