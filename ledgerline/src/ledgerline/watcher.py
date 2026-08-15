"""Watched location.

New documents keep arriving. Each arrival produces a focused update to the deliverable, and this is
the thing that notices the arrival.

Polling, not inotify. A poll is portable, survives a restart with no state to rebuild, and works
over a network mount, which is where these folders actually live. inotify would save a second of
latency on a workload measured in minutes.

Two properties matter more than the mechanism:

Content addressing, not filenames. A file is identified by the sha256 of its bytes, so re-dropping
the same document, or a mail client saving it twice under a different name, costs nothing and
creates nothing. This is what makes the watcher idempotent.

Stability before ingestion. A file still being copied is a file whose bytes will change. Each
candidate must report the same size on two consecutive polls before it is read, otherwise a large
upload gets ingested half-written and the citations point into a truncated document.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select

from ledgerline.config import get_settings
from ledgerline.db import session_scope
from ledgerline.ingest.extract_text import SUPPORTED_SUFFIXES, UnsupportedFormatError, extract
from ledgerline.models import Pile, Run, RunStatus, SourceDocument

log = logging.getLogger("ledgerline.watcher")


@dataclass
class WatchResult:
    ingested: list[str] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    unsupported: list[tuple[str, str]] = field(default_factory=list)
    pending_stability: list[str] = field(default_factory=list)
    run_id: str | None = None


class Watcher:
    def __init__(self, pile_id: str, directory: Path) -> None:
        self.pile_id = pile_id
        self.directory = directory
        self._sizes: dict[Path, int] = {}

    def _stable_files(self) -> list[Path]:
        """A file whose size has not moved since the previous poll. Anything still growing waits."""
        stable, seen = [], {}
        for path in sorted(self.directory.iterdir()):
            if not path.is_file() or path.name.startswith("."):
                continue
            size = path.stat().st_size
            seen[path] = size
            if self._sizes.get(path) == size:
                stable.append(path)
        self._sizes = seen
        return stable

    def poll(self, *, queue_run: bool = True) -> WatchResult:
        result = WatchResult()
        if not self.directory.exists():
            return result

        stable = self._stable_files()
        result.pending_stability = [
            path.name for path in self._sizes if path not in set(stable)
        ]

        for path in stable:
            if path.suffix.lower() not in SUPPORTED_SUFFIXES:
                result.unsupported.append((path.name, f"unsupported suffix '{path.suffix}'"))
                continue
            try:
                canonical = extract(path)
            except UnsupportedFormatError as exc:
                result.unsupported.append((path.name, str(exc)))
                continue

            with session_scope() as session:
                existing = session.scalars(
                    select(SourceDocument).where(
                        SourceDocument.pile_id == self.pile_id,
                        SourceDocument.content_sha256 == canonical.content_sha256,
                    )
                ).first()
                if existing is not None:
                    result.duplicates.append(path.name)
                    continue

                document = SourceDocument(
                    pile_id=self.pile_id,
                    filename=path.name,
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
                result.ingested.append(document.id)

        if result.ingested and queue_run:
            with session_scope() as session:
                run = Run(
                    pile_id=self.pile_id,
                    status=RunStatus.queued,
                    trigger="watcher",
                    new_document_ids=result.ingested,
                )
                session.add(run)
                session.flush()
                result.run_id = run.id

        return result


def watch_forever(pile_name: str, directory: Path, interval: float = 2.0) -> None:
    settings = get_settings()
    with session_scope() as session:
        pile = session.scalars(select(Pile).where(Pile.name == pile_name)).first()
        if pile is None:
            raise LookupError(f"pile '{pile_name}' not found; run `ledgerline seed` first")
        pile_id = pile.id

    directory.mkdir(parents=True, exist_ok=True)
    watcher = Watcher(pile_id, directory)
    log.info("watching %s for pile %s (%s)", directory, pile_name, settings.safe_dump()["model_client"])

    while True:
        result = watcher.poll()
        if result.ingested:
            log.info("ingested %d new document(s), queued run %s", len(result.ingested), result.run_id)
        for name, reason in result.unsupported:
            log.warning("skipped %s: %s", name, reason)
        time.sleep(interval)
