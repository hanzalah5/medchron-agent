.PHONY: install lint format typecheck test check

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
