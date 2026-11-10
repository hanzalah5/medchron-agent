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

## Update (v1.1): the services connect as non-owner roles
Postgres does not apply row-level security to superusers, and a table owner can disable it. A plain setting (`medchron.system`) also enabled the system bypass, so any connection able to run SQL could switch it on. Migration 0008 closes both gaps:
- The API connects as `medchron_api` and the worker as `medchron_worker`. Neither is a superuser or owner, and neither has `BYPASSRLS`. Both belong to `medchron_app`, which may append to the audit log but not update or delete it.
- The bypass now also requires membership in `medchron_system`, which only the worker role and the owner have. The API role cannot use it even through direct SQL.
- `medchron_matter_exists`, which answers "does this ID exist?" for the API's 409, is a `SECURITY DEFINER` function, the one sanctioned bypass.

Compose, CI and the end-to-end tests run this way. Tests connect as the real roles and check the following:
- a non-member sees nothing, even after `set_config('medchron.system', 'on')`
- the API role cannot disable RLS, drop a policy, disable the audit trigger, or update or delete audit rows
- the whole API flow works as those roles

The MCP server (ADR-0011) uses the same path.
