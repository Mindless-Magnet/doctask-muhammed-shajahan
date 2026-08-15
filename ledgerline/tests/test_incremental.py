"""Behaviour three, end to end.

A pile is built and committed. Then one new document arrives. The claims under test:

  * the second run costs like an update, not like a re-run: it makes model calls only about the new
    document, and none about the ones already understood
  * only fields the new document could affect are even considered
  * every other field is byte-identical afterwards, and the system produces the proof rather than
    asserting it
  * a new source that contradicts the register raises a conflict instead of overwriting
  * at any moment the system can answer what changed, when, and because of which source

The measurement matters more than the assertion here. A re-run that happens to reproduce the same
bytes would pass a naive equality check; it fails the call-count and the impact-set checks.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

import ledgerline.db as db_module
from ledgerline.models import ItemKind, LedgerEntry, PendingItem, RegisterField, UntouchedProof
from ledgerline.service import commit_approved, decide_items, execute_run
from tests.conftest import MSA_TEXT, Corpus

INVOICE_ONE = (
    "INVOICE INV-2201\n\n"
    "Vendor: Northwind Logistics\n\n"
    "Invoice date: 2025-03-02\n\n"
    "Quantity: 40 pallets at a unit price of 160.00 each.\n\n"
    "Total due: 6,400.00. Purchase order reference: PO-1042.\n"
)

INVOICE_TWO = (
    "INVOICE INV-2207\n\n"
    "Vendor: Northwind Logistics\n\n"
    "Invoice date: 2025-08-28\n\n"
    "Quantity: 45 pallets at a unit price of 145.00 each.\n\n"
    "Total due: 6,525.00. Purchase order reference: PO-1043.\n"
)

AMENDMENT = (
    "AMENDMENT NO. 1\n\n"
    "This amendment is dated 2025-06-15.\n\n"
    "Payment terms are amended to net 30 days from the date of invoice.\n"
)


def _seed_first_run(corpus: Corpus) -> tuple[str, str]:
    msa_id = corpus.add("msa.txt", MSA_TEXT, "msa")
    invoice_id = corpus.add("inv_2201.txt", INVOICE_ONE, "invoice")
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
            ("invoices.INV-2201.unit_price", 160.0, "160.00"),
            ("invoices.INV-2201.date", "2025-03-02", "2025-03-02"),
            ("invoices.INV-2201.po_ref", "PO-1042", "PO-1042"),
        ],
    )
    return msa_id, invoice_id


def _commit_everything(corpus: Corpus, run_id: str, expected_version: int) -> int:
    with db_module.session_scope() as session:
        items = session.scalars(
            select(PendingItem).where(
                PendingItem.pile_id == corpus.pile_id, PendingItem.kind == ItemKind.update
            )
        ).all()
        decide_items(session, [item.id for item in items], approve=True, decided_by="reviewer")
    with db_module.session_scope() as session:
        result = commit_approved(
            session, corpus.pile_id, run_id, expected_version=expected_version
        )
    return result["register_version"]


def _register(pile_id: str) -> dict[str, str]:
    with db_module.session_scope() as session:
        return {
            row.field_path: row.value_sha256
            for row in session.scalars(
                select(RegisterField).where(RegisterField.pile_id == pile_id)
            )
        }


def _proof(run_id: str) -> UntouchedProof:
    with db_module.session_scope() as session:
        return session.scalars(
            select(UntouchedProof).where(UntouchedProof.run_id == run_id)
        ).one()


def test_a_new_invoice_costs_an_update_not_a_rerun(env, corpus, tmp_path):
    _seed_first_run(corpus)
    first_run = corpus.queue_run()
    first = execute_run(first_run)
    version = _commit_everything(corpus, first_run, expected_version=0)

    before = _register(corpus.pile_id)
    assert len(before) == 8, before

    # One new document arrives.
    new_invoice = corpus.add("inv_2207.txt", INVOICE_TWO, "invoice")
    corpus.seed_extract(
        new_invoice,
        "invoice",
        [
            ("invoices.INV-2207.unit_price", 145.0, "145.00"),
            ("invoices.INV-2207.date", "2025-08-28", "2025-08-28"),
            ("invoices.INV-2207.po_ref", "PO-1043", "PO-1043"),
        ],
    )
    second_run = corpus.queue_run(document_ids=[new_invoice])
    second = execute_run(second_run)

    # Cost. The first run read two documents, the second read one. Nothing about the MSA was
    # re-derived, so the second run must be strictly cheaper and must not touch it at all.
    first_calls = first.cost_report["total_calls"]
    second_calls = second.cost_report["total_calls"]
    assert second_calls < first_calls, (first_calls, second_calls)
    assert second.cost_report["by_stage"]["classify"]["calls"] == 1
    assert second.cost_report["by_stage"]["extract"]["calls"] == 1

    # Impact. An invoice cannot reach a contract field, so those were never even considered.
    reconcile_log = next(entry for entry in second.stage_log if entry["stage"] == "reconcile")
    # Invoices, plus the one derived field that depends on them. Nothing contract-shaped.
    assert reconcile_log["impact_prefixes"] == ["derived.invoiced_total", "invoices."]
    assert not any(prefix.startswith("contract.") for prefix in reconcile_log["impact_prefixes"])

    # Proof, measured rather than claimed.
    proof = _proof(second_run)
    assert proof.fields_unchanged == 8
    assert proof.fields_recomputed == 3
    assert not proof.detail["mismatched_paths"]
    assert all(path.startswith("invoices.INV-2207.") for path in proof.detail["touched_paths"])

    # And the register really is untouched until the new work is approved and committed.
    assert _register(corpus.pile_id) == before

    _commit_everything(corpus, second_run, expected_version=version)
    after = _register(corpus.pile_id)
    for path, digest in before.items():
        assert after[path] == digest, f"{path} changed and should not have"
    assert set(after) - set(before) == {
        "invoices.INV-2207.unit_price",
        "invoices.INV-2207.date",
        "invoices.INV-2207.po_ref",
    }


def test_the_second_run_reproposes_nothing_it_already_committed(env, corpus, tmp_path):
    """The same documents arriving twice produce no work at all. An update that costs like an
    update must also cost nothing when there is nothing to update."""
    msa_id, invoice_id = _seed_first_run(corpus)
    first_run = corpus.queue_run()
    execute_run(first_run)
    _commit_everything(corpus, first_run, expected_version=0)
    before = _register(corpus.pile_id)

    repeat_run = corpus.queue_run(document_ids=[msa_id, invoice_id])
    execute_run(repeat_run)

    with db_module.session_scope() as session:
        proposed = session.scalars(
            select(PendingItem).where(
                PendingItem.pile_id == corpus.pile_id,
                PendingItem.kind == ItemKind.update,
                PendingItem.run_id == repeat_run,
            )
        ).all()
    assert proposed == [], "the same facts were proposed again"

    proof = _proof(repeat_run)
    assert proof.fields_recomputed == 0
    assert proof.fields_unchanged == len(before)
    assert proof.holds if hasattr(proof, "holds") else not proof.detail["mismatched_paths"]
    assert _register(corpus.pile_id) == before


def test_a_contradicting_source_raises_a_conflict_and_overwrites_nothing(env, corpus, tmp_path):
    _seed_first_run(corpus)
    first_run = corpus.queue_run()
    execute_run(first_run)
    _commit_everything(corpus, first_run, expected_version=0)
    before = _register(corpus.pile_id)

    amendment_id = corpus.add("amendment_1.txt", AMENDMENT, "amendment")
    corpus.seed_extract(
        amendment_id,
        "amendment",
        [("contract.payment_terms.net_days", 30, "net 30 days")],
    )
    second_run = corpus.queue_run(document_ids=[amendment_id])
    execute_run(second_run)

    with db_module.session_scope() as session:
        conflicts = session.scalars(
            select(PendingItem).where(
                PendingItem.pile_id == corpus.pile_id, PendingItem.kind == ItemKind.conflict
            )
        ).all()

    paths = {item.field_path for item in conflicts}
    assert "contract.payment_terms.net_days" in paths, "a contradiction was silently resolved"
    conflict = next(item for item in conflicts if item.field_path == "contract.payment_terms.net_days")
    assert sorted(conflict.payload["values"]) == [30, 60]
    assert len(conflict.evidence) >= 2, "a conflict must cite both sides"

    # Nothing moved. A conflict is surfaced, not applied.
    assert _register(corpus.pile_id) == before


def test_the_ledger_answers_what_changed_when_and_because_of_which_source(env, corpus, tmp_path):
    _seed_first_run(corpus)
    first_run = corpus.queue_run()
    execute_run(first_run)
    version = _commit_everything(corpus, first_run, expected_version=0)

    new_invoice = corpus.add("inv_2207.txt", INVOICE_TWO, "invoice")
    corpus.seed_extract(
        new_invoice,
        "invoice",
        [("invoices.INV-2207.unit_price", 145.0, "145.00")],
    )
    second_run = corpus.queue_run(document_ids=[new_invoice])
    execute_run(second_run)
    _commit_everything(corpus, second_run, expected_version=version)

    with db_module.session_scope() as session:
        entries = session.scalars(
            select(LedgerEntry)
            .where(LedgerEntry.pile_id == corpus.pile_id)
            .order_by(LedgerEntry.created_at)
        ).all()

    by_run: dict[str, list[LedgerEntry]] = {}
    for entry in entries:
        by_run.setdefault(entry.run_id, []).append(entry)

    assert set(by_run) == {first_run, second_run}
    assert len(by_run[second_run]) == 1
    late = by_run[second_run][0]
    assert late.field_path == "invoices.INV-2207.unit_price"
    assert late.action == "create"
    assert late.caused_by_document_id == new_invoice
    assert late.approved_by == "reviewer"
    assert late.created_at is not None


@pytest.mark.parametrize(
    "doc_type,expected",
    [
        ("invoice", ["derived.invoiced_total", "invoices."]),
        ("rate_card", ["rate_table."]),
        ("purchase_order", ["purchase_orders."]),
    ],
)
def test_impact_prefixes_are_data_not_logic(doc_type: str, expected: list[str]):
    """A new document type is an entry in IMPACT_MAP, not a code change."""
    from ledgerline.register.reconcile import IMPACT_MAP, impacted_paths

    assert doc_type in IMPACT_MAP
    assert list(impacted_paths({doc_type})) == expected
