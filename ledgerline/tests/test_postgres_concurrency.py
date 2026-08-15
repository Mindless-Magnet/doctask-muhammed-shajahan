"""Concurrency against real Postgres.

The SQLite suite proves the optimistic version check. It cannot prove `FOR UPDATE SKIP LOCKED`,
because SQLite has neither. Claiming the behaviour without testing it on the dialect that
implements it would be an assertion, not proof, so these tests exist and they skip loudly rather
than silently when Postgres is absent.

Threads, not sequential calls. Two workers racing for one queued run, and two committers racing for
one register, are the two ways this can actually corrupt state.

Run with:
    LEDGERLINE_TEST_POSTGRES=postgresql+psycopg://ledgerline:ledgerline@localhost:5432/ledgerline pytest
docker compose sets this for you.
"""

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select, text

import ledgerline.db as db_module
from ledgerline.agent.graph import bootstrap
from ledgerline.config import get_settings
from ledgerline.models import Base, ItemKind, PendingItem, Pile, RegisterField, RunStatus
from ledgerline.service import (
    RegisterVersionConflict,
    claim_next_run,
    commit_approved,
    decide_items,
    execute_run,
)
from tests.conftest import INVOICE_TEXT, MSA_TEXT, Corpus

POSTGRES_URL = os.environ.get("LEDGERLINE_TEST_POSTGRES")

pytestmark = pytest.mark.skipif(
    not POSTGRES_URL,
    reason="set LEDGERLINE_TEST_POSTGRES to run the dialect-specific concurrency tests",
)


@pytest.fixture
def pg_env(tmp_path, monkeypatch):
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    monkeypatch.setenv("LEDGERLINE_DATABASE_URL", POSTGRES_URL)
    monkeypatch.setenv("LEDGERLINE_FIXTURES_DIR", str(fixtures))
    monkeypatch.setenv("LEDGERLINE_PLAYBOOK_PATH", "src/ledgerline/rules/playbook.yaml")
    monkeypatch.setenv("LEDGERLINE_MODEL_CLIENT", "replay")

    get_settings.cache_clear()
    db_module._engine = None
    db_module._factory = None

    engine = db_module.engine()
    Base.metadata.drop_all(engine)
    with engine.begin() as connection:
        for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes", "checkpoint_migrations"):
            connection.execute(text(f"DROP TABLE IF EXISTS {table} CASCADE"))

    settings = get_settings()
    bootstrap(settings.database_url)
    assert settings.database_url.startswith("postgresql"), "these tests require Postgres"
    yield settings

    get_settings.cache_clear()
    db_module._engine = None
    db_module._factory = None


def _seed(corpus: Corpus) -> None:
    msa_id = corpus.add("msa.txt", MSA_TEXT, "msa")
    invoice_id = corpus.add("inv_2205.txt", INVOICE_TEXT, "invoice")
    corpus.seed_extract(
        msa_id,
        "msa",
        [
            ("contract.parties.client", "Harborview Foods", "Harborview Foods"),
            ("contract.payment_terms.net_days", 60, "net 60 days"),
            ("contract.liability_cap.amount", 100000, "USD 100,000"),
            ("contract.renewal.type", "auto", "renews automatically"),
            ("contract.renewal.notice_days", 30, "30 days notice"),
        ],
    )
    corpus.seed_extract(
        invoice_id,
        "invoice",
        [
            ("invoices.INV-2205.unit_price", 160.0, "160.00"),
            ("invoices.INV-2205.date", "2025-08-14", "2025-08-14"),
            ("invoices.INV-2205.po_ref", "PO-1042", "PO-1042"),
        ],
    )


def test_the_dialect_is_actually_postgres(pg_env):
    with db_module.session_scope() as session:
        assert session.bind.dialect.name == "postgresql"
        version = session.execute(text("select version()")).scalar_one()
    assert "PostgreSQL" in version


def test_racing_workers_never_take_the_same_run(pg_env, tmp_path):
    """Ten workers, five runs, all claiming at once. SKIP LOCKED means nobody blocks and nobody
    double-claims. Without it this either deadlocks or hands the same run to two workers."""
    corpus = Corpus(pg_env, tmp_path, name="race-pile")
    _seed(corpus)
    expected = {corpus.queue_run() for _ in range(5)}

    barrier = threading.Barrier(10)
    claimed: list[str | None] = []
    lock = threading.Lock()

    def worker(index: int) -> None:
        barrier.wait()
        with db_module.session_scope() as session:
            run = claim_next_run(session, worker=f"worker-{index}")
            with lock:
                claimed.append(None if run is None else run.id)

    with ThreadPoolExecutor(max_workers=10) as pool:
        list(pool.map(worker, range(10)))

    taken = [run_id for run_id in claimed if run_id is not None]
    assert len(taken) == 5, f"expected 5 claims, got {len(taken)}"
    assert len(set(taken)) == 5, "the same run was claimed twice"
    assert set(taken) == expected


def test_racing_commits_produce_one_winner_and_no_lost_update(pg_env, tmp_path):
    """Both committers read version 0 and commit at the same instant. One wins, one is refused,
    and the register reflects exactly one of them. A lost update would show as a version that
    moved twice or as fields from a commit that was told it failed."""
    corpus = Corpus(pg_env, tmp_path, name="commit-race-pile")
    _seed(corpus)
    run_id = corpus.queue_run()
    execute_run(run_id)

    with db_module.session_scope() as session:
        items = session.scalars(
            select(PendingItem).where(
                PendingItem.pile_id == corpus.pile_id, PendingItem.kind == ItemKind.update
            )
        ).all()
        item_ids = [item.id for item in items]
        decide_items(session, item_ids, approve=True, decided_by="reviewer")

    barrier = threading.Barrier(2)
    outcomes: list[str] = []
    lock = threading.Lock()

    def committer(_: int) -> None:
        barrier.wait()
        try:
            with db_module.session_scope() as session:
                commit_approved(session, corpus.pile_id, run_id, expected_version=0)
            result = "committed"
        except RegisterVersionConflict:
            result = "refused"
        except Exception as exc:  # surfaced rather than swallowed
            result = f"error:{type(exc).__name__}"
        with lock:
            outcomes.append(result)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(committer, range(2)))

    assert sorted(outcomes) == ["committed", "refused"], outcomes

    with db_module.session_scope() as session:
        pile = session.get(Pile, corpus.pile_id)
        fields = session.scalars(
            select(RegisterField).where(RegisterField.pile_id == corpus.pile_id)
        ).all()

    assert pile.register_version == 1, "the register moved more than once"
    assert len(fields) == len(item_ids)
    assert all(row.version == 1 for row in fields), "a field was written twice"


def test_two_runs_on_one_pile_execute_in_parallel(pg_env, tmp_path):
    """Runs are not serialised. Only the commit is. Both reach the gate."""
    corpus = Corpus(pg_env, tmp_path, name="parallel-pile")
    _seed(corpus)
    run_a = corpus.queue_run()
    run_b = corpus.queue_run()

    barrier = threading.Barrier(2)
    results: list[str] = []
    lock = threading.Lock()

    def execute(run_id: str) -> None:
        barrier.wait()
        outcome = execute_run(run_id)
        with lock:
            results.append(outcome.status.value)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(execute, [run_a, run_b]))

    assert results == [RunStatus.awaiting_approval.value] * 2

    with db_module.session_scope() as session:
        items = session.scalars(
            select(PendingItem).where(PendingItem.pile_id == corpus.pile_id)
        ).all()
    keys = [item.dedupe_key for item in items]
    assert len(keys) == len(set(keys)), "concurrent runs queued the same proposal twice"
