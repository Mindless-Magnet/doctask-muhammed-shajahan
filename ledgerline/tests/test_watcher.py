"""The watched location.

Two properties beyond "it noticed the file": a document dropped twice is ingested once, and a file
still being written is not read until it stops changing. Both are the difference between a watcher
that works on a demo folder and one that works on a network share.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select

import ledgerline.db as db_module
from ledgerline.models import Run, RunStatus, SourceDocument
from ledgerline.watcher import Watcher
from tests.conftest import MSA_TEXT, Corpus

INVOICE = (
    "INVOICE INV-2207\n\nVendor: Northwind Logistics\n\nInvoice date: 2025-08-28\n\n"
    "Quantity: 45 pallets at a unit price of 145.00 each.\n"
)


def _watcher(env, tmp_path) -> tuple[Watcher, Path, str]:
    corpus = Corpus(env, tmp_path, name="watched")
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    return Watcher(corpus.pile_id, inbox), inbox, corpus.pile_id


def test_a_file_is_only_read_once_it_stops_changing(env, tmp_path):
    watcher, inbox, _ = _watcher(env, tmp_path)
    path = inbox / "msa.txt"
    path.write_text(MSA_TEXT[:200], encoding="utf-8")

    # First sight of the file. Its size has not been confirmed, so nothing is read.
    first = watcher.poll()
    assert first.ingested == []
    assert "msa.txt" in first.pending_stability

    # Still being written. Size moved, so still nothing.
    path.write_text(MSA_TEXT, encoding="utf-8")
    second = watcher.poll()
    assert second.ingested == []

    # Settled.
    third = watcher.poll()
    assert len(third.ingested) == 1

    with db_module.session_scope() as session:
        document = session.get(SourceDocument, third.ingested[0])
    assert document.canonical_text.strip().endswith(MSA_TEXT.strip()[-40:])


def test_the_same_bytes_under_a_different_name_are_ingested_once(env, tmp_path):
    watcher, inbox, pile_id = _watcher(env, tmp_path)
    (inbox / "msa.txt").write_text(MSA_TEXT, encoding="utf-8")
    watcher.poll()
    assert len(watcher.poll().ingested) == 1

    (inbox / "msa_copy.txt").write_text(MSA_TEXT, encoding="utf-8")
    watcher.poll()
    result = watcher.poll()

    assert result.ingested == []
    assert "msa_copy.txt" in result.duplicates
    assert result.run_id is None, "a duplicate must not queue a run"

    with db_module.session_scope() as session:
        documents = session.scalars(
            select(SourceDocument).where(SourceDocument.pile_id == pile_id)
        ).all()
    assert len(documents) == 1


def test_an_arrival_queues_a_run_scoped_to_the_new_documents(env, tmp_path):
    watcher, inbox, pile_id = _watcher(env, tmp_path)
    (inbox / "msa.txt").write_text(MSA_TEXT, encoding="utf-8")
    watcher.poll()
    first = watcher.poll()

    (inbox / "inv_2207.txt").write_text(INVOICE, encoding="utf-8")
    watcher.poll()
    second = watcher.poll()

    assert second.run_id is not None and second.run_id != first.run_id
    with db_module.session_scope() as session:
        run = session.get(Run, second.run_id)
    assert run.status is RunStatus.queued
    assert run.trigger == "watcher"
    # Scoped to what arrived, not to the whole pile. This is what makes the update cost like one.
    assert run.new_document_ids == second.ingested
    assert len(run.new_document_ids) == 1


def test_an_unreadable_arrival_is_reported_and_skipped(env, tmp_path):
    watcher, inbox, _ = _watcher(env, tmp_path)
    (inbox / "scan.tiff").write_bytes(b"not a document")
    (inbox / "msa.txt").write_text(MSA_TEXT, encoding="utf-8")
    watcher.poll()
    result = watcher.poll()

    assert len(result.ingested) == 1
    names = {name for name, _ in result.unsupported}
    assert "scan.tiff" in names
    reason = next(reason for name, reason in result.unsupported if name == "scan.tiff")
    assert "unsupported suffix" in reason
