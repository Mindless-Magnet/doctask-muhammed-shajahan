"""The two claims the brief singles out: a run killed and resumed, and two runs at once.

Both are tested against the real graph and the real checkpointer. The model is stubbed; the
durability and concurrency machinery is not.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections import Counter

import pytest
from sqlalchemy import select

import ledgerline.db as db_module
from ledgerline.models import ItemKind, PendingItem, RegisterField, Run, RunStatus
from ledgerline.service import (
    RegisterVersionConflict,
    claim_next_run,
    commit_approved,
    decide_items,
    execute_run,
    requeue_stale_runs,
    resume_strategy,
)
from tests.conftest import INVOICE_TEXT, MSA_TEXT, Corpus


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


# --------------------------------------------------------------------------------------------
# Resume
# --------------------------------------------------------------------------------------------


@pytest.mark.timeout(90)
def test_a_killed_run_resumes_without_losing_or_repeating_work(env, corpus, tmp_path):
    _seed(corpus)
    run_id = corpus.queue_run()

    child = subprocess.run(
        [
            sys.executable,
            "-m",
            "tests.kill_child",
            env.database_url,
            str(env.fixtures_dir),
            run_id,
            "extract",
        ],
        cwd=os.getcwd(),
        env={**os.environ, "PYTHONPATH": "src"},
        capture_output=True,
        timeout=60,
    )
    assert child.returncode == -9, (
        f"child did not die by SIGKILL (rc={child.returncode}); "
        f"stderr: {child.stderr.decode()[-800:]}"
    )

    # Nothing reached the gate: the process died before it got there.
    with db_module.session_scope() as session:
        assert session.scalars(
            select(PendingItem).where(PendingItem.pile_id == corpus.pile_id)
        ).all() == []

    outcome = execute_run(run_id)

    stages = [entry["stage"] for entry in outcome.stage_log]
    counts = Counter(stages)

    # Every stage appears exactly once across both processes. A stage running twice would show
    # here, because the log accumulates and the checkpointer is the only thing preventing it.
    assert counts["intake"] == 1, f"intake ran {counts['intake']} times: {stages}"
    assert counts["classify"] == 1, f"classify ran {counts['classify']} times: {stages}"
    assert counts["extract"] == 1, f"extract ran {counts['extract']} times: {stages}"
    assert stages[-1] == "gate"

    assert session_items(corpus.pile_id, ItemKind.update), "the resumed run produced no work"


@pytest.mark.timeout(90)
def test_a_resumed_run_reaches_the_same_result_as_an_uninterrupted_one(env, corpus, tmp_path):
    _seed(corpus)
    killed_run = corpus.queue_run()
    subprocess.run(
        [sys.executable, "-m", "tests.kill_child", env.database_url,
         str(env.fixtures_dir), killed_run, "classify"],
        cwd=os.getcwd(), env={**os.environ, "PYTHONPATH": "src"},
        capture_output=True, timeout=60,
    )
    execute_run(killed_run)
    resumed = {
        (item.kind, item.field_path, item.dedupe_key)
        for item in session_items(corpus.pile_id)
    }

    # Same corpus, same fixtures, fresh pile, no interruption.
    clean = Corpus(env, tmp_path, name="northwind-clean")
    _seed(clean)
    execute_run(clean.queue_run())
    uninterrupted = {
        (item.kind, item.field_path, item.dedupe_key)
        for item in session_items(clean.pile_id)
    }

    assert resumed == uninterrupted


# --------------------------------------------------------------------------------------------
# Resume decision
# --------------------------------------------------------------------------------------------

# No database, no subprocess: unit-level coverage for the exact defect the two tests above found
# only intermittently (one run in several dozen, under a real SIGKILL). Instrumenting a real kill
# to land at the SQLite checkpointer's specific internal commit boundary is exactly as fragile as
# it sounds; testing the decision `execute_run` makes from a snapshot shape is not, and it is the
# decision that was wrong. See `resume_strategy`'s docstring for how that shape was found: a real
# resumed run whose checkpoint showed `classified` genuinely populated but `next` empty, produced
# by running the kill/resume test in a loop until one failed with zero pending items instead of
# the expected set.


def test_progress_with_nothing_scheduled_and_no_gate_restarts_not_silently_finishes():
    """The corrupted shape itself: real work landed (`classified` populated, `stage_log` has two
    entries) but `next` is empty and the run never reached the gate. Before the fix this read as
    "already_complete"; the correct read is "this checkpoint cannot be trusted, restart"."""
    values = {
        "classified": {"doc-1": {"doc_type": "msa", "confidence": 0.9}},
        "stage_log": [{"stage": "intake"}, {"stage": "classify"}],
    }
    assert resume_strategy(values, ()) == "fresh"


def test_progress_with_a_scheduled_task_resumes():
    values = {"classified": {"doc-1": {}}, "stage_log": [{"stage": "intake"}, {"stage": "classify"}]}
    assert resume_strategy(values, ("extract",)) == "resumed"


def test_no_prior_state_is_fresh():
    assert resume_strategy({}, ()) == "fresh"


def test_a_stage_log_reaching_the_gate_with_nothing_scheduled_is_already_complete():
    values = {"stage_log": [{"stage": "intake"}, {"stage": "gate"}]}
    assert resume_strategy(values, ()) == "already_complete"


# --------------------------------------------------------------------------------------------
# Concurrency
# --------------------------------------------------------------------------------------------


def session_items(pile_id: str, kind: ItemKind | None = None) -> list[PendingItem]:
    with db_module.session_scope() as session:
        statement = select(PendingItem).where(PendingItem.pile_id == pile_id)
        if kind is not None:
            statement = statement.where(PendingItem.kind == kind)
        return list(session.scalars(statement))


def test_two_workers_never_take_the_same_run(env, corpus, tmp_path):
    _seed(corpus)
    first = corpus.queue_run()
    second = corpus.queue_run()

    with db_module.session_scope() as session_a:
        claimed_a = claim_next_run(session_a, worker="worker-a")
    with db_module.session_scope() as session_b:
        claimed_b = claim_next_run(session_b, worker="worker-b")

    assert claimed_a is not None and claimed_b is not None
    assert claimed_a.id != claimed_b.id
    assert {claimed_a.id, claimed_b.id} == {first, second}

    with db_module.session_scope() as session:
        assert claim_next_run(session, worker="worker-c") is None


def test_two_runs_on_one_pile_stay_two_runs(env, corpus, tmp_path):
    """Both runs execute fully. The register is protected at the commit, not by serialising work."""
    _seed(corpus)
    run_a = corpus.queue_run()
    run_b = corpus.queue_run()

    outcome_a = execute_run(run_a)
    outcome_b = execute_run(run_b)

    assert outcome_a.status == RunStatus.awaiting_approval
    assert outcome_b.status == RunStatus.awaiting_approval

    # The same proposals arriving twice do not become duplicate items: the second run recognises
    # them by dedupe key and adds nothing. Two runs, one set of decisions for the reviewer.
    items = session_items(corpus.pile_id, ItemKind.update)
    keys = [item.dedupe_key for item in items]
    assert len(keys) == len(set(keys)), "the same proposal was queued twice"

    run_ids = {item.run_id for item in session_items(corpus.pile_id)}
    assert run_ids == {run_a}, "the second run re-raised work the first had already presented"


def test_a_second_committer_does_not_clobber_the_first(env, corpus, tmp_path):
    _seed(corpus)
    run_a = corpus.queue_run()
    execute_run(run_a)

    updates = session_items(corpus.pile_id, ItemKind.update)
    with db_module.session_scope() as session:
        decide_items(session, [item.id for item in updates], approve=True, decided_by="reviewer")

    # Both committers read version 0. The first wins; the second is refused and writes nothing.
    with db_module.session_scope() as session:
        result = commit_approved(session, corpus.pile_id, run_a, expected_version=0)
    assert result["register_version"] == 1

    with db_module.session_scope() as session:
        before = session.scalars(
            select(RegisterField).where(RegisterField.pile_id == corpus.pile_id)
        ).all()
        before_hashes = {row.field_path: row.value_sha256 for row in before}

    with db_module.session_scope() as session, pytest.raises(RegisterVersionConflict):
        commit_approved(session, corpus.pile_id, run_a, expected_version=0)

    with db_module.session_scope() as session:
        after = session.scalars(
            select(RegisterField).where(RegisterField.pile_id == corpus.pile_id)
        ).all()
        after_hashes = {row.field_path: row.value_sha256 for row in after}

    assert before_hashes == after_hashes, "a refused commit still moved the register"


def test_stale_running_runs_are_requeued(env, corpus, tmp_path):
    _seed(corpus)
    run_id = corpus.queue_run()
    with db_module.session_scope() as session:
        run = session.get(Run, run_id)
        run.status = RunStatus.running
        run.worker_id = "worker-that-died"
        run.heartbeat_at = run.created_at.replace(year=run.created_at.year - 1)
        session.commit()

    with db_module.session_scope() as session:
        assert requeue_stale_runs(session, older_than_seconds=30) == 1

    with db_module.session_scope() as session:
        claimed = claim_next_run(session, worker="worker-b")
    assert claimed is not None and claimed.id == run_id
    assert claimed.attempt == 1
