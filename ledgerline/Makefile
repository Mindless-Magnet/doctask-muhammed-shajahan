.PHONY: up down test test-pg lint logs web

# The one documented command: clone, then this. Postgres, schema, API, two workers, the review
# interface, and a synthetic corpus loaded and ready. Safe to run again: seeding reuses an existing
# pile and skips documents already present, because a setup command that only works once is a setup
# command that does not work.
up:
	docker compose up -d --build
	docker compose run --rm migrate ledgerline seed --pile northwind
	@echo "Review interface on http://localhost:5173"
	@echo "API docs on        http://localhost:8000/docs"

down:
	docker compose down -v

# No AWS account, no key, no network.
test:
	pytest -q

# Adds the dialect-specific concurrency tests. Needs the compose database up.
test-pg:
	LEDGERLINE_TEST_POSTGRES=postgresql+psycopg://ledgerline:ledgerline@localhost:5432/ledgerline pytest -q

# The review interface with hot reload, against an API you are already running.
web:
	cd web && npm install && npm run dev

# Record the demo run once, against real Bedrock, so that everyone afterwards can replay it with
# no AWS account and no spend. Run this yourself, commit tests/fixtures/llm/, and `make up` then
# works for anyone who clones the repository.
#
# Needs credentials. Everything else in this Makefile does not.
demo-fixtures:
	docker compose up -d db
	LEDGERLINE_MODEL_CLIENT=record ledgerline init-db
	LEDGERLINE_MODEL_CLIENT=record ledgerline seed --pile fixture-corpus
	LEDGERLINE_MODEL_CLIENT=record ledgerline run-latest --pile fixture-corpus
	@echo
	@echo "Recorded $$(ls tests/fixtures/llm | wc -l) responses. Commit tests/fixtures/llm/."

lint:
	ruff check src tests

logs:
	docker compose logs -f worker
