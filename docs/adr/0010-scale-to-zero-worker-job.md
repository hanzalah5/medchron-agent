# ADR-0010: Scale-to-zero worker as a Cloud Run job started by the API

- **Status:** Accepted
- **Date:** 2026-11-09

## Context
A polling worker bills 24/7. Background CPU on a request-scoped service is unreliable. A managed queue service adds cost and moving parts.

## Decision
The queue stays in Postgres. After enqueueing, the API starts a Cloud Run job execution that drains the queue and exits. Heartbeats and stale-job recovery cover crashes. Missed triggers lose nothing, because the next execution picks the job up.

## Consequences
Close to zero idle cost and one image for everything. Latency to first progress includes the job's cold start (seconds), which is acceptable for an asynchronous, minutes-long case job.
