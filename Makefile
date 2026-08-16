# Thin wrappers over the commands in README section 3. Nothing here is load-bearing:
# every target is a one-liner you can run by hand.

SHELL := /bin/bash
VENV  := .venv
PY    := $(VENV)/bin/python
PIP   := $(VENV)/bin/pip
CLI   := $(VENV)/bin/aker-etl
COMPOSE := docker compose -f docker/docker-compose.yml --env-file .env

.DEFAULT_GOAL := help
.PHONY: help venv install db-up db-down db-logs init-db load load-dry validate \
        validate-strict status serve insights insights-dry insights-show \
        insights-json insights-import \
        export-json reset test test-unit test-integration lint typecheck check clean

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# --- environment -----------------------------------------------------------

$(VENV): ## Create the virtualenv
	python3 -m venv $(VENV)

venv: $(VENV) ## Create the virtualenv

install: venv ## Install the package plus dev extras (editable)
	$(PIP) install -e '.[dev]'

# --- database --------------------------------------------------------------

db-up: ## Start Postgres (host port from POSTGRES_PORT in .env, default 5434)
	$(COMPOSE) up -d

db-down: ## Stop Postgres
	$(COMPOSE) down

db-logs: ## Follow the Postgres log
	$(COMPOSE) logs -f

init-db: ## Apply sql/*.sql in order (idempotent)
	$(CLI) init-db

# --- ETL -------------------------------------------------------------------

load: ## Load the corpus (skips files whose SHA-256 is unchanged)
	$(CLI) load

load-force: ## Reload every file, ignoring the SHA-256 skip
	$(CLI) load --force

load-dry: ## Parse and reconcile without writing
	$(CLI) load --dry-run

validate: ## Re-run the validation rules against the latest run
	$(CLI) validate

validate-strict: ## Same, but exit non-zero on warnings (see REMAINING.md section 4.1)
	$(CLI) validate --strict

status: ## Recent runs plus the golden-number check
	$(CLI) status

export-json: ## Dump the analytical payload to payload.json
	$(CLI) export-json payload.json

reset: ## TRUNCATE core.* and raw.* -- destructive, asks for --yes explicitly
	$(CLI) reset --yes

# --- dashboard -------------------------------------------------------------

serve: ## Dashboard on http://127.0.0.1:8000
	$(CLI) serve

# --- insight layer (needs Ollama) ------------------------------------------

insights: ## Generate insights for the latest snapshot
	$(CLI) insights generate --force

insights-dry: ## Build and cost the context payload without calling the model
	$(CLI) insights generate --dry-run

insights-show: ## Print the stored insights
	$(CLI) insights show

insights-json: ## Generate insights to insights.json instead of the database
	$(CLI) insights generate --out insights.json

insights-import: ## Verify insights.json against the database and store it
	$(CLI) insights import insights.json

# --- quality ---------------------------------------------------------------

test-unit: ## Unit tests only -- no services needed
	$(VENV)/bin/pytest -m "not integration"

test-integration: ## Integration tests -- TRUNCATEs the database, see REMAINING.md section 4.4
	$(VENV)/bin/pytest -m integration

test: ## Full suite
	$(VENV)/bin/pytest

lint: ## ruff
	$(VENV)/bin/ruff check src tests

typecheck: ## mypy
	$(VENV)/bin/mypy src

check: lint typecheck test-unit ## Everything that needs no running services

clean: ## Remove caches and build artefacts
	rm -rf .pytest_cache .ruff_cache .mypy_cache build dist src/*.egg-info
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
