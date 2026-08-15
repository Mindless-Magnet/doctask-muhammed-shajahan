# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project layout

The actual project lives in `ledgerline/`, not the repo root. `cd ledgerline` before running any
command below.

## Commands

```bash
cd ledgerline
make up          # docker compose: Postgres+pgvector, API, 2 workers; seeds a pile
make down        # tear down, including the volume
make test        # pytest -q — 80 tests, offline via replay fixtures, no AWS account/network
make test-pg     # adds 4 Postgres-dialect concurrency tests; needs `docker compose up db` first
make lint        # ruff check src tests

pytest tests/test_foundation.py::test_name -q   # run a single test

ledgerline init-db                              # create schema (app tables + langgraph checkpointer), idempotent
ledgerline seed --pile <name> [--variant a|b]   # generate + load a synthetic vendor corpus
ledgerline run <pile_id>                        # queue and execute a run synchronously (demo/debug only)
ledgerline worker                               # claim loop, run continuously
ledgerline watch --pile <name> --directory ./inbox   # poll a folder for new documents
```

Configuration is env vars prefixed `LEDGERLINE_` (or `.env`, see `config.py`).
`LEDGERLINE_MODEL_CLIENT` selects the model backend: `replay` (default, what tests use, no
network), `record` (live call + write a fixture), `bedrock` (live, no recording), `faulty` (fault
injection for degradation tests).

## Architecture

One LangGraph state machine is the whole pipeline. Read the docstring at the top of
`agent/graph.py` first — it documents every conditional edge and why it exists
(classify→extract-or-gate, extract→retry-or-reconcile, degradation routing through `examine`
rather than failing the run). From there:

- **`agent/state.py`** — `RunState`/`GraphState` (the checkpointed, JSON-serialisable graph state)
  and `FIELD_SETS`/`ALLOWED_PATTERNS`, the write-allowlist that is the third of three prompt
  injection defences: a document type may only ever write the field paths it owns.
- **`agent/stages.py`** — the node implementations (classify, extract, reconcile, examine, gate).
- **`service.py`** — run lifecycle outside the graph itself: claiming (`FOR UPDATE SKIP LOCKED` so
  two workers never take the same run), `execute_run` (resume semantics live here — passing `None`
  vs. a fresh state dict to the graph is the entire difference between resuming and replaying, see
  the file's docstring), and `commit_approved` (optimistic concurrency on `Pile.register_version`;
  only the commit critical section serialises, not the run).
- **`worker.py`** — claims and executes runs in a loop, a separate process from the API, so a
  crash mid-run is a real crash to recover from and no HTTP request blocks on a model call.
- **`watcher.py`** — polls a directory, content-addresses documents by sha256 (re-dropping the same
  bytes is a no-op), waits for file-size stability across two polls before ingesting, then queues a
  run scoped to just the new document(s).
- **`api/app.py`** (HTTP) and **`mcp/server.py`** (MCP) are both thin wrappers over the same
  functions in `service.py`, including approval — add new behaviour to `service.py`, never to
  either surface directly, or the two will drift. Auth is a single bearer token; no multi-tenancy.
- **`llm/client.py`** — one `Protocol`, four implementations: `BedrockClient` (real),
  `RecordingClient` (wraps real, writes fixtures), `ReplayClient` (fixtures only, what the whole
  test suite runs on), `FaultyClient` (injects throttles/errors). Fixtures in
  `tests/fixtures/llm/` are keyed by a hash of the full request, so a changed prompt misses the
  cache loudly instead of silently replaying a stale answer. `TieredModelClient` layers on tier
  selection, fallback chains (`config.py: model_fallbacks`), retry, and cost accounting
  (`llm/pricing.py`).
- **`rules/engine.py`** + **`rules/playbook.yaml`** — deterministic checks that run before any
  model call. A new rule or threshold is a YAML edit, not a code change.
- **`verification.py`** — grades every field EXACT / NUMERIC / STRUCTURAL / JUDGED. The judge tier
  (`model_deep`) is deliberately a different model family from the extractor and has no fallback —
  losing model independence is worse than losing the tier, so it degrades to deterministic-only
  instead.
- **`security/injection.py`** — pattern-based detection (defence layer 2; layer 1 is structural
  prompt framing in `prompting.py`, source text only ever appears in the user position; layer 3 is
  the write-allowlist above).
- **`register/reconcile.py`** — builds the per-run proof that fields the new source didn't touch
  are byte-identical, backed by `RegisterField.inputs_hash` in `models.py` (hash of every span a
  field cites plus prompt/playbook version — if it hasn't moved, the field isn't recomputed).
- **`corpus/generate.py`** — synthetic vendor-document generator behind `ledgerline seed`; no real
  vendor or client data anywhere in this repo.

## Testing

The full suite runs offline against recorded fixtures in `tests/fixtures/llm/` — no AWS account or
network needed for `make test`. Read `tests/conftest.py` before adding a test that needs a new
model call recorded; it explains how fixtures are hashed and matched against the real request the
stage builds. Tests marked `live` (requires real Bedrock credentials) are excluded from the default
run (`pyproject.toml`: `addopts = "-m 'not live'"`).

`README.md` has a table mapping each of the ten required system behaviours to its implementation
file and the test that proves it — check there before assuming a behaviour is untested.

## Also read

`README.md` covers design rationale not repeated here: why sync SQLAlchemy over async, why no job
broker, why verification is graded rather than binary, how graceful degradation works, the full
three-layer injection defence, and a section on known weaknesses in this build. Read it before
making a change that touches any of those areas.
