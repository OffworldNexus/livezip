# Developer entry points for the livezip project. Everything Python runs
# through uv so that the same interpreter and lockfile are used locally and in
# CI; the only requirement on the host is a working `uv`.

.PHONY: help sync format lint typecheck test test-unit test-e2e coverage docs docs-serve build clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-18s\033[0m %s\n", $$1, $$2}'

sync: ## Install every dependency (including the s3 extra) into .venv
	uv sync --all-extras

format: ## Auto-fix imports and formatting (ruff)
	uv run ruff check --fix --select I .
	uv run ruff format .

lint: typecheck ## Check lint, formatting and types without touching the tree
	uv run ruff check .
	uv run ruff format --check .

typecheck: ## Type-check the package with mypy
	uv run mypy

test: ## Run the whole suite (unit + e2e; e2e needs Docker)
	uv run pytest

test-unit: ## Run only the fast unit tests
	uv run pytest -m "not e2e"

test-e2e: ## Run only the SeaweedFS-backed end-to-end tests
	uv run pytest -m e2e

coverage: ## Run the suite with a coverage report
	uv run pytest --cov --cov-report=term-missing

docs: ## Build the documentation site into doc/site
	cd doc && uv run zensical build

docs-serve: ## Serve the documentation on http://localhost:8000
	cd doc && uv run zensical serve

build: ## Build the sdist and wheel into dist/
	uv build

clean: format lint ## Format then lint (leaves the tree ready to commit)
