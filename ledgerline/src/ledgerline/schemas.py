"""Contracts between stages, and the JSON schemas handed to the model as tool input schemas.

Every extracted fact carries a span. There is no shape in this file that lets a model return a
value without one, which is how behaviour 5 is enforced by structure rather than by asking nicely.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

DocType = Literal[
    "msa",
    "amendment",
    "sow",
    "rate_card",
    "purchase_order",
    "invoice",
    "notice",
    "correspondence",
    "unknown",
]

DOC_TYPES: tuple[str, ...] = (
    "msa",
    "amendment",
    "sow",
    "rate_card",
    "purchase_order",
    "invoice",
    "notice",
    "correspondence",
    "unknown",
)

PROMPT_VERSION = "1"


def sha256_json(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


class Evidence(BaseModel):
    document_id: str = ""
    document_name: str = ""
    char_start: int
    char_end: int
    quote_sha256: str = ""
    locator: str = ""

    @field_validator("char_end")
    @classmethod
    def _end_after_start(cls, end: int, info) -> int:
        start = info.data.get("char_start", 0)
        if end <= start:
            raise ValueError("char_end must be greater than char_start")
        return end


class ExtractedField(BaseModel):
    field_path: str
    value: Any = None
    char_start: int
    char_end: int
    confidence: float = 1.0


class ExtractionResult(BaseModel):
    fields: list[ExtractedField] = Field(default_factory=list)
    not_stated: list[str] = Field(default_factory=list)


class ClassificationResult(BaseModel):
    doc_type: DocType = "unknown"
    confidence: float = 0.0
    reason: str = ""


class Finding(BaseModel):
    rule_id: str
    severity: Literal["info", "warn", "breach"]
    title: str
    detail: str
    field_path: str | None = None
    evidence: list[Evidence] = Field(default_factory=list)

    def dedupe_key(self) -> str:
        return sha256_json([self.rule_id, self.field_path, self.title])[:64]


class Conflict(BaseModel):
    field_path: str
    values: list[Any]
    evidence: list[Evidence]
    confidence: float = 1.0
    detail: str = ""

    def dedupe_key(self) -> str:
        return sha256_json(["conflict", self.field_path, self.values])[:64]


class ProposedUpdate(BaseModel):
    field_path: str
    before: Any = None
    after: Any = None
    evidence: list[Evidence] = Field(default_factory=list)
    inputs_hash: str = ""
    caused_by_document_id: str | None = None

    def dedupe_key(self) -> str:
        return sha256_json(["update", self.field_path, self.after, self.inputs_hash])[:64]


# --------------------------------------------------------------------------------------------
# Tool input schemas handed to Bedrock. Kept as literal dicts because the model sees them verbatim
# and any pydantic-generated noise (titles, $defs) costs tokens and confuses smaller models.
# --------------------------------------------------------------------------------------------

CLASSIFY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "doc_type": {"type": "string", "enum": list(DOC_TYPES)},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "reason": {"type": "string"},
    },
    "required": ["doc_type", "confidence", "reason"],
}

EXTRACT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "fields": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "field_path": {"type": "string"},
                    "value": {},
                    "char_start": {"type": "integer", "minimum": 0},
                    "char_end": {"type": "integer", "minimum": 1},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
                "required": ["field_path", "value", "char_start", "char_end", "confidence"],
            },
        },
        "not_stated": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["fields", "not_stated"],
}

READ_SPAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "stated_value": {
            "description": "What this passage states for the named field, or null if it does not "
                           "state it. Report what the passage says, not what you expect.",
        },
        "note": {"type": "string"},
    },
    "required": ["stated_value", "note"],
}

JUDGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "triggered": {"type": "boolean"},
        "detail": {"type": "string"},
        "char_start": {"type": "integer", "minimum": 0},
        "char_end": {"type": "integer", "minimum": 0},
    },
    "required": ["triggered", "detail"],
}
