"""Persistent schema.

Two design points worth stating, because the rest of the system leans on them.

Optimistic concurrency lives on `Pile.register_version`. Two runs against the same pile execute in
parallel; only the commit critical section serialises. A committer whose version has moved rebases
and re-presents its conflicting items rather than clobbering.

`RegisterField.inputs_hash` is what makes an update cost like an update. It hashes the source span
text plus the rule and prompt versions that produced the field. If a new document does not change
that hash, the field is not recomputed and is proven byte-identical instead.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(UTC)


JSONType = JSON().with_variant(JSONB(), "postgresql")

# SQLite has no BIGSERIAL: an autoincrementing primary key must be plain INTEGER there. Postgres
# keeps BIGINT. Same schema, two dialects, one declaration.
AutoPK = BigInteger().with_variant(Integer, "sqlite")


class Base(DeclarativeBase):
    pass


class RunStatus(StrEnum):
    queued = "queued"
    running = "running"
    awaiting_approval = "awaiting_approval"
    committed = "committed"
    failed = "failed"


class DocumentStatus(StrEnum):
    received = "received"
    classified = "classified"
    extracted = "extracted"
    escalated = "escalated"
    rejected = "rejected"
    duplicate = "duplicate"


class FieldStatus(StrEnum):
    supported = "supported"
    unsupported = "unsupported"
    conflicted = "conflicted"


class ItemKind(StrEnum):
    update = "update"
    conflict = "conflict"
    finding = "finding"
    escalation = "escalation"


class ItemStatus(StrEnum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"
    superseded = "superseded"


class Pile(Base):
    __tablename__ = "piles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    register_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    documents: Mapped[list[SourceDocument]] = relationship(back_populates="pile")


class SourceDocument(Base):
    __tablename__ = "source_documents"
    __table_args__ = (
        UniqueConstraint("pile_id", "content_sha256", name="uq_document_content_per_pile"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    pile_id: Mapped[str] = mapped_column(ForeignKey("piles.id", ondelete="CASCADE"), index=True)
    filename: Mapped[str] = mapped_column(String(500))
    content_sha256: Mapped[str] = mapped_column(String(64), index=True)
    source_format: Mapped[str] = mapped_column(String(16))
    canonical_text: Mapped[str] = mapped_column(Text)
    locators: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, default=list)
    doc_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    doc_type_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[DocumentStatus] = mapped_column(
        Enum(DocumentStatus, native_enum=False), default=DocumentStatus.received
    )
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    pile: Mapped[Pile] = relationship(back_populates="documents")


class DocumentChunk(Base):
    """Only populated once a pile crosses the retrieval threshold. Below it, spans pass directly."""

    __tablename__ = "document_chunks"

    id: Mapped[int] = mapped_column(AutoPK, primary_key=True, autoincrement=True)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("source_documents.id", ondelete="CASCADE"), index=True
    )
    char_start: Mapped[int] = mapped_column(Integer)
    char_end: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float] | None] = mapped_column(JSONType, nullable=True)


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    pile_id: Mapped[str] = mapped_column(ForeignKey("piles.id", ondelete="CASCADE"), index=True)
    status: Mapped[RunStatus] = mapped_column(
        Enum(RunStatus, native_enum=False), default=RunStatus.queued, index=True
    )
    trigger: Mapped[str] = mapped_column(String(32), default="manual")
    worker_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    degraded: Mapped[bool] = mapped_column(Boolean, default=False)
    degraded_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    cost_report: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    stage_timings: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    new_document_ids: Mapped[list[str]] = mapped_column(JSONType, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RegisterField(Base):
    """One row per field of the vendor obligation register. This is the deliverable."""

    __tablename__ = "register_fields"
    __table_args__ = (UniqueConstraint("pile_id", "field_path", name="uq_field_per_pile"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    pile_id: Mapped[str] = mapped_column(ForeignKey("piles.id", ondelete="CASCADE"), index=True)
    field_path: Mapped[str] = mapped_column(String(200))
    value: Mapped[Any] = mapped_column(JSONType, nullable=True)
    status: Mapped[FieldStatus] = mapped_column(
        Enum(FieldStatus, native_enum=False), default=FieldStatus.supported
    )
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, default=list)
    inputs_hash: Mapped[str] = mapped_column(String(64), index=True)
    value_sha256: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_by_run: Mapped[str | None] = mapped_column(String(36), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class UnsupportedClaim(Base):
    """A claim the system declined to publish. Rendered in the register as 'not stated in sources'."""

    __tablename__ = "unsupported_claims"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    pile_id: Mapped[str] = mapped_column(ForeignKey("piles.id", ondelete="CASCADE"), index=True)
    run_id: Mapped[str] = mapped_column(String(36), index=True)
    field_path: Mapped[str] = mapped_column(String(200))
    attempted_value: Mapped[Any] = mapped_column(JSONType, nullable=True)
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class PendingItem(Base):
    """Everything that waits for a human. Approved and rejected items are decided independently."""

    __tablename__ = "pending_items"
    __table_args__ = (
        Index("ix_pending_pile_status", "pile_id", "status"),
        # The dedupe guard has to be a constraint, not a read-then-write in application code.
        # Two runs executing in parallel both read an empty set and both insert; only the database
        # can settle that race. Found by test_two_runs_on_one_pile_execute_in_parallel.
        UniqueConstraint("pile_id", "dedupe_key", name="uq_pending_item_per_pile"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    pile_id: Mapped[str] = mapped_column(ForeignKey("piles.id", ondelete="CASCADE"), index=True)
    run_id: Mapped[str] = mapped_column(String(36), index=True)
    kind: Mapped[ItemKind] = mapped_column(Enum(ItemKind, native_enum=False))
    field_path: Mapped[str | None] = mapped_column(String(200), nullable=True)
    title: Mapped[str] = mapped_column(String(400))
    detail: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, default=list)
    rule_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    dedupe_key: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[ItemStatus] = mapped_column(
        Enum(ItemStatus, native_enum=False), default=ItemStatus.pending
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class LedgerEntry(Base):
    """What changed, when, because of which source, under which approval."""

    __tablename__ = "change_ledger"

    id: Mapped[int] = mapped_column(AutoPK, primary_key=True, autoincrement=True)
    pile_id: Mapped[str] = mapped_column(ForeignKey("piles.id", ondelete="CASCADE"), index=True)
    run_id: Mapped[str] = mapped_column(String(36), index=True)
    item_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    field_path: Mapped[str] = mapped_column(String(200), index=True)
    action: Mapped[str] = mapped_column(String(32))
    before_value: Mapped[Any] = mapped_column(JSONType, nullable=True)
    after_value: Mapped[Any] = mapped_column(JSONType, nullable=True)
    before_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    after_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    caused_by_document_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    approved_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)


class UntouchedProof(Base):
    """Per-run evidence that fields the new source did not affect are byte-identical."""

    __tablename__ = "untouched_proofs"

    id: Mapped[int] = mapped_column(AutoPK, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(36), index=True)
    pile_id: Mapped[str] = mapped_column(String(36), index=True)
    fields_recomputed: Mapped[int] = mapped_column(Integer)
    fields_unchanged: Mapped[int] = mapped_column(Integer)
    unchanged_digest: Mapped[str] = mapped_column(String(64))
    detail: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
