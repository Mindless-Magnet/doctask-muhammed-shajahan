"""End-to-end tests through the real graph. No key, no network."""

from __future__ import annotations

import pytest
from sqlalchemy import select

import ledgerline.db as db_module
from ledgerline.models import ItemKind, ItemStatus, PendingItem, RegisterField, UnsupportedClaim
from ledgerline.service import (
    RegisterVersionConflict,
    commit_approved,
    decide_items,
    execute_run,
)
from tests.conftest import INVOICE_TEXT, MSA_TEXT, POISONED_INVOICE_TEXT


def _seed_standard(corpus):
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
            ("contract.effective_date", "2025-01-01", "2025-01-01"),
        ],
        not_stated=["contract.spend_cap.amount"],
    )
    corpus.seed_extract(
        invoice_id,
        "invoice",
        [
            ("invoices.INV-2205.unit_price", 160.0, "160.00"),
            ("invoices.INV-2205.date", "2025-08-14", "2025-08-14"),
            ("invoices.INV-2205.po_ref", "PO-1042", "PO-1042"),
            ("invoices.INV-2205.quantity", 40, "40 pallets"),
        ],
    )
    return msa_id, invoice_id


def _items(pile_id: str, kind: ItemKind | None = None) -> list[PendingItem]:
    with db_module.session_scope() as session:
        statement = select(PendingItem).where(PendingItem.pile_id == pile_id)
        if kind is not None:
            statement = statement.where(PendingItem.kind == kind)
        return list(session.scalars(statement))


def test_full_run_reaches_the_gate_and_commits_nothing(corpus):
    _seed_standard(corpus)
    run_id = corpus.queue_run()

    outcome = execute_run(run_id)

    stages = [entry["stage"] for entry in outcome.stage_log]
    assert stages == ["intake", "classify", "extract", "reconcile", "examine", "gate"]

    assert _items(corpus.pile_id, ItemKind.update), "no updates proposed"
    with db_module.session_scope() as session:
        committed = session.scalars(
            select(RegisterField).where(RegisterField.pile_id == corpus.pile_id)
        ).all()
    assert committed == [], "the gate must commit nothing on its own"


def test_findings_describe_the_state_the_reviewer_is_asked_to_accept(corpus):
    _seed_standard(corpus)
    execute_run(corpus.queue_run())

    rule_ids = {item.rule_id for item in _items(corpus.pile_id, ItemKind.finding)}
    # net 60 breaches the net 45 ceiling, and auto-renewal with 30 days breaches the 60 day floor.
    # Both are only visible if rules run against the projected register, not the empty one.
    assert "PAY_TERMS_MAX" in rule_ids
    assert "AUTORENEW_NOTICE" in rule_ids


def test_a_document_that_gives_orders_is_reported_and_obeyed_by_nothing(corpus):
    msa_id, _ = _seed_standard(corpus)
    poisoned_id = corpus.add("inv_2209.txt", POISONED_INVOICE_TEXT, "invoice")
    corpus.seed_extract(
        poisoned_id,
        "invoice",
        [
            ("invoices.INV-2209.unit_price", 145.0, "145.00"),
            ("invoices.INV-2209.po_ref", "PO-1042", "PO-1042"),
        ],
    )

    execute_run(corpus.queue_run())

    findings = _items(corpus.pile_id, ItemKind.finding)
    injection = [item for item in findings if item.rule_id == "INSTRUCTION_IN_SOURCE"]
    assert injection, "the instruction in the source was not reported"
    assert "did not influence any decision" in injection[0].detail

    # The instruction asked for every invoice to be approved and for discrepancies to go unreported.
    # Both must have failed.
    assert all(item.status == ItemStatus.pending for item in _items(corpus.pile_id))
    assert {item.rule_id for item in findings} & {"PAY_TERMS_MAX", "AUTORENEW_NOTICE"}


def test_an_invoice_cannot_write_a_contract_field(corpus):
    msa_id, invoice_id = _seed_standard(corpus)
    # Re-seed the invoice extraction so the model tries to set the liability cap from an invoice.
    corpus.seed_extract(
        invoice_id,
        "invoice",
        [("invoices.INV-2205.unit_price", 160.0, "160.00")],
        raw_fields=[
            {
                "field_path": "contract.liability_cap.amount",
                "value": 6400,
                "quote": "6,400.00",
                "confidence": 0.99,
            }
        ],
    )

    execute_run(corpus.queue_run())

    blocked = [
        item for item in _items(corpus.pile_id, ItemKind.finding)
        if item.rule_id == "OUT_OF_SCOPE_WRITE"
    ]
    assert blocked, "an out-of-scope write was not blocked"
    assert blocked[0].field_path == "contract.liability_cap.amount"

    # The MSA legitimately proposes this field, so its presence is correct. What must not happen
    # is the invoice's value winning, or a conflict being raised between a real source and a
    # proposal that should never have reached reconciliation at all.
    proposed = {
        item.field_path: item.payload.get("after")
        for item in _items(corpus.pile_id, ItemKind.update)
    }
    assert proposed["contract.liability_cap.amount"] == 100000
    conflicts = {item.field_path for item in _items(corpus.pile_id, ItemKind.conflict)}
    assert "contract.liability_cap.amount" not in conflicts


def test_a_value_not_in_its_span_is_retried_then_recorded_as_unsupported(corpus):
    msa_id = corpus.add("msa.txt", MSA_TEXT, "msa")
    # The model cites "net 60 days" but claims the value is 45. Verification must reject it.
    corpus.seed_extract(
        msa_id,
        "msa",
        [
            ("contract.payment_terms.net_days", 45, "net 60 days"),
            ("contract.liability_cap.amount", 100000, "USD 100,000"),
        ],
    )
    # Deterministic verification cannot settle 45 against "net 60 days", so the judge is asked
    # what the span states. It says 60. 60 is not 45, so the claim is refused. The judge exists to
    # resolve paraphrase, not to agree with the extractor.
    _, msa_text = corpus.documents[msa_id]
    corpus.seed_read_span(
        msa_text[msa_text.index("net 60 days") : msa_text.index("net 60 days") + len("net 60 days")],
        "contract.payment_terms.net_days",
        60,
    )
    # The retry, with the tighter prompt, honestly reports the field as not stated.
    corpus.seed_extract(
        msa_id,
        "msa",
        [],
        not_stated=["contract.payment_terms.net_days"],
        only_paths=["contract.payment_terms.net_days"],
        tighten=True,
    )

    outcome = execute_run(corpus.queue_run())

    stages = [entry["stage"] for entry in outcome.stage_log]
    assert "extract_retry" in stages, "the retry path did not fire"

    with db_module.session_scope() as session:
        unsupported = session.scalars(
            select(UnsupportedClaim).where(UnsupportedClaim.pile_id == corpus.pile_id)
        ).all()
    paths = {claim.field_path for claim in unsupported}
    assert "contract.payment_terms.net_days" in paths

    proposed = {item.field_path for item in _items(corpus.pile_id, ItemKind.update)}
    assert "contract.payment_terms.net_days" not in proposed, "an unverified value reached the gate"


def test_nothing_confidently_classified_routes_straight_to_the_gate(corpus):
    corpus.add("mystery.txt", "Some pages of something.\n\nNo clear type.\n", "unknown", 0.2)
    outcome = execute_run(corpus.queue_run())

    stages = [entry["stage"] for entry in outcome.stage_log]
    assert stages == ["intake", "classify", "gate"]
    assert _items(corpus.pile_id, ItemKind.escalation)


def test_rejecting_one_item_leaves_the_rest_alone(corpus):
    _seed_standard(corpus)
    run_id = corpus.queue_run()
    execute_run(run_id)

    updates = _items(corpus.pile_id, ItemKind.update)
    assert len(updates) >= 3
    target, *rest = updates

    with db_module.session_scope() as session:
        decide_items(session, [target.id], approve=False, decided_by="reviewer")
        decide_items(session, [item.id for item in rest], approve=True, decided_by="reviewer")

    after = {item.id: item.status for item in _items(corpus.pile_id, ItemKind.update)}
    assert after[target.id] == ItemStatus.rejected
    assert all(after[item.id] == ItemStatus.approved for item in rest)

    with db_module.session_scope() as session:
        result = commit_approved(session, corpus.pile_id, run_id, expected_version=0)

    assert result["applied"] == len(rest)
    with db_module.session_scope() as session:
        committed = {
            row.field_path
            for row in session.scalars(
                select(RegisterField).where(RegisterField.pile_id == corpus.pile_id)
            )
        }
    assert target.field_path not in committed


def test_a_stale_commit_writes_nothing(corpus):
    _seed_standard(corpus)
    run_id = corpus.queue_run()
    execute_run(run_id)

    updates = _items(corpus.pile_id, ItemKind.update)
    with db_module.session_scope() as session:
        decide_items(session, [item.id for item in updates], approve=True, decided_by="reviewer")

    with db_module.session_scope() as session:
        commit_approved(session, corpus.pile_id, run_id, expected_version=0)

    with db_module.session_scope() as session, pytest.raises(RegisterVersionConflict) as exc:
        commit_approved(session, corpus.pile_id, run_id, expected_version=0)
    assert "Nothing was written" in str(exc.value)


def test_the_run_reports_what_it_cost_and_where_the_time_went(corpus):
    _seed_standard(corpus)
    outcome = execute_run(corpus.queue_run())

    report = outcome.cost_report
    assert report["total_calls"] > 0
    assert "classify" in report["by_stage"]
    assert "extract" in report["by_stage"]
    for bucket in report["by_stage"].values():
        assert "latency_ms_p95" in bucket
        assert "latency_ms_max" in bucket
    assert report["prices_pulled_at"]
