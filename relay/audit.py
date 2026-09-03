"""Append-only per-session JSONL audit storage and ordered replay."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from pathlib import Path
from threading import RLock

_FORBIDDEN_KEYS = frozenset(
    {"authorization", "credential", "password", "secret", "signature", "token"}
)


class AuditLogError(RuntimeError):
    pass


class SessionAuditLog:
    """One JSON event per O_APPEND write, wrapped with a monotonic sequence."""

    def __init__(self, root: Path, session: str) -> None:
        if not session or len(session) > 512:
            raise ValueError("session must be a non-empty string of at most 512 chars")
        self.session = session
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(session.encode()).hexdigest()
        self.path = self.root / f"{digest}.jsonl"
        self._lock = RLock()
        records = self.replay()
        self._next_sequence = 1 if not records else records[-1]["seq"] + 1

    @property
    def last_sequence(self) -> int:
        with self._lock:
            return self._next_sequence - 1

    def append(self, event: Mapping[str, object]) -> dict[str, object]:
        """Validate and durably append one safe JSON-native event."""
        with self._lock:
            _reject_sensitive_fields(event)
            if event.get("session") != self.session:
                raise AuditLogError("event session does not match this log")
            if not isinstance(event.get("event_id"), str) or not event["event_id"]:
                raise AuditLogError("logged events require event_id")
            record = {"seq": self._next_sequence, "event": dict(event)}
            try:
                encoded = (
                    json.dumps(record, allow_nan=False, separators=(",", ":"), sort_keys=True)
                    + "\n"
                ).encode()
            except (TypeError, ValueError) as error:
                raise AuditLogError(f"event is not JSON-native: {error}") from None

            flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(self.path, flags, 0o600)
            try:
                written = os.write(descriptor, encoded)
                if written != len(encoded):
                    raise AuditLogError("short append to session log")
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            self._next_sequence += 1
            return json.loads(encoded)

    def replay(self, *, after_sequence: int = 0) -> list[dict[str, object]]:
        if after_sequence < 0:
            raise ValueError("after_sequence must be non-negative")
        with self._lock:
            if not self.path.exists():
                return []
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
            return records


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
