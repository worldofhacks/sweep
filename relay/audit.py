"""Append-only per-session JSONL audit storage and ordered replay."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import sqlite3
import tempfile
from collections.abc import Iterable, Iterator, Mapping
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from threading import Condition, RLock
from time import monotonic
from typing import BinaryIO

_FORBIDDEN_KEYS = frozenset(
    {"authorization", "credential", "password", "secret", "signature", "token"}
)
_LOGGER = logging.getLogger(__name__)
LIVE_REPLAY_TIMEOUT_SECONDS = 1.0
_MIRROR_READ_BUFFER = 1 << 20
# Four maximum 128 KiB aircraft projections plus both maximum 128 KiB control
# projections remain below this ceiling, leaving space for the state envelope.
# The same bound applies before any persisted length can drive a sized read.
MAX_AUDIT_RECORD_BYTES = 1 << 20
_MAX_SQLITE_INTEGER = (1 << 63) - 1
_MAX_PENDING_CURSOR_BYTES = len(str(_MAX_SQLITE_INTEGER)) + 1
_RECORDS_TABLE = (
    "seq INTEGER PRIMARY KEY, operation_id INTEGER NOT NULL, "
    "digest BLOB NOT NULL, length INTEGER NOT NULL, line BLOB, "
    "FOREIGN KEY(operation_id) REFERENCES operations(id)"
)


class AuditLogError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class _CommittedRecord:
    """Fencing metadata for one committed record.

    ``digest`` and ``length`` describe the exact JSONL line; ``line`` itself is
    retained until the next non-empty operation commits, so the database can
    rebuild a mirror tail the process did not finish writing without duplicating
    history.
    """

    seq: int
    operation_id: int
    digest: bytes
    length: int
    line: bytes | None


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
        self._operation_complete = Condition(self._lock)
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
            self._migrate_legacy_database()
            self._initialize_database()
            self._recover_mirror_from_database()
            self._mirror_fingerprint = self._current_mirror_fingerprint()
        elif self.path.exists():
            lines = self._read_jsonl(repair_tail=True)
            self._migrate_legacy_records(lines)
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
                    database.execute("BEGIN IMMEDIATE")
                    cursor = database.execute("INSERT INTO operations(status) VALUES ('pending')")
                    database.commit()
                    assert cursor.lastrowid is not None
                    return cursor.lastrowid
            except sqlite3.Error as error:
                self._append_usable = False
                self._replay_usable = False
                raise AuditLogError(f"cannot begin audit operation: {error}") from None

    def abandon_operation(self, operation_id: int) -> None:
        _require_sqlite_operation_id(operation_id)
        with self._lock:
            if self._operation_status(operation_id) == "pending":
                self._append_usable = False
                self._replay_usable = False
                self._operation_complete.notify_all()

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
        if operation_id is not None:
            _require_sqlite_operation_id(operation_id)
        with self._lock:
            if not self._append_usable:
                raise AuditLogError("session log is unusable after a failed append operation")
            prepared = self._prepare_records(events)
            if not prepared and operation_id is None:
                return []
            owns_operation = operation_id is None
            try:
                # A stand-alone append has no external side effect to protect, so
                # inspect the mirror before creating its durable pending marker.
                # An outer relay operation supplies a marker created before its
                # mutation; it must remain pending if this verification fails.
                if owns_operation:
                    self._verify_mirror_for_append()
                    operation_id = self.begin_operation()
                else:
                    self._verify_mirror_for_append()
                assert operation_id is not None
                if not prepared:
                    self._complete_operation(operation_id, [])
                    return []
                encoded_records = [encoded for _, encoded in prepared]
                encoded = b"".join(encoded_records)
                self._complete_operation(operation_id, prepared)
                self._next_sequence += len(prepared)
                self._append_mirror(encoded)
            except AuditLogError:
                self._append_usable = False
                self._replay_usable = False
                raise
            finally:
                self._operation_complete.notify_all()
            return [json.loads(encoded_record) for encoded_record in encoded_records]

    def replay(self, *, after_sequence: int = 0) -> list[dict[str, object]]:
        records, _ = self.replay_snapshot(after_sequence=after_sequence)
        return records

    def replay_snapshot(
        self,
        *,
        after_sequence: int = 0,
        deadline: float | None = None,
        max_records: int | None = None,
        max_bytes: int | None = None,
    ) -> tuple[list[dict[str, object]], int]:
        if after_sequence < 0:
            raise ValueError("after_sequence must be non-negative")
        if max_records is not None and (
            not isinstance(max_records, int) or isinstance(max_records, bool) or max_records <= 0
        ):
            raise ValueError("max_records must be positive")
        if max_bytes is not None and (
            not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes <= 0
        ):
            raise ValueError("max_bytes must be positive")
        if deadline is None:
            deadline = monotonic() + LIVE_REPLAY_TIMEOUT_SECONDS
        remaining = deadline - monotonic()
        if remaining <= 0 or not self._lock.acquire(timeout=remaining):
            raise AuditLogError("session log replay exceeded the live replay deadline")
        try:
            if not self._replay_usable:
                raise AuditLogError("session log replay is uncertain after an incomplete operation")
            while self._has_pending_operation():
                remaining = deadline - monotonic()
                if remaining <= 0 or not self._operation_complete.wait(timeout=remaining):
                    raise AuditLogError(
                        "session log replay exceeded the live replay deadline while an operation "
                        "was pending"
                    )
                if not self._replay_usable:
                    raise AuditLogError(
                        "session log replay is uncertain after an incomplete operation"
                    )
            records = self._committed_records(
                parse=True,
                max_records=max_records,
                max_bytes=max_bytes,
                deadline=deadline,
            )
            return [record for record in records if record["seq"] > after_sequence], len(records)
        finally:
            self._lock.release()

    def _prepare_records(
        self, events: Iterable[Mapping[str, object]]
    ) -> list[tuple[dict[str, object], bytes]]:
        records: list[dict[str, object]] = []
        for offset, event in enumerate(events):
            self._validate_event(event)
            sequence = self._next_sequence + offset
            if sequence > _MAX_SQLITE_INTEGER:
                raise AuditLogError("audit sequence exceeds SQLite's signed integer range")
            records.append({"seq": sequence, "event": dict(event)})
        return [(record, self._encode_record(record)) for record in records]

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
            encoded = (
                json.dumps(record, allow_nan=False, separators=(",", ":"), sort_keys=True) + "\n"
            ).encode()
        except (TypeError, ValueError) as error:
            raise AuditLogError(f"event is not JSON-native: {error}") from None
        if len(encoded) > MAX_AUDIT_RECORD_BYTES:
            raise AuditLogError(f"encoded audit record exceeds {MAX_AUDIT_RECORD_BYTES} bytes")
        return encoded

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
                database.execute(f"CREATE TABLE IF NOT EXISTS records ({_RECORDS_TABLE})")
                database.execute(
                    "CREATE INDEX IF NOT EXISTS records_retained_lines "
                    "ON records(seq) WHERE line IS NOT NULL"
                )
                database.execute(
                    "CREATE INDEX IF NOT EXISTS operations_pending "
                    "ON operations(status) WHERE status = 'pending'"
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

    def _complete_operation(
        self, operation_id: int, prepared: list[tuple[dict[str, object], bytes]]
    ) -> None:
        try:
            with closing(self._connect()) as database:
                database.execute("BEGIN IMMEDIATE")
                status = database.execute(
                    "SELECT CASE WHEN typeof(status) = 'text' "
                    "AND status IN ('pending', 'complete') THEN status ELSE NULL END "
                    "FROM operations WHERE id = ?",
                    (operation_id,),
                ).fetchone()
                if status != ("pending",):
                    raise AuditLogError("audit operation is missing or already complete")
                if prepared:
                    # Do not discard the last known recovery source until the
                    # mirror has been verified and its successor lines can be
                    # committed in this same transaction.
                    database.execute("UPDATE records SET line = NULL WHERE line IS NOT NULL")
                for record, encoded in prepared:
                    database.execute(
                        "INSERT INTO records(seq, operation_id, digest, length, line) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (
                            record["seq"],
                            operation_id,
                            hashlib.sha256(encoded).digest(),
                            len(encoded),
                            encoded,
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
                    "SELECT CASE WHEN typeof(status) = 'text' "
                    "AND status IN ('pending', 'complete') THEN status ELSE NULL END "
                    "FROM operations WHERE id = ?",
                    (operation_id,),
                ).fetchone()
                if row is None:
                    return None
                if row[0] not in {"pending", "complete"}:
                    raise AuditLogError("invalid audit operation metadata")
                return row[0]
        except sqlite3.Error as error:
            raise AuditLogError(f"cannot inspect audit operation: {error}") from None

    def _database_last_sequence(self) -> int:
        if self._database_identity is None and not self.database_path.exists():
            return 0
        try:
            with closing(self._connect()) as database:
                row = database.execute("SELECT MAX(seq) FROM records").fetchone()
        except sqlite3.Error as error:
            raise AuditLogError(f"cannot replay {self.path.name}: {error}") from None
        last = None if row is None else row[0]
        if last is None:
            return 0
        if (
            not isinstance(last, int)
            or isinstance(last, bool)
            or not 0 <= last <= _MAX_SQLITE_INTEGER
        ):
            raise AuditLogError(f"cannot replay {self.path.name}: invalid audit sequence")
        return last

    @staticmethod
    def _database_schema(database: sqlite3.Connection) -> str:
        columns = {str(row[1]) for row in database.execute("PRAGMA table_info(records)")}
        if not columns:
            return "empty"
        return "digest" if "digest" in columns else "legacy"

    def _database_rows(self) -> Iterator[_CommittedRecord]:
        """Yield committed records in sequence order from either database schema."""
        if self._database_identity is None and not self.database_path.exists():
            return
        try:
            with closing(self._connect()) as database:
                schema = self._database_schema(database)
                if schema == "empty":
                    return
                cursor = self._bounded_record_cursor(database, schema)
                for expected, row in enumerate(cursor, start=1):
                    yield self._committed_row(schema, row, expected)
        except sqlite3.Error as error:
            raise AuditLogError(f"cannot replay {self.path.name}: {error}") from None

    def _committed_row(
        self, schema: str, row: tuple[object, ...], expected: int
    ) -> _CommittedRecord:
        sequence = row[0]
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence != expected:
            raise AuditLogError(f"cannot replay {self.path.name}: non-contiguous audit database")
        operation_id = row[1]
        if (
            not isinstance(operation_id, int)
            or isinstance(operation_id, bool)
            or not 1 <= operation_id <= _MAX_SQLITE_INTEGER
        ):
            raise AuditLogError(f"cannot replay {self.path.name}: invalid audit operation")
        if row[-1] != "complete":
            raise AuditLogError(f"cannot replay {self.path.name}: invalid audit operation")
        if schema == "legacy":
            event_json, value_type, encoded_length = row[2], row[3], row[4]
            if (
                value_type != "text"
                or not isinstance(encoded_length, int)
                or isinstance(encoded_length, bool)
                or not 1 <= encoded_length <= MAX_AUDIT_RECORD_BYTES
                or not isinstance(event_json, str)
            ):
                raise AuditLogError(
                    f"cannot replay {self.path.name}: invalid legacy audit record metadata"
                )
            try:
                record = {"seq": sequence, "event": json.loads(event_json)}
                _validate_record(record, expected, self.session, expected)
                line = self._encode_record(record)
            except (TypeError, UnicodeError, ValueError, RecursionError, AuditLogError) as error:
                raise AuditLogError(f"cannot replay {self.path.name}: {error}") from None
            return _CommittedRecord(
                seq=sequence,
                operation_id=operation_id,
                digest=hashlib.sha256(line).digest(),
                length=len(line),
                line=line,
            )
        digest, length, line, retained_length = row[2], row[3], row[4], row[5]
        if (
            not isinstance(digest, bytes)
            or len(digest) != hashlib.sha256().digest_size
            or not isinstance(length, int)
            or isinstance(length, bool)
            or not 1 <= length <= MAX_AUDIT_RECORD_BYTES
            or not (line is None or isinstance(line, bytes))
            or not (
                retained_length is None
                or (
                    isinstance(retained_length, int)
                    and not isinstance(retained_length, bool)
                    and 1 <= retained_length <= MAX_AUDIT_RECORD_BYTES
                )
            )
        ):
            raise AuditLogError(f"cannot replay {self.path.name}: invalid audit record metadata")
        if line is not None:
            if (
                retained_length != length
                or len(line) != length
                or hashlib.sha256(line).digest() != digest
            ):
                raise AuditLogError(f"cannot replay {self.path.name}: invalid retained audit line")
            try:
                record = json.loads(line)
                _validate_record(record, expected, self.session, expected)
                canonical = self._encode_record(record)
            except (UnicodeError, ValueError, RecursionError, AuditLogError) as error:
                raise AuditLogError(
                    f"cannot replay {self.path.name}: invalid retained audit line: {error}"
                ) from None
            if canonical != line:
                raise AuditLogError(
                    f"cannot replay {self.path.name}: retained audit line is not canonical"
                )
        return _CommittedRecord(
            seq=sequence,
            operation_id=operation_id,
            digest=digest,
            length=length,
            line=line,
        )

    @staticmethod
    def _bounded_record_cursor(database: sqlite3.Connection, schema: str) -> sqlite3.Cursor:
        """Select bodies only after SQLite proves their stored byte length is bounded."""
        if schema == "legacy":
            return database.execute(
                "SELECT records.seq, "
                "CASE WHEN typeof(records.operation_id) = 'integer' "
                "THEN records.operation_id ELSE NULL END, "
                "CASE WHEN typeof(event_json) = 'text' "
                "AND length(CAST(event_json AS BLOB)) <= ? THEN event_json ELSE NULL END, "
                "typeof(event_json), length(CAST(event_json AS BLOB)), "
                "CASE WHEN typeof(operations.status) = 'text' "
                "AND operations.status IN ('pending', 'complete') "
                "THEN operations.status ELSE NULL END "
                "FROM records LEFT JOIN operations ON operations.id = records.operation_id "
                "ORDER BY records.seq",
                (MAX_AUDIT_RECORD_BYTES,),
            )
        return database.execute(
            "SELECT records.seq, "
            "CASE WHEN typeof(records.operation_id) = 'integer' "
            "THEN records.operation_id ELSE NULL END, "
            "CASE WHEN typeof(digest) = 'blob' AND length(digest) = ? "
            "THEN digest ELSE zeroblob(0) END, "
            "CASE WHEN typeof(records.length) = 'integer' THEN records.length ELSE NULL END, "
            "CASE WHEN line IS NULL THEN NULL "
            "WHEN length(line) <= ? THEN line ELSE zeroblob(0) END, "
            "CASE WHEN line IS NULL THEN NULL ELSE length(line) END, "
            "CASE WHEN typeof(operations.status) = 'text' "
            "AND operations.status IN ('pending', 'complete') "
            "THEN operations.status ELSE NULL END "
            "FROM records LEFT JOIN operations ON operations.id = records.operation_id "
            "ORDER BY records.seq",
            (hashlib.sha256().digest_size, MAX_AUDIT_RECORD_BYTES),
        )

    def _migrate_legacy_database(self) -> None:
        """Replace a database that duplicates every event body with digest rows.

        The mirror is first recovered from the legacy bodies exactly as before, so
        the migration never loses a committed record the mirror did not hold.
        """
        try:
            with closing(self._connect()) as database:
                if self._database_schema(database) != "legacy":
                    return
        except sqlite3.Error as error:
            raise AuditLogError(f"cannot initialize audit database: {error}") from None
        self._recover_mirror_from_database(all_lines_recoverable=True)
        try:
            with closing(self._connect()) as database:
                database.execute("BEGIN IMMEDIATE")
                if self._database_schema(database) != "legacy":
                    database.commit()
                    return
                database.execute(f"CREATE TABLE records_digest ({_RECORDS_TABLE})")
                last_operation = database.execute(
                    "SELECT CASE WHEN typeof(operation_id) = 'integer' "
                    "THEN operation_id ELSE NULL END "
                    "FROM records ORDER BY seq DESC LIMIT 1"
                ).fetchone()
                last_operation = None if last_operation is None else last_operation[0]
                if last_operation is not None and (
                    not isinstance(last_operation, int)
                    or isinstance(last_operation, bool)
                    or not 1 <= last_operation <= _MAX_SQLITE_INTEGER
                ):
                    raise AuditLogError("legacy audit database has an invalid operation ID")
                reader = self._bounded_record_cursor(database, "legacy")
                for expected, row in enumerate(reader, start=1):
                    record = self._committed_row("legacy", row, expected)
                    database.execute(
                        "INSERT INTO records_digest(seq, operation_id, digest, length, line) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (
                            record.seq,
                            record.operation_id,
                            record.digest,
                            record.length,
                            record.line if record.operation_id == last_operation else None,
                        ),
                    )
                database.execute("DROP TABLE records")
                database.execute("ALTER TABLE records_digest RENAME TO records")
                database.commit()
                database.execute("VACUUM")
        except sqlite3.Error as error:
            raise AuditLogError(f"cannot migrate legacy audit database: {error}") from None
        _LOGGER.info("migrated legacy audit database to digest rows session_log=%s", self.path.name)

    def _migrate_legacy_records(self, lines: list[tuple[bytes, dict[str, object]]]) -> None:
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
                database.execute(f"CREATE TABLE records ({_RECORDS_TABLE})")
                cursor = database.execute("INSERT INTO operations(status) VALUES ('complete')")
                assert cursor.lastrowid is not None
                for raw, record in lines:
                    database.execute(
                        "INSERT INTO records(seq, operation_id, digest, length, line) "
                        "VALUES (?, ?, ?, ?, NULL)",
                        (
                            record["seq"],
                            cursor.lastrowid,
                            hashlib.sha256(raw).digest(),
                            len(raw),
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

    def _recover_mirror_from_database(self, *, all_lines_recoverable: bool = False) -> None:
        """Repair the mirror from committed metadata and any retained line bodies.

        The legacy schema retained every canonical event body, so its one-time
        migration may recover across operation boundaries. The digest schema
        deliberately retains only the newest operation and must not do so.
        """
        pending = self._has_pending_operation()
        try:
            mirror = open(self.path, "rb", buffering=_MIRROR_READ_BUFFER)
        except FileNotFoundError:
            mirror = None
        except OSError as error:
            raise AuditLogError(f"cannot replay {self.path.name}: {error}") from None
        try:
            rows = self._database_rows()
            offset = 0
            divergent: _CommittedRecord | None = None
            for row in rows:
                chunk = b"" if mirror is None else mirror.read(row.length)
                if len(chunk) == row.length and hashlib.sha256(chunk).digest() == row.digest:
                    offset += row.length
                    continue
                divergent = row
                break
            if divergent is None:
                if mirror is None or not mirror.read(1):
                    return
                mirror.seek(0, os.SEEK_END)
                extra_length = mirror.tell() - offset
                if pending:
                    self._replace_mirror_parts(mirror, offset, ())
                    return
                mirror.seek(-1, os.SEEK_END)
                if mirror.read(1) != b"\n":
                    self.recovered_tail_bytes += extra_length
                    self._replace_mirror_parts(mirror, offset, ())
                    _LOGGER.warning(
                        "recovered unterminated audit tail removed_bytes=%d", extra_length
                    )
                    return
                raise AuditLogError(
                    f"cannot replay {self.path.name}: mirror differs from committed audit"
                )
            if divergent.line is None:
                if mirror is None:
                    raise AuditLogError(f"cannot replay {self.path.name}: audit mirror is missing")
                raise AuditLogError(
                    f"cannot replay {self.path.name}: mirror differs from committed audit"
                )
            recovery_operation = divergent.operation_id

            def retained_lines() -> Iterator[bytes]:
                mirror_exhausted = mirror is None

                def retained_rows() -> Iterator[_CommittedRecord]:
                    yield divergent
                    yield from rows

                for row in retained_rows():
                    if row.line is None or (
                        not all_lines_recoverable and row.operation_id != recovery_operation
                    ):
                        detail = (
                            "audit mirror is missing"
                            if mirror is None
                            else "mirror differs from committed audit"
                        )
                        raise AuditLogError(f"cannot replay {self.path.name}: {detail}")
                    if not mirror_exhausted:
                        assert mirror is not None
                        actual = mirror.read(len(row.line))
                        if row.line[: len(actual)] != actual:
                            raise AuditLogError(
                                f"cannot replay {self.path.name}: "
                                "mirror differs from committed audit"
                            )
                        if len(actual) < len(row.line):
                            if mirror.read(1):
                                raise AuditLogError(
                                    f"cannot replay {self.path.name}: "
                                    "mirror differs from committed audit"
                                )
                            mirror_exhausted = True
                    yield row.line
                if not mirror_exhausted:
                    assert mirror is not None
                    if mirror.read(1):
                        raise AuditLogError(
                            f"cannot replay {self.path.name}: mirror differs from committed audit"
                        )

            self._replace_mirror_parts(mirror, offset, retained_lines())
        finally:
            if mirror is not None:
                mirror.close()

    def _committed_records(
        self,
        *,
        parse: bool,
        max_records: int | None = None,
        max_bytes: int | None = None,
        deadline: float | None = None,
    ) -> list[dict[str, object]]:
        """Verify every mirror line against its committed digest, parsing on request."""
        rows = self._database_rows()
        if not self.path.exists():
            if next(rows, None) is not None:
                raise AuditLogError(f"cannot replay {self.path.name}: audit mirror is missing")
            return []
        records: list[dict[str, object]] = []
        try:
            with open(self.path, "rb", buffering=_MIRROR_READ_BUFFER) as stream:
                if max_bytes is not None and os.fstat(stream.fileno()).st_size > max_bytes:
                    raise AuditLogError("session log mirror exceeds the replay byte limit")
                for line_number, row in enumerate(rows, start=1):
                    if deadline is not None and monotonic() >= deadline:
                        raise AuditLogError("session log replay exceeded the live replay deadline")
                    if max_records is not None and line_number > max_records:
                        raise AuditLogError("session log exceeds the replay record limit")
                    chunk = stream.read(row.length)
                    if len(chunk) != row.length or hashlib.sha256(chunk).digest() != row.digest:
                        if max_bytes is not None:
                            raise AuditLogError(
                                f"cannot replay {self.path.name}: "
                                "mirror differs from committed audit"
                            )
                        self._fail_divergent_mirror()
                    if not parse:
                        continue
                    try:
                        record = json.loads(chunk)
                    except (UnicodeError, ValueError, RecursionError) as error:
                        raise AuditLogError(f"cannot replay {self.path.name}: {error}") from None
                    _validate_record(record, line_number, self.session, line_number)
                    records.append(record)
                if stream.read(1):
                    if max_bytes is not None:
                        if os.fstat(stream.fileno()).st_size > max_bytes:
                            raise AuditLogError("session log mirror exceeds the replay byte limit")
                        raise AuditLogError(
                            f"cannot replay {self.path.name}: mirror differs from committed audit"
                        )
                    self._fail_divergent_mirror()
                if deadline is not None and monotonic() >= deadline:
                    raise AuditLogError("session log replay exceeded the live replay deadline")
        except OSError as error:
            raise AuditLogError(f"cannot replay {self.path.name}: {error}") from None
        return records

    def _fail_divergent_mirror(self) -> None:
        # Surface a malformed or reordered mirror record before the generic verdict.
        self._read_jsonl(repair_tail=False)
        raise AuditLogError(f"cannot replay {self.path.name}: mirror differs from committed audit")

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
            self._committed_records(parse=False)
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
        self._replace_mirror_parts(None, 0, (content,))

    def _replace_mirror_parts(
        self, source: BinaryIO | None, prefix_length: int, suffixes: Iterable[bytes]
    ) -> None:
        """Atomically publish a mirror assembled with bounded streaming I/O."""
        temporary_path: Path | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                dir=self.root, prefix=f".{self.path.name}.", suffix=".repair"
            )
            temporary_path = Path(temporary_name)
            with os.fdopen(descriptor, "wb") as stream:
                if source is not None:
                    source.seek(0)
                if prefix_length:
                    if source is None:
                        raise OSError("mirror repair source is missing")
                    remaining_prefix = prefix_length
                    while remaining_prefix:
                        chunk = source.read(min(_MIRROR_READ_BUFFER, remaining_prefix))
                        if not chunk:
                            raise OSError("mirror repair source ended before its committed prefix")
                        self._write_all(stream.fileno(), chunk)
                        remaining_prefix -= len(chunk)
                for suffix in suffixes:
                    self._write_all(stream.fileno(), suffix)
                os.fsync(stream.fileno())
            os.replace(temporary_path, self.path)
            self._fsync_root()
        except AuditLogError:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise
        except OSError as error:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise AuditLogError(f"cannot repair {self.path.name}: {error}") from None

    @staticmethod
    def _write_all(descriptor: int, content: bytes) -> None:
        remaining = memoryview(content)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("mirror repair made no progress")
            remaining = remaining[written:]

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

    def _read_jsonl(self, *, repair_tail: bool) -> list[tuple[bytes, dict[str, object]]]:
        """Parse every mirror line with its exact bytes, optionally dropping a torn tail."""
        lines: list[tuple[bytes, dict[str, object]]] = []
        removed = 0
        valid_prefix_length = 0
        try:
            with open(self.path, "rb", buffering=_MIRROR_READ_BUFFER) as stream:
                line_number = 0
                while raw := stream.readline(MAX_AUDIT_RECORD_BYTES + 1):
                    if len(raw) > MAX_AUDIT_RECORD_BYTES:
                        raise AuditLogError(
                            f"cannot replay {self.path.name}: audit record exceeds "
                            f"{MAX_AUDIT_RECORD_BYTES} bytes"
                        )
                    if repair_tail and not raw.endswith(b"\n"):
                        removed = len(raw)
                        break
                    line_number += 1
                    try:
                        record = json.loads(raw)
                        _validate_record(record, line_number, self.session, line_number)
                    except (UnicodeError, ValueError, RecursionError) as error:
                        raise AuditLogError(f"cannot replay {self.path.name}: {error}") from None
                    lines.append((raw, record))
                    valid_prefix_length += len(raw)
                if removed:
                    self._replace_mirror_parts(stream, valid_prefix_length, ())
        except OSError as error:
            raise AuditLogError(f"cannot replay {self.path.name}: {error}") from None
        if removed:
            self.recovered_tail_bytes += removed
            _LOGGER.warning("recovered unterminated audit tail removed_bytes=%d", removed)
        return lines

    def _recover_legacy_pending_operation(self) -> None:
        try:
            flags = os.O_RDONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(self.pending_path, flags)
            with os.fdopen(descriptor, encoding="ascii") as stream:
                text = stream.read(_MAX_PENDING_CURSOR_BYTES + 1)
                if len(text) > _MAX_PENDING_CURSOR_BYTES or stream.read(1):
                    raise ValueError
            original_size = int(text)
            if not 0 <= original_size <= _MAX_SQLITE_INTEGER or text != f"{original_size}\n":
                raise ValueError
            if not self.path.exists():
                if original_size != 0:
                    raise AuditLogError("pending audit cursor exceeds missing session log")
                self._replace_mirror(b"")
            else:
                if self.path.stat().st_size < original_size:
                    raise AuditLogError("pending audit cursor exceeds session log")
                with open(self.path, "rb", buffering=_MIRROR_READ_BUFFER) as mirror:
                    self._replace_mirror_parts(mirror, original_size, ())
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


def _require_sqlite_operation_id(operation_id: object) -> None:
    if (
        not isinstance(operation_id, int)
        or isinstance(operation_id, bool)
        or not 1 <= operation_id <= _MAX_SQLITE_INTEGER
    ):
        raise AuditLogError("audit operation ID must be a positive signed 64-bit integer")


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
