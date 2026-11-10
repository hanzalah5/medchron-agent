#!/usr/bin/env bash
# Start a fresh API + worker for the Playwright end-to-end test.
# Needs E2E_DATABASE_URL (Postgres + pgvector) and a built UI in ui/dist.
set -euo pipefail
cd "$(dirname "$0")/.."
export DATABASE_URL="${E2E_DATABASE_URL:?set E2E_DATABASE_URL}"
export MEDCHRON_STORAGE_DIR="${MEDCHRON_STORAGE_DIR:-runs/e2e/storage}"
export MEDCHRON_RUNS_DIR="${MEDCHRON_RUNS_DIR:-runs/e2e/runs}"
export MEDCHRON_UI_DIST="ui/dist"
.venv/bin/alembic downgrade base >/dev/null
.venv/bin/alembic upgrade head >/dev/null
# The API and worker connect as non-owner roles, so row-level security applies to them.
eval "$(.venv/bin/medchron db-users --shell)"
DATABASE_URL="$MEDCHRON_WORKER_DATABASE_URL" .venv/bin/medchron worker &
WORKER=$!
trap 'kill $WORKER 2>/dev/null || true' EXIT
DATABASE_URL="$MEDCHRON_API_DATABASE_URL" .venv/bin/medchron serve --port "${PORT:-8765}"
