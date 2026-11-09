# ADR-0009: Matter isolation enforced in the API and in Postgres

- **Status:** Accepted
- **Date:** 2026-11-09

## Context
The worst failure for a legal system is one client's records appearing in another's matter. API-only checks fail open when one endpoint forgets them.

## Decision
Three layers:
1. role permissions
2. a matter-membership check in every endpoint (non-members get 404)
3. Postgres row-level security on every matter table, `FORCE`d, keyed on a per-transaction actor that the session layer sets automatically

Admins manage membership but can't read records. Backend processes run as an explicit "system" actor only after the API has authorized the work.

## Consequences
A missing check in new code still can't leak rows, and tests verify this with raw SQL through an unprivileged role. Production must connect as a non-owner, non-superuser role, because superusers bypass RLS.
