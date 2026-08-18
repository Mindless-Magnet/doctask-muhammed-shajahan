"""The read-only endpoints the review interface needs.

The interface performs no operation the machine surfaces do not already expose, so these tests cover
reading only. The one worth reading closely is the span excerpt: it is the single place where the
system hands a person the exact words behind a claim, and if it silently returned the wrong window
the interface would look correct while showing evidence for something else.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from ledgerline.api.app import API_TOKEN, app
from ledgerline.service import execute_run
from tests.conftest import MSA_TEXT

HEADERS = {"Authorization": f"Bearer {API_TOKEN}"}


@pytest.fixture
def seeded(env, corpus):
    msa_id = corpus.add("msa.txt", MSA_TEXT, "msa")
    corpus.seed_extract(
        msa_id,
        "msa",
        [
            ("contract.parties.client", "Harborview Foods", "Harborview Foods"),
            ("contract.payment_terms.net_days", 60, "net 60 days"),
            ("contract.liability_cap.amount", 100000, "USD 100,000"),
        ],
    )
    run_id = corpus.queue_run()
    execute_run(run_id)
    return corpus, msa_id, run_id


def test_listing_piles_shows_where_the_work_is(seeded):
    corpus, _, _ = seeded
    client = TestClient(app)
    body = client.get("/piles", headers=HEADERS).json()

    pile = next(p for p in body["piles"] if p["pile_id"] == corpus.pile_id)
    assert pile["documents"] == 1
    assert pile["pending"] > 0
    assert pile["committed_fields"] == 0, "the gate must not have committed anything"


def test_the_latest_run_carries_its_proof_and_its_cost(seeded):
    corpus, _, run_id = seeded
    client = TestClient(app)
    run = client.get(f"/piles/{corpus.pile_id}/latest-run", headers=HEADERS).json()["run"]

    assert run["run_id"] == run_id
    assert run["status"] == "awaiting_approval"
    assert run["proof"]["holds"] is True
    assert run["cost"]["total_calls"] > 0
    assert "total_seconds" in run["timings"]


def test_a_span_returns_the_quote_inside_the_words_around_it(seeded):
    corpus, msa_id, _ = seeded
    client = TestClient(app)

    _, text = corpus.documents[msa_id]
    start = text.index("net 60 days")
    end = start + len("net 60 days")

    body = client.get(
        f"/documents/{msa_id}/span?start={start}&end={end}", headers=HEADERS
    ).json()

    assert body["quote"] == "net 60 days"
    # Context on both sides, and reassembling the three parts must reproduce the source exactly.
    assert body["before"] and body["after"]
    assert body["before"] + body["quote"] + body["after"] in text


def test_a_span_outside_the_document_is_refused_with_a_named_fix(seeded):
    corpus, msa_id, _ = seeded
    client = TestClient(app)
    response = client.get(f"/documents/{msa_id}/span?start=0&end=999999", headers=HEADERS)

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "Cause:" in detail and "Fix:" in detail


def test_an_inverted_span_is_refused(seeded):
    corpus, msa_id, _ = seeded
    client = TestClient(app)
    assert client.get(f"/documents/{msa_id}/span?start=50&end=10", headers=HEADERS).status_code == 422


def test_the_document_list_reports_classification_confidence(seeded):
    corpus, _, _ = seeded
    client = TestClient(app)
    body = client.get(f"/piles/{corpus.pile_id}/documents", headers=HEADERS).json()

    document = body["documents"][0]
    assert document["doc_type"] == "msa"
    assert 0 < document["confidence"] <= 1
    assert document["characters"] > 0


def test_the_review_endpoints_require_a_token(env):
    client = TestClient(app)
    assert client.get("/piles").status_code == 401


# --------------------------------------------------------------------------------------------
# Setup path: everything a reviewer needs without a terminal
# --------------------------------------------------------------------------------------------


def test_seeding_twice_converges_instead_of_failing(env, tmp_path):
    """`make up` runs the seed. A setup command that fails the second time it is run is a setup
    command that fails for everyone who tries it twice, which is exactly the person the
    'a stranger can run it' requirement is about."""
    from ledgerline.corpus.generate import generate_and_load

    first = generate_and_load(pile_name="twice", out_dir=tmp_path / "c", variant="a")
    second = generate_and_load(pile_name="twice", out_dir=tmp_path / "c", variant="a")

    assert first["created_pile"] is True
    assert second["created_pile"] is False
    assert second["pile_id"] == first["pile_id"], "re-seeding must reuse the pile, not fork it"
    assert second["documents"] == [], "no document was added the second time"
    assert len(second["skipped_already_present"]) == len(first["documents"])


def test_a_reviewer_can_go_from_empty_to_a_queue_through_http_alone(env, tmp_path):
    """The path the interface drives: create a pile, upload a document, queue a run. If any step
    here needed a shell, the interface would be a viewer rather than a tool."""
    from sqlalchemy import select

    import ledgerline.db as db_module
    from ledgerline.models import Run, RunStatus

    client = TestClient(app)

    pile_id = client.post("/piles", json={"name": "via-http"}, headers=HEADERS).json()["pile_id"]

    path = tmp_path / "msa.txt"
    path.write_text(MSA_TEXT, encoding="utf-8")
    uploaded = client.post(
        f"/piles/{pile_id}/documents",
        files={"file": ("msa.txt", path.read_bytes(), "text/plain")},
        headers=HEADERS,
    ).json()
    assert uploaded["created"] is True

    queued = client.post(f"/piles/{pile_id}/runs", json=[], headers=HEADERS)
    assert queued.status_code == 202

    with db_module.session_scope() as session:
        run = session.scalars(select(Run).where(Run.pile_id == pile_id)).one()
    assert run.status is RunStatus.queued

    listed = client.get("/piles", headers=HEADERS).json()["piles"]
    assert any(p["pile_id"] == pile_id and p["documents"] == 1 for p in listed)


def test_the_mcp_server_can_ingest_documents_too(env, tmp_path):
    """Behaviour 4 says a machine can run the whole flow. Without ingestion on the MCP surface,
    'the whole flow' would quietly exclude getting documents in."""
    import asyncio

    from ledgerline.mcp import server as mcp_server

    def call(tool_name, arguments):
        result = asyncio.run(mcp_server.mcp.call_tool(tool_name, arguments))
        if isinstance(result, tuple):
            result = result[-1]
        if isinstance(result, dict):
            return result
        content = getattr(result, "structured_content", None) or getattr(
            result, "structuredContent", None
        )
        return content

    created = call("create_pile", {"name": "via-mcp"})
    pile_id = created["pile_id"]

    path = tmp_path / "msa.txt"
    path.write_text(MSA_TEXT, encoding="utf-8")

    added = call("add_document", {"pile_id": pile_id, "path": str(path)})
    assert added["created"] is True

    # Content addressed: the same bytes again is a no-op, not an error and not a duplicate.
    again = call("add_document", {"pile_id": pile_id, "path": str(path)})
    assert again["created"] is False
    assert again["document_id"] == added["document_id"]

    piles = call("list_piles", {})["piles"]
    assert any(p["pile_id"] == pile_id and p["documents"] == 1 for p in piles)


def test_adding_a_document_that_does_not_exist_names_the_fix(env):
    import asyncio

    from ledgerline.mcp import server as mcp_server

    result = asyncio.run(
        mcp_server.mcp.call_tool("add_document", {"pile_id": "x", "path": "/nope/missing.txt"})
    )
    if isinstance(result, tuple):
        result = result[-1]
    payload = result if isinstance(result, dict) else (
        getattr(result, "structured_content", None) or getattr(result, "structuredContent", None)
    )
    assert "Cause:" in payload["error"] and "Fix:" in payload["error"]


# --------------------------------------------------------------------------------------------
# Behaviour 1: the decisions have to be visible, not merely recorded
# --------------------------------------------------------------------------------------------


def test_every_surface_shows_what_each_stage_decided(seeded):
    """A decision log that only the CLI prints makes "steps we can watch" true of the code and
    false of the product. All three surfaces have to carry it."""
    corpus, _, run_id = seeded
    client = TestClient(app)

    http = client.get(f"/runs/{run_id}", headers=HEADERS).json()["stage_log"]
    latest = client.get(f"/piles/{corpus.pile_id}/latest-run", headers=HEADERS).json()["run"]["stage_log"]

    import asyncio

    from ledgerline.mcp import server as mcp_server

    result = asyncio.run(mcp_server.mcp.call_tool("get_run_status", {"run_id": run_id}))
    if isinstance(result, tuple):
        result = result[-1]
    payload = result if isinstance(result, dict) else (
        getattr(result, "structured_content", None) or getattr(result, "structuredContent", None)
    )
    mcp = payload["stage_log"]

    for log in (http, latest, mcp):
        assert log, "a surface returned no decision log"
        stages = [entry["stage"] for entry in log]
        assert stages[0] == "intake" and stages[-1] == "gate"
        assert all(entry.get("decision") for entry in log), "a stage recorded no decision"

    assert [e["stage"] for e in http] == [e["stage"] for e in mcp]


def test_the_decision_log_records_the_reroute_not_just_the_stage(env, corpus):
    """The difference between an agentic system and a script with labels is that a decision can
    change the path. A log that lists stages proves nothing; one that names the decision that
    rerouted execution is the evidence."""
    corpus.add("mystery.txt", "Some pages of something.\n\nNo clear type.\n", "unknown", 0.2)
    run_id = corpus.queue_run()
    execute_run(run_id)

    client = TestClient(app)
    log = client.get(f"/runs/{run_id}", headers=HEADERS).json()["stage_log"]

    stages = [entry["stage"] for entry in log]
    # Nothing classified confidently, so extraction never ran: the path itself is different.
    assert stages == ["intake", "classify", "gate"]

    classify = next(entry for entry in log if entry["stage"] == "classify")
    assert classify["decision"] == "escalated_low_confidence"
    assert classify["escalated"] == 1


def test_a_document_can_be_read_with_its_citations_marked(seeded):
    corpus, msa_id, _ = seeded
    client = TestClient(app)
    body = client.get(f"/documents/{msa_id}", headers=HEADERS).json()

    assert body["text"], "no text returned"
    assert body["citations"], "no citations returned for a document that was cited"

    # Every marked range must resolve against the text that was returned, or the interface would
    # highlight the wrong words while looking correct.
    for citation in body["citations"]:
        assert 0 <= citation["char_start"] < citation["char_end"] <= len(body["text"])
        assert body["text"][citation["char_start"]:citation["char_end"]].strip()
        assert citation["label"]


def test_a_failed_run_records_why_where_a_reviewer_can_read_it(env, corpus, monkeypatch):
    """"See the worker log" is not an error message. It is an instruction to go somewhere the person
    reading it usually cannot reach — which, for anyone running this in Docker, is every time."""
    import ledgerline.db as db_module
    from ledgerline.models import Run, RunStatus
    from ledgerline.worker import run_forever

    corpus.add("msa.txt", MSA_TEXT, "msa")
    run_id = corpus.queue_run()

    def explode(*_args, **_kwargs):
        raise RuntimeError("bedrock credentials not found")

    monkeypatch.setattr("ledgerline.worker.execute_run", explode)
    # One pass of the claim loop, then stop.
    monkeypatch.setattr("ledgerline.worker._stopping", False)

    import ledgerline.worker as worker_module

    calls = {"n": 0}
    original_sleep = worker_module.time.sleep

    def stop_after_one(seconds):
        calls["n"] += 1
        worker_module._stopping = True
        original_sleep(0)

    monkeypatch.setattr(worker_module.time, "sleep", stop_after_one)
    try:
        run_forever()
    except Exception:
        pass
    finally:
        worker_module._stopping = False

    with db_module.session_scope() as session:
        run = session.get(Run, run_id)

    assert run.status is RunStatus.failed
    assert "bedrock credentials not found" in (run.error or "")
    assert "see worker log" not in (run.error or "").lower()

    client = TestClient(app)
    body = client.get(f"/runs/{run_id}", headers=HEADERS).json()
    assert "bedrock credentials not found" in body["error"]
