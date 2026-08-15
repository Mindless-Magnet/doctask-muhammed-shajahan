"""Merge verified facts into the register, detect conflicts, and keep updates cheap.

The expensive requirement in the brief is that a new document produces a focused update rather than
a re-run that happens to reproduce the same bytes, and that the system can prove the untouched parts
are untouched. Three things make that true, and all three live here.

`inputs_hash` binds a field to exactly what produced it: the text of every span it cites, plus the
prompt version and the playbook version. If none of those moved, the field cannot legitimately have
changed, so it is not recomputed.

`impacted_paths` decides which fields a newly arrived document could possibly affect, from the
document's type alone. An invoice cannot change the liability cap. Skipping those fields is not an
optimisation, it is the difference between an update and a re-run.

`build_proof` measures the claim instead of asserting it: it hashes every field the run did not
touch, before and after, and records both. If a single byte moved, the digests differ and the run
reports it.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from ledgerline.schemas import Conflict, Evidence, ProposedUpdate, sha256_json
from ledgerline.verification import VerifiedField

# Which register field prefixes a document of each type is allowed to influence.
# This is data, not logic: a new document type is an entry here, not a code change.
IMPACT_MAP: dict[str, tuple[str, ...]] = {
    "msa": ("contract.",),
    "amendment": ("contract.", "rate_table."),
    "sow": ("contract.spend_cap", "contract.payment_terms", "sow."),
    "rate_card": ("rate_table.",),
    "purchase_order": ("purchase_orders.",),
    "invoice": ("invoices.",),
    "notice": ("notice.", "contract.renewal."),
    "correspondence": ("notice.",),
    "unknown": (),
}

# Fields computed from other fields rather than extracted. Recomputed whenever any of their inputs
# is in the impacted set.
DERIVED_DEPENDENCIES: dict[str, tuple[str, ...]] = {
    "derived.notice_deadline": ("contract.effective_date", "contract.term_months",
                                "contract.renewal.notice_days"),
    "derived.invoiced_total": ("invoices.",),
}


@dataclass(frozen=True, slots=True)
class FieldSnapshot:
    field_path: str
    value: Any
    status: str
    inputs_hash: str
    value_sha256: str
    evidence: tuple[dict[str, Any], ...] = ()


@dataclass(slots=True)
class ReconcileResult:
    updates: list[ProposedUpdate] = field(default_factory=list)
    conflicts: list[Conflict] = field(default_factory=list)
    unchanged_paths: list[str] = field(default_factory=list)
    skipped_out_of_impact: list[str] = field(default_factory=list)


def compute_inputs_hash(
    quote_hashes: list[str], *, prompt_version: str, playbook_version: str
) -> str:
    payload = "|".join(
        ["p:" + prompt_version, "r:" + playbook_version, *sorted(quote_hashes)]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def impacted_paths(doc_types: set[str]) -> tuple[str, ...]:
    """Prefixes that documents of these types may affect. Empty means nothing is in scope."""
    prefixes: set[str] = set()
    for doc_type in doc_types:
        prefixes.update(IMPACT_MAP.get(doc_type, ()))
    for derived, dependencies in DERIVED_DEPENDENCIES.items():
        if any(dep.startswith(tuple(prefixes)) or dep in prefixes for dep in dependencies if prefixes):
            prefixes.add(derived)
    return tuple(sorted(prefixes))


def in_impact(path: str, prefixes: tuple[str, ...]) -> bool:
    return any(path.startswith(prefix) for prefix in prefixes)


def reconcile(
    *,
    existing: dict[str, FieldSnapshot],
    incoming: list[VerifiedField],
    prompt_version: str,
    playbook_version: str,
    impact_prefixes: tuple[str, ...],
    caused_by_document_id: str | None,
    conflict_confidence_floor: float,
) -> ReconcileResult:
    """Produce proposed updates and conflicts. Commits nothing and resolves nothing.

    A field whose inputs_hash is unchanged produces no update at all, which is what keeps the cost
    of an update proportional to the change.
    """
    result = ReconcileResult()

    grouped: dict[str, list[VerifiedField]] = {}
    for item in incoming:
        grouped.setdefault(item.field_path, []).append(item)

    for path, candidates in sorted(grouped.items()):
        if impact_prefixes and not in_impact(path, impact_prefixes):
            result.skipped_out_of_impact.append(path)
            continue

        distinct = {sha256_json(candidate.value) for candidate in candidates}
        if len(distinct) > 1:
            confidence = min(candidate.confidence for candidate in candidates)
            result.conflicts.append(
                Conflict(
                    field_path=path,
                    values=[candidate.value for candidate in candidates],
                    evidence=[candidate.evidence for candidate in candidates],
                    confidence=confidence,
                    detail=(
                        f"{len(candidates)} sources state different values for {path}. "
                        "Not resolved automatically."
                        + (
                            " Confidence is below the escalation floor."
                            if confidence < conflict_confidence_floor
                            else ""
                        )
                    ),
                )
            )
            continue

        chosen = candidates[0]
        new_hash = compute_inputs_hash(
            [candidate.evidence.quote_sha256 for candidate in candidates],
            prompt_version=prompt_version,
            playbook_version=playbook_version,
        )
        current = existing.get(path)

        if current is not None and current.inputs_hash == new_hash:
            result.unchanged_paths.append(path)
            continue

        if current is not None and sha256_json(current.value) == sha256_json(chosen.value):
            # Same value from a different source. Record the new citation, no value change.
            result.unchanged_paths.append(path)
            continue

        if current is not None and current.value is not None:
            # The register already says something different. That is a conflict, not an overwrite.
            result.conflicts.append(
                Conflict(
                    field_path=path,
                    values=[current.value, chosen.value],
                    evidence=[
                        Evidence(**raw) for raw in current.evidence
                    ] + [chosen.evidence],
                    confidence=chosen.confidence,
                    detail=(
                        f"A new source contradicts what the register already states for {path}. "
                        "Surfaced for review rather than silently replaced."
                    ),
                )
            )
            continue

        result.updates.append(
            ProposedUpdate(
                field_path=path,
                before=None if current is None else current.value,
                after=chosen.value,
                evidence=[candidate.evidence for candidate in candidates],
                inputs_hash=new_hash,
                caused_by_document_id=caused_by_document_id,
            )
        )

    return result


@dataclass(frozen=True, slots=True)
class UntouchedProofData:
    fields_recomputed: int
    fields_unchanged: int
    unchanged_digest: str
    mismatches: tuple[str, ...]
    detail: dict[str, Any]

    @property
    def holds(self) -> bool:
        return not self.mismatches

    def summary_line(self, *, documents: int, calls: int, cost_usd: float, seconds: float) -> str:
        verdict = "byte-identical" if self.holds else "CHANGED UNEXPECTEDLY"
        return (
            f"{documents} new document(s). {self.fields_recomputed} field(s) recomputed, "
            f"{self.fields_unchanged} {verdict}. {calls} model call(s), "
            f"${cost_usd:.4f}, {seconds:.1f}s."
        )


def build_proof(
    before: dict[str, FieldSnapshot],
    after: dict[str, FieldSnapshot],
    touched_paths: set[str],
) -> UntouchedProofData:
    """Prove the fields the run did not touch are byte-identical. Measured, not asserted."""
    untouched = sorted(set(before) - touched_paths)
    mismatches: list[str] = []
    digest = hashlib.sha256()

    for path in untouched:
        old = before[path]
        new = after.get(path)
        digest.update(path.encode("utf-8"))
        digest.update(old.value_sha256.encode("utf-8"))
        if new is None or new.value_sha256 != old.value_sha256:
            mismatches.append(path)

    return UntouchedProofData(
        fields_recomputed=len(touched_paths),
        fields_unchanged=len(untouched),
        unchanged_digest=digest.hexdigest(),
        mismatches=tuple(mismatches),
        detail={
            "untouched_paths": untouched,
            "touched_paths": sorted(touched_paths),
            "mismatched_paths": mismatches,
        },
    )
