"""Endpoints the review interface needs that the machine surfaces did not.

A separate router rather than additions to `app.py`, so this drops in without touching a file that
is still changing. Everything here is read-only: the interface performs no operation that the API
and the MCP server do not already expose, because approval has to mean the same thing whoever calls
it.

The one genuinely new capability is `/documents/{id}/span`. The API already returns evidence as
offsets, which is what a program wants; a person needs to see the quoted words sitting in the
sentence around them, and computing that in the browser would mean shipping whole documents to the
client. So it is computed here, from the same canonical text the citation was verified against.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import desc, func, select

from ledgerline.api.deps import require_token
from ledgerline.db import session_scope
from ledgerline.models import (
    ItemStatus,
    PendingItem,
    Pile,
    RegisterField,
    Run,
    SourceDocument,
    UnsupportedClaim,
    UntouchedProof,
)

# Applied at the router, not per route: a read-only endpoint is the easiest place to leave an
# authentication hole, because nothing about it looks dangerous. Declaring it once means a new
# endpoint added to this file cannot ship without it.
router = APIRouter(tags=["review"], dependencies=[Depends(require_token)])

MAX_CONTEXT_CHARS = 400


@router.get("/piles")
def list_piles() -> dict[str, Any]:
    """Every pile, with enough counts for the reviewer to see where the work is."""
    with session_scope() as session:
        piles = session.scalars(select(Pile).order_by(desc(Pile.created_at))).all()

        pending_counts = dict(
            session.execute(
                select(PendingItem.pile_id, func.count())
                .where(PendingItem.status == ItemStatus.pending)
                .group_by(PendingItem.pile_id)
            ).all()
        )
        field_counts = dict(
            session.execute(
                select(RegisterField.pile_id, func.count()).group_by(RegisterField.pile_id)
            ).all()
        )
        doc_counts = dict(
            session.execute(
                select(SourceDocument.pile_id, func.count()).group_by(SourceDocument.pile_id)
            ).all()
        )

        return {
            "piles": [
                {
                    "pile_id": pile.id,
                    "name": pile.name,
                    "register_version": pile.register_version,
                    "documents": doc_counts.get(pile.id, 0),
                    "pending": pending_counts.get(pile.id, 0),
                    "committed_fields": field_counts.get(pile.id, 0),
                }
                for pile in piles
            ]
        }


@router.get("/piles/{pile_id}/latest-run")
def latest_run(pile_id: str) -> dict[str, Any]:
    """The most recent run, its cost, and its untouched-fields proof.

    The proof line is the clearest single statement this system makes, so the interface leads with
    it rather than burying it behind a details panel.
    """
    with session_scope() as session:
        if session.get(Pile, pile_id) is None:
            raise HTTPException(status_code=404, detail=f"pile {pile_id} not found")

        run = session.scalars(
            select(Run).where(Run.pile_id == pile_id).order_by(desc(Run.created_at)).limit(1)
        ).first()
        if run is None:
            return {"run": None}

        proof = session.scalars(
            select(UntouchedProof).where(UntouchedProof.run_id == run.id)
        ).first()

        unsupported = session.scalars(
            select(UnsupportedClaim).where(UnsupportedClaim.run_id == run.id)
        ).all()

        return {
            "run": {
                "run_id": run.id,
                "status": run.status.value,
                "trigger": run.trigger,
                "attempt": run.attempt,
                "degraded": run.degraded,
                "degraded_reason": run.degraded_reason,
                "error": run.error,
                "created_at": run.created_at.isoformat(),
                "timings": run.stage_timings or {},
                "stage_log": run.stage_log or [],
                "cost": run.cost_report or {},
                "proof": None
                if proof is None
                else {
                    "fields_recomputed": proof.fields_recomputed,
                    "fields_unchanged": proof.fields_unchanged,
                    "holds": not proof.detail.get("mismatched_paths"),
                    "mismatched": proof.detail.get("mismatched_paths", []),
                },
                "not_stated": [
                    {"field_path": claim.field_path, "reason": claim.reason}
                    for claim in unsupported
                ],
            }
        }


@router.get("/documents/{document_id}/span")
def document_span(
    document_id: str, start: int, end: int, context: int = 220
) -> dict[str, Any]:
    """The cited words, plus the text on either side of them.

    A reviewer cannot judge a citation from the quote alone: "net 45 days" is only meaningful with
    the clause around it. The span is returned separately from its context rather than wrapped in
    markup, so the client decides how to render it and never has to parse HTML out of an API.
    """
    if end <= start:
        raise HTTPException(status_code=422, detail="end must be greater than start")
    context = max(0, min(context, MAX_CONTEXT_CHARS))

    with session_scope() as session:
        document = session.get(SourceDocument, document_id)
        if document is None:
            raise HTTPException(status_code=404, detail=f"document {document_id} not found")

        text = document.canonical_text
        if start < 0 or end > len(text):
            raise HTTPException(
                status_code=422,
                detail=(
                    f"span {start}:{end} falls outside '{document.filename}', "
                    f"which is {len(text)} characters. "
                    "Cause: the citation does not resolve against the stored document. "
                    "Fix: re-run extraction for this document; the source may have been replaced."
                ),
            )

        before_from = max(0, start - context)
        after_to = min(len(text), end + context)

        return {
            "document_id": document_id,
            "filename": document.filename,
            "doc_type": document.doc_type,
            "before": text[before_from:start],
            "quote": text[start:end],
            "after": text[end:after_to],
            "truncated_start": before_from > 0,
            "truncated_end": after_to < len(text),
        }


@router.get("/documents/{document_id}")
def read_document(document_id: str) -> dict[str, Any]:
    """The whole canonical text, plus every span anything in this pile cites from it.

    A reviewer judging a citation wants to see where it sits in the document, not only the sentence
    around it. Returning the citations alongside the text rather than as separate calls means the
    client can mark them without a second round trip per span, and means the marks come from the
    same stored evidence the verifier checked rather than from the client re-deriving them.
    """
    with session_scope() as session:
        document = session.get(SourceDocument, document_id)
        if document is None:
            raise HTTPException(status_code=404, detail=f"document {document_id} not found")

        cited: list[dict[str, Any]] = []
        seen: set[tuple[int, int]] = set()

        rows = session.scalars(
            select(PendingItem).where(PendingItem.pile_id == document.pile_id)
        ).all()
        fields = session.scalars(
            select(RegisterField).where(RegisterField.pile_id == document.pile_id)
        ).all()

        for source, label_of in ((rows, lambda r: r.title), (fields, lambda f: f.field_path)):
            for row in source:
                for evidence in row.evidence or []:
                    if evidence.get("document_id") != document_id:
                        continue
                    key = (evidence["char_start"], evidence["char_end"])
                    if key in seen:
                        continue
                    seen.add(key)
                    cited.append(
                        {
                            "char_start": evidence["char_start"],
                            "char_end": evidence["char_end"],
                            "label": label_of(row),
                            "locator": evidence.get("locator", ""),
                        }
                    )

        cited.sort(key=lambda c: c["char_start"])

        return {
            "document_id": document.id,
            "filename": document.filename,
            "doc_type": document.doc_type,
            "confidence": document.doc_type_confidence,
            "status": document.status.value,
            "format": document.source_format,
            "text": document.canonical_text,
            "citations": cited,
        }


@router.get("/piles/{pile_id}/documents")
def list_documents(pile_id: str) -> dict[str, Any]:
    with session_scope() as session:
        if session.get(Pile, pile_id) is None:
            raise HTTPException(status_code=404, detail=f"pile {pile_id} not found")
        documents = session.scalars(
            select(SourceDocument).where(SourceDocument.pile_id == pile_id)
        ).all()
        return {
            "documents": [
                {
                    "document_id": doc.id,
                    "filename": doc.filename,
                    "doc_type": doc.doc_type,
                    "confidence": doc.doc_type_confidence,
                    "status": doc.status.value,
                    "format": doc.source_format,
                    "characters": len(doc.canonical_text),
                }
                for doc in documents
            ]
        }
