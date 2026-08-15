"""The verifier is not the implementer.

The extractor proposes a value and a span. This module independently establishes whether the text
at that span supports the value, and records *how* it established it. Nothing reaches the register
without passing, which is what makes "it never bluffs" a property of the code.

Verification is graded, not binary, because the honest answer differs by case:

  EXACT       the value appears literally in the cited span
  NUMERIC     the value appears as a number, differently formatted (USD 1,000.00 for 1000)
  STRUCTURAL  a composite value whose every element verified, or a value that cannot be quoted
  JUDGED      the span states the value in different words, confirmed by a separate model pass
  UNSUPPORTED nothing established it

The first three are free and deterministic and settle most fields. JUDGED exists because a
correctly cited paraphrase is a real and common case, and rejecting it would leave a capability
that works only on the inputs it was tested against. It is a distinct status rather than a silent
pass: a reviewer sees which fields rest on judgement, and every run reports its grade mix.

The judge is never shown the proposed value as a claim to confirm. It is asked what the span
states, and the comparison happens afterwards in code. Asking a model "does this support X"
invites it to agree.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from ledgerline.ingest.extract_text import CanonicalDocument
from ledgerline.schemas import Evidence, ExtractedField

MAX_SPAN_CHARS = 600

_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_NUMBER = re.compile(r"-?\d[\d,]*(?:\.\d+)?")


class Grade(StrEnum):
    exact = "exact"
    numeric = "numeric"
    structural = "structural"
    judged = "judged"
    unsupported = "unsupported"


DETERMINISTIC_GRADES = frozenset({Grade.exact, Grade.numeric, Grade.structural})


@dataclass(frozen=True, slots=True)
class VerifiedField:
    field_path: str
    value: Any
    evidence: Evidence
    confidence: float
    grade: Grade = Grade.exact
    judge_note: str | None = None


@dataclass(frozen=True, slots=True)
class RejectedField:
    field_path: str
    value: Any
    reason: str
    # True when the model reported absence rather than a wrong citation — the caller's own
    # `not_stated` list, or a value shaped the same way in the wrong slot (see
    # `agent.stages.resolve_extracted_field`). Kept separate from `reason` because it changes
    # behaviour (no retry), not just wording.
    not_stated: bool = False


@dataclass(slots=True)
class VerificationReport:
    verified: list[VerifiedField] = field(default_factory=list)
    rejected: list[RejectedField] = field(default_factory=list)
    needs_judgement: list[tuple[ExtractedField, Evidence, str]] = field(default_factory=list)

    @property
    def all_passed(self) -> bool:
        return not self.rejected and not self.needs_judgement

    def grade_counts(self) -> dict[str, int]:
        counts = {grade.value: 0 for grade in Grade}
        for item in self.verified:
            counts[item.grade.value] += 1
        counts[Grade.unsupported.value] = len(self.rejected)
        return counts


# --------------------------------------------------------------------------------------------
# Deterministic layer
# --------------------------------------------------------------------------------------------


def _normalise(text: str) -> str:
    return _NON_ALNUM.sub("", text.lower())


def _numbers(text: str) -> set[str]:
    found = set()
    for match in _NUMBER.finditer(text):
        raw = match.group(0).replace(",", "")
        try:
            number = float(raw)
        except ValueError:
            continue
        found.add(f"{number:.4f}".rstrip("0").rstrip("."))
    return found


def _canonical_number(value: float) -> str:
    return f"{float(value):.4f}".rstrip("0").rstrip(".")


def grade_value(value: Any, quote: str) -> Grade:
    """How, if at all, does `quote` support `value`? No model call."""
    if value is None or isinstance(value, bool):
        # Neither is quotable. They rest on span existence alone, a weaker guarantee, so they are
        # graded structural rather than exact and counted separately in the run report.
        return Grade.structural

    if isinstance(value, (int, float)):
        return Grade.numeric if _canonical_number(value) in _numbers(quote) else Grade.unsupported

    if isinstance(value, str):
        normalised = _normalise(value)
        if not normalised:
            return Grade.unsupported
        if normalised in _normalise(quote):
            return Grade.exact
        value_numbers = _numbers(value)
        if value_numbers and value_numbers <= _numbers(quote):
            return Grade.numeric
        return Grade.unsupported

    if isinstance(value, list):
        if not value:
            return Grade.unsupported
        grades = [grade_value(element, quote) for element in value]
        return Grade.structural if all(g is not Grade.unsupported for g in grades) else Grade.unsupported

    if isinstance(value, dict):
        if not value:
            return Grade.unsupported
        grades = [grade_value(element, quote) for element in value.values()]
        return Grade.structural if all(g is not Grade.unsupported for g in grades) else Grade.unsupported

    return Grade.unsupported


# --------------------------------------------------------------------------------------------
# Judge layer
# --------------------------------------------------------------------------------------------


class SpanReader(Protocol):
    """Reads a span and reports what it states, without being told what answer is wanted."""

    def read_span(self, quote: str, field_path: str) -> tuple[Any, str]: ...


def _coerce_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        numbers = _numbers(value)
        if len(numbers) == 1:
            return float(next(iter(numbers)))
    return None


def values_agree(proposed: Any, read: Any) -> bool:
    if proposed is None or read is None:
        return proposed is read
    if isinstance(proposed, bool) or isinstance(read, bool):
        return bool(proposed) == bool(read)

    proposed_number = _coerce_number(proposed)
    read_number = _coerce_number(read)
    if proposed_number is not None and read_number is not None:
        return abs(proposed_number - read_number) < 0.005

    return _normalise(str(proposed)) == _normalise(str(read))


def apply_judgement(report: VerificationReport, reader: SpanReader) -> VerificationReport:
    """Resolve everything the deterministic layer could not settle.

    The reader is asked what the span states. The comparison happens here, in code. A span that
    states something else does not become supported because a model was willing to say yes.
    """
    for extracted, evidence, quote in report.needs_judgement:
        read_value, note = reader.read_span(quote, extracted.field_path)
        if values_agree(extracted.value, read_value):
            report.verified.append(
                VerifiedField(
                    field_path=extracted.field_path,
                    value=extracted.value,
                    evidence=evidence,
                    confidence=extracted.confidence,
                    grade=Grade.judged,
                    judge_note=note,
                )
            )
        else:
            report.rejected.append(
                RejectedField(
                    extracted.field_path,
                    extracted.value,
                    (
                        f"the cited span states {read_value!r}, not {extracted.value!r} "
                        f"({evidence.locator})"
                    ),
                )
            )
    report.needs_judgement = []
    return report


def reject_unjudged(report: VerificationReport) -> VerificationReport:
    """Used when the judge is unavailable. Unsettled fields become unsupported, never assumed."""
    for extracted, evidence, _ in report.needs_judgement:
        report.rejected.append(
            RejectedField(
                extracted.field_path,
                extracted.value,
                (
                    f"value is not literally stated in the cited span ({evidence.locator}) and the "
                    "judgement pass was unavailable, so it could not be established"
                ),
            )
        )
    report.needs_judgement = []
    return report


# --------------------------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------------------------


def verify_fields(
    fields: list[ExtractedField],
    document: CanonicalDocument,
    document_id: str,
    *,
    offset_shift: int = 0,
    allow_judgement: bool = True,
) -> VerificationReport:
    """`offset_shift` converts block-relative offsets from the model into document offsets."""
    report = VerificationReport()

    for extracted in fields:
        start = extracted.char_start - offset_shift
        end = extracted.char_end - offset_shift

        if end <= start:
            report.rejected.append(
                RejectedField(
                    extracted.field_path, extracted.value, "span end is not after span start"
                )
            )
            continue
        if start < 0 or end > len(document.text):
            report.rejected.append(
                RejectedField(
                    extracted.field_path,
                    extracted.value,
                    f"span {start}:{end} falls outside the document (length {len(document.text)})",
                )
            )
            continue
        if end - start > MAX_SPAN_CHARS:
            report.rejected.append(
                RejectedField(
                    extracted.field_path,
                    extracted.value,
                    f"span is {end - start} chars, above the {MAX_SPAN_CHARS} char ceiling; "
                    "a citation this wide does not locate the value",
                )
            )
            continue

        quote = document.quote(start, end)
        evidence = Evidence(
            document_id=document_id,
            document_name=document.source_name,
            char_start=start,
            char_end=end,
            quote_sha256=document.quote_sha256(start, end),
            locator=document.cite(start, end),
        )
        grade = grade_value(extracted.value, quote)

        if grade is not Grade.unsupported:
            report.verified.append(
                VerifiedField(
                    field_path=extracted.field_path,
                    value=extracted.value,
                    evidence=evidence,
                    confidence=extracted.confidence,
                    grade=grade,
                )
            )
        elif allow_judgement:
            report.needs_judgement.append((extracted, evidence, quote))
        else:
            report.rejected.append(
                RejectedField(
                    extracted.field_path,
                    extracted.value,
                    f"value is not stated in the cited span ({evidence.locator})",
                )
            )

    return report


def reverify_evidence(evidence: Evidence, document: CanonicalDocument) -> bool:
    """Confirm a stored citation still resolves against the source it was taken from.

    Called when a field is carried forward untouched during an incremental update. If a source is
    ever re-ingested with different bytes, this catches the drift instead of keeping a citation
    that no longer points anywhere.
    """
    try:
        return document.quote_sha256(evidence.char_start, evidence.char_end) == evidence.quote_sha256
    except IndexError:
        return False
