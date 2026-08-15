.PHONY: up down test test-pg lint demo logs

# The one documented command: clone, then this. Brings up Postgres, migrates, starts the API and
# two workers, generates a synthetic corpus and runs it end to end.
up:
	docker compose up -d --build
	docker compose run --rm migrate ledgerline seed --pile northwind
	@echo "API on http://localhost:8000/docs"

down:
	docker compose down -v

# No AWS account, no key, no network.
test:
	pytest -q

# Adds the dialect-specific concurrency tests. Needs the compose database up.
test-pg:
	LEDGERLINE_TEST_POSTGRES=postgresql+psycopg://ledgerline:ledgerline@localhost:5432/ledgerline pytest -q

lint:
	ruff check src tests

logs:
	docker compose logs -f worker
