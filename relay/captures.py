"""Session-retained capture media and bundles and their bounded state projection.

The node reports one ``media_file`` frame per captured file (``pending`` at capture time,
``completed`` once the bytes are on the phone with their SHA-256) and the autonomy
composition records the bundle the dispatcher composed from the plan (room, pattern,
coverage, validated status). Both land here, keyed by aircraft, connection epoch, and
capture id, so the ``state`` fan-out can list the active MVP capture set. The append-only
audit is the canonical durable record; this projection is deliberately bounded and is
not a second persistence system.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from relay.contracts import CaptureBundleFrame, MediaFileRecord

CaptureKey = tuple[int, int, str]
MAX_CAPTURE_ENTRIES = 64
MAX_MEDIA_FILES_PER_CAPTURE = 64


class CaptureLedgerError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


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
    """A bounded live projection; complete durable history stays in the audit log."""

    def __init__(
        self,
        *,
        max_entries: int = MAX_CAPTURE_ENTRIES,
        max_media_per_capture: int = MAX_MEDIA_FILES_PER_CAPTURE,
    ) -> None:
        if min(max_entries, max_media_per_capture) <= 0:
            raise ValueError("capture ledger limits must be positive")
        self._max_entries = max_entries
        self._max_media_per_capture = max_media_per_capture
        self._entries: dict[CaptureKey, CaptureEntry] = {}

    def snapshot_entry(self, key: CaptureKey) -> CaptureEntry | None:
        entry = self._entries.get(key)
        return None if entry is None else entry.copy()

    def restore_entry(self, key: CaptureKey, snapshot: CaptureEntry | None) -> None:
        if snapshot is None:
            self._entries.pop(key, None)
        else:
            self._entries[key] = snapshot

    def record_media(self, record: MediaFileRecord, *, t: int) -> CaptureEntry:
        """Retain one media record; a later record for the same file replaces it."""
        key = (record.drone_id, record.connection_epoch, record.capture_id)
        self._check_capacity(key, (record.file_id,))
        entry = self._candidate(record.drone_id, record.connection_epoch, record.capture_id, t)
        _merge_media(entry, record)
        entry.updated_at = max(entry.updated_at, t)
        self._evict_completed_for(key)
        self._entries[key] = entry
        return entry

    def record_bundle(self, bundle: CaptureBundleFrame, *, t: int) -> CaptureEntry:
        """Close a capture with its bundle; nested media replace records of the same file."""
        key = (bundle.drone_id, bundle.connection_epoch, bundle.capture_id)
        self._check_capacity(key, tuple(record.file_id for record in bundle.media))
        entry = self._candidate(bundle.drone_id, bundle.connection_epoch, bundle.capture_id, t)
        for record in bundle.media:
            _merge_media(entry, record)
        entry.room_id = bundle.room_id
        entry.pattern = bundle.pattern
        entry.coverage = bundle.coverage
        entry.status = bundle.status
        entry.reason = bundle.reason
        entry.detail = bundle.detail
        entry.updated_at = max(entry.updated_at, t)
        self._evict_completed_for(key)
        self._entries[key] = entry
        return entry

    def eviction_candidate(self, key: CaptureKey) -> CaptureKey | None:
        """Oldest closed entry displaced by a new capture, if the projection is full.

        Open captures are never discarded: if every retained entry is still open, the new
        frame is refused instead. The append-only audit remains the complete history.
        """
        if key in self._entries or len(self._entries) < self._max_entries:
            return None
        closed = [entry for entry in self._entries.values() if entry.status is not None]
        if not closed:
            raise CaptureLedgerError(
                "capture_limit_exceeded",
                f"session capture projection is limited to {self._max_entries} open captures",
            )
        return min(
            closed,
            key=lambda entry: (
                entry.updated_at,
                entry.drone_id,
                entry.connection_epoch,
                entry.capture_id,
            ),
        ).key

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

    def _candidate(
        self, drone_id: int, connection_epoch: int, capture_id: str, t: int
    ) -> CaptureEntry:
        """Return an isolated candidate so a bad bundle cannot partially mutate the projection."""
        key = (drone_id, connection_epoch, capture_id)
        entry = self._entries.get(key)
        if entry is not None:
            return entry.copy()
        return CaptureEntry(
            capture_id=capture_id,
            drone_id=drone_id,
            connection_epoch=connection_epoch,
            updated_at=t,
        )

    def _check_capacity(self, key: CaptureKey, incoming_file_ids: tuple[str, ...]) -> None:
        # Resolve this before building a candidate so a full projection of open captures
        # fails without mutation; a closed candidate is evicted only at commit below.
        self.eviction_candidate(key)
        entry = self._entries.get(key)
        existing = () if entry is None else tuple(record.file_id for record in entry.media)
        if len(set(existing).union(incoming_file_ids)) > self._max_media_per_capture:
            raise CaptureLedgerError(
                "capture_limit_exceeded",
                f"capture projection is limited to {self._max_media_per_capture} files",
            )

    def _evict_completed_for(self, key: CaptureKey) -> None:
        candidate = self.eviction_candidate(key)
        if candidate is not None:
            self._entries.pop(candidate)


def _merge_media(entry: CaptureEntry, record: MediaFileRecord) -> None:
    for index, existing in enumerate(entry.media):
        if existing.file_id == record.file_id:
            if _capture_evidence(existing) != _capture_evidence(record):
                raise CaptureLedgerError(
                    "capture_evidence_mismatch",
                    f"media file {record.file_id} changed immutable capture evidence",
                )
            if existing.retrieval_status == "completed" and record.retrieval_status != "completed":
                raise CaptureLedgerError(
                    "capture_status_regression",
                    f"completed media file {record.file_id} cannot return to "
                    f"{record.retrieval_status}",
                )
            if existing.retrieval_status == record.retrieval_status and existing != record:
                raise CaptureLedgerError(
                    "capture_record_conflict",
                    f"media file {record.file_id} changed within retrieval status "
                    f"{record.retrieval_status}",
                )
            entry.media[index] = record
            return
    entry.media.append(record)


def _capture_evidence(record: MediaFileRecord) -> tuple[object, ...]:
    """Fields fixed at shutter time; retrieval may change only location, checksum, and status."""
    return (
        record.capture_id,
        record.file_id,
        record.timestamp_ms,
        record.drone_id,
        record.connection_epoch,
        record.pose,
        record.actual_yaw_deg,
        record.gimbal_pitch_deg,
        record.intrinsics,
    )
