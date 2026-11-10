# Load test: ~2,000 pages, several workers, a worker killed mid-run

Script: [`scripts/loadtest.py`](../scripts/loadtest.py). Every number on this page was measured by that script on the machine described below. **The provider is the gold-label oracle (a test double)**, so this exercises the system's plumbing, not model latency or cost:
- ingestion, OCR and de-duplication
- the job queue and checkpoints
- persistence under row-level security
- the audit log

```bash
python scripts/loadtest.py --db-url postgresql+psycopg://medchron:...@localhost:5432/medchron_load
```

## What it does

1. Generates one fictional patient's long record production (`generate_long_case`: 235 scenario episodes for one patient, 2,029 pages, 33 of them scanned) and three small cases. All are synthetic, with gold labels.
2. Runs the big case once in-process, uninterrupted. This is the **reference**.
3. Resets a dedicated database, queues all four cases and starts **3 worker processes**. The workers share a checkpoint directory and connect as the non-owner `medchron_worker` role.
4. When 30% of the big case's documents have extraction checkpoints, it **SIGKILLs** the worker holding that job, so there is no clean shutdown. The stale-job timeout is 20 s and the heartbeat interval 3 s.
5. Checks:
   - every job completed
   - the big job took exactly two attempts
   - its chronology in the database is identical to the reference
   - page and event row counts match, so nothing was duplicated
   - the audit hash chain verifies

## Machine

4 vCPUs, 15.7 GB RAM, Linux (cloud container), Python 3.12.3, PostgreSQL 16 with pgvector on the same machine, Tesseract 5.

## Results

*Being re-measured after the de-duplication fix described below; the numbers are added when that run finishes. Earlier measured run (before the fix): see finding 2.*

## What the load test found (and what changed)

**1. Concurrent OCR oversubscribed the CPU.** On the first attempt, the three workers made no progress past OCR:
- Tesseract parallelises a single page across every core with OpenMP, so three concurrent workers ran about 12 OCR threads on 4 vCPUs
- the load average reached 9.85
- a single page's Tesseract process had used 2 min 17 s of CPU before the run was stopped

Fix: ingestion now sets `OMP_THREAD_LIMIT=1` (`ingestion/pdf.py`), so parallelism comes from documents and workers. A single-process smoke ingest of a 12-page case then took 1.0 s.

**2. Templated notes from different encounters were treated as duplicates.** The second attempt passed every check, but its reference run found 1,947 documents where the gold has 2,023. **76 pages had been marked as duplicates** and never extracted. They were separate encounters built from the same template: identical lab panels, and copy-paste progress notes.

The committed 6–18-page benchmark cases never triggered this, because they rarely repeat a template. Real records do repeat templates.

Fix: two pages are never duplicates when header identity fields (MRN, author, date of service) clearly differ. A re-sent copy repeats them exactly. On the same 2,029 pages this left **5 false duplicates**:
- consecutive-day progress notes by the same author, with no date on the page
- radiology reports whose MRN was unreadable after OCR

These remain a known limitation. De-duplication on 100 short generated cases is still exact (no misses, no false positives).

## Limits

- One machine, with Postgres on the same host. There is no network latency to the database or to the model.
- The oracle answers instantly. With a real model, extraction calls dominate the wall time and can run concurrently (4 per worker by default) or through the Batches API.
- Only 33 of the 2,029 pages are scanned. OCR-heavy productions will be much slower per page.
- One run per configuration, so there are no confidence intervals.
