from __future__ import annotations

import builtins
import hashlib
import json
import os
import sqlite3
import stat
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from threading import Event

import pytest

from relay.audit import MAX_AUDIT_RECORD_BYTES, AuditLogError, SessionAuditLog


def _event(event_id: str, event_type: str = "state") -> dict[str, object]:
    return {
        "v": 1,
        "t": 100,
        "type": event_type,
        "event_id": event_id,
        "session": "session-1",
    }


def _exact_size_event(size: int) -> dict[str, object]:
    event = _event("boundary")
    event["payload"] = ""
    empty_size = len(SessionAuditLog._encode_record({"seq": 1, "event": event}))
    assert size >= empty_size
    event["payload"] = "x" * (size - empty_size)
    return event


class _BoundedReadMirror:
    def __init__(self, stream: object, sizes: list[int]) -> None:
        self._stream = stream
        self._sizes = sizes

    def read(self, size: int = -1) -> bytes:
        self._sizes.append(size)
        assert 0 <= size <= MAX_AUDIT_RECORD_BYTES
        return self._stream.read(size)  # type: ignore[no-any-return, union-attr]

    def __getattr__(self, name: str) -> object:
        return getattr(self._stream, name)


def _guard_mirror_reads(monkeypatch: pytest.MonkeyPatch, path: Path) -> list[int]:
    real_open = builtins.open
    sizes: list[int] = []

    def guarded_open(file: object, *args: object, **kwargs: object) -> object:
        stream = real_open(file, *args, **kwargs)
        if Path(file) == path and args and args[0] == "rb":  # type: ignore[arg-type]
            return _BoundedReadMirror(stream, sizes)
        return stream

    monkeypatch.setattr(builtins, "open", guarded_open)
    return sizes


def test_append_replay_and_reopen_preserve_contiguous_order(tmp_path: Path) -> None:
    log = SessionAuditLog(tmp_path, "session-1")
    first = log.append(_event("event-1", "intent_record"))
    second = log.append(_event("event-2", "acknowledgement"))

    reopened = SessionAuditLog(tmp_path, "session-1")
    third = reopened.append(_event("event-3", "state"))

    assert [first["seq"], second["seq"], third["seq"]] == [1, 2, 3]
    assert [record["event"]["event_id"] for record in reopened.replay()] == [
        "event-1",
        "event-2",
        "event-3",
    ]
    assert [record["seq"] for record in reopened.replay(after_sequence=1)] == [2, 3]
    assert reopened.last_sequence == 3


def test_operation_batch_preserves_one_jsonl_record_per_event(tmp_path: Path) -> None:
    log = SessionAuditLog(tmp_path, "session-1")

    records = log.append_batch([_event("event-1", "membership"), _event("event-2", "state")])

    assert [record["seq"] for record in records] == [1, 2]
    assert len(log.path.read_text(encoding="utf-8").splitlines()) == 2
    assert SessionAuditLog(tmp_path, "session-1").replay() == records


def test_exact_maximum_record_round_trips_and_oversize_is_rejected_before_operation(
    tmp_path: Path,
) -> None:
    log = SessionAuditLog(tmp_path, "session-1")
    boundary = _exact_size_event(MAX_AUDIT_RECORD_BYTES)

    record = log.append(boundary)

    assert log.path.stat().st_size == MAX_AUDIT_RECORD_BYTES
    assert SessionAuditLog(tmp_path, "session-1").replay() == [record]
    committed = log.path.read_bytes()
    oversized = dict(boundary)
    oversized["payload"] = str(oversized["payload"]) + "x"
    with pytest.raises(AuditLogError, match="encoded audit record exceeds"):
        log.append(oversized)
    assert log.path.read_bytes() == committed
    assert log.append(_event("after-oversize"))["seq"] == 2


def test_replay_deadline_includes_waiting_to_acquire_the_audit_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = SessionAuditLog(tmp_path, "session-1")
    append_holds_lock = Event()
    release_append = Event()
    real_append_mirror = log._append_mirror

    def pause_append_mirror(encoded: bytes) -> None:
        append_holds_lock.set()
        assert release_append.wait(timeout=2)
        real_append_mirror(encoded)

    monkeypatch.setattr(log, "_append_mirror", pause_append_mirror)
    with ThreadPoolExecutor(max_workers=1) as executor:
        append = executor.submit(log.append, _event("event-1"))
        assert append_holds_lock.wait(timeout=2)
        started = time.monotonic()
        try:
            with pytest.raises(AuditLogError, match="live replay deadline"):
                log.replay_snapshot(deadline=started + 0.1)
            elapsed = time.monotonic() - started
        finally:
            release_append.set()
            append.result(timeout=2)

    assert elapsed < 1.0
    assert [record["seq"] for record in log.replay()] == [1]


@pytest.mark.parametrize("reopen", [False, True])
def test_schema_initialization_runs_once_per_log_with_full_durability_on_every_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reopen: bool
) -> None:
    if reopen:
        SessionAuditLog(tmp_path, "session-1").append(_event("seed"))
    connect = sqlite3.connect
    statements: list[str] = []
    connections = 0

    def trace_connection(*args: object, **kwargs: object) -> sqlite3.Connection:
        nonlocal connections
        connections += 1
        database = connect(*args, **kwargs)
        database.set_trace_callback(statements.append)
        return database

    monkeypatch.setattr(sqlite3, "connect", trace_connection)
    log = SessionAuditLog(tmp_path, "session-1")
    for index in range(3):
        log.append(_event(f"event-{index}"))

    assert statements.count("PRAGMA journal_mode=WAL") == 1
    assert sum(statement.startswith("CREATE TABLE") for statement in statements) == 2
    # Each connection keeps FULL synchronous and foreign keys; initialization also
    # explicitly requests FULL. Only the redundant schema connection was removed.
    assert statements.count("PRAGMA synchronous=FULL") == connections + 1
    assert statements.count("PRAGMA foreign_keys=ON") == connections
    connections_before = connections
    record = log.append(_event("last-event"))
    assert connections - connections_before == 2
    assert SessionAuditLog(tmp_path, "session-1").replay()[-1] == record


@pytest.mark.parametrize("operation", ["begin", "complete", "replay"])
@pytest.mark.parametrize("mutation", ["remove", "replace", "truncate", "corrupt", "drop_schema"])
def test_initialized_database_changes_fail_closed_without_recreation(
    tmp_path: Path, operation: str, mutation: str
) -> None:
    log = SessionAuditLog(tmp_path, "session-1")
    log.append(_event("event-1"))
    operation_id = log.begin_operation() if operation == "complete" else None
    mirror = log.path.read_bytes()
    original_database = log.database_path.read_bytes()
    if mutation == "remove":
        log.database_path.unlink()
    elif mutation == "replace":
        replacement = tmp_path / "replacement.sqlite3"
        replacement.write_bytes(original_database)
        os.replace(replacement, log.database_path)
    elif mutation == "drop_schema":
        with closing(sqlite3.connect(log.database_path)) as database:
            database.executescript("DROP TABLE records; DROP TABLE operations;")
    else:
        log.database_path.write_bytes(b"" if mutation == "truncate" else b"not a database")
    changed_database = log.database_path.read_bytes() if log.database_path.exists() else None

    with pytest.raises(AuditLogError):
        if operation == "begin":
            log.begin_operation()
        elif operation == "complete":
            log.append_batch([_event("event-2")], operation_id=operation_id)
        else:
            log.replay()

    assert log.path.read_bytes() == mirror
    if changed_database is None:
        assert not log.database_path.exists()
    else:
        assert log.database_path.read_bytes() == changed_database
    with pytest.raises(AuditLogError):
        log.append(_event("later-event"))
    with pytest.raises(AuditLogError):
        log.replay()


def test_database_removed_during_sqlite_open_is_not_recreated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = SessionAuditLog(tmp_path, "session-1")
    log.append(_event("event-1"))
    connect = sqlite3.connect

    def remove_then_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        log.database_path.unlink()
        return connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", remove_then_connect)
    with pytest.raises(AuditLogError, match="cannot begin audit operation"):
        log.begin_operation()
    assert not log.database_path.exists()


def test_database_uri_preserves_special_characters_in_log_directory(tmp_path: Path) -> None:
    root = tmp_path / "audit ?mode=rwc # ü"
    log = SessionAuditLog(root, "session-1")
    record = log.append(_event("event-1"))

    assert log.database_path.parent == root
    assert SessionAuditLog(root, "session-1").replay() == [record]


@pytest.mark.parametrize("reopen", [False, True])
def test_live_append_does_not_read_or_reencode_existing_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reopen: bool
) -> None:
    log = SessionAuditLog(tmp_path, "session-1")
    log.append_batch(_event(f"event-{index}") for index in range(1000))
    if reopen:
        log = SessionAuditLog(tmp_path, "session-1")
    encode_record = log._encode_record
    encoded_sequences: list[int] = []

    def reject_history_read(*_args: object) -> None:
        pytest.fail("live append read existing history")

    def record_encoding(record: dict[str, object]) -> bytes:
        encoded_sequences.append(record["seq"])
        return encode_record(record)

    with monkeypatch.context() as live:
        live.setattr(log, "_database_rows", reject_history_read)
        live.setattr(log, "_committed_records", reject_history_read)
        live.setattr(Path, "read_bytes", reject_history_read)
        live.setattr(log, "_encode_record", record_encoding)
        records = []
        for index in range(50):
            encoded_sequences.clear()
            batch = log.append_batch([_event(f"telemetry-{index}"), _event(f"state-{index}")])
            assert set(encoded_sequences) == {1001 + 2 * index, 1002 + 2 * index}
            records.extend(batch)

    assert [record["seq"] for record in records] == list(range(1001, 1101))
    assert log.replay(after_sequence=1000) == records


@pytest.mark.parametrize("mutation", ["same-size", "replace", "truncate", "remove"])
def test_live_append_detects_mirror_changes_before_committing_new_records(
    tmp_path: Path, mutation: str
) -> None:
    log = SessionAuditLog(tmp_path, "session-1")
    log.append(_event("event-1"))
    original = log.path.read_bytes()
    metadata = log.path.stat()
    if mutation == "same-size":
        log.path.write_bytes(original.replace(b"event-1", b"event-2"))
        os.utime(log.path, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
    elif mutation == "replace":
        replacement = tmp_path / "replacement.jsonl"
        replacement.write_bytes(original.replace(b"event-1", b"event-2"))
        os.replace(replacement, log.path)
    elif mutation == "truncate":
        log.path.write_bytes(original[:-5])
    else:
        log.path.unlink()

    with pytest.raises(AuditLogError):
        log.append(_event("new-record"))

    with sqlite3.connect(log.database_path) as database:
        assert database.execute("SELECT COUNT(*) FROM records").fetchone() == (1,)


def test_rejected_standalone_append_preserves_the_previous_recovery_source(
    tmp_path: Path,
) -> None:
    log = SessionAuditLog(tmp_path, "session-1")
    first = log.append(_event("event-1"))
    expected = log.path.read_bytes()
    log.path.write_bytes(expected[:-5])

    with pytest.raises(AuditLogError):
        log.append(_event("event-2"))
    with pytest.raises(AuditLogError, match="unusable"):
        log.append(_event("later"))
    with sqlite3.connect(log.database_path) as database:
        assert database.execute(
            "SELECT COUNT(*) FROM operations WHERE status = 'pending'"
        ).fetchone() == (0,)
        assert database.execute(
            "SELECT line IS NOT NULL FROM records WHERE seq = 1"
        ).fetchone() == (1,)

    reopened = SessionAuditLog(tmp_path, "session-1")
    assert reopened.replay() == [first]
    assert reopened.path.read_bytes() == expected
    assert reopened.append(_event("event-2"))["seq"] == 2


def test_rejected_outer_operation_repairs_prior_tail_but_remains_fenced(
    tmp_path: Path,
) -> None:
    log = SessionAuditLog(tmp_path, "session-1")
    log.append(_event("event-1"))
    expected = log.path.read_bytes()
    operation_id = log.begin_operation()
    log.path.write_bytes(expected[:-5])

    with pytest.raises(AuditLogError):
        log.append_batch([_event("event-2")], operation_id=operation_id)

    reopened = SessionAuditLog(tmp_path, "session-1")
    assert reopened.path.read_bytes() == expected
    with pytest.raises(AuditLogError, match="incomplete operation"):
        reopened.replay()
    with pytest.raises(AuditLogError, match="unusable"):
        reopened.append(_event("later"))


def test_initial_database_and_jsonl_creation_sync_the_log_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = SessionAuditLog(tmp_path, "session-1")
    real_fsync = os.fsync
    directory_syncs = 0

    def record_fsync(descriptor: int) -> None:
        nonlocal directory_syncs
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            directory_syncs += 1
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", record_fsync)

    log.append(_event("event-1"))

    assert directory_syncs >= 2


@pytest.mark.parametrize("failure_point", ["database_fsync", "publish"])
def test_legacy_migration_publishes_database_only_after_complete_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_point: str
) -> None:
    seed = SessionAuditLog(tmp_path, "session-1")
    legacy_record = {"seq": 1, "event": _event("event-1")}
    legacy_bytes = (
        json.dumps(legacy_record, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode()
    seed.path.write_bytes(legacy_bytes)
    real_replace = os.replace

    def fail_database_publish(source: object, target: object) -> None:
        if Path(target) == seed.database_path:
            raise OSError("injected migration publish failure")
        real_replace(source, target)

    def fail_database_fsync(_log: SessionAuditLog, _path: Path) -> None:
        raise OSError("injected migration fsync failure")

    if failure_point == "database_fsync":
        monkeypatch.setattr(SessionAuditLog, "_fsync_path", fail_database_fsync)
    else:
        monkeypatch.setattr(os, "replace", fail_database_publish)
    with pytest.raises(AuditLogError, match="cannot migrate legacy audit log"):
        SessionAuditLog(tmp_path, "session-1")

    assert seed.path.read_bytes() == legacy_bytes
    assert not seed.database_path.exists()

    monkeypatch.undo()
    assert SessionAuditLog(tmp_path, "session-1").replay() == [legacy_record]


def test_committed_database_recovers_missing_or_torn_jsonl_mirror(tmp_path: Path) -> None:
    log = SessionAuditLog(tmp_path, "session-1")
    record = log.append(_event("event-1"))
    expected = log.path.read_bytes()

    log.path.unlink()
    assert SessionAuditLog(tmp_path, "session-1").replay() == [record]
    assert log.path.read_bytes() == expected

    log.path.write_bytes(expected[:-5])
    assert SessionAuditLog(tmp_path, "session-1").replay() == [record]
    assert log.path.read_bytes() == expected


@pytest.mark.parametrize("damage", ["clean", "torn"])
def test_database_reopen_compares_and_repairs_without_path_read_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    log = SessionAuditLog(tmp_path, "session-1")
    records = log.append_batch(_event(f"event-{index}") for index in range(1, 501))
    expected = log.path.read_bytes()
    if damage == "torn":
        log.path.write_bytes(expected[:-17])

    def reject_whole_file_read(*_args: object, **_kwargs: object) -> bytes:
        pytest.fail("database-backed recovery read the whole mirror")

    with monkeypatch.context() as guarded:
        guarded.setattr(Path, "read_bytes", reject_whole_file_read)
        reopened = SessionAuditLog(tmp_path, "session-1")
        assert reopened.replay() == records

    assert log.path.read_bytes() == expected


def test_interrupted_mirror_replacement_preserves_recoverable_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = SessionAuditLog(tmp_path, "session-1")
    record = log.append(_event("event-1"))
    torn = log.path.read_bytes()[:-5]
    log.path.write_bytes(torn)
    real_replace = os.replace

    def fail_mirror_publish(source: object, target: object) -> None:
        if Path(target) == log.path:
            raise OSError("injected mirror publish failure")
        real_replace(source, target)

    monkeypatch.setattr(os, "replace", fail_mirror_publish)
    with pytest.raises(AuditLogError, match="cannot repair"):
        SessionAuditLog(tmp_path, "session-1")

    assert log.path.read_bytes() == torn
    assert log.database_path.exists()

    monkeypatch.setattr(os, "replace", real_replace)
    assert SessionAuditLog(tmp_path, "session-1").replay() == [record]


@pytest.mark.parametrize("field", ["token", "signature", "authorization", "secret"])
def test_sensitive_fields_are_refused_at_any_depth(tmp_path: Path, field: str) -> None:
    log = SessionAuditLog(tmp_path, "session-1")
    event = _event("event-1")
    event["nested"] = {field: "must-not-land"}

    with pytest.raises(AuditLogError, match="sensitive field"):
        log.append(event)
    assert not log.path.exists()


@pytest.mark.parametrize("value", [float("nan"), {1, 2}])
def test_rejected_append_does_not_poison_next_append(tmp_path: Path, value: object) -> None:
    log = SessionAuditLog(tmp_path, "session-1")
    event = _event("event-1")
    event["value"] = value

    with pytest.raises(AuditLogError):
        log.append(event)

    if log.database_path.exists():
        with sqlite3.connect(log.database_path) as database:
            assert database.execute(
                "SELECT COUNT(*) FROM operations WHERE status = 'pending'"
            ).fetchone() == (0,)
    assert not log.path.exists()
    assert log.append(_event("valid"))["seq"] == 1
    assert len(log.replay()) == 1
    assert len(SessionAuditLog(tmp_path, "session-1").replay()) == 1


@pytest.mark.parametrize(
    "encoded",
    [
        b'{"seq":1,"event":{"session":"session-1","event_id":"bad","value":'
        + b"9" * 5000
        + b"}}\n",
        b'{"seq":1,"event":{"session":"session-1","event_id":"bad","value":'
        + b"[" * 2000
        + b"0"
        + b"]" * 2000
        + b"}}\n",
    ],
    ids=["integer-limit", "nesting-limit"],
)
def test_legacy_decoder_resource_limits_are_audit_errors_without_mutation(
    tmp_path: Path, encoded: bytes
) -> None:
    seed = SessionAuditLog(tmp_path, "session-1")
    seed.path.write_bytes(encoded)

    with pytest.raises(AuditLogError, match="cannot replay"):
        SessionAuditLog(tmp_path, "session-1")

    assert seed.path.read_bytes() == encoded
    assert not seed.database_path.exists()


def test_oversized_legacy_jsonl_line_is_rejected_by_a_bounded_read(
    tmp_path: Path,
) -> None:
    seed = SessionAuditLog(tmp_path, "session-1")
    oversized = b"x" * (MAX_AUDIT_RECORD_BYTES + 1) + b"\n"
    seed.path.write_bytes(oversized)

    with pytest.raises(AuditLogError, match="audit record exceeds"):
        SessionAuditLog(tmp_path, "session-1")

    assert seed.path.stat().st_size == len(oversized)
    assert not seed.database_path.exists()


@pytest.mark.parametrize(
    "cursor",
    ["-1\n", f"{(1 << 63) - 1}\n", "9" * 100 + "\n"],
    ids=["negative", "signed-long-max", "oversized-metadata"],
)
def test_invalid_legacy_pending_cursor_is_read_with_a_tiny_bound(
    tmp_path: Path, cursor: str
) -> None:
    seed = SessionAuditLog(tmp_path, "session-1")
    seed.pending_path.write_text(cursor, encoding="ascii")

    with pytest.raises(AuditLogError):
        SessionAuditLog(tmp_path, "session-1")

    assert seed.pending_path.read_text(encoding="ascii") == cursor


def test_zero_legacy_pending_cursor_remains_a_valid_empty_recovery(tmp_path: Path) -> None:
    seed = SessionAuditLog(tmp_path, "session-1")
    seed.pending_path.write_text("0\n", encoding="ascii")

    reopened = SessionAuditLog(tmp_path, "session-1")

    assert reopened.replay() == []
    assert reopened.path.read_bytes() == b""
    assert not reopened.pending_path.exists()


def test_corrupt_or_reordered_log_fails_closed(tmp_path: Path) -> None:
    log = SessionAuditLog(tmp_path, "session-1")
    log.append(_event("event-1"))
    bad_record = {"seq": 3, "event": _event("event-2")}
    with log.path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(bad_record) + "\n")

    with pytest.raises(AuditLogError, match="non-contiguous"):
        log.replay()


def test_boolean_sequence_is_not_accepted_as_integer_one(tmp_path: Path) -> None:
    log = SessionAuditLog(tmp_path, "session-1")
    with log.path.open("w", encoding="utf-8") as stream:
        stream.write(json.dumps({"seq": True, "event": _event("event-1")}) + "\n")

    with pytest.raises(AuditLogError, match="non-contiguous"):
        log.replay()


def test_session_name_is_hashed_and_cannot_escape_log_root(tmp_path: Path) -> None:
    log = SessionAuditLog(tmp_path, "../../another/session")

    assert log.path.parent == tmp_path
    assert log.path.name.endswith(".jsonl")
    assert ".." not in log.path.name


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="requires no-follow file opens")
def test_sqlite_database_symlink_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "outside.sqlite3"
    target.touch()
    digest = hashlib.sha256(b"session-1").hexdigest()
    (tmp_path / f"{digest}.sqlite3").symlink_to(target)

    with pytest.raises(AuditLogError, match="cannot initialize audit database"):
        SessionAuditLog(tmp_path, "session-1")


def test_reopen_repairs_only_an_unterminated_tail(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    log = SessionAuditLog(tmp_path, "session-1")
    log.append(_event("event-1"))
    valid_prefix = log.path.read_bytes()
    torn = json.dumps({"seq": 2, "event": _event("event-2")}).encode()[:19]
    log.path.write_bytes(valid_prefix + torn)

    reopened = SessionAuditLog(tmp_path, "session-1")

    assert [record["seq"] for record in reopened.replay()] == [1]
    assert reopened.last_sequence == 1
    assert reopened.recovered_tail_bytes == len(torn)
    assert reopened.path.read_bytes() == valid_prefix
    assert stat.S_IMODE(reopened.path.stat().st_mode) == 0o600
    assert f"removed_bytes={len(torn)}" in caplog.text
    assert "event-2" not in caplog.text

    replayed = SessionAuditLog(tmp_path, "session-1")
    assert replayed.recovered_tail_bytes == 0
    assert replayed.replay() == reopened.replay()


def test_torn_first_record_remains_persisted_session_evidence(tmp_path: Path) -> None:
    log = SessionAuditLog(tmp_path, "session-1")
    log.path.write_bytes(b'{"seq":1')

    reopened = SessionAuditLog(tmp_path, "session-1")

    assert reopened.last_sequence == 0
    assert reopened.had_persisted_log is True
    assert reopened.path.read_bytes() == b""


def test_complete_corrupt_tail_fails_without_mutation(tmp_path: Path) -> None:
    log = SessionAuditLog(tmp_path, "session-1")
    log.append(_event("event-1"))
    corrupted = log.path.read_bytes() + b'{"seq":2,not-json}\n'
    log.path.write_bytes(corrupted)

    with pytest.raises(AuditLogError):
        SessionAuditLog(tmp_path, "session-1")

    assert log.path.read_bytes() == corrupted


def test_torn_tail_does_not_hide_a_corrupt_complete_prefix(tmp_path: Path) -> None:
    log = SessionAuditLog(tmp_path, "session-1")
    corrupted = b'{"seq":1,not-json}\n{"seq":2'
    log.path.write_bytes(corrupted)

    with pytest.raises(AuditLogError):
        SessionAuditLog(tmp_path, "session-1")

    assert log.path.read_bytes() == corrupted


def test_torn_tail_does_not_hide_nonfinite_complete_record(tmp_path: Path) -> None:
    log = SessionAuditLog(tmp_path, "session-1")
    corrupted = b'{"seq":1,"event":{"session":"session-1","event_id":"bad","value":NaN}}\n{"seq":2'
    log.path.write_bytes(corrupted)

    with pytest.raises(AuditLogError, match="non-finite"):
        SessionAuditLog(tmp_path, "session-1")

    assert log.path.read_bytes() == corrupted


def test_short_write_retries_until_record_is_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = SessionAuditLog(tmp_path, "session-1")
    real_write = os.write
    calls = 0

    def short_then_complete(descriptor: int, data: bytes | memoryview) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            return real_write(descriptor, data[:7])
        if calls == 2:
            raise InterruptedError
        return real_write(descriptor, data)

    monkeypatch.setattr(os, "write", short_then_complete)

    record = log.append(_event("event-1"))

    assert record["seq"] == 1
    assert log.replay() == [record]


def test_failed_partial_mirror_write_fences_writer_and_reopen_recovers_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = SessionAuditLog(tmp_path, "session-1")
    first = log.append(_event("event-1"))
    real_write = os.write
    calls = 0

    def partial_then_fail(descriptor: int, data: bytes | memoryview) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            return real_write(descriptor, data[:5])
        raise OSError("injected append failure")

    monkeypatch.setattr(os, "write", partial_then_fail)
    with pytest.raises(AuditLogError, match="cannot append"):
        log.append(_event("event-2"))

    with pytest.raises(AuditLogError, match="cursor is uncertain"):
        _ = log.last_sequence
    with pytest.raises(AuditLogError, match="replay is uncertain"):
        log.replay()

    monkeypatch.setattr(os, "write", real_write)
    reopened = SessionAuditLog(tmp_path, "session-1")
    assert [record["seq"] for record in reopened.replay()] == [1, 2]
    third = reopened.append(_event("event-3"))
    assert [first["seq"], third["seq"]] == [1, 3]


def test_failed_mirror_write_never_uses_in_place_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = SessionAuditLog(tmp_path, "session-1")
    real_write = os.write
    writes = 0

    def partial_then_fail(descriptor: int, data: bytes | memoryview) -> int:
        nonlocal writes
        writes += 1
        if writes == 1:
            return real_write(descriptor, data[:5])
        raise OSError("injected append failure")

    truncates = 0

    def failed_truncate(_descriptor: int, _length: int) -> None:
        nonlocal truncates
        truncates += 1
        raise OSError("injected rollback failure")

    monkeypatch.setattr(os, "write", partial_then_fail)
    monkeypatch.setattr(os, "ftruncate", failed_truncate)

    with pytest.raises(AuditLogError, match="cannot append session log"):
        log.append(_event("event-1"))
    writes_after_failure = writes
    with pytest.raises(AuditLogError, match="unusable"):
        log.append(_event("event-2"))

    assert writes == writes_after_failure
    with pytest.raises(AuditLogError, match="cursor is uncertain"):
        _ = log.last_sequence
    with pytest.raises(AuditLogError, match="replay is uncertain"):
        log.replay()
    assert truncates == 0

    monkeypatch.setattr(os, "write", real_write)
    assert [record["seq"] for record in SessionAuditLog(tmp_path, "session-1").replay()] == [1]


def test_close_failure_fences_writer_and_reopen_continues_sequence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = SessionAuditLog(tmp_path, "session-1")
    real_close = os.close
    real_open = os.open
    mirror_descriptors: set[int] = set()

    def track_mirror_open(path: object, flags: int, *args: object) -> int:
        descriptor = real_open(path, flags, *args)
        if flags & os.O_APPEND:
            mirror_descriptors.add(descriptor)
        return descriptor

    def close_then_fail(descriptor: int) -> None:
        real_close(descriptor)
        if descriptor in mirror_descriptors:
            raise OSError("injected close failure")

    monkeypatch.setattr(os, "open", track_mirror_open)
    monkeypatch.setattr(os, "close", close_then_fail)

    with pytest.raises(AuditLogError, match="cannot close session log"):
        log.append(_event("event-1"))
    with pytest.raises(AuditLogError, match="unusable"):
        log.append(_event("event-2"))
    with pytest.raises(AuditLogError, match="cursor is uncertain"):
        _ = log.last_sequence
    with pytest.raises(AuditLogError, match="replay is uncertain"):
        log.replay()

    monkeypatch.setattr(os, "close", real_close)
    reopened = SessionAuditLog(tmp_path, "session-1")
    assert [record["seq"] for record in reopened.replay()] == [1]
    second = reopened.append(_event("event-2"))

    assert second["seq"] == 2
    assert [record["seq"] for record in reopened.replay()] == [1, 2]


def test_open_failure_is_normalized_as_an_audit_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = SessionAuditLog(tmp_path, "session-1")
    real_open = os.open

    def failing_open(path: object, flags: int, *args: object) -> int:
        if flags & os.O_APPEND:
            raise PermissionError("injected open failure")
        return real_open(path, flags, *args)

    monkeypatch.setattr(os, "open", failing_open)

    with pytest.raises(AuditLogError, match="cannot open session log"):
        log.append(_event("event-1"))


def test_fstat_failure_closes_the_open_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = SessionAuditLog(tmp_path, "session-1")
    descriptors: list[int] = []
    real_open = os.open
    real_fstat = os.fstat

    def recording_open(path: object, flags: int, *args: object) -> int:
        descriptor = real_open(path, flags, *args)
        if flags & os.O_APPEND:
            descriptors.append(descriptor)
        return descriptor

    def failing_fstat(descriptor: int) -> os.stat_result:
        if descriptor in descriptors:
            raise OSError("injected fstat failure")
        return real_fstat(descriptor)

    monkeypatch.setattr(os, "open", recording_open)
    monkeypatch.setattr(os, "fstat", failing_fstat)

    with pytest.raises(AuditLogError, match="cannot inspect session log"):
        log.append(_event("event-1"))

    monkeypatch.setattr(os, "fstat", real_fstat)
    with pytest.raises(OSError, match="Bad file descriptor"):
        os.fstat(descriptors[0])


def _seed_legacy_database(tmp_path: Path, records: list[dict[str, object]]) -> tuple[Path, Path]:
    """Write what the previous writer left behind: every event body in SQLite plus the mirror."""
    digest = hashlib.sha256(b"session-1").hexdigest()
    mirror = tmp_path / f"{digest}.jsonl"
    database_path = tmp_path / f"{digest}.sqlite3"
    mirror.write_bytes(
        b"".join(
            (json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n").encode()
            for record in records
        )
    )
    with sqlite3.connect(database_path) as database:
        database.execute("PRAGMA journal_mode=WAL")
        database.execute(
            "CREATE TABLE operations (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "status TEXT NOT NULL CHECK(status IN ('pending', 'complete')))"
        )
        database.execute(
            "CREATE TABLE records (seq INTEGER PRIMARY KEY, operation_id INTEGER NOT NULL, "
            "event_json TEXT NOT NULL, FOREIGN KEY(operation_id) REFERENCES operations(id))"
        )
        for record in records:
            cursor = database.execute("INSERT INTO operations(status) VALUES ('complete')")
            database.execute(
                "INSERT INTO records(seq, operation_id, event_json) VALUES (?, ?, ?)",
                (
                    record["seq"],
                    cursor.lastrowid,
                    json.dumps(record["event"], separators=(",", ":"), sort_keys=True),
                ),
            )
        database.commit()
    return mirror, database_path


def _record_rows(database_path: Path) -> list[tuple[object, ...]]:
    with sqlite3.connect(database_path) as database:
        return database.execute(
            "SELECT seq, operation_id, digest, length, line FROM records ORDER BY seq"
        ).fetchall()


def test_oversized_legacy_database_body_is_not_materialized_for_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mirror, database_path = _seed_legacy_database(
        tmp_path, [{"seq": 1, "event": _event("event-1")}]
    )
    with sqlite3.connect(database_path) as database:
        database.execute(
            "UPDATE records SET event_json = CAST(zeroblob(?) AS TEXT) WHERE seq = 1",
            (MAX_AUDIT_RECORD_BYTES + 1,),
        )
        database.commit()
    read_sizes = _guard_mirror_reads(monkeypatch, mirror)

    with pytest.raises(AuditLogError, match="invalid legacy audit record metadata"):
        SessionAuditLog(tmp_path, "session-1")

    assert read_sizes == []


def test_exact_maximum_legacy_record_still_migrates(tmp_path: Path) -> None:
    record = {"seq": 1, "event": _exact_size_event(MAX_AUDIT_RECORD_BYTES)}
    mirror, _database_path = _seed_legacy_database(tmp_path, [record])

    reopened = SessionAuditLog(tmp_path, "session-1")

    assert reopened.replay() == [record]
    assert mirror.stat().st_size == MAX_AUDIT_RECORD_BYTES


def test_database_stores_digests_and_retains_only_the_latest_operation_lines(
    tmp_path: Path,
) -> None:
    log = SessionAuditLog(tmp_path, "session-1")
    log.append_batch([_event("event-1", "membership"), _event("event-2", "state")])
    log.append(_event("event-3", "telemetry"))
    lines = log.path.read_bytes().splitlines(keepends=True)

    rows = _record_rows(log.database_path)

    assert [(seq, digest, length) for seq, _, digest, length, _ in rows] == [
        (index, hashlib.sha256(line).digest(), len(line)) for index, line in enumerate(lines, 1)
    ]
    assert [line for *_, line in rows] == [None, None, lines[2]]
    assert [operation for _, operation, *_ in rows] == [1, 1, 2]
    with sqlite3.connect(log.database_path) as database:
        columns = {row[1] for row in database.execute("PRAGMA table_info(records)")}
    assert "event_json" not in columns
    assert SessionAuditLog(tmp_path, "session-1").replay() == log.replay()


@pytest.mark.parametrize(
    "corrupt_length",
    [-1, 0, MAX_AUDIT_RECORD_BYTES + 1, (1 << 63) - 1, 1.5, "not-an-integer"],
    ids=["negative", "zero", "over-record", "signed-long-max", "float", "text"],
)
def test_corrupt_record_length_fails_before_any_sized_mirror_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    corrupt_length: object,
) -> None:
    log = SessionAuditLog(tmp_path, "session-1")
    log.append(_event("event-1"))
    with sqlite3.connect(log.database_path) as database:
        database.execute("UPDATE records SET length = ? WHERE seq = 1", (corrupt_length,))
        database.commit()
    read_sizes = _guard_mirror_reads(monkeypatch, log.path)

    with pytest.raises(AuditLogError, match="invalid audit record metadata"):
        SessionAuditLog(tmp_path, "session-1")

    assert read_sizes == []


def test_oversized_retained_blob_is_not_materialized_or_used_for_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = SessionAuditLog(tmp_path, "session-1")
    log.append(_event("event-1"))
    with sqlite3.connect(log.database_path) as database:
        database.execute(
            "UPDATE records SET line = zeroblob(?) WHERE seq = 1",
            (MAX_AUDIT_RECORD_BYTES + 1,),
        )
        database.commit()
    read_sizes = _guard_mirror_reads(monkeypatch, log.path)

    with pytest.raises(AuditLogError, match="invalid audit record metadata"):
        SessionAuditLog(tmp_path, "session-1")

    assert read_sizes == []


@pytest.mark.parametrize("column", ["digest", "length"], ids=["digest", "length"])
def test_oversized_blob_metadata_is_not_materialized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, column: str
) -> None:
    log = SessionAuditLog(tmp_path, "session-1")
    log.append(_event("event-1"))
    with sqlite3.connect(log.database_path) as database:
        database.execute(
            f"UPDATE records SET {column} = zeroblob(?) WHERE seq = 1",
            (MAX_AUDIT_RECORD_BYTES + 1,),
        )
        database.commit()
    read_sizes = _guard_mirror_reads(monkeypatch, log.path)

    with pytest.raises(AuditLogError, match="invalid audit record metadata"):
        SessionAuditLog(tmp_path, "session-1")

    assert read_sizes == []


def test_oversized_operation_status_is_not_materialized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = SessionAuditLog(tmp_path, "session-1")
    log.append(_event("event-1"))
    with sqlite3.connect(log.database_path) as database:
        database.execute("PRAGMA ignore_check_constraints = ON")
        database.execute(
            "UPDATE operations SET status = CAST(zeroblob(?) AS TEXT) WHERE id = 1",
            (MAX_AUDIT_RECORD_BYTES + 1,),
        )
        database.commit()
    read_sizes = _guard_mirror_reads(monkeypatch, log.path)

    with pytest.raises(AuditLogError, match="invalid audit operation"):
        SessionAuditLog(tmp_path, "session-1")

    assert read_sizes == []


@pytest.mark.parametrize("operation_id", [-1, 0, (1 << 63) - 1])
def test_invalid_or_unbound_record_operation_ids_fail_closed(
    tmp_path: Path, operation_id: int
) -> None:
    log = SessionAuditLog(tmp_path, "session-1")
    log.append(_event("event-1"))
    with sqlite3.connect(log.database_path) as database:
        database.execute("UPDATE records SET operation_id = ? WHERE seq = 1", (operation_id,))
        database.commit()

    with pytest.raises(AuditLogError, match="invalid audit operation"):
        SessionAuditLog(tmp_path, "session-1")


@pytest.mark.parametrize("sequence", [-1, 0, (1 << 63) - 1])
def test_invalid_or_noncontiguous_signed_64_bit_sequences_fail_closed(
    tmp_path: Path, sequence: int
) -> None:
    log = SessionAuditLog(tmp_path, "session-1")
    log.append(_event("event-1"))
    with sqlite3.connect(log.database_path) as database:
        database.execute("UPDATE records SET seq = ? WHERE seq = 1", (sequence,))
        database.commit()

    with pytest.raises(AuditLogError, match="non-contiguous audit database"):
        SessionAuditLog(tmp_path, "session-1")


def test_pending_operation_lookup_has_a_partial_status_index(tmp_path: Path) -> None:
    log = SessionAuditLog(tmp_path, "session-1")
    log.append(_event("event-1"))

    with sqlite3.connect(log.database_path) as database:
        indexes = {row[1] for row in database.execute("PRAGMA index_list(operations)")}

    assert "operations_pending" in indexes


def test_exhausted_signed_64_bit_sequence_is_an_audit_error_before_operation(
    tmp_path: Path,
) -> None:
    log = SessionAuditLog(tmp_path, "session-1")
    log._next_sequence = 1 << 63

    with pytest.raises(AuditLogError, match="sequence exceeds SQLite"):
        log.append(_event("event-1"))

    assert not log.database_path.exists()
    assert not log.path.exists()


@pytest.mark.parametrize("operation_id", [True, 0, -1, 1 << 63])
def test_external_operation_id_is_bounded_before_sqlite_binding(
    tmp_path: Path, operation_id: object
) -> None:
    log = SessionAuditLog(tmp_path, "session-1")

    with pytest.raises(AuditLogError, match="positive signed 64-bit"):
        log.append_batch([_event("event-1")], operation_id=operation_id)  # type: ignore[arg-type]
    with pytest.raises(AuditLogError, match="positive signed 64-bit"):
        log.abandon_operation(operation_id)  # type: ignore[arg-type]

    assert not log.database_path.exists()
    assert not log.path.exists()
    assert log.append(_event("valid"))["seq"] == 1


def test_database_growth_is_bounded_by_record_metadata(tmp_path: Path) -> None:
    log = SessionAuditLog(tmp_path, "session-1")
    for index in range(200):
        event = _event(f"event-{index}")
        event["payload"] = "x" * 4_000
        log.append(event)
    mirror_size = log.path.stat().st_size

    with sqlite3.connect(log.database_path) as database:
        database.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        retained = database.execute("SELECT COUNT(*) FROM records WHERE line IS NOT NULL")
        retained_count = retained.fetchone()[0]
        page_size = database.execute("PRAGMA page_size").fetchone()[0]
        page_count = database.execute("PRAGMA page_count").fetchone()[0]

    assert mirror_size > 800_000
    assert retained_count == 1
    assert page_count * page_size < mirror_size / 4


def test_torn_mirror_is_rebuilt_only_from_retained_operation_lines(tmp_path: Path) -> None:
    log = SessionAuditLog(tmp_path, "session-1")
    log.append(_event("event-1"))
    log.append(_event("event-2"))
    full = log.path.read_bytes()
    first_end = full.index(b"\n") + 1

    log.path.write_bytes(full[:-5])
    assert [record["seq"] for record in SessionAuditLog(tmp_path, "session-1").replay()] == [1, 2]
    assert log.path.read_bytes() == full

    log.path.write_bytes(full[: first_end - 3])
    with pytest.raises(AuditLogError, match="mirror differs from committed audit"):
        SessionAuditLog(tmp_path, "session-1")
    assert log.path.read_bytes() == full[: first_end - 3]

    log.path.unlink()
    with pytest.raises(AuditLogError, match="audit mirror is missing"):
        SessionAuditLog(tmp_path, "session-1")


def test_corrupt_retained_line_cannot_repair_or_extend_the_mirror(tmp_path: Path) -> None:
    log = SessionAuditLog(tmp_path, "session-1")
    log.append(_event("event-1"))
    mirror = log.path.read_bytes()
    with sqlite3.connect(log.database_path) as database:
        retained = database.execute("SELECT line FROM records WHERE seq = 1").fetchone()[0]
        assert isinstance(retained, bytes)
        database.execute(
            "UPDATE records SET line = ? WHERE seq = 1",
            (retained.replace(b"event-1", b"event-x"),),
        )
        database.commit()

    with pytest.raises(AuditLogError, match="invalid retained audit line"):
        SessionAuditLog(tmp_path, "session-1")

    assert log.path.read_bytes() == mirror


@pytest.mark.parametrize(
    "corruption",
    ["malformed", "wrong_sequence", "wrong_session", "missing_event_id", "noncanonical"],
)
def test_semantically_invalid_retained_line_cannot_be_used_for_recovery(
    tmp_path: Path, corruption: str
) -> None:
    log = SessionAuditLog(tmp_path, "session-1")
    log.append(_event("event-1"))
    record = {"seq": 1, "event": _event("event-1")}
    if corruption == "malformed":
        retained = b"{not-json}\n"
    else:
        if corruption == "wrong_sequence":
            record["seq"] = 2
        elif corruption == "wrong_session":
            record["event"]["session"] = "other-session"
        elif corruption == "missing_event_id":
            record["event"].pop("event_id")
        if corruption == "noncanonical":
            retained = (json.dumps(record, indent=2, sort_keys=False) + "\n").encode()
        else:
            retained = SessionAuditLog._encode_record(record)
    with sqlite3.connect(log.database_path) as database:
        database.execute(
            "UPDATE records SET digest = ?, length = ?, line = ? WHERE seq = 1",
            (hashlib.sha256(retained).digest(), len(retained), retained),
        )
        database.commit()
    log.path.unlink()

    with pytest.raises(AuditLogError, match="retained audit line"):
        SessionAuditLog(tmp_path, "session-1")

    assert not log.path.exists()


@pytest.mark.parametrize("mirror_state", ["complete", "torn", "missing"])
def test_legacy_event_body_database_is_migrated_to_digest_rows(
    tmp_path: Path, mirror_state: str
) -> None:
    records: list[dict[str, object]] = [
        {"seq": index, "event": _event(f"event-{index}")} for index in (1, 2, 3)
    ]
    mirror, database_path = _seed_legacy_database(tmp_path, records)
    expected = mirror.read_bytes()
    if mirror_state == "torn":
        mirror.write_bytes(expected[:-7])
    elif mirror_state == "missing":
        mirror.unlink()
    lines = expected.splitlines(keepends=True)

    reopened = SessionAuditLog(tmp_path, "session-1")

    assert reopened.replay() == records
    assert mirror.read_bytes() == expected
    rows = _record_rows(database_path)
    assert [(seq, digest, length) for seq, _, digest, length, _ in rows] == [
        (index, hashlib.sha256(line).digest(), len(line)) for index, line in enumerate(lines, 1)
    ]
    assert [line for *_, line in rows] == [None, None, lines[2]]
    with sqlite3.connect(database_path) as database:
        columns = {row[1] for row in database.execute("PRAGMA table_info(records)")}
        operations = database.execute("SELECT COUNT(*) FROM operations").fetchone()[0]
    assert "event_json" not in columns
    assert operations == 3
    fourth = SessionAuditLog(tmp_path, "session-1").append(_event("event-4"))
    assert fourth["seq"] == 4
    assert [line is None for *_, line in _record_rows(database_path)] == [True, True, True, False]


def test_legacy_migration_keeps_a_pending_operation_fenced(tmp_path: Path) -> None:
    mirror, database_path = _seed_legacy_database(
        tmp_path, [{"seq": 1, "event": _event("event-1")}]
    )
    with sqlite3.connect(database_path) as database:
        database.execute("INSERT INTO operations(status) VALUES ('pending')")
        database.commit()
    mirror.write_bytes(mirror.read_bytes() + b'{"seq":2,"event":{"partial')

    reopened = SessionAuditLog(tmp_path, "session-1")

    assert mirror.read_bytes().count(b"\n") == 1
    with pytest.raises(AuditLogError, match="incomplete operation"):
        reopened.replay()
    with pytest.raises(AuditLogError, match="incomplete operation"):
        SessionAuditLog(tmp_path, "session-1").replay()
