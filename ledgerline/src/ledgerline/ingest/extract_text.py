"""Convert source files into one canonical text string while preserving an exact offset map.

Everything downstream cites `(char_start, char_end)` into `CanonicalDocument.text`. If this module
is wrong, every citation in the system is wrong and no amount of model quality repairs it. That is
why it is the first thing built and the first thing tested.

Invariants, asserted in tests:
  * `text[loc.start:loc.end]` is exactly the content the locator describes, for every locator.
  * Locators are contiguous and non-overlapping, covering the whole of `text`.
  * Extraction is deterministic: the same bytes produce the same text and the same hash.
"""

from __future__ import annotations

import email
import email.policy
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

LocatorKind = Literal["page", "paragraph", "table_row", "cell", "header", "body", "line"]

BLOCK_SEPARATOR = "\n\n"

SUPPORTED_SUFFIXES = {".pdf", ".docx", ".eml", ".xlsx", ".txt", ".md"}


class UnsupportedFormatError(ValueError):
    """Raised with a named cause and a named fix, never a bare failure."""


@dataclass(frozen=True, slots=True)
class Locator:
    kind: LocatorKind
    ref: str
    start: int
    end: int

    def overlaps(self, start: int, end: int) -> bool:
        return start < self.end and end > self.start


@dataclass(frozen=True, slots=True)
class CanonicalDocument:
    text: str
    locators: tuple[Locator, ...]
    content_sha256: str
    source_format: str
    source_name: str

    def quote(self, start: int, end: int) -> str:
        if not (0 <= start < end <= len(self.text)):
            raise IndexError(f"span {start}:{end} outside document of length {len(self.text)}")
        return self.text[start:end]

    def quote_sha256(self, start: int, end: int) -> str:
        return hashlib.sha256(self.quote(start, end).encode("utf-8")).hexdigest()

    def locate(self, start: int, end: int) -> list[Locator]:
        return [loc for loc in self.locators if loc.overlaps(start, end)]

    def cite(self, start: int, end: int) -> str:
        """Human-readable citation, e.g. 'p.3' or 'Sheet1!B4, Sheet1!B5'."""
        return ", ".join(loc.ref for loc in self.locate(start, end)) or "unlocated"


class _Builder:
    """Accumulates blocks and records their exact offsets. The only way to build canonical text."""

    def __init__(self) -> None:
        self._parts: list[str] = []
        self._locators: list[Locator] = []
        self._cursor = 0

    def add(self, kind: LocatorKind, ref: str, content: str) -> None:
        content = content.replace("\r\n", "\n").replace("\r", "\n").strip()
        if not content:
            return
        if self._parts:
            self._parts.append(BLOCK_SEPARATOR)
            self._cursor += len(BLOCK_SEPARATOR)
        start = self._cursor
        self._parts.append(content)
        self._cursor += len(content)
        self._locators.append(Locator(kind=kind, ref=ref, start=start, end=self._cursor))

    def build(self, *, source_format: str, source_name: str, raw: bytes) -> CanonicalDocument:
        text = "".join(self._parts)
        return CanonicalDocument(
            text=text,
            locators=tuple(self._locators),
            content_sha256=hashlib.sha256(raw).hexdigest(),
            source_format=source_format,
            source_name=source_name,
        )


def _extract_pdf(raw: bytes, builder: _Builder) -> None:
    from io import BytesIO

    from pypdf import PdfReader

    reader = PdfReader(BytesIO(raw))
    total_chars = 0
    for index, page in enumerate(reader.pages, start=1):
        content = page.extract_text() or ""
        total_chars += len(content.strip())
        builder.add("page", f"p.{index}", content)
    if total_chars < 40:
        raise UnsupportedFormatError(
            "PDF has no usable text layer (likely a scan). "
            "Cause: no extractable characters found across any page. "
            "Fix: run OCR before ingesting, or supply the DOCX or TXT original. "
            "Scanned PDFs are out of scope for this system by design."
        )


def _extract_docx(raw: bytes, builder: _Builder) -> None:
    from io import BytesIO

    import docx

    document = docx.Document(BytesIO(raw))
    for index, paragraph in enumerate(document.paragraphs, start=1):
        style = (paragraph.style.name or "").lower()
        kind: LocatorKind = "header" if style.startswith("heading") else "paragraph"
        builder.add(kind, f"para {index}", paragraph.text)
    for t_index, table in enumerate(document.tables, start=1):
        for r_index, row in enumerate(table.rows, start=1):
            cells = " | ".join(cell.text.strip() for cell in row.cells)
            builder.add("table_row", f"table {t_index} row {r_index}", cells)


def _extract_eml(raw: bytes, builder: _Builder) -> None:
    message = email.message_from_bytes(raw, policy=email.policy.default)
    headers = "\n".join(
        f"{name}: {message[name]}" for name in ("From", "To", "Date", "Subject") if message[name]
    )
    builder.add("header", "headers", headers)
    body = message.get_body(preferencelist=("plain",))
    if body is not None:
        content = body.get_content()
        for index, block in enumerate(content.split("\n\n"), start=1):
            builder.add("body", f"body block {index}", block)


def _extract_xlsx(raw: bytes, builder: _Builder) -> None:
    from io import BytesIO

    import openpyxl

    workbook = openpyxl.load_workbook(BytesIO(raw), data_only=True, read_only=True)
    try:
        for sheet in workbook.worksheets:
            for row in sheet.iter_rows():
                for cell in row:
                    if cell.value is None:
                        continue
                    builder.add("cell", f"{sheet.title}!{cell.coordinate}", str(cell.value))
    finally:
        workbook.close()


def _extract_text(raw: bytes, builder: _Builder) -> None:
    content = raw.decode("utf-8", errors="replace")
    for index, block in enumerate(content.split("\n\n"), start=1):
        builder.add("line", f"block {index}", block)


_EXTRACTORS = {
    ".pdf": _extract_pdf,
    ".docx": _extract_docx,
    ".eml": _extract_eml,
    ".xlsx": _extract_xlsx,
    ".txt": _extract_text,
    ".md": _extract_text,
}


def extract(path: Path) -> CanonicalDocument:
    suffix = path.suffix.lower()
    if suffix not in _EXTRACTORS:
        raise UnsupportedFormatError(
            f"Unsupported format '{suffix}'. "
            f"Cause: no extractor registered. "
            f"Fix: convert to one of {sorted(SUPPORTED_SUFFIXES)} before ingesting."
        )
    raw = path.read_bytes()
    builder = _Builder()
    _EXTRACTORS[suffix](raw, builder)
    document = builder.build(source_format=suffix.lstrip("."), source_name=path.name, raw=raw)
    if not document.text.strip():
        raise UnsupportedFormatError(
            f"'{path.name}' produced no text. "
            f"Cause: the file parsed but every block was empty. "
            f"Fix: check the file is not corrupt or password protected."
        )
    return document
