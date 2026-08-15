"""A source document that tries to give orders is data to report on, not commands to follow.

Two layers, deliberately. The structural layer is in `prompting.py`: source text never occupies an
instruction position, it always arrives inside a delimited data block with an explicit frame. This
module is the second layer, a hard-coded detector that turns instruction-shaped content into a
finding.

The hard-coded layer is not a patch on top of the intelligent one. It exists because the structural
defence cannot itself report what it defended against, and a reviewer needs to see the attempt.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "override_instruction",
        re.compile(
            r"\b(ignore|disregard|forget|override|bypass)\b[^.\n]{0,60}"
            r"\b(previous|prior|above|earlier|all)\b[^.\n]{0,40}"
            r"\b(instruction|instructions|rule|rules|prompt|context|direction)\w*\b",
            re.IGNORECASE,
        ),
    ),
    (
        "role_reassignment",
        re.compile(
            r"\b(you are now|act as|from now on you|your new (role|task|instruction))\b",
            re.IGNORECASE,
        ),
    ),
    (
        "decision_command",
        re.compile(
            r"\b(approve|accept|pass|clear|mark)\b[^.\n]{0,40}"
            r"\b(all|every|each|this)\b[^.\n]{0,40}"
            r"\b(invoice|invoices|item|items|finding|findings|claim|claims|change|changes)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "suppression_command",
        re.compile(
            r"\b(do not|don't|never)\b[^.\n]{0,40}"
            r"\b(report|flag|raise|surface|mention|record|log)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "system_frame_injection",
        re.compile(
            r"(</?(system|assistant|human)>|\[/?(INST|SYSTEM)\]|```system)",
            re.IGNORECASE,
        ),
    ),
    (
        "exfiltration_attempt",
        re.compile(
            r"\b(reveal|print|output|show)\b[^.\n]{0,40}"
            r"\b(system prompt|api key|credential|secret|instructions you)\b",
            re.IGNORECASE,
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class InjectionHit:
    pattern_id: str
    char_start: int
    char_end: int
    excerpt: str


def scan(text: str, *, context_chars: int = 30, max_hits: int = 50) -> list[InjectionHit]:
    """Find instruction-shaped content. Overlapping hits from different patterns are all kept:
    a reviewer should see every reason the document was flagged, not the first one only.
    """
    hits: list[InjectionHit] = []
    for pattern_id, pattern in _PATTERNS:
        for match in pattern.finditer(text):
            start = max(0, match.start() - context_chars)
            end = min(len(text), match.end() + context_chars)
            hits.append(
                InjectionHit(
                    pattern_id=pattern_id,
                    char_start=match.start(),
                    char_end=match.end(),
                    excerpt=text[start:end].replace("\n", " ").strip(),
                )
            )
            if len(hits) >= max_hits:
                return sorted(hits, key=lambda hit: hit.char_start)
    return sorted(hits, key=lambda hit: hit.char_start)
