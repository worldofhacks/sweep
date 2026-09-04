from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

import pytest

from relay.audit import AuditLogError, SessionAuditLog


def _event(event_id: str, event_type: str = "state") -> dict[str, object]:
    return {
        "v": 1,
        "t": 100,
        "type": event_type,
        "event_id": event_id,
        "session": "session-1",
    }


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
