.PHONY: install lint format typecheck test check synth smoke-data db-up migrate eval-selfcheck eval-live eval-report calib-seed calib-selfcheck

install:  ## Create .venv and install the package with dev extras
	uv venv --python 3.12 --allow-existing .venv || uv venv --allow-existing .venv
	uv pip install --python .venv -e ".[dev]"

lint:
	.venv/bin/ruff check .
	.venv/bin/ruff format --check .

format:
	.venv/bin/ruff check --fix .
	.venv/bin/ruff format .

typecheck:
	.venv/bin/mypy

test:
	.venv/bin/pytest

check: lint typecheck test  ## Everything CI runs

synth:  ## Generate N synthetic cases into data/generated (make synth CASES=20)
	.venv/bin/medchron synth --cases $(or $(CASES),5) --out data/generated

smoke-data:  ## Regenerate the committed smoke set
	.venv/bin/medchron synth --cases 5 --start-seed 1 --out data/synthetic

db-up:  ## Start Postgres + pgvector and apply migrations
	docker compose up -d --wait db
	.venv/bin/alembic upgrade head

migrate:
	.venv/bin/alembic upgrade head

eval-selfcheck:  ## Harness self-check with the gold-label oracle (no API key, not model results)
	.venv/bin/medchron eval --label oracle-selfcheck --provider oracle
	.venv/bin/medchron eval --label oracle-noise-0.3 --provider oracle --oracle-noise 0.3

eval-live:  ## Live evaluation with Claude on the smoke set (needs ANTHROPIC_API_KEY; capped per case)
	.venv/bin/medchron eval --label live-$(or $(LABEL),smoke) --provider anthropic --budget-per-case $(or $(BUDGET),1.00)

eval-report:  ## Regenerate evals/REPORT.md from all results (newest first)
	.venv/bin/medchron eval-report $(shell ls -t evals/results/*.json)

calib-seed:  ## Rebuild the gold-derived verifier calibration seed set (labels by construction)
	.venv/bin/medchron calib seed --data data/synthetic --out evals/calibration/gold-seed.jsonl

calib-selfcheck:  ## Score the oracle judge on the seed set (tooling self-check, not calibration)
	.venv/bin/medchron calib judge evals/calibration/gold-seed.jsonl --provider oracle --out runs/calib/oracle-judged.jsonl
	.venv/bin/medchron calib score runs/calib/oracle-judged.jsonl --out evals/calibration/selfcheck.md
