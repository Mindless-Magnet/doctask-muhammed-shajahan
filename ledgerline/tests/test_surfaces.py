"""Behaviour four: another program drives the whole flow, gate included, with nobody clicking.

The MCP test deliberately never touches the HTTP surface, the database directly, or any service
function. It goes through the registered MCP tools only, exactly as a coding agent would, and it
includes the approval step. If approval were a property of the user interface rather than an
operation, this test could not exist.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import select

import ledgerline.db as db_module
from ledgerline.api.app import API_TOKEN, app
from ledgerline.mcp import server as mcp_server
from ledgerline.models import RegisterField, RunStatus
from ledgerline.service import execute_run
from tests.conftest import INVOICE_TEXT, MSA_TEXT

HEADERS = {"Authorization": f"Bearer {API_TOKEN}"}


def _seed(corpus):
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


def _call(name: str, **arguments: Any) -> Any:
    """Invoke a registered MCP tool the way a client would, by name and arguments."""
    return asyncio.run(_call_async(name, arguments))


async def _call_async(name: str, arguments: dict[str, Any]) -> Any:
    tools = await mcp_server.mcp.list_tools()
    names = {getattr(tool, "name", None) for tool in tools}
    assert name in names, f"tool '{name}' is not registered; available: {sorted(n for n in names if n)}"
    return await mcp_server.mcp.call_tool(name, arguments)


def _payload(result: Any) -> dict[str, Any]:
    """Tool results differ in shape between SDK versions; take the structured content either way."""
    if isinstance(result, tuple):
        result = result[-1]
    if isinstance(result, dict):
        return result
    content = getattr(result, "structured_content", None) or getattr(result, "structuredContent", None)
    if isinstance(content, dict):
        return content
    import json

    blocks = getattr(result, "content", None) or []
    for block in blocks:
        text = getattr(block, "text", None)
        if text:
            return json.loads(text)
    raise AssertionError(f"could not read a payload out of {result!r}")


# --------------------------------------------------------------------------------------------
# MCP
# --------------------------------------------------------------------------------------------


def test_a_program_drives_the_entire_flow_through_mcp_alone(env, corpus):
    _seed(corpus)

    started = _payload(_call("start_run", pile_id=corpus.pile_id))
    run_id = started["run_id"]

    # A worker executes the queued run. In production this is the worker process; here it is called
    # directly because the point of this test is the surface, not the scheduler.
    execute_run(run_id)

    status = _payload(_call("get_run_status", run_id=run_id))
    assert status["status"] == RunStatus.awaiting_approval.value
    # Behaviour 10 has to hold on this surface too: a program driving the flow through MCP alone
    # must be able to read what its own run cost, not just its outcome.
    assert status["cost"], "get_run_status returned no cost for a completed run"
    assert status["cost"].get("total_cost_usd") is not None
    assert status["cost"].get("by_stage")

    pending = _payload(_call("list_pending_items", pile_id=corpus.pile_id, kind="update"))
    assert pending["items"], "nothing to approve"
    version = pending["register_version"]

    ids = [item["id"] for item in pending["items"]]
    keep, drop = ids[:-1], ids[-1:]

    approved = _payload(_call("approve_items", item_ids=keep, decided_by="agent"))
    rejected = _payload(_call("reject_items", item_ids=drop, decided_by="agent"))
    assert approved["decided"] == keep
    assert rejected["decided"] == drop

    committed = _payload(
        _call("commit_run", pile_id=corpus.pile_id, run_id=run_id, expected_version=version)
    )
    assert committed["applied"] == len(keep)
    assert committed["register_version"] == version + 1

    register = _payload(_call("get_register", pile_id=corpus.pile_id))
    assert len(register["fields"]) == len(keep)
    for field in register["fields"].values():
        assert field["evidence"], "a committed field carries no citation"

    ledger = _payload(_call("get_change_ledger", pile_id=corpus.pile_id))
    assert len(ledger["entries"]) == len(keep)

    any_path = next(iter(register["fields"]))
    explained = _payload(_call("explain_field", pile_id=corpus.pile_id, field_path=any_path))
    assert explained["evidence"]
    assert explained["history"]


def test_mcp_commit_refuses_a_stale_version_without_writing(env, corpus):
    _seed(corpus)
    run_id = _payload(_call("start_run", pile_id=corpus.pile_id))["run_id"]
    execute_run(run_id)

    pending = _payload(_call("list_pending_items", pile_id=corpus.pile_id, kind="update"))
    ids = [item["id"] for item in pending["items"]]
    _call("approve_items", item_ids=ids, decided_by="agent")

    first = _payload(_call("commit_run", pile_id=corpus.pile_id, run_id=run_id, expected_version=0))
    assert first["applied"] == len(ids)

    with db_module.session_scope() as session:
        before = {
            row.field_path: row.value_sha256
            for row in session.scalars(
                select(RegisterField).where(RegisterField.pile_id == corpus.pile_id)
            )
        }

    second = _payload(_call("commit_run", pile_id=corpus.pile_id, run_id=run_id, expected_version=0))
    assert second.get("conflict") is True
    assert "Nothing was written" in second["error"]

    with db_module.session_scope() as session:
        after = {
            row.field_path: row.value_sha256
            for row in session.scalars(
                select(RegisterField).where(RegisterField.pile_id == corpus.pile_id)
            )
        }
    assert before == after


# --------------------------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------------------------


def test_http_rejects_a_missing_token(env):
    client = TestClient(app)
    assert client.post("/piles", json={"name": "x"}).status_code == 401
    assert client.get("/health").status_code == 200


def test_uploading_the_same_bytes_twice_creates_one_document(env, tmp_path):
    client = TestClient(app)
    pile_id = client.post("/piles", json={"name": "http-pile"}, headers=HEADERS).json()["pile_id"]

    path = tmp_path / "msa.txt"
    path.write_text(MSA_TEXT, encoding="utf-8")

    first = client.post(
        f"/piles/{pile_id}/documents",
        files={"file": ("msa.txt", path.read_bytes(), "text/plain")},
        headers=HEADERS,
    ).json()
    second = client.post(
        f"/piles/{pile_id}/documents",
        files={"file": ("msa.txt", path.read_bytes(), "text/plain")},
        headers=HEADERS,
    ).json()

    assert first["created"] is True
    assert second["created"] is False
    assert second["document_id"] == first["document_id"]


def test_an_unreadable_upload_names_its_cause_and_fix(env, tmp_path):
    client = TestClient(app)
    pile_id = client.post("/piles", json={"name": "bad-pile"}, headers=HEADERS).json()["pile_id"]
    response = client.post(
        f"/piles/{pile_id}/documents",
        files={"file": ("scan.tiff", b"not a document", "image/tiff")},
        headers=HEADERS,
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "Cause:" in detail and "Fix:" in detail


def test_the_register_endpoint_reports_what_the_sources_do_not_state(env, corpus):
    msa_id = corpus.add("msa.txt", MSA_TEXT, "msa")
    corpus.seed_extract(
        msa_id,
        "msa",
        [("contract.liability_cap.amount", 100000, "USD 100,000")],
        not_stated=["contract.spend_cap.amount"],
    )
    execute_run(corpus.queue_run())

    client = TestClient(app)
    body = client.get(f"/piles/{corpus.pile_id}/register", headers=HEADERS).json()
    gaps = {row["field_path"] for row in body["not_stated"]}
    assert "contract.spend_cap.amount" in gaps


# --------------------------------------------------------------------------------------------
# Corpus
# --------------------------------------------------------------------------------------------


def test_the_generated_corpus_loads_and_carries_its_designed_contradictions(env, tmp_path):
    from ledgerline.corpus.generate import DESIGNED_CONTRADICTIONS, generate_and_load

    result = generate_and_load(pile_name="gen-a", out_dir=tmp_path / "corpus", variant="a")
    assert len(result["documents"]) == 11
    assert result["designed_contradictions"] == DESIGNED_CONTRADICTIONS

    # Variant b is a different vendor and a different document set, for the second run.
    second = generate_and_load(pile_name="gen-b", out_dir=tmp_path / "corpus-b", variant="b")
    assert second["pile_id"] != result["pile_id"]
    assert {doc["filename"] for doc in second["documents"]} != {
        doc["filename"] for doc in result["documents"]
    }


def test_every_generated_document_parses_with_a_usable_offset_map(env, tmp_path):
    from ledgerline.corpus.generate import write_corpus
    from ledgerline.ingest.extract_text import extract

    for path in write_corpus(tmp_path / "c", "a"):
        document = extract(path)
        assert document.text.strip()
        for locator in document.locators:
            assert document.text[locator.start : locator.end].strip()
        assert document.cite(0, 5) != "unlocated"
