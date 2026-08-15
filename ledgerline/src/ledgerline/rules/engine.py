"""Two-tier rule evaluation.

Tier one is pure Python over the assembled register. It costs nothing, it is fully deterministic,
and it settles most of the playbook. Tier two calls a model, and only for rules whose question
determinism genuinely cannot answer.

Ordering is the cost story: the deep model never sees a question that a comparison could have
answered. It is also the correctness story, because a rule that can be decided arithmetically
should never be decided probabilistically.

A clean corpus produces zero findings and says so. An empty findings list is a real result here,
not a failure to run.
"""

from __future__ import annotations

import fnmatch
import hashlib
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from ledgerline.schemas import Evidence, Finding


class PlaybookError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Rule:
    id: str
    title: str
    severity: str
    tier: str
    check: dict[str, Any]
    message: str
    rule_text: str | None = None
    applies_to_doc_types: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Playbook:
    version: str
    name: str
    rules: tuple[Rule, ...]
    source_sha256: str

    @property
    def deterministic(self) -> tuple[Rule, ...]:
        return tuple(rule for rule in self.rules if rule.tier == "deterministic")

    @property
    def judged(self) -> tuple[Rule, ...]:
        return tuple(rule for rule in self.rules if rule.tier == "judge")


def load_playbook(path: Path) -> Playbook:
    raw = path.read_bytes()
    data = yaml.safe_load(raw)
    if not isinstance(data, dict) or "rules" not in data:
        raise PlaybookError(
            f"{path} is not a playbook. "
            "Cause: no top-level 'rules' key. "
            "Fix: see src/ledgerline/rules/playbook.yaml for the expected shape."
        )
    rules = []
    seen: set[str] = set()
    for entry in data["rules"]:
        rule_id = entry.get("id")
        if not rule_id:
            raise PlaybookError("every rule needs an id")
        if rule_id in seen:
            raise PlaybookError(f"duplicate rule id '{rule_id}'")
        seen.add(rule_id)
        tier = entry.get("tier", "deterministic")
        if tier not in {"deterministic", "judge"}:
            raise PlaybookError(f"rule '{rule_id}' has unknown tier '{tier}'")
        rules.append(
            Rule(
                id=rule_id,
                title=entry.get("title", rule_id),
                severity=entry.get("severity", "warn"),
                tier=tier,
                check=entry.get("check", {}),
                message=entry.get("message", entry.get("title", rule_id)),
                rule_text=entry.get("rule_text"),
                applies_to_doc_types=tuple(entry.get("applies_to_doc_types", ())),
            )
        )
    return Playbook(
        version=str(data.get("version", "0")),
        name=data.get("name", path.stem),
        rules=tuple(rules),
        source_sha256=hashlib.sha256(raw).hexdigest(),
    )


# --------------------------------------------------------------------------------------------


class Register:
    """Read view over register fields. Field paths are dotted; patterns may use '*'."""

    def __init__(self, fields: dict[str, dict[str, Any]]) -> None:
        self._fields = fields

    def get(self, path: str) -> Any:
        entry = self._fields.get(path)
        return None if entry is None else entry.get("value")

    def has(self, path: str) -> bool:
        entry = self._fields.get(path)
        return entry is not None and entry.get("status") == "supported"

    def evidence(self, *paths: str) -> list[Evidence]:
        collected: list[Evidence] = []
        for path in paths:
            entry = self._fields.get(path)
            if not entry:
                continue
            for raw in entry.get("evidence", []):
                collected.append(Evidence(**raw))
        return collected

    def match(self, pattern: str) -> list[tuple[str, Any]]:
        return [
            (path, entry.get("value"))
            for path, entry in sorted(self._fields.items())
            if fnmatch.fnmatchcase(path, pattern)
        ]

    def sibling(self, path: str, sibling_leaf: str) -> Any:
        parent = path.rsplit(".", 1)[0]
        return self.get(f"{parent}.{sibling_leaf}")

    def sibling_path(self, path: str, sibling_leaf: str) -> str:
        return f"{path.rsplit('.', 1)[0]}.{sibling_leaf}"


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.replace(",", "").replace("$", "").strip()
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


def _as_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip()[:10])
        except ValueError:
            return None
    return None


# --------------------------------------------------------------------------------------------
# Deterministic checks. Each returns a list of findings for one rule.
# --------------------------------------------------------------------------------------------


def _check_required(rule: Rule, register: Register) -> list[Finding]:
    field = rule.check["field"]
    if register.has(field):
        return []
    return [
        Finding(
            rule_id=rule.id,
            severity=rule.severity,
            title=rule.title,
            detail=rule.message.format(value="not stated", limit=""),
            field_path=field,
        )
    ]


def _check_numeric_bound(rule: Rule, register: Register, *, maximum: bool) -> list[Finding]:
    field = rule.check["field"]
    limit = _as_number(rule.check.get("limit"))
    value = _as_number(register.get(field))
    if value is None or limit is None:
        return []
    breached = value > limit if maximum else value < limit
    if not breached:
        return []
    return [
        Finding(
            rule_id=rule.id,
            severity=rule.severity,
            title=rule.title,
            detail=rule.message.format(value=_fmt(value), limit=_fmt(limit)),
            field_path=field,
            evidence=register.evidence(field),
        )
    ]


def _check_conditional_numeric_min(rule: Rule, register: Register) -> list[Finding]:
    condition_value = register.get(rule.check["condition_field"])
    if str(condition_value).lower() != str(rule.check["condition_equals"]).lower():
        return []
    field = rule.check["field"]
    limit = _as_number(rule.check.get("limit"))
    value = _as_number(register.get(field))
    if limit is None:
        return []
    if value is None:
        detail = rule.message.format(value="not stated", limit=_fmt(limit))
    elif value >= limit:
        return []
    else:
        detail = rule.message.format(value=_fmt(value), limit=_fmt(limit))
    return [
        Finding(
            rule_id=rule.id,
            severity=rule.severity,
            title=rule.title,
            detail=detail,
            field_path=field,
            evidence=register.evidence(rule.check["condition_field"], field),
        )
    ]


def _check_aggregate_max(rule: Rule, register: Register) -> list[Finding]:
    limit = _as_number(register.get(rule.check["limit_field"]))
    if limit is None:
        return []
    matched = register.match(rule.check["pattern"])
    total = sum(number for _, value in matched if (number := _as_number(value)) is not None)
    if total <= limit:
        return []
    return [
        Finding(
            rule_id=rule.id,
            severity=rule.severity,
            title=rule.title,
            detail=rule.message.format(value=_fmt(total), limit=_fmt(limit)),
            field_path=rule.check["limit_field"],
            evidence=register.evidence(
                rule.check["limit_field"], *[path for path, _ in matched]
            )[:12],
        )
    ]


def _check_reference_exists(rule: Rule, register: Register) -> list[Finding]:
    targets = {
        str(value).strip().upper()
        for _, value in register.match(rule.check["target_pattern"])
        if value is not None
    }
    findings: list[Finding] = []
    for path, value in register.match(rule.check["pattern"]):
        if value is None:
            continue
        if str(value).strip().upper() in targets:
            continue
        findings.append(
            Finding(
                rule_id=rule.id,
                severity=rule.severity,
                title=rule.title,
                detail=rule.message.format(value=value, limit=""),
                field_path=path,
                evidence=register.evidence(path),
            )
        )
    return findings


def _check_date_before(rule: Rule, register: Register) -> list[Finding]:
    field = rule.check["field"]
    actual = _as_date(register.get(field))
    limit = _as_date(register.get(rule.check["limit_field"]))
    if actual is None or limit is None or actual <= limit:
        return []
    return [
        Finding(
            rule_id=rule.id,
            severity=rule.severity,
            title=rule.title,
            detail=rule.message.format(value=actual.isoformat(), limit=limit.isoformat()),
            field_path=field,
            evidence=register.evidence(field, rule.check["limit_field"]),
        )
    ]


def _check_rate_consistency(rule: Rule, register: Register) -> list[Finding]:
    """Which rate was in force on the invoice date, and did the invoice bill it?

    A deliberate hard-coded evaluator. The question is arithmetic once the facts are extracted, and
    handing it to a model would make a settled comparison probabilistic.
    """
    schedule: list[tuple[date, float, str]] = []
    for path, value in register.match(rule.check["rate_pattern"]):
        price = _as_number(value)
        effective = _as_date(
            register.sibling(path, rule.check["rate_effective_pattern"].rsplit(".", 1)[-1])
        )
        if price is not None and effective is not None:
            schedule.append((effective, price, path))
    if not schedule:
        return []
    schedule.sort(key=lambda row: row[0])

    findings: list[Finding] = []
    for path, value in register.match(rule.check["invoice_price_pattern"]):
        billed = _as_number(value)
        invoice_date = _as_date(
            register.sibling(path, rule.check["invoice_date_pattern"].rsplit(".", 1)[-1])
        )
        if billed is None or invoice_date is None:
            continue
        in_force = [row for row in schedule if row[0] <= invoice_date]
        if not in_force:
            continue
        effective_from, expected, rate_path = in_force[-1]
        if abs(billed - expected) < 0.005:
            continue
        findings.append(
            Finding(
                rule_id=rule.id,
                severity=rule.severity,
                title=rule.title,
                detail=(
                    rule.message.format(value=_fmt(billed), limit=_fmt(expected))
                    + f" Rate effective from {effective_from.isoformat()}."
                ),
                field_path=path,
                evidence=register.evidence(
                    path, register.sibling_path(path, "date"), rate_path
                ),
            )
        )
    return findings


_CHECKS = {
    "required": _check_required,
    "numeric_max": lambda rule, reg: _check_numeric_bound(rule, reg, maximum=True),
    "numeric_min": lambda rule, reg: _check_numeric_bound(rule, reg, maximum=False),
    "conditional_numeric_min": _check_conditional_numeric_min,
    "aggregate_max": _check_aggregate_max,
    "reference_exists": _check_reference_exists,
    "date_before": _check_date_before,
    "rate_consistency": _check_rate_consistency,
}


def _fmt(number: float) -> str:
    return f"{number:,.2f}".rstrip("0").rstrip(".") if number % 1 else f"{number:,.0f}"


def evaluate_deterministic(playbook: Playbook, register: Register) -> list[Finding]:
    findings: list[Finding] = []
    for rule in playbook.deterministic:
        check_type = rule.check.get("type")
        handler = _CHECKS.get(check_type)
        if handler is None:
            raise PlaybookError(
                f"rule '{rule.id}' uses unknown check type '{check_type}'. "
                f"Cause: no evaluator registered. "
                f"Fix: add one to rules/engine.py or correct the playbook. Known types: "
                f"{sorted(_CHECKS)}"
            )
        findings.extend(handler(rule, register))
    return findings
