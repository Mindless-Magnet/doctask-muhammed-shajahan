"""MCP surface.

Behaviour four in its strongest form: another program drives the whole flow end to end, and the
gate is part of that flow rather than a property of a user interface. Approval is a tool. Whoever
drives the system, human or program, makes that call explicitly.

Every tool here is a thin wrapper over the same service functions the HTTP routes call. There is
one implementation and two surfaces; if the MCP server had logic of its own the two would drift and
one of them would quietly become wrong.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

# The SDK renamed FastMCP to MCPServer in 2.0 and both are in the wild. One shim beats pinning the
# whole project to whichever happens to be installed on the machine that ran it last.
try:
    from mcp.server.mcpserver import MCPServer as _Server  # SDK >= 2.0
except ImportError:  # pragma: no cover - depends on the installed SDK
    try:
        from mcp.server.fastmcp import FastMCP as _Server  # SDK 1.x
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "No usable MCP server class. "
            "Cause: neither mcp.server.mcpserver.MCPServer (SDK 2.x) nor "
            "mcp.server.fastmcp.FastMCP (SDK 1.x) could be imported. "
            "Fix: pip install 'mcp>=1.1'."
        ) from exc

from ledgerline.db import session_scope
from ledgerline.models import (
    ItemStatus,
    LedgerEntry,
    PendingItem,
    Pile,
    RegisterField,
    Run,
    RunStatus,
)
from ledgerline.service import RegisterVersionConflict, commit_approved, decide_items

mcp = _Server("ledgerline")


@mcp.tool()
def list_piles() -> dict[str, Any]:
    """Every pile, with how many documents it holds and how much is waiting on a person."""
    from ledgerline.api.review import list_piles as _list

    return _list()


@mcp.tool()
def create_pile(name: str) -> dict[str, Any]:
    """Create a pile. A pile is one vendor relationship: one contract file, however many documents."""
    with session_scope() as session:
        if session.scalars(select(Pile).where(Pile.name == name)).first():
            return {"error": f"a pile named '{name}' already exists"}
        pile = Pile(name=name)
        session.add(pile)
        session.flush()
        return {"pile_id": pile.id, "name": pile.name, "register_version": pile.register_version}


@mcp.tool()
def add_document(pile_id: str, path: str) -> dict[str, Any]:
    """Ingest a document from a local path into a pile.

    This is the ingestion half of behaviour 4: without it a program could drive every stage of the
    flow except getting documents in, which would make "a machine can run the whole flow" untrue in
    the one place it is easiest not to notice.

    Content-addressed by sha256, so re-adding the same bytes is a no-op and costs nothing. Takes a
    path rather than base64 because this server runs beside the files it reads; an agent that has
    the documents already has the filesystem.
    """
    from pathlib import Path

    from ledgerline.ingest.extract_text import UnsupportedFormatError, extract
    from ledgerline.models import SourceDocument

    source = Path(path).expanduser()
    if not source.is_file():
        return {
            "error": (
                f"'{path}' is not a file. "
                "Cause: the path does not resolve from this server's working directory. "
                "Fix: pass an absolute path."
            )
        }

    try:
        canonical = extract(source)
    except UnsupportedFormatError as exc:
        return {"error": str(exc)}

    with session_scope() as session:
        if session.get(Pile, pile_id) is None:
            return {"error": f"pile {pile_id} not found"}
        existing = session.scalars(
            select(SourceDocument).where(
                SourceDocument.pile_id == pile_id,
                SourceDocument.content_sha256 == canonical.content_sha256,
            )
        ).first()
        if existing is not None:
            return {
                "document_id": existing.id,
                "filename": existing.filename,
                "created": False,
                "note": "identical bytes were already in this pile; nothing was added",
            }

        document = SourceDocument(
            pile_id=pile_id,
            filename=source.name,
            content_sha256=canonical.content_sha256,
            source_format=canonical.source_format,
            canonical_text=canonical.text,
            locators=[
                {"kind": loc.kind, "ref": loc.ref, "start": loc.start, "end": loc.end}
                for loc in canonical.locators
            ],
        )
        session.add(document)
        session.flush()
        return {
            "document_id": document.id,
            "filename": document.filename,
            "characters": len(canonical.text),
            "created": True,
        }


@mcp.tool()
def start_run(pile_id: str, document_ids: list[str] | None = None) -> dict[str, Any]:
    """Queue a run over a pile. Returns immediately; poll get_run_status.

    This is the whole mechanism behind adding documents in batches over time: call add_document as
    many times as you like, whenever new paperwork shows up, then call start_run naming only the
    document_ids you just added. Only those documents are treated as new — fields no new document
    could affect are proven byte-identical instead of recomputed, so a later batch costs like an
    update, not a full re-run (get_run_status.cost is where that shows up).

    Passing document_ids=None (or []) instead runs the whole pile as if every document were new,
    which is correct the first time a pile is run but is the expensive path for every run after
    that. There is no other way to get the cheap, focused-update behaviour on this surface: the
    scoping decision is made here, by what you pass, not inferred by the system afterwards.
    """
    with session_scope() as session:
        if session.get(Pile, pile_id) is None:
            return {"error": f"pile {pile_id} not found"}
        run = Run(pile_id=pile_id, status=RunStatus.queued, new_document_ids=document_ids or [])
        session.add(run)
        session.flush()
        return {"run_id": run.id, "status": run.status.value}


@mcp.tool()
def get_run_status(run_id: str) -> dict[str, Any]:
    """Status, degradation, per-stage timings and the untouched-fields proof for one run."""
    with session_scope() as session:
        run = session.get(Run, run_id)
        if run is None:
            return {"error": f"run {run_id} not found"}
        return {
            "run_id": run.id,
            "pile_id": run.pile_id,
            "status": run.status.value,
            "attempt": run.attempt,
            "degraded": run.degraded,
            "degraded_reason": run.degraded_reason,
            "error": run.error,
            "timings": run.stage_timings,
            # Every stage the run moved through and what it decided there, including the decisions
            # that rerouted it. A program driving this system can read the path it took, not just
            # the outcome.
            "stage_log": run.stage_log or [],
            # Same field the HTTP API returns (api/app.py, api/review.py): without it, a program
            # driving this system over MCP alone could never answer behaviour 10 for its own run.
            "cost": run.cost_report or {"note": "run has not executed yet"},
        }


@mcp.tool()
def list_pending_items(pile_id: str, kind: str | None = None) -> dict[str, Any]:
    """Everything waiting on a human: proposed updates, conflicts, findings and escalations."""
    with session_scope() as session:
        pile = session.get(Pile, pile_id)
        if pile is None:
            return {"error": f"pile {pile_id} not found"}
        statement = select(PendingItem).where(
            PendingItem.pile_id == pile_id, PendingItem.status == ItemStatus.pending
        )
        if kind:
            statement = statement.where(PendingItem.kind == kind)
        items = session.scalars(statement).all()
        return {
            "pile_id": pile_id,
            "register_version": pile.register_version,
            "items": [
                {
                    "id": item.id,
                    "kind": item.kind.value,
                    "title": item.title,
                    "detail": item.detail,
                    "field_path": item.field_path,
                    "rule_id": item.rule_id,
                    "run_id": item.run_id,
                    "evidence": item.evidence,
                }
                for item in items
            ],
        }


@mcp.tool()
def approve_items(item_ids: list[str], decided_by: str, note: str | None = None) -> dict[str, Any]:
    """Approve specific items. Items not named here are untouched."""
    with session_scope() as session:
        return decide_items(session, item_ids, approve=True, decided_by=decided_by, note=note)


@mcp.tool()
def reject_items(item_ids: list[str], decided_by: str, note: str | None = None) -> dict[str, Any]:
    """Reject specific items. Rejecting one does not discard the rest."""
    with session_scope() as session:
        return decide_items(session, item_ids, approve=False, decided_by=decided_by, note=note)


@mcp.tool()
def commit_run(pile_id: str, run_id: str, expected_version: int, committed_by: str = "mcp") -> dict[str, Any]:
    """Apply approved updates. Fails without writing if the register moved since expected_version."""
    with session_scope() as session:
        try:
            return commit_approved(
                session, pile_id, run_id,
                expected_version=expected_version, committed_by=committed_by,
            )
        except RegisterVersionConflict as exc:
            return {"error": str(exc), "conflict": True, "actual_version": exc.actual}


@mcp.tool()
def get_register(pile_id: str) -> dict[str, Any]:
    """The deliverable. Every field carries the citation it was derived from."""
    with session_scope() as session:
        pile = session.get(Pile, pile_id)
        if pile is None:
            return {"error": f"pile {pile_id} not found"}
        fields = session.scalars(
            select(RegisterField).where(RegisterField.pile_id == pile_id)
        ).all()
        return {
            "pile_id": pile_id,
            "register_version": pile.register_version,
            "fields": {
                row.field_path: {
                    "value": row.value,
                    "status": row.status.value,
                    "evidence": row.evidence,
                }
                for row in fields
            },
        }


@mcp.tool()
def get_change_ledger(pile_id: str, field_path: str | None = None, limit: int = 100) -> dict[str, Any]:
    """What changed, when, and because of which source."""
    with session_scope() as session:
        statement = (
            select(LedgerEntry)
            .where(LedgerEntry.pile_id == pile_id)
            .order_by(LedgerEntry.created_at.desc())
            .limit(min(limit, 500))
        )
        if field_path:
            statement = statement.where(LedgerEntry.field_path == field_path)
        return {
            "entries": [
                {
                    "field_path": entry.field_path,
                    "action": entry.action,
                    "before": entry.before_value,
                    "after": entry.after_value,
                    "caused_by_document_id": entry.caused_by_document_id,
                    "approved_by": entry.approved_by,
                    "at": entry.created_at.isoformat(),
                }
                for entry in session.scalars(statement)
            ]
        }


@mcp.tool()
def explain_field(pile_id: str, field_path: str) -> dict[str, Any]:
    """Why the register says what it says: current value, its citations, and its full history."""
    with session_scope() as session:
        field = session.scalars(
            select(RegisterField).where(
                RegisterField.pile_id == pile_id, RegisterField.field_path == field_path
            )
        ).first()
        history = session.scalars(
            select(LedgerEntry)
            .where(LedgerEntry.pile_id == pile_id, LedgerEntry.field_path == field_path)
            .order_by(LedgerEntry.created_at)
        ).all()
        if field is None:
            return {
                "field_path": field_path,
                "value": None,
                "status": "not stated in sources",
                "history": [entry.action for entry in history],
            }
        return {
            "field_path": field_path,
            "value": field.value,
            "status": field.status.value,
            "version": field.version,
            "evidence": field.evidence,
            "inputs_hash": field.inputs_hash,
            "history": [
                {
                    "action": entry.action,
                    "before": entry.before_value,
                    "after": entry.after_value,
                    "approved_by": entry.approved_by,
                    "at": entry.created_at.isoformat(),
                }
                for entry in history
            ],
        }


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
