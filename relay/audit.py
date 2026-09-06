"""Append-only per-session JSONL audit storage and ordered replay."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import sqlite3
import tempfile
from collections.abc import Iterable, Mapping
from contextlib import closing
from pathlib import Path
from threading import RLock

_FORBIDDEN_KEYS = frozenset(
    {"authorization", "credential", "password", "secret", "signature", "token"}
)
_LOGGER = logging.getLogger(__name__)


class AuditLogError(RuntimeError):
    pass


class SessionAuditLog:
    """Commit relay operations durably while preserving the public JSONL mirror."""

    def __init__(self, root: Path, session: str) -> None:
        if not session or len(session) > 512:
            raise ValueError("session must be a non-empty string of at most 512 chars")
        self.session = session
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(session.encode()).hexdigest()
        self.path = self.root / f"{digest}.jsonl"
        self.database_path = self.root / f"{digest}.sqlite3"
        self.pending_path = self.root / f"{digest}.pending"
        self._lock = RLock()
        self._append_usable = True
        self._replay_usable = True
        self._database_initialized = False
        self._database_identity: tuple[int, int] | None = None
        self._mirror_fingerprint: tuple[int, int, int, int, int] | None = None
        self.recovered_tail_bytes = 0
        self.had_persisted_log = (
            self.path.exists() or self.database_path.exists() or self.pending_path.exists()
        )
        if self.pending_path.exists():
            self._recover_legacy_pending_operation()
        if self.database_path.exists():
            self._initialize_database()
            self._recover_mirror_from_database()
            self._mirror_fingerprint = self._current_mirror_fingerprint()
        elif self.path.exists():
            records = self._read_jsonl(repair_tail=True)
            self._migrate_legacy_records(records)
            self._initialize_database()
        self._next_sequence = self._database_last_sequence() + 1
        if self.database_path.exists() and self._has_pending_operation():
            self._append_usable = False
            self._replay_usable = False

    @property
    def last_sequence(self) -> int:
        with self._lock:
            if not self._replay_usable:
                raise AuditLogError("session log cursor is uncertain after an incomplete operation")
            return self._next_sequence - 1

    def begin_operation(self) -> int:
        """Durably mark an outer relay operation before its first side effect."""
        with self._lock:
            if not self._append_usable:
                raise AuditLogError("session log is unusable after a failed append operation")
            if not self._database_initialized:
                self._initialize_database()
            try:
                with closing(self._connect()) as database:
                    cursor = database.execute("INSERT INTO operations(status) VALUES ('pending')")
                    database.commit()
                    assert cursor.lastrowid is not None
                    return cursor.lastrowid
            except sqlite3.Error as error:
                self._append_usable = False
                self._replay_usable = False
                raise AuditLogError(f"cannot begin audit operation: {error}") from None

    def abandon_operation(self, operation_id: int) -> None:
        with self._lock:
            if self._operation_status(operation_id) == "pending":
                self._append_usable = False
                self._replay_usable = False

    def append(self, event: Mapping[str, object]) -> dict[str, object]:
        """Validate and durably append one safe JSON-native event."""
        return self.append_batch([event])[0]

    def append_batch(
        self,
        events: Iterable[Mapping[str, object]],
        *,
        operation_id: int | None = None,
    ) -> list[dict[str, object]]:
        """Durably append and complete one outer relay operation."""
        with self._lock:
            if not self._append_usable:
                raise AuditLogError("session log is unusable after a failed append operation")
            records = self._prepare_records(events)
            if not records and operation_id is None:
                return []
            if operation_id is None:
                operation_id = self.begin_operation()
            if not records:
                self._complete_operation(operation_id, [])
                return []
            encoded_records = [self._encode_record(record) for record in records]
            encoded = b"".join(encoded_records)
            self._verify_mirror_for_append()
            self._complete_operation(operation_id, records)
            self._next_sequence += len(records)
            try:
                self._append_mirror(encoded)
            except AuditLogError:
                self._append_usable = False
                self._replay_usable = False
                raise
            return [json.loads(encoded_record) for encoded_record in encoded_records]

    def replay(self, *, after_sequence: int = 0) -> list[dict[str, object]]:
        records, _ = self.replay_snapshot(after_sequence=after_sequence)
        return records

    def replay_snapshot(self, *, after_sequence: int = 0) -> tuple[list[dict[str, object]], int]:
        if after_sequence < 0:
            raise ValueError("after_sequence must be non-negative")
        with self._lock:
            if not self._replay_usable or self._has_pending_operation():
                raise AuditLogError("session log replay is uncertain after an incomplete operation")
            records = self._database_records()
            self._verify_mirror(records)
            return [record for record in records if record["seq"] > after_sequence], len(records)

    def _prepare_records(self, events: Iterable[Mapping[str, object]]) -> list[dict[str, object]]:
        records: list[dict[str, object]] = []
        for offset, event in enumerate(events):
            self._validate_event(event)
            records.append({"seq": self._next_sequence + offset, "event": dict(event)})
        for record in records:
            self._encode_record(record)
        return records

    def _validate_event(self, event: Mapping[str, object]) -> None:
        _reject_nonfinite_numbers(event)
        _reject_sensitive_fields(event)
        if event.get("session") != self.session:
            raise AuditLogError("event session does not match this log")
        if not isinstance(event.get("event_id"), str) or not event["event_id"]:
            raise AuditLogError("logged events require event_id")

    @staticmethod
    def _encode_record(record: Mapping[str, object]) -> bytes:
        try:
            return (
                json.dumps(record, allow_nan=False, separators=(",", ":"), sort_keys=True) + "\n"
            ).encode()
        except (TypeError, ValueError) as error:
            raise AuditLogError(f"event is not JSON-native: {error}") from None

    def _initialize_database(self) -> None:
        try:
            with closing(self._connect()) as database:
                database.execute("PRAGMA journal_mode=WAL")
                database.execute("PRAGMA synchronous=FULL")
                database.execute(
                    "CREATE TABLE IF NOT EXISTS operations ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "status TEXT NOT NULL CHECK(status IN ('pending', 'complete')))"
                )
                database.execute(
                    "CREATE TABLE IF NOT EXISTS records ("
                    "seq INTEGER PRIMARY KEY, operation_id INTEGER NOT NULL, "
                    "event_json TEXT NOT NULL, "
                    "FOREIGN KEY(operation_id) REFERENCES operations(id))"
                )
                database.commit()
            self._database_initialized = True
        except sqlite3.Error as error:
            self._append_usable = False
            self._replay_usable = False
            raise AuditLogError(f"cannot initialize audit database: {error}") from None

    def _connect(self) -> sqlite3.Connection:
        database: sqlite3.Connection | None = None
        guard = None
        created = False
        try:
            if self._database_identity is None:
                create_flags = os.O_CREAT | os.O_EXCL | os.O_RDWR
                if hasattr(os, "O_NOFOLLOW"):
                    create_flags |= os.O_NOFOLLOW
                try:
                    descriptor = os.open(self.database_path, create_flags, 0o600)
                except FileExistsError:
                    pass
                else:
                    created = True
                    os.fdopen(descriptor, "rb").close()
            guard_flags = os.O_RDONLY
            if hasattr(os, "O_NOFOLLOW"):
                guard_flags |= os.O_NOFOLLOW
            guard = os.fdopen(os.open(self.database_path, guard_flags), "rb")
            guarded = os.fstat(guard.fileno())
            identity = (guarded.st_dev, guarded.st_ino)
            if self._database_identity is not None and identity != self._database_identity:
                raise OSError("audit database changed since it was initialized")
            # Creation is explicit above. SQLite must not recreate a removed database,
            # including if it disappears between the guarded open and SQLite's open.
            database = sqlite3.connect(
                f"{self.database_path.absolute().as_uri()}?mode=rw", timeout=30, uri=True
            )
            opened = os.stat(self.database_path, follow_symlinks=False)
            if identity != (opened.st_dev, opened.st_ino):
                raise OSError("audit database changed while it was opened")
            os.chmod(self.database_path, 0o600, follow_symlinks=False)
            database.execute("PRAGMA synchronous=FULL")
            database.execute("PRAGMA foreign_keys=ON")
            if created:
                self._fsync_root()
            self._database_identity = identity
        except OSError as error:
            self._append_usable = False
            self._replay_usable = False
            if database is not None:
                database.close()
            raise sqlite3.OperationalError(str(error)) from None
        except sqlite3.Error:
            self._append_usable = False
            self._replay_usable = False
            if database is not None:
                database.close()
            raise
        finally:
            if guard is not None:
                guard.close()
        assert database is not None
        return database

    def _complete_operation(self, operation_id: int, records: list[dict[str, object]]) -> None:
        try:
            with closing(self._connect()) as database:
                database.execute("BEGIN IMMEDIATE")
                status = database.execute(
                    "SELECT status FROM operations WHERE id = ?", (operation_id,)
                ).fetchone()
                if status != ("pending",):
                    raise AuditLogError("audit operation is missing or already complete")
                for record in records:
                    database.execute(
                        "INSERT INTO records(seq, operation_id, event_json) VALUES (?, ?, ?)",
                        (
                            record["seq"],
                            operation_id,
                            json.dumps(
                                record["event"],
                                allow_nan=False,
                                separators=(",", ":"),
                                sort_keys=True,
                            ),
                        ),
                    )
                database.execute(
                    "UPDATE operations SET status = 'complete' WHERE id = ?", (operation_id,)
                )
                database.commit()
        except sqlite3.Error as error:
            self._append_usable = False
            self._replay_usable = False
            raise AuditLogError(f"cannot complete audit operation: {error}") from None

    def _has_pending_operation(self) -> bool:
        if self._database_identity is None and not self.database_path.exists():
            return False
        try:
            with closing(self._connect()) as database:
                return (
                    database.execute(
                        "SELECT 1 FROM operations WHERE status = 'pending' LIMIT 1"
                    ).fetchone()
                    is not None
                )
        except sqlite3.Error as error:
            raise AuditLogError(f"cannot inspect audit operations: {error}") from None

    def _operation_status(self, operation_id: int) -> str | None:
        try:
            with closing(self._connect()) as database:
                row = database.execute(
                    "SELECT status FROM operations WHERE id = ?", (operation_id,)
                ).fetchone()
                return None if row is None else str(row[0])
        except sqlite3.Error as error:
            raise AuditLogError(f"cannot inspect audit operation: {error}") from None

    def _database_last_sequence(self) -> int:
        return len(self._database_records())

    def _database_records(self) -> list[dict[str, object]]:
        if self._database_identity is None and not self.database_path.exists():
            return []
        try:
            with closing(self._connect()) as database:
                rows = database.execute(
                    "SELECT seq, event_json FROM records ORDER BY seq"
                ).fetchall()
        except sqlite3.Error as error:
            raise AuditLogError(f"cannot replay {self.path.name}: {error}") from None
        records: list[dict[str, object]] = []
        for line_number, (sequence, event_json) in enumerate(rows, start=1):
            try:
                record = {"seq": sequence, "event": json.loads(event_json)}
            except (TypeError, json.JSONDecodeError) as error:
                raise AuditLogError(f"cannot replay {self.path.name}: {error}") from None
            _validate_record(record, line_number, self.session, line_number)
            records.append(record)
        return records

    def _migrate_legacy_records(self, records: list[dict[str, object]]) -> None:
        temporary_path = self._new_temporary_path(self.database_path.name, ".migrate")
        try:
            with closing(sqlite3.connect(temporary_path)) as database:
                database.execute("PRAGMA journal_mode=DELETE")
                database.execute("PRAGMA synchronous=FULL")
                database.execute(
                    "CREATE TABLE operations ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "status TEXT NOT NULL CHECK(status IN ('pending', 'complete')))"
                )
                database.execute(
                    "CREATE TABLE records ("
                    "seq INTEGER PRIMARY KEY, operation_id INTEGER NOT NULL, "
                    "event_json TEXT NOT NULL, "
                    "FOREIGN KEY(operation_id) REFERENCES operations(id))"
                )
                cursor = database.execute("INSERT INTO operations(status) VALUES ('complete')")
                assert cursor.lastrowid is not None
                for record in records:
                    database.execute(
                        "INSERT INTO records(seq, operation_id, event_json) VALUES (?, ?, ?)",
                        (
                            record["seq"],
                            cursor.lastrowid,
                            json.dumps(
                                record["event"],
                                allow_nan=False,
                                separators=(",", ":"),
                                sort_keys=True,
                            ),
                        ),
                    )
                database.commit()
            temporary_path.chmod(0o600)
            self._fsync_path(temporary_path)
            os.replace(temporary_path, self.database_path)
            self._fsync_root()
        except (OSError, sqlite3.Error) as error:
            temporary_path.unlink(missing_ok=True)
            raise AuditLogError(f"cannot migrate legacy audit log: {error}") from None

    def _recover_mirror_from_database(self) -> None:
        committed = self._database_records()
        expected = b"".join(self._encode_record(record) for record in committed)
        if not self.path.exists():
            if expected:
                self._replace_mirror(expected)
            return
        try:
            actual = self.path.read_bytes()
        except OSError as error:
            raise AuditLogError(f"cannot replay {self.path.name}: {error}") from None
        pending = self._has_pending_operation()
        if actual == expected:
            return
        if expected.startswith(actual) or (pending and actual.startswith(expected)):
            self._replace_mirror(expected)
            return
        if actual.startswith(expected) and not actual.endswith(b"\n"):
            removed = len(actual) - len(expected)
            self.recovered_tail_bytes += removed
            self._replace_mirror(expected)
            _LOGGER.warning("recovered unterminated audit tail removed_bytes=%d", removed)
            return
        raise AuditLogError(f"cannot replay {self.path.name}: mirror differs from committed audit")

    def _verify_mirror(self, records: list[dict[str, object]]) -> None:
        if not self.path.exists():
            if records:
                raise AuditLogError(f"cannot replay {self.path.name}: audit mirror is missing")
            return
        expected = b"".join(self._encode_record(record) for record in records)
        try:
            actual = self.path.read_bytes()
        except OSError as error:
            raise AuditLogError(f"cannot replay {self.path.name}: {error}") from None
        if actual != expected:
            self._read_jsonl(repair_tail=False)
            raise AuditLogError(
                f"cannot replay {self.path.name}: mirror differs from committed audit"
            )

    @staticmethod
    def _fingerprint(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
        return (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
        )

    def _current_mirror_fingerprint(self) -> tuple[int, int, int, int, int] | None:
        try:
            return self._fingerprint(self.path.stat(follow_symlinks=False))
        except FileNotFoundError:
            return None
        except OSError as error:
            raise AuditLogError(f"cannot inspect session log: {error}") from None

    def _verify_mirror_for_append(self) -> None:
        fingerprint = self._current_mirror_fingerprint()
        if fingerprint != self._mirror_fingerprint:
            # ctime catches same-size edits even when the writer restores mtime.
            self._verify_mirror(self._database_records())
            if self._current_mirror_fingerprint() != fingerprint:
                raise AuditLogError("audit mirror changed during verification")
            self._mirror_fingerprint = fingerprint

    def _append_mirror(self, content: bytes) -> None:
        flags = os.O_APPEND | os.O_WRONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor: int | None = None
        created = False
        try:
            try:
                descriptor = os.open(self.path, flags)
            except FileNotFoundError:
                try:
                    descriptor = os.open(self.path, flags | os.O_CREAT | os.O_EXCL, 0o600)
                    created = True
                except FileExistsError:
                    descriptor = os.open(self.path, flags)
            try:
                before = self._fingerprint(os.fstat(descriptor))
            except OSError as error:
                raise AuditLogError(f"cannot inspect session log: {error}") from None
            if (created and self._mirror_fingerprint is not None) or (
                not created and before != self._mirror_fingerprint
            ):
                raise AuditLogError("audit mirror changed before append")
            remaining = memoryview(content)
            while remaining:
                try:
                    written = os.write(descriptor, remaining)
                except InterruptedError:
                    continue
                if written <= 0:
                    raise OSError("append made no progress")
                remaining = remaining[written:]
            os.fsync(descriptor)
            if created:
                self._fsync_root()
            after = self._fingerprint(os.fstat(descriptor))
            if after[2] != before[2] + len(content) or (
                self._current_mirror_fingerprint() != after
            ):
                raise AuditLogError("audit mirror changed during append")
            self._mirror_fingerprint = after
        except AuditLogError:
            raise
        except OSError as error:
            action = "open" if descriptor is None else "append"
            raise AuditLogError(f"cannot {action} session log: {error}") from None
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError as error:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
                    raise AuditLogError(f"cannot close session log: {error}") from None

    def _replace_mirror(self, content: bytes) -> None:
        temporary_path: Path | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                dir=self.root, prefix=f".{self.path.name}.", suffix=".repair"
            )
            temporary_path = Path(temporary_name)
            with os.fdopen(descriptor, "wb") as stream:
                remaining = memoryview(content)
                while remaining:
                    written = os.write(stream.fileno(), remaining)
                    if written <= 0:
                        raise OSError("mirror repair made no progress")
                    remaining = remaining[written:]
                os.fsync(stream.fileno())
            os.replace(temporary_path, self.path)
            self._fsync_root()
        except OSError as error:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise AuditLogError(f"cannot repair {self.path.name}: {error}") from None

    def _new_temporary_path(self, stem: str, suffix: str) -> Path:
        temporary_path: Path | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                dir=self.root, prefix=f".{stem}.", suffix=suffix
            )
            temporary_path = Path(temporary_name)
            os.fdopen(descriptor, "rb").close()
            return temporary_path
        except OSError as error:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise AuditLogError(f"cannot create audit temporary file: {error}") from None

    def _fsync_path(self, path: Path) -> None:
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _fsync_root(self) -> None:
        flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        descriptor = os.open(self.root, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _read_jsonl(self, *, repair_tail: bool) -> list[dict[str, object]]:
        try:
            data = self.path.read_bytes()
        except OSError as error:
            raise AuditLogError(f"cannot replay {self.path.name}: {error}") from None
        repair_content: bytes | None = None
        removed = 0
        if repair_tail and data and not data.endswith(b"\n"):
            prefix_end = data.rfind(b"\n") + 1
            removed = len(data) - prefix_end
            data = data[:prefix_end]
            repair_content = data
        records: list[dict[str, object]] = []
        try:
            for line_number, line in enumerate(data.decode().splitlines(), start=1):
                record = json.loads(line)
                _validate_record(record, line_number, self.session, line_number)
                records.append(record)
        except (UnicodeError, ValueError, RecursionError) as error:
            raise AuditLogError(f"cannot replay {self.path.name}: {error}") from None
        if repair_content is not None:
            self._replace_mirror(repair_content)
            self.recovered_tail_bytes += removed
            _LOGGER.warning("recovered unterminated audit tail removed_bytes=%d", removed)
        return records

    def _recover_legacy_pending_operation(self) -> None:
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
                self._replace_mirror(b"")
            else:
                if self.path.stat().st_size < original_size:
                    raise AuditLogError("pending audit cursor exceeds session log")
                self._replace_mirror(self.path.read_bytes()[:original_size])
            self.pending_path.unlink()
        except (OSError, UnicodeError, ValueError) as error:
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
    _reject_nonfinite_numbers(event)
    _reject_sensitive_fields(event)


def _reject_nonfinite_numbers(value: object) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise AuditLogError("audit events cannot contain non-finite numbers")
    if isinstance(value, Mapping):
        for item in value.values():
            _reject_nonfinite_numbers(item)
    elif isinstance(value, list | tuple):
        for item in value:
            _reject_nonfinite_numbers(item)


def _reject_sensitive_fields(value: object) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(key, str) and key.lower() in _FORBIDDEN_KEYS:
                raise AuditLogError(f"sensitive field {key!r} may not be logged")
            _reject_sensitive_fields(item)
    elif isinstance(value, list | tuple):
        for item in value:
            _reject_sensitive_fields(item)
