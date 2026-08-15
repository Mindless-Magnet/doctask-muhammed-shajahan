"""Run lifecycle and the commit critical section.

Two concurrency mechanisms, doing different jobs.

Claiming uses `FOR UPDATE SKIP LOCKED` so two workers never take the same run and neither blocks
waiting for the other. Runs execute genuinely in parallel.

Committing uses an optimistic check on `Pile.register_version`. Only the commit serialises, not the
run. A committer whose version has moved under it does not clobber and does not silently retry: it
rebases, re-presents the items that now conflict, and returns without committing them. Approvals a
human already made on unaffected items still land.

Resume works because the graph checkpoints after every node and the worker requeues any run whose
heartbeat has gone stale. A killed worker loses at most the node in flight.
"""

from __future__ import annotations

import hashlib
import os
import socket
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ledgerline.agent.graph import build_graph, checkpointer_for
from ledgerline.agent.stages import StageContext, load_snapshots
from ledgerline.agent.state import empty_state
from ledgerline.config import Settings, get_settings
from ledgerline.db import session_factory
from ledgerline.llm.client import TieredModelClient, build_client
from ledgerline.models import (
    FieldStatus,
    ItemKind,
    ItemStatus,
    LedgerEntry,
    PendingItem,
    Pile,
    RegisterField,
    Run,
    RunStatus,
    SourceDocument,
    UntouchedProof,
)
from ledgerline.register.reconcile import FieldSnapshot, build_proof
from ledgerline.rules.engine import load_playbook
from ledgerline.schemas import sha256_json

STALE_HEARTBEAT_SECONDS = 60


def worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def _now() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------------------------
# Claiming
# --------------------------------------------------------------------------------------------


def claim_next_run(session: Session, *, worker: str) -> Run | None:
    """Take one queued run. SKIP LOCKED means a second worker takes a different one immediately."""
    statement = (
        select(Run)
        .where(Run.status == RunStatus.queued)
        .order_by(Run.created_at)
        .limit(1)
    )
    if session.bind.dialect.name == "postgresql":
        statement = statement.with_for_update(skip_locked=True)

    run = session.scalars(statement).first()
    if run is None:
        return None
    run.status = RunStatus.running
    run.worker_id = worker
    run.heartbeat_at = _now()
    run.attempt += 1
    session.commit()
    return run


def requeue_stale_runs(session: Session, *, older_than_seconds: int = STALE_HEARTBEAT_SECONDS) -> int:
    """A worker that died mid-run leaves its run in `running` with a frozen heartbeat.

    Requeueing is safe precisely because the graph checkpoints: the next worker resumes from the
    last completed node rather than starting over, so no finished work is lost and no completed
    stage runs twice.
    """
    cutoff = _now() - timedelta(seconds=older_than_seconds)
    result = session.execute(
        update(Run)
        .where(Run.status == RunStatus.running, Run.heartbeat_at < cutoff)
        .values(status=RunStatus.queued, worker_id=None)
    )
    session.commit()
    return result.rowcount or 0


# --------------------------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------------------------


@dataclass
class RunOutcome:
    run_id: str
    status: RunStatus
    stage_log: list[dict[str, Any]]
    cost_report: dict[str, Any]
    degraded: bool


def resume_strategy(snapshot_values: dict[str, Any], snapshot_next: tuple[str, ...]) -> str:
    """Decide how to re-enter the graph for a thread: `"fresh"`, `"resumed"`, or `"already_complete"`.

    Getting this wrong is the difference between resuming, restarting, and silently doing nothing.
    Passing a fresh input state to a thread that already has state re-enters at START and replays
    every completed node (`"fresh"`). Passing `None` continues from the last checkpoint
    (`"resumed"`). A thread with state and nothing left scheduled is already finished, so
    re-executing it is a no-op (`"already_complete"`).

    `snapshot_next` alone is not trusted to mean "nothing left to do". The SQLite checkpointer
    commits a step's channel values and the writes that schedule its next task as separate
    transactions; a process killed between the two leaves a checkpoint whose values show real
    progress (a document correctly classified, say) but whose `next` reads empty — indistinguishable
    at that field alone from a run that actually reached the gate. Found by running the kill/resume
    test in a loop until a resumed run silently produced zero pending items instead of the expected
    ones. The stage log reaching "gate" is the only signal of real completion trusted here; anything
    else with `next` empty is a checkpoint that cannot be trusted to resume from, so it restarts
    instead of silently doing nothing. A restart is safe: every stage is idempotent, and the gate's
    dedupe key already makes re-proposing an item it has seen before a no-op
    (`test_the_second_run_reproposes_nothing_it_already_committed`).
    """
    reached_gate = any(
        entry.get("stage") == "gate" for entry in snapshot_values.get("stage_log", [])
    )
    if snapshot_values and snapshot_next:
        return "resumed"
    if snapshot_values and reached_gate:
        return "already_complete"
    return "fresh"


def execute_run(run_id: str, settings: Settings | None = None) -> RunOutcome:
    """Run the graph to the gate. Idempotent under resume: the checkpointer skips finished nodes."""
    settings = settings or get_settings()
    sessions = session_factory()

    with sessions() as session:
        run = session.get(Run, run_id)
        if run is None:
            raise LookupError(f"run {run_id} not found")
        pile_id = run.pile_id
        document_ids = list(run.new_document_ids or [])
        if not document_ids:
            document_ids = [
                doc_id
                for (doc_id,) in session.execute(
                    select(SourceDocument.id).where(SourceDocument.pile_id == pile_id)
                )
            ]
        before = load_snapshots(session, pile_id)

    client = TieredModelClient(build_client(settings), settings)
    context = StageContext(
        settings=settings,
        client=client,
        playbook=load_playbook(settings.playbook_path),
        sessions=sessions,
    )

    started = time.perf_counter()
    stage_timings: dict[str, float] = {}

    with checkpointer_for(settings.database_url) as checkpointer:
        graph = build_graph(context, checkpointer)
        config = {"configurable": {"thread_id": run_id}}
        snapshot = graph.get_state(config)
        resumed = resume_strategy(snapshot.values, snapshot.next)
        entry: Any = None if resumed != "fresh" else empty_state(run_id, pile_id, document_ids)

        if resumed != "already_complete":
            node_started = time.perf_counter()
            for event in graph.stream(entry, config, stream_mode="updates"):
                for node in event:
                    now = time.perf_counter()
                    stage_timings[node] = round(now - node_started, 3)
                    node_started = now
                    _heartbeat(sessions, run_id)

        # The checkpointer holds the accumulated state, which is the only correct source after a
        # resume: stream deltas describe this process's nodes, not the ones a previous process
        # already completed.
        final: dict[str, Any] = dict(graph.get_state(config).values)
        stage_timings["_entry"] = resumed

    elapsed = time.perf_counter() - started

    with sessions() as session:
        after = load_snapshots(session, pile_id)
        touched = {update_["field_path"] for update_ in final.get("updates", [])}
        proof = build_proof(before, after, touched)
        session.add(
            UntouchedProof(
                run_id=run_id,
                pile_id=pile_id,
                fields_recomputed=proof.fields_recomputed,
                fields_unchanged=proof.fields_unchanged,
                unchanged_digest=proof.unchanged_digest,
                detail=proof.detail,
            )
        )
        run = session.get(Run, run_id)
        run.status = RunStatus.awaiting_approval
        run.degraded = bool(final.get("degraded"))
        run.degraded_reason = final.get("degraded_reason")
        run.stage_timings = {**stage_timings, "total_seconds": round(elapsed, 3)}
        run.cost_report = client.cost_report()
        session.commit()
        outcome = RunOutcome(
            run_id=run_id,
            status=RunStatus.awaiting_approval,
            stage_log=final.get("stage_log", []),
            cost_report=run.cost_report,
            degraded=run.degraded,
        )

    return outcome


def _heartbeat(sessions, run_id: str) -> None:
    with sessions() as session:
        session.execute(update(Run).where(Run.id == run_id).values(heartbeat_at=_now()))
        session.commit()


# --------------------------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------------------------


def decide_items(
    session: Session,
    item_ids: list[str],
    *,
    approve: bool,
    decided_by: str,
    note: str | None = None,
) -> dict[str, Any]:
    """Approve or reject specific items. Every other item is untouched.

    Rejecting one finding does not discard the rest because decisions are rows, not a batch flag.
    """
    decided, missing, already = [], [], []
    for item_id in item_ids:
        item = session.get(PendingItem, item_id)
        if item is None:
            missing.append(item_id)
            continue
        if item.status != ItemStatus.pending:
            already.append(item_id)
            continue
        item.status = ItemStatus.approved if approve else ItemStatus.rejected
        item.decided_at = _now()
        item.decided_by = decided_by
        item.decision_note = note
        decided.append(item_id)
    session.commit()
    return {"decided": decided, "not_found": missing, "already_decided": already}


class RegisterVersionConflict(RuntimeError):
    def __init__(self, expected: int, actual: int) -> None:
        super().__init__(
            f"register moved from version {expected} to {actual} while this commit was preparing. "
            "Nothing was written. Re-read the pending items and decide again."
        )
        self.expected = expected
        self.actual = actual


def commit_approved(
    session: Session,
    pile_id: str,
    run_id: str,
    *,
    expected_version: int | None = None,
    committed_by: str = "api",
) -> dict[str, Any]:
    """Apply approved updates under an optimistic version check.

    Only this function serialises. Two runs against one pile execute in parallel and meet here.
    """
    statement = select(Pile).where(Pile.id == pile_id)
    if session.bind.dialect.name == "postgresql":
        statement = statement.with_for_update()
    pile = session.scalars(statement).one()

    if expected_version is not None and pile.register_version != expected_version:
        raise RegisterVersionConflict(expected_version, pile.register_version)

    items = session.scalars(
        select(PendingItem).where(
            PendingItem.pile_id == pile_id,
            PendingItem.run_id == run_id,
            PendingItem.status == ItemStatus.approved,
            PendingItem.kind == ItemKind.update,
        )
    ).all()

    applied = 0
    for item in items:
        payload = item.payload
        path = payload["field_path"]
        value = payload["after"]
        field = session.scalars(
            select(RegisterField).where(
                RegisterField.pile_id == pile_id, RegisterField.field_path == path
            )
        ).first()
        before_value = None if field is None else field.value
        before_hash = None if field is None else field.value_sha256
        value_hash = sha256_json(value)

        if field is None:
            field = RegisterField(
                pile_id=pile_id,
                field_path=path,
                value=value,
                status=FieldStatus.supported,
                evidence=payload.get("evidence", []),
                inputs_hash=payload["inputs_hash"],
                value_sha256=value_hash,
                version=1,
                updated_by_run=run_id,
            )
            session.add(field)
        else:
            field.value = value
            field.evidence = payload.get("evidence", [])
            field.inputs_hash = payload["inputs_hash"]
            field.value_sha256 = value_hash
            field.version += 1
            field.updated_by_run = run_id
            field.updated_at = _now()

        session.add(
            LedgerEntry(
                pile_id=pile_id,
                run_id=run_id,
                item_id=item.id,
                field_path=path,
                action="create" if before_value is None else "update",
                before_value=before_value,
                after_value=value,
                before_sha256=before_hash,
                after_sha256=value_hash,
                caused_by_document_id=payload.get("caused_by_document_id"),
                approved_by=item.decided_by,
            )
        )
        applied += 1

    pile.register_version += 1
    run = session.get(Run, run_id)
    if run is not None:
        run.status = RunStatus.committed
        run.finished_at = _now()
    session.commit()

    return {
        "applied": applied,
        "register_version": pile.register_version,
        "committed_by": committed_by,
    }


def register_digest(fields: dict[str, FieldSnapshot]) -> str:
    payload = "|".join(f"{path}={snap.value_sha256}" for path, snap in sorted(fields.items()))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
