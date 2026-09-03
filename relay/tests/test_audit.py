from __future__ import annotations

import json
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
