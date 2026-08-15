"""Graph state, and the field-path allowlist.

State is plain JSON-serialisable dicts because the checkpointer has to write it to a database and
read it back after a crash. Nothing live (sessions, clients) goes in here; those arrive through the
StageContext bound at graph construction.

The allowlist is the third injection defence, and the one that matters most. Structural framing and
pattern detection both operate on what the model reads. This operates on what it is permitted to
write: an invoice may only produce `invoices.*` fields, so a document that talks its way into
proposing a new liability cap has that proposal dropped at the boundary and reported. Without it,
prompt injection is a data-integrity problem rather than a nuisance.
"""

from __future__ import annotations

import re
from typing import Any, TypedDict


class RunState(TypedDict, total=False):
    run_id: str
    pile_id: str
    document_ids: list[str]

    classified: dict[str, dict[str, Any]]
    escalations: list[dict[str, Any]]
    injection_findings: list[dict[str, Any]]

    verified_fields: list[dict[str, Any]]
    rejected_fields: list[dict[str, Any]]
    retry_paths: list[str]
    retry_count: int

    updates: list[dict[str, Any]]
    conflicts: list[dict[str, Any]]
    findings: list[dict[str, Any]]
    unchanged_paths: list[str]
    blocked_paths: list[dict[str, Any]]

    degraded: bool
    degraded_reason: str | None
    stage_log: list[dict[str, Any]]


def empty_state(run_id: str, pile_id: str, document_ids: list[str]) -> RunState:
    return RunState(
        run_id=run_id,
        pile_id=pile_id,
        document_ids=document_ids,
        classified={},
        escalations=[],
        injection_findings=[],
        verified_fields=[],
        rejected_fields=[],
        retry_paths=[],
        retry_count=0,
        updates=[],
        conflicts=[],
        findings=[],
        unchanged_paths=[],
        blocked_paths=[],
        degraded=False,
        degraded_reason=None,
        stage_log=[],
    )


# --------------------------------------------------------------------------------------------
# Field sets and the write allowlist
# --------------------------------------------------------------------------------------------

FIELD_SETS: dict[str, list[str]] = {
    "msa": [
        "contract.parties.client",
        "contract.parties.vendor",
        "contract.effective_date",
        "contract.term_months",
        "contract.payment_terms.net_days",
        "contract.liability_cap.amount",
        "contract.renewal.type",
        "contract.renewal.notice_days",
    ],
    "amendment": [
        "contract.payment_terms.net_days",
        "contract.term_months",
        "contract.renewal.type",
        "contract.renewal.notice_days",
        "rate_table.<n>.unit_price",
        "rate_table.<n>.effective_from",
    ],
    "sow": [
        "contract.spend_cap.amount",
        "contract.payment_terms.net_days",
        "sow.number",
        "sow.scope",
    ],
    "rate_card": [
        "rate_table.<n>.unit_price",
        "rate_table.<n>.effective_from",
        "rate_table.<n>.unit",
    ],
    "purchase_order": [
        "purchase_orders.<po_number>.number",
        "purchase_orders.<po_number>.amount",
        "purchase_orders.<po_number>.issued_date",
    ],
    "invoice": [
        "invoices.<invoice_number>.amount",
        "invoices.<invoice_number>.unit_price",
        "invoices.<invoice_number>.quantity",
        "invoices.<invoice_number>.date",
        "invoices.<invoice_number>.po_ref",
    ],
    "notice": [
        "notice.type",
        "notice.sent_date",
        "notice.effective_date",
    ],
    "correspondence": [
        "notice.type",
        "notice.sent_date",
    ],
    "unknown": [],
}

_SEGMENT = r"[A-Za-z0-9][A-Za-z0-9_\-]{0,63}"

ALLOWED_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    "msa": (re.compile(r"^contract\.[A-Za-z0-9_.]{1,80}$"),),
    "amendment": (
        re.compile(r"^contract\.[A-Za-z0-9_.]{1,80}$"),
        re.compile(rf"^rate_table\.{_SEGMENT}\.(unit_price|effective_from|unit)$"),
    ),
    "sow": (
        re.compile(r"^contract\.(spend_cap\.amount|payment_terms\.net_days)$"),
        re.compile(r"^sow\.[A-Za-z0-9_]{1,40}$"),
    ),
    "rate_card": (
        re.compile(rf"^rate_table\.{_SEGMENT}\.(unit_price|effective_from|unit)$"),
    ),
    "purchase_order": (
        re.compile(rf"^purchase_orders\.{_SEGMENT}\.(number|amount|issued_date)$"),
    ),
    "invoice": (
        re.compile(rf"^invoices\.{_SEGMENT}\.(amount|unit_price|quantity|date|po_ref)$"),
    ),
    "notice": (re.compile(r"^notice\.(type|sent_date|effective_date)$"),),
    "correspondence": (re.compile(r"^notice\.(type|sent_date)$"),),
    "unknown": (),
}


def path_allowed(doc_type: str, field_path: str) -> bool:
    return any(pattern.match(field_path) for pattern in ALLOWED_PATTERNS.get(doc_type, ()))
