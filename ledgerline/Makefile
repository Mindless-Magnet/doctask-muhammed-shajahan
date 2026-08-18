# Every target that runs Python uses the virtualenv binary explicitly rather than assuming PATH.
# make invokes /bin/sh, which never inherits a shell's activated venv — and on fish, which has its
# own activation syntax, it never could. Bare `pytest` here works only for whoever happened to
# activate first, which is the definition of a setup step that works on one machine.
VENV ?= .venv/bin
PY   ?= $(VENV)/python
DB   ?= postgresql+psycopg://ledgerline:ledgerline@localhost:5432/ledgerline

.PHONY: up down venv test test-pg lint logs web demo-fixtures

# The one documented command: clone, then this. Postgres, schema, API, two workers, the review
# interface, and a synthetic corpus loaded and ready. Safe to run again: seeding reuses an existing
# pile and skips documents already present, because a setup command that only works once is a setup
# command that does not work.
#
# Needs Docker and nothing else. No Python environment, no AWS account.
up:
	docker compose up -d --build
	docker compose run --rm migrate ledgerline seed --pile northwind
	@echo
	@echo "  Review interface  http://localhost:5173"
	@echo "  API docs          http://localhost:8000/docs"
	@echo

down:
	docker compose down -v

# Only needed to run the tests or record fixtures locally; `make up` does not use it.
venv:
	python -m venv .venv
	$(VENV)/pip install -q -e ".[dev]"
	@echo "Environment ready. 'make test' will use it."

# No AWS account, no key, no network.
test: | .venv
	$(VENV)/pytest -q

# Adds the dialect-specific concurrency tests. Needs the compose database up: `docker compose up -d db`.
test-pg: | .venv
	LEDGERLINE_TEST_POSTGRES=$(DB) $(VENV)/pytest -q

lint: | .venv
	$(VENV)/ruff check src tests

# The review interface with hot reload, against an API you are already running.
web:
	cd web && npm install && npm run dev

logs:
	docker compose logs -f worker

# Record the demo run once, against real Bedrock, so everyone afterwards can replay it with no AWS
# account and no spend. Run this yourself, commit tests/fixtures/llm/, and `make up` then works for
# anyone who clones the repository.
#
# The database URL is passed explicitly because .env points at `db:5432`, which only resolves inside
# a container; this runs on the host.
#
# Needs credentials. Nothing else in this Makefile does.
demo-fixtures: | .venv
	docker compose up -d db
	@sleep 2
	LEDGERLINE_MODEL_CLIENT=record LEDGERLINE_DATABASE_URL=$(DB) $(VENV)/ledgerline init-db
	LEDGERLINE_MODEL_CLIENT=record LEDGERLINE_DATABASE_URL=$(DB) $(VENV)/ledgerline seed --pile fixture-corpus
	LEDGERLINE_MODEL_CLIENT=record LEDGERLINE_DATABASE_URL=$(DB) $(VENV)/ledgerline run-latest --pile fixture-corpus
	@echo
	@echo "Recorded $$(ls tests/fixtures/llm | wc -l) responses. Commit tests/fixtures/llm/."

# Created by `make venv`. Declared so the targets above fail with a useful message rather than a
# bare "no such file" if it is missing.
.venv:
	@echo "No virtualenv. Run: make venv" && exit 1
