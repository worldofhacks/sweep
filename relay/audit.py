"""Append-only per-session JSONL audit storage and ordered replay."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from collections.abc import Iterable, Mapping
from pathlib import Path
from threading import RLock

_FORBIDDEN_KEYS = frozenset(
    {"authorization", "credential", "password", "secret", "signature", "token"}
)
_LOGGER = logging.getLogger(__name__)


class AuditLogError(RuntimeError):
    pass


class SessionAuditLog:
    """Append durable event batches with monotonic per-event sequences."""

    def __init__(self, root: Path, session: str) -> None:
        if not session or len(session) > 512:
            raise ValueError("session must be a non-empty string of at most 512 chars")
        self.session = session
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(session.encode()).hexdigest()
        self.path = self.root / f"{digest}.jsonl"
        self.pending_path = self.root / f"{digest}.pending"
        self._lock = RLock()
        self._append_usable = True
        self._replay_usable = True
        self.had_persisted_log = self.path.exists() or self.pending_path.exists()
        self.recovered_tail_bytes = 0
        self._recover_incomplete_operation()
        records = self.replay()
        self._next_sequence = 1 if not records else records[-1]["seq"] + 1

    @property
    def last_sequence(self) -> int:
        with self._lock:
            if not self._replay_usable:
                raise AuditLogError("session log cursor is uncertain after a failed rollback")
            return self._next_sequence - 1

    def append(self, event: Mapping[str, object]) -> dict[str, object]:
        """Validate and durably append one safe JSON-native event."""
        return self.append_batch([event])[0]

    def append_batch(self, events: Iterable[Mapping[str, object]]) -> list[dict[str, object]]:
        """Durably append one complete relay operation."""
        with self._lock:
            if not self._append_usable:
                raise AuditLogError("session log is unusable after a failed append operation")
            records: list[dict[str, object]] = []
            for offset, event in enumerate(events):
                _reject_sensitive_fields(event)
                if event.get("session") != self.session:
                    raise AuditLogError("event session does not match this log")
                if not isinstance(event.get("event_id"), str) or not event["event_id"]:
                    raise AuditLogError("logged events require event_id")
                records.append({"seq": self._next_sequence + offset, "event": dict(event)})
            if not records:
                return []
            try:
                encoded_records = [
                    (
                        json.dumps(
                            record,
                            allow_nan=False,
                            separators=(",", ":"),
                            sort_keys=True,
                        )
                        + "\n"
                    ).encode()
                    for record in records
                ]
                encoded = b"".join(encoded_records)
            except (TypeError, ValueError) as error:
                raise AuditLogError(f"event is not JSON-native: {error}") from None

            flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                descriptor = os.open(self.path, flags, 0o600)
            except OSError as error:
                raise AuditLogError(f"cannot open session log: {error}") from None
            try:
                original_size = os.fstat(descriptor).st_size
            except OSError as error:
                try:
                    os.close(descriptor)
                except OSError as close_error:
                    self._append_usable = False
                    raise AuditLogError(f"cannot close session log: {close_error}") from None
                raise AuditLogError(f"cannot inspect session log: {error}") from None
            try:
                self._mark_operation_pending(original_size)
                remaining = memoryview(encoded)
                while remaining:
                    try:
                        written = os.write(descriptor, remaining)
                    except InterruptedError:
                        continue
                    if written <= 0:
                        raise OSError("append made no progress")
                    remaining = remaining[written:]
                os.fsync(descriptor)
                self._clear_pending_operation()
                self._next_sequence += len(records)
            except OSError as error:
                try:
                    os.ftruncate(descriptor, original_size)
                    os.fsync(descriptor)
                    self._clear_pending_operation()
                except OSError as rollback_error:
                    self._append_usable = False
                    self._replay_usable = False
                    raise AuditLogError(
                        f"cannot append or restore session log: {rollback_error}"
                    ) from None
                raise AuditLogError(f"cannot append session log: {error}") from None
            finally:
                try:
                    os.close(descriptor)
                except OSError as error:
                    self._append_usable = False
                    raise AuditLogError(f"cannot close session log: {error}") from None
            return [json.loads(encoded_record) for encoded_record in encoded_records]

    def replay(self, *, after_sequence: int = 0) -> list[dict[str, object]]:
        records, _ = self.replay_snapshot(after_sequence=after_sequence)
        return records

    def replay_snapshot(self, *, after_sequence: int = 0) -> tuple[list[dict[str, object]], int]:
        if after_sequence < 0:
            raise ValueError("after_sequence must be non-negative")
        with self._lock:
            if not self._replay_usable:
                raise AuditLogError("session log replay is uncertain after a failed rollback")
            if not self.path.exists():
                return [], 0
            self._recover_unterminated_tail()
            records: list[dict[str, object]] = []
            expected = 1
            try:
                with self.path.open(encoding="utf-8") as stream:
                    for line_number, line in enumerate(stream, start=1):
                        record = json.loads(line)
                        _validate_record(record, expected, self.session, line_number)
                        if record["seq"] > after_sequence:
                            records.append(record)
                        expected += 1
            except (OSError, UnicodeError, json.JSONDecodeError) as error:
                raise AuditLogError(f"cannot replay {self.path.name}: {error}") from None
            return records, expected - 1

    def _recover_unterminated_tail(self) -> None:
        try:
            data = self.path.read_bytes()
        except OSError as error:
            raise AuditLogError(f"cannot replay {self.path.name}: {error}") from None
        if not data or data.endswith(b"\n"):
            return

        prefix_end = data.rfind(b"\n") + 1
        prefix = data[:prefix_end]
        try:
            text = prefix.decode("utf-8")
            for line_number, line in enumerate(text.splitlines(), start=1):
                record = json.loads(line)
                _validate_record(record, line_number, self.session, line_number)
        except (UnicodeError, json.JSONDecodeError) as error:
            raise AuditLogError(f"cannot replay {self.path.name}: {error}") from None

        flags = os.O_WRONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(self.path, flags)
            try:
                os.ftruncate(descriptor, prefix_end)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError as error:
            raise AuditLogError(f"cannot repair {self.path.name}: {error}") from None

        removed = len(data) - prefix_end
        self.recovered_tail_bytes += removed
        _LOGGER.warning("recovered unterminated audit tail removed_bytes=%d", removed)

    def _mark_operation_pending(self, original_size: int) -> None:
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(self.pending_path, flags, 0o600)
            with os.fdopen(descriptor, "w", encoding="ascii") as stream:
                stream.write(f"{original_size}\n")
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as error:
            raise AuditLogError(f"cannot mark pending audit operation: {error}") from None

    def _clear_pending_operation(self) -> None:
        self.pending_path.unlink()

    def _recover_incomplete_operation(self) -> None:
        if not self.pending_path.exists():
            return
        try:
            flags = os.O_RDONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(self.pending_path, flags)
            with os.fdopen(descriptor, encoding="ascii") as stream:
                text = stream.read()
            original_size = int(text)
            if original_size < 0 or text != f"{original_size}\n":
                raise ValueError
            if not self.path.exists():
                if original_size != 0:
                    raise AuditLogError("pending audit cursor exceeds missing session log")
            else:
                flags = os.O_WRONLY
                if hasattr(os, "O_NOFOLLOW"):
                    flags |= os.O_NOFOLLOW
                descriptor = os.open(self.path, flags)
                try:
                    if os.fstat(descriptor).st_size < original_size:
                        raise AuditLogError("pending audit cursor exceeds session log")
                    os.ftruncate(descriptor, original_size)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            self.pending_path.unlink()
        except (UnicodeError, ValueError):
            raise AuditLogError("invalid pending audit operation") from None
        except OSError as error:
            raise AuditLogError(f"cannot recover pending audit operation: {error}") from None


def _validate_record(record: object, expected: int, session: str, line_number: int) -> None:
    if not isinstance(record, dict) or set(record) != {"seq", "event"}:
        raise AuditLogError(f"invalid record at line {line_number}")
    sequence = record["seq"]
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence != expected:
        raise AuditLogError(f"non-contiguous sequence at line {line_number}")
    event = record["event"]
    if not isinstance(event, dict) or event.get("session") != session:
        raise AuditLogError(f"wrong event session at line {line_number}")
    if not isinstance(event.get("event_id"), str) or not event["event_id"]:
        raise AuditLogError(f"missing event_id at line {line_number}")
    _reject_sensitive_fields(event)


def _reject_sensitive_fields(value: object) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(key, str) and key.lower() in _FORBIDDEN_KEYS:
                raise AuditLogError(f"sensitive field {key!r} may not be logged")
            _reject_sensitive_fields(item)
    elif isinstance(value, list | tuple):
        for item in value:
            _reject_sensitive_fields(item)
