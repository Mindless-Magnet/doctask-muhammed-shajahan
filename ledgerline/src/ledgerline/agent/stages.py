"""The stages. One file rather than eight, because eight files of forty lines each is filing, not
structure.

Every stage takes a StageContext and the state, does one thing, and returns a partial state plus a
log entry naming what it decided. Sessions are opened per stage and closed before any model call
returns, so a multi-second Bedrock round trip never holds a transaction open.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from ledgerline.agent.state import FIELD_SETS, RunState, path_allowed
from ledgerline.config import Settings
from ledgerline.ingest.extract_text import CanonicalDocument, Locator
from ledgerline.llm.client import ModelRequest, ModelUnavailableError, TieredModelClient
from ledgerline.models import DocumentStatus, RegisterField, SourceDocument
from ledgerline.prompting import (
    CLASSIFY_SYSTEM,
    EXTRACT_SYSTEM,
    JUDGE_SYSTEM,
    READ_SPAN_SYSTEM,
    block_offset,
    classify_user_content,
    extract_user_content,
    judge_user_content,
    read_span_user_content,
)
from ledgerline.register.reconcile import (
    FieldSnapshot,
    impacted_paths,
    reconcile,
)
from ledgerline.rules.engine import Playbook, Register, evaluate_deterministic
from ledgerline.schemas import (
    CLASSIFY_SCHEMA,
    DOC_TYPES,
    EXTRACT_SCHEMA,
    JUDGE_SCHEMA,
    PROMPT_VERSION,
    READ_SPAN_SCHEMA,
    ClassificationResult,
    ExtractedField,
    ExtractionResult,
    sha256_json,
)
from ledgerline.security.injection import scan
from ledgerline.verification import (
    Grade,
    VerifiedField,
    apply_judgement,
    reject_unjudged,
    verify_fields,
)


class ModelSpanReader:
    """Asks the model what a span states, without showing it the value being checked.

    A separate role from the extractor and a separate call. It resolves the case the deterministic
    layer cannot: a citation that is correct but paraphrased. The comparison between what it read
    and what was proposed happens in `verification.values_agree`, not here.
    """

    def __init__(self, client: TieredModelClient) -> None:
        self._client = client

    def read_span(self, quote: str, field_path: str) -> tuple[Any, str]:
        response = self._client.invoke(
            ModelRequest(
                tier="standard",
                stage="verify_judge",
                system=READ_SPAN_SYSTEM,
                user_content=read_span_user_content(quote, field_path),
                output_schema=READ_SPAN_SCHEMA,
                max_tokens=512,
            )
        )
        return response.data.get("stated_value"), str(response.data.get("note", ""))


@dataclass
class StageContext:
    settings: Settings
    client: TieredModelClient
    playbook: Playbook
    sessions: sessionmaker[Session]


def _log(stage: str, decision: str, **detail: Any) -> dict[str, Any]:
    return {"stage": stage, "decision": decision, "at": time.time(), **detail}


def _load_document(session: Session, document_id: str) -> tuple[SourceDocument, CanonicalDocument]:
    row = session.get(SourceDocument, document_id)
    if row is None:
        raise LookupError(f"document {document_id} not found")
    canonical = CanonicalDocument(
        text=row.canonical_text,
        locators=tuple(Locator(**loc) for loc in row.locators),
        content_sha256=row.content_sha256,
        source_format=row.source_format,
        source_name=row.filename,
    )
    return row, canonical


# --------------------------------------------------------------------------------------------
# 1. intake
# --------------------------------------------------------------------------------------------


def stage_intake(context: StageContext, state: RunState) -> dict[str, Any]:
    """Scan every document for instruction-shaped content before anything reads it for meaning.

    Running this first is deliberate. By the time a document reaches classification the reviewer
    already has a finding telling them it tried to give orders.
    """
    findings: list[dict[str, Any]] = []
    with context.sessions() as session:
        for document_id in state["document_ids"]:
            row, canonical = _load_document(session, document_id)
            for hit in scan(canonical.text):
                findings.append(
                    {
                        "rule_id": "INSTRUCTION_IN_SOURCE",
                        "severity": "warn",
                        "title": f"'{row.filename}' contains instruction-shaped text",
                        "detail": (
                            f"Pattern '{hit.pattern_id}' matched at "
                            f"{canonical.cite(hit.char_start, hit.char_end)}. "
                            f"Reported as content. It did not influence any decision in this run. "
                            f"Excerpt: {hit.excerpt}"
                        ),
                        "field_path": None,
                        "evidence": [
                            {
                                "document_id": document_id,
                                "document_name": row.filename,
                                "char_start": hit.char_start,
                                "char_end": hit.char_end,
                                "quote_sha256": canonical.quote_sha256(hit.char_start, hit.char_end),
                                "locator": canonical.cite(hit.char_start, hit.char_end),
                            }
                        ],
                    }
                )
    return {
        "injection_findings": findings,
        "stage_log": [
            _log("intake", "scanned", documents=len(state["document_ids"]), hits=len(findings))
        ],
    }


# --------------------------------------------------------------------------------------------
# 2. classify
# --------------------------------------------------------------------------------------------


def stage_classify(context: StageContext, state: RunState) -> dict[str, Any]:
    classified: dict[str, dict[str, Any]] = {}
    escalations: list[dict[str, Any]] = []
    degraded = state.get("degraded", False)
    degraded_reason = state.get("degraded_reason")

    for document_id in state["document_ids"]:
        with context.sessions() as session:
            row, canonical = _load_document(session, document_id)
            filename = row.filename
            text = canonical.text

        try:
            response = context.client.invoke(
                ModelRequest(
                    tier="cheap",
                    stage="classify",
                    system=CLASSIFY_SYSTEM,
                    user_content=classify_user_content(text, filename, DOC_TYPES),
                    output_schema=CLASSIFY_SCHEMA,
                )
            )
            result = ClassificationResult(**response.data)
        except ModelUnavailableError as exc:
            degraded, degraded_reason = True, str(exc)
            result = ClassificationResult(doc_type="unknown", confidence=0.0, reason=str(exc))

        floor = context.settings.classify_confidence_floor
        if result.doc_type == "unknown" or result.confidence < floor:
            escalations.append(
                {
                    "document_id": document_id,
                    "filename": filename,
                    "doc_type": result.doc_type,
                    "confidence": result.confidence,
                    "reason": result.reason,
                }
            )
            status = DocumentStatus.escalated
        else:
            classified[document_id] = {
                "doc_type": result.doc_type,
                "confidence": result.confidence,
            }
            status = DocumentStatus.classified

        with context.sessions() as session:
            row = session.get(SourceDocument, document_id)
            row.doc_type = result.doc_type
            row.doc_type_confidence = result.confidence
            row.status = status
            session.commit()

    return {
        "classified": classified,
        "escalations": escalations,
        "degraded": degraded,
        "degraded_reason": degraded_reason,
        "stage_log": [
            _log(
                "classify",
                "escalated_low_confidence" if escalations else "all_classified",
                classified=len(classified),
                escalated=len(escalations),
            )
        ],
    }


def route_after_classify(state: RunState) -> str:
    """A real path change: nothing confidently classified means nothing to extract from."""
    return "extract" if state.get("classified") else "gate"


# --------------------------------------------------------------------------------------------
# 3. extract
# --------------------------------------------------------------------------------------------


def _extract_once(
    context: StageContext,
    document_id: str,
    doc_type: str,
    canonical: CanonicalDocument,
    filename: str,
    *,
    only_paths: list[str] | None = None,
    tighten: bool = False,
) -> tuple[list[VerifiedField], list[dict[str, Any]], list[dict[str, Any]], bool, str | None]:
    field_paths = only_paths or FIELD_SETS.get(doc_type, [])
    if not field_paths:
        return [], [], [], False, None

    system = EXTRACT_SYSTEM
    if tighten:
        system += (
            "\nA previous attempt returned spans that did not contain the values they claimed. "
            "Quote the narrowest substring that literally states each value. If you cannot find "
            "such a substring, put the field in not_stated instead of guessing."
        )

    try:
        response = context.client.invoke(
            ModelRequest(
                tier="standard",
                stage="extract_retry" if tighten else "extract",
                system=system,
                user_content=extract_user_content(canonical.text, doc_type, field_paths),
                output_schema=EXTRACT_SCHEMA,
            )
        )
    except ModelUnavailableError as exc:
        return [], [], [], True, str(exc)

    result = ExtractionResult(**response.data)

    permitted: list[ExtractedField] = []
    blocked: list[dict[str, Any]] = []
    for field in result.fields:
        if path_allowed(doc_type, field.field_path):
            permitted.append(field)
        else:
            blocked.append(
                {
                    "document_id": document_id,
                    "filename": filename,
                    "doc_type": doc_type,
                    "field_path": field.field_path,
                    "reason": (
                        f"A document of type '{doc_type}' may not write '{field.field_path}'. "
                        "Proposal dropped at the write boundary and reported."
                    ),
                }
            )

    report = verify_fields(permitted, canonical, document_id, offset_shift=block_offset())
    if report.needs_judgement:
        try:
            report = apply_judgement(report, ModelSpanReader(context.client))
        except ModelUnavailableError:
            # The judge is unavailable. Unsettled fields become unsupported, never assumed.
            report = reject_unjudged(report)
    rejected = [
        {
            "field_path": item.field_path,
            "value": item.value,
            "reason": item.reason,
            "document_id": document_id,
        }
        for item in report.rejected
    ]
    for name in result.not_stated:
        rejected.append(
            {
                "field_path": name,
                "value": None,
                "reason": "not stated in this source",
                "document_id": document_id,
                "not_stated": True,
            }
        )
    return report.verified, rejected, blocked, False, None


def stage_extract(context: StageContext, state: RunState) -> dict[str, Any]:
    verified: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    degraded = state.get("degraded", False)
    degraded_reason = state.get("degraded_reason")

    for document_id, meta in state["classified"].items():
        with context.sessions() as session:
            row, canonical = _load_document(session, document_id)
            filename = row.filename

        fields, rejects, blocks, failed, reason = _extract_once(
            context, document_id, meta["doc_type"], canonical, filename
        )
        if failed:
            degraded, degraded_reason = True, reason
        verified.extend(_serialise(field) for field in fields)
        rejected.extend(rejects)
        blocked.extend(blocks)

        with context.sessions() as session:
            row = session.get(SourceDocument, document_id)
            row.status = DocumentStatus.extracted
            session.commit()

    retry_paths = sorted(
        {item["field_path"] for item in rejected if not item.get("not_stated")}
    )
    return {
        "verified_fields": verified,
        "rejected_fields": rejected,
        "blocked_paths": blocked,
        "retry_paths": retry_paths,
        "degraded": degraded,
        "degraded_reason": degraded_reason,
        "stage_log": [
            _log(
                "extract",
                "blocked_out_of_scope_writes" if blocked else "extracted",
                verified=len(verified),
                rejected=len(rejected),
                blocked=len(blocked),
                grades=_grade_mix(verified),
            )
        ],
    }


def _grade_mix(verified: list[dict[str, Any]]) -> dict[str, int]:
    """How each accepted claim was established. A reviewer reads this before trusting the run."""
    mix: dict[str, int] = {}
    for item in verified:
        grade = item.get("grade", "exact")
        mix[grade] = mix.get(grade, 0) + 1
    return mix


def route_after_extract(state: RunState) -> str:
    """One retry, then unsupported. A retry is a path change; an infinite loop is a bug."""
    if state.get("retry_paths") and state.get("retry_count", 0) < 1:
        return "extract_retry"
    return "reconcile"


def stage_extract_retry(context: StageContext, state: RunState) -> dict[str, Any]:
    verified = list(state.get("verified_fields", []))
    still_rejected: list[dict[str, Any]] = [
        item for item in state.get("rejected_fields", []) if item.get("not_stated")
    ]
    recovered = 0
    targets = set(state.get("retry_paths", []))

    for document_id, meta in state["classified"].items():
        paths = sorted(
            {
                item["field_path"]
                for item in state.get("rejected_fields", [])
                if item.get("document_id") == document_id
                and not item.get("not_stated")
                and item["field_path"] in targets
            }
        )
        if not paths:
            continue
        with context.sessions() as session:
            row, canonical = _load_document(session, document_id)
            filename = row.filename
        fields, rejects, blocks, failed, _ = _extract_once(
            context, document_id, meta["doc_type"], canonical, filename,
            only_paths=paths, tighten=True,
        )
        recovered += len(fields)
        verified.extend(_serialise(field) for field in fields)
        still_rejected.extend(rejects)
        still_rejected.extend(
            {
                "field_path": block["field_path"],
                "value": None,
                "reason": block["reason"],
                "document_id": document_id,
            }
            for block in blocks
        )

    return {
        "verified_fields": verified,
        "rejected_fields": still_rejected,
        "retry_count": state.get("retry_count", 0) + 1,
        "retry_paths": [],
        "stage_log": [
            _log("extract_retry", "retried_with_tighter_prompt", recovered=recovered),
        ],
    }


def _serialise(field: VerifiedField) -> dict[str, Any]:
    return {
        "field_path": field.field_path,
        "value": field.value,
        "confidence": field.confidence,
        "grade": field.grade.value,
        "judge_note": field.judge_note,
        "evidence": field.evidence.model_dump(),
    }


def _deserialise(raw: dict[str, Any]) -> VerifiedField:
    from ledgerline.schemas import Evidence

    return VerifiedField(
        field_path=raw["field_path"],
        value=raw["value"],
        confidence=raw.get("confidence", 1.0),
        grade=Grade(raw.get("grade", "exact")),
        judge_note=raw.get("judge_note"),
        evidence=Evidence(**raw["evidence"]),
    )


# --------------------------------------------------------------------------------------------
# 4. reconcile
# --------------------------------------------------------------------------------------------


def load_snapshots(session: Session, pile_id: str) -> dict[str, FieldSnapshot]:
    rows = session.scalars(
        select(RegisterField).where(RegisterField.pile_id == pile_id)
    ).all()
    return {
        row.field_path: FieldSnapshot(
            field_path=row.field_path,
            value=row.value,
            status=row.status.value if hasattr(row.status, "value") else str(row.status),
            inputs_hash=row.inputs_hash,
            value_sha256=row.value_sha256,
            evidence=tuple(row.evidence or ()),
        )
        for row in rows
    }


def stage_reconcile(context: StageContext, state: RunState) -> dict[str, Any]:
    with context.sessions() as session:
        existing = load_snapshots(session, state["pile_id"])

    doc_types = {meta["doc_type"] for meta in state["classified"].values()}
    prefixes = impacted_paths(doc_types)

    result = reconcile(
        existing=existing,
        incoming=[_deserialise(raw) for raw in state.get("verified_fields", [])],
        prompt_version=PROMPT_VERSION,
        playbook_version=context.playbook.source_sha256[:12],
        impact_prefixes=prefixes,
        caused_by_document_id=state["document_ids"][0] if state["document_ids"] else None,
        conflict_confidence_floor=context.settings.conflict_confidence_floor,
    )

    return {
        "updates": [update.model_dump() for update in result.updates],
        "conflicts": [conflict.model_dump() for conflict in result.conflicts],
        "unchanged_paths": result.unchanged_paths,
        "stage_log": [
            _log(
                "reconcile",
                "conflicts_surfaced" if result.conflicts else "merged",
                updates=len(result.updates),
                conflicts=len(result.conflicts),
                unchanged=len(result.unchanged_paths),
                out_of_impact=len(result.skipped_out_of_impact),
                impact_prefixes=list(prefixes),
            )
        ],
    }


# --------------------------------------------------------------------------------------------
# 5. examine
# --------------------------------------------------------------------------------------------


def _projected_register(
    existing: dict[str, FieldSnapshot], updates: list[dict[str, Any]]
) -> Register:
    """Rules run against the register as it would be if every proposed update were approved.

    Findings therefore describe the state the reviewer is being asked to accept, not the state
    before the run, which is the only version that is useful at the gate.
    """
    fields: dict[str, dict[str, Any]] = {
        path: {"value": snap.value, "status": snap.status, "evidence": list(snap.evidence)}
        for path, snap in existing.items()
    }
    for update in updates:
        fields[update["field_path"]] = {
            "value": update["after"],
            "status": "supported",
            "evidence": update.get("evidence", []),
        }
    return Register(fields)


def stage_examine(context: StageContext, state: RunState) -> dict[str, Any]:
    with context.sessions() as session:
        existing = load_snapshots(session, state["pile_id"])

    register = _projected_register(existing, state.get("updates", []))
    findings = [finding.model_dump() for finding in evaluate_deterministic(context.playbook, register)]

    findings.extend(state.get("injection_findings", []))

    for item in state.get("blocked_paths", []):
        findings.append(
            {
                "rule_id": "OUT_OF_SCOPE_WRITE",
                "severity": "warn",
                "title": f"'{item['filename']}' proposed a field outside its document type",
                "detail": item["reason"],
                "field_path": item["field_path"],
                "evidence": [],
            }
        )

    judged_findings, judge_calls, judge_failed = _run_judge_rules(context, state)
    findings.extend(judged_findings)

    degraded = state.get("degraded", False) or judge_failed
    decision = "deterministic_only" if judge_calls == 0 else "two_tier"
    if judge_failed:
        decision = "judge_unavailable_deterministic_only"

    return {
        "findings": findings,
        "degraded": degraded,
        "degraded_reason": (
            "the judge tier was unavailable; deterministic rules only"
            if judge_failed
            else state.get("degraded_reason")
        ),
        "stage_log": [
            _log(
                "examine",
                decision,
                deterministic_rules=len(context.playbook.deterministic),
                judge_rules_run=judge_calls,
                findings=len(findings),
            )
        ],
    }


def _run_judge_rules(
    context: StageContext, state: RunState
) -> tuple[list[dict[str, Any]], int, bool]:
    """Tier two. Only rules whose question determinism cannot settle, and only against documents
    of the types the rule declares. If the model is unavailable the run continues with the
    deterministic findings it already has, flagged as degraded, rather than failing."""
    if not context.playbook.judged or state.get("degraded"):
        return [], 0, bool(state.get("degraded"))

    findings: list[dict[str, Any]] = []
    calls = 0

    for rule in context.playbook.judged:
        for document_id, meta in state.get("classified", {}).items():
            if rule.applies_to_doc_types and meta["doc_type"] not in rule.applies_to_doc_types:
                continue
            with context.sessions() as session:
                row, canonical = _load_document(session, document_id)
                filename = row.filename

            try:
                response = context.client.invoke(
                    ModelRequest(
                        tier="deep",
                        stage="examine_judge",
                        system=JUDGE_SYSTEM,
                        user_content=judge_user_content(canonical.text, rule.rule_text or rule.title),
                        output_schema=JUDGE_SCHEMA,
                    )
                )
            except ModelUnavailableError:
                return findings, calls, True

            calls += 1
            if not response.data.get("triggered"):
                continue

            start = int(response.data.get("char_start", 0)) - block_offset()
            end = int(response.data.get("char_end", 0)) - block_offset()
            evidence: list[dict[str, Any]] = []
            if 0 <= start < end <= len(canonical.text):
                evidence.append(
                    {
                        "document_id": document_id,
                        "document_name": filename,
                        "char_start": start,
                        "char_end": end,
                        "quote_sha256": canonical.quote_sha256(start, end),
                        "locator": canonical.cite(start, end),
                    }
                )
            # A judged finding without a resolvable span is reported as a judgement, never dressed
            # up as a citation. The reviewer can see which findings rest on one.
            findings.append(
                {
                    "rule_id": rule.id,
                    "severity": rule.severity,
                    "title": f"{rule.title} ({filename})",
                    "detail": (
                        str(response.data.get("detail", ""))
                        + ("" if evidence else " No span resolved; this rests on judgement alone.")
                    ),
                    "field_path": None,
                    "evidence": evidence,
                }
            )

    return findings, calls, False


def route_after_reconcile(state: RunState) -> str:
    """Degradation is a path change, not an exception: the run continues without the model tier."""
    return "examine"


# --------------------------------------------------------------------------------------------
# 6. gate
# --------------------------------------------------------------------------------------------


def stage_gate(context: StageContext, state: RunState) -> dict[str, Any]:
    """Write every proposal as a pending item and stop. Nothing commits without a human decision."""
    from ledgerline.models import ItemKind, PendingItem, UnsupportedClaim

    created = {"update": 0, "conflict": 0, "finding": 0, "escalation": 0}

    with context.sessions() as session:
        existing_keys = {
            key
            for (key,) in session.execute(
                select(PendingItem.dedupe_key).where(
                    PendingItem.pile_id == state["pile_id"],
                    PendingItem.status.in_(["pending", "rejected"]),
                )
            )
        }

        def add(kind: ItemKind, key: str, title: str, detail: str, payload: dict, evidence: list,
                field_path: str | None = None, rule_id: str | None = None) -> None:
            """Insert one item, or do nothing if an identical one already exists.

            The in-memory set is a fast path only. The unique constraint on (pile_id, dedupe_key)
            is what actually settles it, because two runs in parallel both read an empty set. Each
            insert gets its own savepoint so one collision does not roll back the others.
            """
            if key in existing_keys:
                return
            existing_keys.add(key)
            try:
                with session.begin_nested():
                    session.add(
                        PendingItem(
                            pile_id=state["pile_id"],
                            run_id=state["run_id"],
                            kind=kind,
                            field_path=field_path,
                            title=title[:400],
                            detail=detail,
                            payload=payload,
                            evidence=evidence,
                            rule_id=rule_id,
                            dedupe_key=key,
                        )
                    )
            except IntegrityError:
                # Another run presented this exact item first. Nothing to add and nothing wrong.
                return
            created[kind.value] += 1

        for update in state.get("updates", []):
            add(
                ItemKind.update,
                sha256_json(["update", update["field_path"], update["after"], update["inputs_hash"]])[:64],
                f"Set {update['field_path']} to {update['after']}",
                f"Previously {update['before']!r}.",
                update,
                update.get("evidence", []),
                field_path=update["field_path"],
            )

        for conflict in state.get("conflicts", []):
            add(
                ItemKind.conflict,
                sha256_json(["conflict", conflict["field_path"], conflict["values"]])[:64],
                f"Sources disagree on {conflict['field_path']}",
                conflict["detail"],
                conflict,
                conflict.get("evidence", []),
                field_path=conflict["field_path"],
            )

        for finding in state.get("findings", []):
            add(
                ItemKind.finding,
                sha256_json(
                    ["finding", finding["rule_id"], finding.get("field_path"), finding["title"]]
                )[:64],
                finding["title"],
                finding["detail"],
                finding,
                finding.get("evidence", []),
                field_path=finding.get("field_path"),
                rule_id=finding["rule_id"],
            )

        for escalation in state.get("escalations", []):
            add(
                ItemKind.escalation,
                sha256_json(["escalation", escalation["document_id"]])[:64],
                f"Could not classify '{escalation['filename']}'",
                (
                    f"Best guess '{escalation['doc_type']}' at confidence "
                    f"{escalation['confidence']:.2f}, below the floor. "
                    f"Reason given: {escalation['reason']}"
                ),
                escalation,
                [],
            )

        for item in state.get("rejected_fields", []):
            session.add(
                UnsupportedClaim(
                    pile_id=state["pile_id"],
                    run_id=state["run_id"],
                    field_path=item["field_path"],
                    attempted_value=item.get("value"),
                    reason=item["reason"],
                )
            )

        session.commit()

    return {
        "stage_log": [
            _log("gate", "awaiting_human_decision", **created),
        ]
    }
