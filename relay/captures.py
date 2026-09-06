"""Session-retained capture media and bundles, their state projection, and their files.

The node reports one ``media_file`` frame per captured file (``pending`` at capture time,
``completed`` once the bytes are on the phone with their SHA-256) and the autonomy
composition records the bundle the dispatcher composed from the plan (room, pattern,
coverage, validated status). Both land here, keyed by aircraft, connection epoch, and
capture id, so the ``state`` fan-out can list every capture of the session and each
record is also written as JSON under the session log directory.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path

from relay.contracts import CaptureBundleFrame, MediaFileRecord

_LOGGER = logging.getLogger(__name__)

CaptureKey = tuple[int, int, str]


@dataclass(slots=True)
class CaptureEntry:
    """One capture of one aircraft in one connection epoch."""

    capture_id: str
    drone_id: int
    connection_epoch: int
    updated_at: int
    room_id: str | None = None
    pattern: str | None = None
    coverage: str | None = None
    status: str | None = None
    reason: str | None = None
    detail: str | None = None
    media: list[MediaFileRecord] = field(default_factory=list)

    @property
    def key(self) -> CaptureKey:
        return (self.drone_id, self.connection_epoch, self.capture_id)

    def to_projection(self) -> dict[str, object]:
        """The ``state.captures`` entry; ``status`` is null until a bundle closes the set."""
        return {
            "capture_id": self.capture_id,
            "drone_id": self.drone_id,
            "connection_epoch": self.connection_epoch,
            "room_id": self.room_id,
            "pattern": self.pattern,
            "coverage": self.coverage,
            "status": self.status,
            "reason": self.reason,
            "detail": self.detail,
            "files": [record.to_dict() for record in self.media],
            "updated_at": self.updated_at,
        }

    def copy(self) -> CaptureEntry:
        return replace(self, media=list(self.media))


class CaptureLedger:
    """Retained captures for one session; every mutation returns a snapshot for undo."""

    def __init__(self, directory: Path | None) -> None:
        self._directory = directory
        self._entries: dict[CaptureKey, CaptureEntry] = {}

    def snapshot(self) -> dict[CaptureKey, CaptureEntry]:
        return {key: entry.copy() for key, entry in self._entries.items()}

    def restore(self, snapshot: dict[CaptureKey, CaptureEntry]) -> None:
        self._entries = snapshot

    def record_media(self, record: MediaFileRecord, *, t: int) -> CaptureEntry:
        """Retain one media record; a later record for the same file replaces it."""
        entry = self._entry(record.drone_id, record.connection_epoch, record.capture_id, t)
        _merge_media(entry, record)
        entry.updated_at = max(entry.updated_at, t)
        self._write(entry, f"{_safe(record.file_id)}.json", record.to_dict())
        return entry

    def record_bundle(self, bundle: CaptureBundleFrame, *, t: int) -> CaptureEntry:
        """Close a capture with its bundle; nested media replace records of the same file."""
        entry = self._entry(bundle.drone_id, bundle.connection_epoch, bundle.capture_id, t)
        for record in bundle.media:
            _merge_media(entry, record)
        entry.room_id = bundle.room_id
        entry.pattern = bundle.pattern
        entry.coverage = bundle.coverage
        entry.status = bundle.status
        entry.reason = bundle.reason
        entry.detail = bundle.detail
        entry.updated_at = max(entry.updated_at, t)
        self._write(entry, "bundle.json", bundle.to_event())
        return entry

    def media_files(
        self, drone_id: int, connection_epoch: int, capture_id: str
    ) -> tuple[MediaFileRecord, ...]:
        entry = self._entries.get((drone_id, connection_epoch, capture_id))
        return () if entry is None else tuple(entry.media)

    def projection(self) -> list[dict[str, object]]:
        """Every retained capture, oldest first, newest activity last."""
        ordered = sorted(
            self._entries.values(),
            key=lambda entry: (entry.updated_at, entry.drone_id, entry.capture_id),
        )
        return [entry.to_projection() for entry in ordered]

    def _entry(self, drone_id: int, connection_epoch: int, capture_id: str, t: int) -> CaptureEntry:
        key = (drone_id, connection_epoch, capture_id)
        entry = self._entries.get(key)
        if entry is None:
            entry = CaptureEntry(
                capture_id=capture_id,
                drone_id=drone_id,
                connection_epoch=connection_epoch,
                updated_at=t,
            )
            self._entries[key] = entry
        return entry

    def _write(self, entry: CaptureEntry, name: str, payload: Mapping[str, object]) -> None:
        """Write one JSON record under the session log directory; a disk failure is logged.

        The audit JSONL already holds the same record durably; these files exist so the
        capture set can be handed on without replaying the session log.
        """
        root = self._directory
        if root is None:
            return
        folder = (
            root
            / f"drone-{entry.drone_id}"
            / f"epoch-{entry.connection_epoch}"
            / _safe(entry.capture_id)
        )
        try:
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / name
            text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
            temporary = path.with_suffix(path.suffix + ".tmp")
            temporary.write_text(text, encoding="utf-8")
            temporary.replace(path)
        except OSError as error:
            _LOGGER.warning(
                "capture record not written drone=%s capture=%s name=%s: %s",
                entry.drone_id,
                entry.capture_id,
                name,
                error,
            )


def _merge_media(entry: CaptureEntry, record: MediaFileRecord) -> None:
    for index, existing in enumerate(entry.media):
        if existing.file_id == record.file_id:
            entry.media[index] = record
            return
    entry.media.append(record)


def _safe(value: str) -> str:
    """Keep ids usable as path segments; the ids themselves stay verbatim in the JSON."""
    return "".join(char if char.isalnum() or char in "-_." else "_" for char in value)[:200]
