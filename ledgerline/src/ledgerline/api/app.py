"""HTTP surface.

Every operation a human performs through the review interface is an operation here, approval
included. That is deliberate: the gate is part of the flow, not a property of the user interface,
so a program can drive the whole thing without anyone clicking. The MCP server in `mcp/server.py`
calls the same service functions these routes call, so the two surfaces cannot drift.

Auth is a single bearer token from the environment. Multi-tenancy and per-user identity are out of
scope for this build and stated as such rather than half-built.
"""

from __future__ import annotations

import hashlib
import shutil
import tempfile
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select

from ledgerline.api.deps import API_TOKEN, require_token
from ledgerline.api.review import router as review_router
from ledgerline.config import get_settings
from ledgerline.db import session_scope
from ledgerline.ingest.extract_text import UnsupportedFormatError, extract
from ledgerline.models import (
    ItemStatus,
    LedgerEntry,
    PendingItem,
    Pile,
    RegisterField,
    Run,
    RunStatus,
    SourceDocument,
    UnsupportedClaim,
    UntouchedProof,
)
from ledgerline.service import (
    RegisterVersionConflict,
    commit_approved,
    decide_items,
)

# Re-exported so existing callers and tests can keep importing it from here. The definition lives
# in deps.py because the review router needs it too, and two copies of an auth check is how they
# drift apart.
__all__ = ["API_TOKEN", "app", "require_token"]

app = FastAPI(
    title="Ledgerline",
    version="0.1.0",
    description="Vendor document file reconciliation with grounded citations and a human gate.",
)

# Read-only endpoints the review interface needs. Kept in their own module so the machine surfaces
# stay the minimum contract: the interface adds no operation of its own, because approve, reject and
# commit have to mean the same thing whoever calls them.
app.include_router(review_router)

Auth = Depends(require_token)


# --------------------------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------------------------


class CreatePile(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class DecideRequest(BaseModel):
    item_ids: list[str] = Field(min_length=1, max_length=500)
    decided_by: str = Field(min_length=1, max_length=120)
    note: str | None = None


class CommitRequest(BaseModel):
    run_id: str
    expected_version: int
    committed_by: str = "api"


# --------------------------------------------------------------------------------------------
# Piles and documents
# --------------------------------------------------------------------------------------------


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "config": get_settings().safe_dump()}


@app.post("/piles", dependencies=[Auth], status_code=201)
def create_pile(body: CreatePile) -> dict[str, Any]:
    with session_scope() as session:
        if session.scalars(select(Pile).where(Pile.name == body.name)).first():
            raise HTTPException(status_code=409, detail=f"pile '{body.name}' already exists")
        pile = Pile(name=body.name)
        session.add(pile)
        session.flush()
        return {"pile_id": pile.id, "name": pile.name, "register_version": pile.register_version}


@app.post("/piles/{pile_id}/documents", dependencies=[Auth], status_code=201)
def upload_document(pile_id: str, file: UploadFile = File(...)) -> dict[str, Any]:  # noqa: B008
    """Large files go to disk and are parsed from there, never held whole in memory."""
    suffix = Path(file.filename or "upload").suffix
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as handle:
        shutil.copyfileobj(file.file, handle, length=1024 * 1024)
        staged = Path(handle.name)

    try:
        canonical = extract(staged)
    except UnsupportedFormatError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        staged.unlink(missing_ok=True)

    with session_scope() as session:
        if session.get(Pile, pile_id) is None:
            raise HTTPException(status_code=404, detail=f"pile {pile_id} not found")
        existing = session.scalars(
            select(SourceDocument).where(
                SourceDocument.pile_id == pile_id,
                SourceDocument.content_sha256 == canonical.content_sha256,
            )
        ).first()
        if existing is not None:
            # Idempotent by content. Re-uploading the same bytes costs nothing and creates nothing.
            return {
                "document_id": existing.id,
                "filename": existing.filename,
                "duplicate_of": existing.id,
                "created": False,
            }

        document = SourceDocument(
            pile_id=pile_id,
            filename=file.filename or staged.name,
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


# --------------------------------------------------------------------------------------------
# Runs
# --------------------------------------------------------------------------------------------


@app.post("/piles/{pile_id}/runs", dependencies=[Auth], status_code=202)
def start_run(pile_id: str, document_ids: list[str] | None = None) -> dict[str, Any]:
    """Queues a run. A worker picks it up. The request does not wait on a model call."""
    with session_scope() as session:
        if session.get(Pile, pile_id) is None:
            raise HTTPException(status_code=404, detail=f"pile {pile_id} not found")
        run = Run(pile_id=pile_id, status=RunStatus.queued, new_document_ids=document_ids or [])
        session.add(run)
        session.flush()
        return {"run_id": run.id, "status": run.status.value}


@app.get("/runs/{run_id}", dependencies=[Auth])
def get_run(run_id: str) -> dict[str, Any]:
    with session_scope() as session:
        run = session.get(Run, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"run {run_id} not found")
        proof = session.scalars(
            select(UntouchedProof).where(UntouchedProof.run_id == run_id)
        ).first()
        return {
            "run_id": run.id,
            "pile_id": run.pile_id,
            "status": run.status.value,
            "attempt": run.attempt,
            "degraded": run.degraded,
            "degraded_reason": run.degraded_reason,
            "error": run.error,
            "stage_timings": run.stage_timings,
            # What the run decided at each stage, in order. Persisting this and serving it is what
            # makes "steps we can watch" a property of the system rather than of the CLI that
            # happened to print it.
            "stage_log": run.stage_log or [],
            "untouched_proof": None
            if proof is None
            else {
                "fields_recomputed": proof.fields_recomputed,
                "fields_unchanged": proof.fields_unchanged,
                "unchanged_digest": proof.unchanged_digest,
                "holds": not proof.detail.get("mismatched_paths"),
            },
        }


@app.get("/runs/{run_id}/cost", dependencies=[Auth])
def get_run_cost(run_id: str) -> dict[str, Any]:
    with session_scope() as session:
        run = session.get(Run, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"run {run_id} not found")
        return {
            "run_id": run_id,
            "cost": run.cost_report or {"note": "run has not executed yet"},
            "timings": run.stage_timings or {},
        }


# --------------------------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------------------------


@app.get("/piles/{pile_id}/pending", dependencies=[Auth])
def list_pending(pile_id: str, kind: str | None = None) -> dict[str, Any]:
    with session_scope() as session:
        statement = select(PendingItem).where(
            PendingItem.pile_id == pile_id, PendingItem.status == ItemStatus.pending
        )
        if kind:
            statement = statement.where(PendingItem.kind == kind)
        items = session.scalars(statement).all()
        pile = session.get(Pile, pile_id)
        if pile is None:
            raise HTTPException(status_code=404, detail=f"pile {pile_id} not found")
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
                    "payload": item.payload,
                    "evidence": item.evidence,
                }
                for item in items
            ],
        }


@app.post("/piles/{pile_id}/approve", dependencies=[Auth])
def approve(pile_id: str, body: DecideRequest) -> dict[str, Any]:
    with session_scope() as session:
        return decide_items(
            session, body.item_ids, approve=True, decided_by=body.decided_by, note=body.note
        )


@app.post("/piles/{pile_id}/reject", dependencies=[Auth])
def reject(pile_id: str, body: DecideRequest) -> dict[str, Any]:
    with session_scope() as session:
        return decide_items(
            session, body.item_ids, approve=False, decided_by=body.decided_by, note=body.note
        )


@app.post("/piles/{pile_id}/commit", dependencies=[Auth])
def commit(pile_id: str, body: CommitRequest) -> dict[str, Any]:
    with session_scope() as session:
        try:
            return commit_approved(
                session,
                pile_id,
                body.run_id,
                expected_version=body.expected_version,
                committed_by=body.committed_by,
            )
        except RegisterVersionConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc


# --------------------------------------------------------------------------------------------
# The deliverable
# --------------------------------------------------------------------------------------------


@app.get("/piles/{pile_id}/register", dependencies=[Auth])
def get_register(pile_id: str) -> dict[str, Any]:
    with session_scope() as session:
        pile = session.get(Pile, pile_id)
        if pile is None:
            raise HTTPException(status_code=404, detail=f"pile {pile_id} not found")
        fields = session.scalars(
            select(RegisterField).where(RegisterField.pile_id == pile_id)
        ).all()
        unsupported = session.scalars(
            select(UnsupportedClaim).where(UnsupportedClaim.pile_id == pile_id)
        ).all()
        ordered = sorted(fields, key=lambda row: row.field_path)
        digest = hashlib.sha256(
            "|".join(f"{row.field_path}={row.value_sha256}" for row in ordered).encode()
        ).hexdigest()
        return {
            "pile_id": pile_id,
            "register_version": pile.register_version,
            "digest": digest,
            "fields": {
                row.field_path: {
                    "value": row.value,
                    "status": row.status.value,
                    "evidence": row.evidence,
                    "version": row.version,
                }
                for row in fields
            },
            # Rendered as "not stated in sources" rather than omitted. A gap the system knows about
            # is more useful than a field that quietly does not exist.
            "not_stated": [
                {"field_path": row.field_path, "reason": row.reason} for row in unsupported
            ],
        }


@app.get("/piles/{pile_id}/ledger", dependencies=[Auth])
def get_ledger(pile_id: str, field_path: str | None = None, limit: int = 200) -> dict[str, Any]:
    """What changed, when, because of which source, under whose approval."""
    with session_scope() as session:
        statement = (
            select(LedgerEntry)
            .where(LedgerEntry.pile_id == pile_id)
            .order_by(LedgerEntry.created_at.desc())
            .limit(min(limit, 1000))
        )
        if field_path:
            statement = statement.where(LedgerEntry.field_path == field_path)
        entries = session.scalars(statement).all()
        return {
            "pile_id": pile_id,
            "entries": [
                {
                    "field_path": entry.field_path,
                    "action": entry.action,
                    "before": entry.before_value,
                    "after": entry.after_value,
                    "caused_by_document_id": entry.caused_by_document_id,
                    "approved_by": entry.approved_by,
                    "run_id": entry.run_id,
                    "at": entry.created_at.isoformat(),
                }
                for entry in entries
            ],
        }
