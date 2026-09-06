from __future__ import annotations

import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace
from typing import cast

import pytest

import relay.audit as audit_module
from planner.models import CommandOperation
from relay.audit import AuditLogError, SessionAuditLog
from relay.auth import Principal
from relay.capabilities import C1_CAPABILITY_PROFILE, CapabilityProfile
from relay.contracts import LifecycleStatus
from relay.intent_v1 import IntentV1
from relay.session import (
    CapabilityBoundIntentSink,
    IntentSink,
    IntentSinkResult,
    RelayLimits,
    RelaySession,
)
from relay.tests.conftest import (
    ADAPTER_KEY,
    SESSION,
    EventIds,
    MutableClock,
    acknowledgement_payload,
    intent_payload,
    membership_payload,
    telemetry_payload,
)


def _join(
    session: RelaySession, principal: Principal, event_id: str = "join-1"
) -> list[dict[str, object]]:
    return session.process_membership(
        membership_payload(action="join", event_id=event_id), principal
    )


def _new_session(
    tmp_path: Path,
    clock: MutableClock,
    event_ids: EventIds,
    **kwargs: object,
) -> RelaySession:
    sink = kwargs.get("intent_sink")
    if sink is not None and not hasattr(sink, "capability_profile"):
        profile = cast(CapabilityProfile, kwargs.get("capability_profile", C1_CAPABILITY_PROFILE))
        kwargs["intent_sink"] = CapabilityBoundIntentSink(cast(IntentSink, sink), profile)
    return RelaySession(
        session_id=SESSION,
        audit_log=SessionAuditLog(tmp_path, SESSION),
        limits=RelayLimits(5_000, 5_000, 1_000, 1_000),
        clock=clock,
        event_ids=event_ids,
        **kwargs,
    )


@pytest.mark.parametrize("cancelled_result_first", [False, True])
def test_safety_completion_survives_undelivered_group_member_cancellation(
    relay_session: RelaySession,
    console_principal: Principal,
    cancelled_result_first: bool,
) -> None:
    motion = intent_payload(intent_id="motion")
    motion.update(name="translate", args={"dx": 1.0, "dy": 0.0})
    stop = intent_payload(intent_id="stop")
    stop.update(name="estop", selection=[])
    assert relay_session.process_intent(motion, console_principal)[0]["status"] == "accepted"
    assert relay_session.process_intent(stop, console_principal)[0]["status"] == "accepted"
    relay_session.mark_pending_intent_delivered("stop")
    calls: list[str] = []

    def dispatch() -> dict[str, IntentSinkResult]:
        calls.append("estop")
        cancellation = relay_session.fail_pending_intent(
            "motion", reason="acceptance_delivery_failed", detail="socket failed"
        )
        assert cancellation[0]["reason"] == "acceptance_delivery_failed"
        outcomes = [
            (
                "stop",
                IntentSinkResult(
                    status=LifecycleStatus.COMPLETED,
                    source="autonomy",
                    result={},
                    estop_update=True,
                ),
            ),
            (
                "motion",
                IntentSinkResult(
                    status=LifecycleStatus.INVALIDATED,
                    source="autonomy",
                    result={},
                    reason="superseded",
                ),
            ),
        ]
        return dict(reversed(outcomes) if cancelled_result_first else outcomes)

    relay_session.execute_coordinated_group(("stop",), dispatch)
    outcome = relay_session.execute_pending_intent("stop")
    assert any(event.get("status") == "completed" for event in outcome)
    assert relay_session.execute_pending_intent("motion") == []
    assert relay_session.execute_pending_intent("stop") == []
    assert calls == ["estop"]
    assert relay_session.current_state()["estop"] is True
    events = [record["event"] for record in relay_session.replay()["events"]]
    assert any(
        event.get("intent_id") == "stop" and event.get("status") == "completed" for event in events
    )
    assert any(
        event.get("intent_id") == "motion" and event.get("reason") == "acceptance_delivery_failed"
        for event in events
    )
    assert not any(
        event.get("intent_id") == "motion" and event.get("status") == "invalidated"
        for event in events
    )
    reopened = SessionAuditLog(relay_session.audit_log.root, SESSION)
    assert reopened.replay() == relay_session.audit_log.replay()


def test_missing_downstream_refuses_instead_of_false_acknowledgement(
    tmp_path: Path,
    clock: MutableClock,
    event_ids: EventIds,
    console_principal: Principal,
) -> None:
    session = _new_session(tmp_path, clock, event_ids)

    result = session.process_intent(intent_payload(), console_principal)

    assert result[0]["type"] == "refusal"
    assert result[0]["reason"] == "downstream_unavailable"


def test_accepted_and_refused_intents_are_ordered_in_replay(
    relay_session: RelaySession, console_principal: Principal
) -> None:
    accepted = relay_session.process_intent(intent_payload(), console_principal)
    invalid = intent_payload(intent_id="intent-2")
    invalid["args"] = {"not": "empty"}
    refused = relay_session.process_intent(invalid, console_principal)

    replay = relay_session.replay()
    types = [record["event"]["type"] for record in replay["events"]]
    outcomes = [
        record["event"]["outcome"]
        for record in replay["events"]
        if record["event"]["type"] == "intent_record"
    ]

    assert accepted[0]["type"] == "acknowledgement"
    assert accepted[0]["status"] == "accepted"
    assert refused[0]["type"] == "refusal"
    assert refused[0]["reason"] == "invalid_payload"
    assert set(refused[0]) == {
        "v",
        "t",
        "type",
        "event_id",
        "session",
        "intent_id",
        "command_id",
        "status",
        "source",
        "drone_id",
        "connection_epoch",
        "roster_version",
        "reason",
        "detail",
    }
    assert types == ["intent_record", "acknowledgement", "intent_record", "refusal"]
    assert outcomes == ["accepted", "refused"]
    assert [record["seq"] for record in replay["events"]] == [1, 2, 3, 4]

    empty_increment = relay_session.replay(after_sequence=4)
    assert empty_increment["events"] == []
    assert empty_increment["last_sequence"] == 4


def test_replay_reports_durable_sequence_after_append_close_failure(
    tmp_path: Path,
    clock: MutableClock,
    event_ids: EventIds,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _new_session(tmp_path, clock, event_ids)
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
        session.update_control_projection(selection=())

    with pytest.raises(AuditLogError, match="session is unusable"):
        session.replay()

    monkeypatch.setattr(os, "close", real_close)
    replay = _new_session(tmp_path, clock, event_ids).replay()
    assert [record["seq"] for record in replay["events"]] == [1]
    assert replay["last_sequence"] == 1


def test_membership_operation_reopens_as_a_complete_batch_after_close_failure(
    tmp_path: Path,
    clock: MutableClock,
    event_ids: EventIds,
    adapter_principal: Principal,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _new_session(tmp_path, clock, event_ids)
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
        _join(session, adapter_principal)

    monkeypatch.setattr(os, "close", real_close)
    records = SessionAuditLog(tmp_path, SESSION).replay()
    assert [record["event"]["type"] for record in records] == ["membership", "state"]


def test_reopen_rebuilds_membership_batch_after_partial_mirror_write(
    tmp_path: Path,
    clock: MutableClock,
    event_ids: EventIds,
    adapter_principal: Principal,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _new_session(tmp_path, clock, event_ids)
    real_write = os.write
    real_truncate = os.ftruncate
    writes = 0

    def write_first_record_then_fail(descriptor: int, data: bytes | memoryview) -> int:
        nonlocal writes
        writes += 1
        if writes == 1:
            encoded = bytes(data)
            first_record_end = encoded.index(b"\n") + 1
            return real_write(descriptor, encoded[:first_record_end])
        raise OSError("injected write failure")

    def fail_rollback(_descriptor: int, _length: int) -> None:
        raise OSError("injected rollback failure")

    monkeypatch.setattr(os, "write", write_first_record_then_fail)
    monkeypatch.setattr(os, "ftruncate", fail_rollback)
    with pytest.raises(AuditLogError, match="cannot append session log"):
        _join(session, adapter_principal)

    assert len(session.audit_log.path.read_text(encoding="utf-8").splitlines()) == 1
    with pytest.raises(AuditLogError, match="session is unusable"):
        session.replay()

    monkeypatch.setattr(os, "write", real_write)
    monkeypatch.setattr(os, "ftruncate", real_truncate)
    reopened = SessionAuditLog(tmp_path, SESSION)
    assert [record["event"]["type"] for record in reopened.replay()] == [
        "membership",
        "state",
    ]
    assert not reopened.pending_path.exists()


def test_sink_projection_close_failure_leaves_dispatch_durably_incomplete(
    tmp_path: Path,
    clock: MutableClock,
    event_ids: EventIds,
    console_principal: Principal,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    holder: dict[str, RelaySession] = {}

    def sink(_intent: object, _state: object) -> None:
        holder["session"].update_control_projection(selection=())

    session = _new_session(tmp_path, clock, event_ids, intent_sink=sink)
    holder["session"] = session
    session.process_intent(intent_payload(), console_principal)
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
    with pytest.raises(AuditLogError, match="session is unusable"):
        session.execute_pending_intent("intent-1")

    monkeypatch.setattr(os, "close", real_close)
    with pytest.raises(AuditLogError, match="incomplete operation"):
        SessionAuditLog(tmp_path, SESSION).replay()
    with pytest.raises(AuditLogError, match="session is unusable"):
        session.current_state()


def test_intent_operation_is_durably_pending_before_sink_dispatch(
    tmp_path: Path,
    clock: MutableClock,
    event_ids: EventIds,
    console_principal: Principal,
) -> None:
    class AbruptStop(BaseException):
        pass

    observed: dict[str, object] = {}

    def stop_after_dispatch(_intent: object, _state: object) -> None:
        log = session.audit_log
        with sqlite3.connect(log.database_path) as database:
            observed["journal_mode"] = database.execute("PRAGMA journal_mode").fetchone()[0]
            observed["pending"] = database.execute(
                "SELECT COUNT(*) FROM operations WHERE status = 'pending'"
            ).fetchone()[0]
        raise AbruptStop

    session = _new_session(tmp_path, clock, event_ids, intent_sink=stop_after_dispatch)

    session.process_intent(intent_payload(), console_principal)
    with pytest.raises(AbruptStop):
        session.execute_pending_intent("intent-1")

    assert observed == {"journal_mode": "wal", "pending": 1}
    assert not session.audit_log.pending_path.exists()
    reopened = SessionAuditLog(tmp_path, SESSION)
    assert reopened.had_persisted_log is True
    with pytest.raises(AuditLogError, match="incomplete operation"):
        reopened.replay()


def test_reopen_rebuilds_committed_state_after_mirror_fsync_failure(
    tmp_path: Path,
    clock: MutableClock,
    event_ids: EventIds,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _new_session(tmp_path, clock, event_ids)
    real_fsync = os.fsync
    real_open = os.open
    real_ftruncate = os.ftruncate
    mirror_descriptors: set[int] = set()

    def track_mirror_open(path: object, flags: int, *args: object) -> int:
        descriptor = real_open(path, flags, *args)
        if flags & os.O_APPEND:
            mirror_descriptors.add(descriptor)
        return descriptor

    def failed_fsync(descriptor: int) -> None:
        if descriptor in mirror_descriptors:
            raise OSError("injected fsync failure")
        real_fsync(descriptor)

    def failed_truncate(_descriptor: int, _length: int) -> None:
        raise OSError("injected rollback failure")

    monkeypatch.setattr(os, "open", track_mirror_open)
    monkeypatch.setattr(os, "fsync", failed_fsync)
    monkeypatch.setattr(os, "ftruncate", failed_truncate)

    with pytest.raises(AuditLogError, match="cannot append session log"):
        session.update_control_projection(selection=())
    assert session.audit_log.path.read_bytes().endswith(b"\n")
    with pytest.raises(AuditLogError, match="session is unusable"):
        session.replay()

    monkeypatch.setattr(os, "fsync", real_fsync)
    monkeypatch.setattr(os, "ftruncate", real_ftruncate)
    reopened = SessionAuditLog(tmp_path, SESSION)
    assert [record["seq"] for record in reopened.replay()] == [1]


@pytest.mark.parametrize("field", ["accepted_plan", "pending"])
@pytest.mark.parametrize("bad_value", [object(), float("nan")], ids=["object", "nan"])
def test_projection_copy_failure_cannot_publish_unaudited_selection(
    tmp_path: Path,
    clock: MutableClock,
    event_ids: EventIds,
    field: str,
    bad_value: object,
) -> None:
    session = _new_session(tmp_path, clock, event_ids)
    session.update_control_projection(selection=(1,))
    committed = session.audit_log.path.read_bytes()

    with pytest.raises((TypeError, ValueError)):
        session.update_control_projection(selection=(2,), **{field: {"bad": bad_value}})

    assert session.audit_log.path.read_bytes() == committed
    with pytest.raises(AuditLogError, match="session is unusable"):
        session.update_control_projection(estop=True)
    with pytest.raises(AuditLogError, match="session is unusable"):
        session.current_state()
    with pytest.raises(AuditLogError, match="session is unusable"):
        session.replay()
    reopened = SessionAuditLog(tmp_path, SESSION)
    with pytest.raises(AuditLogError, match="incomplete operation"):
        reopened.replay()


def test_sink_cannot_catch_projection_failure_and_publish_partial_state(
    tmp_path: Path,
    clock: MutableClock,
    event_ids: EventIds,
    console_principal: Principal,
) -> None:
    def sink(_intent: object, _state: object) -> None:
        try:
            session.update_control_projection(selection=(2,), pending={"bad": object()})
        except TypeError:
            pass

    session = _new_session(tmp_path, clock, event_ids, intent_sink=sink)
    session.update_control_projection(selection=(1,))
    accepted = session.process_intent(intent_payload(), console_principal)
    assert accepted[0]["status"] == "accepted"
    committed = session.audit_log.path.read_bytes()

    with pytest.raises(AuditLogError, match="session is unusable"):
        session.execute_pending_intent("intent-1")

    assert session.audit_log.path.read_bytes() == committed
    with pytest.raises(AuditLogError, match="session is unusable"):
        session.current_state()
    with pytest.raises(AuditLogError, match="incomplete operation"):
        SessionAuditLog(tmp_path, SESSION).replay()


def test_replay_records_and_cursor_share_one_snapshot_during_append(
    tmp_path: Path,
    clock: MutableClock,
    event_ids: EventIds,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _new_session(tmp_path, clock, event_ids)
    session.update_control_projection(selection=())
    replay_reading = Event()
    resume_replay = Event()
    append_started = Event()
    real_validate = audit_module._validate_record

    def pause_during_validation(
        record: object, expected: int, session_id: str, line_number: int
    ) -> None:
        real_validate(record, expected, session_id, line_number)
        replay_reading.set()
        assert resume_replay.wait(timeout=2)

    def append_state() -> None:
        append_started.set()
        session.update_control_projection(selection=())

    monkeypatch.setattr(audit_module, "_validate_record", pause_during_validation)

    with ThreadPoolExecutor(max_workers=2) as executor:
        replay_future = executor.submit(session.replay, after_sequence=1)
        assert replay_reading.wait(timeout=2)
        append_future = executor.submit(append_state)
        assert append_started.wait(timeout=2)
        resume_replay.set()
        replay = replay_future.result(timeout=2)
        append_future.result(timeout=2)

    assert replay["events"] == []
    assert replay["last_sequence"] == 1
    next_replay = session.replay(after_sequence=1)
    assert [record["seq"] for record in next_replay["events"]] == [2]
    assert next_replay["last_sequence"] == 2


def test_session_fails_closed_after_rolled_back_audit_append(
    tmp_path: Path,
    clock: MutableClock,
    event_ids: EventIds,
    adapter_principal: Principal,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _new_session(tmp_path, clock, event_ids)
    real_fsync = os.fsync
    real_open = os.open
    mirror_descriptors: set[int] = set()

    def track_mirror_open(path: object, flags: int, *args: object) -> int:
        descriptor = real_open(path, flags, *args)
        if flags & os.O_APPEND:
            mirror_descriptors.add(descriptor)
        return descriptor

    def fail_once(descriptor: int) -> None:
        if descriptor in mirror_descriptors:
            raise OSError("injected fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(os, "open", track_mirror_open)
    monkeypatch.setattr(os, "fsync", fail_once)

    with pytest.raises(AuditLogError, match="cannot append session log"):
        _join(session, adapter_principal)

    with pytest.raises(AuditLogError, match="incomplete operation"):
        session.audit_log.replay()
    with pytest.raises(AuditLogError, match="incomplete operation"):
        _ = session.audit_log.last_sequence
    with pytest.raises(AuditLogError, match="session is unusable"):
        session.current_state()
    with pytest.raises(AuditLogError, match="session is unusable"):
        session.replay()
    with pytest.raises(AuditLogError, match="session is unusable"):
        session.handle_adapter_disconnect(drone_id=1, connection_epoch=1)


def test_complete_durable_batch_remains_replayable_after_close_failure(
    relay_session: RelaySession,
    adapter_principal: Principal,
    clock: MutableClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _join(relay_session, adapter_principal)
    relay_session.process_telemetry(
        telemetry_payload(event_id="telemetry-before-stale"), adapter_principal
    )
    relay_session.process_membership(
        membership_payload(action="readiness", event_id="readiness-1"), adapter_principal
    )
    clock.advance(1_001)
    relay_session.periodic_events()
    real_close = os.close

    def close_then_fail(descriptor: int) -> None:
        real_close(descriptor)
        raise OSError("injected close failure")

    monkeypatch.setattr(os, "close", close_then_fail)

    with pytest.raises(AuditLogError, match="cannot close session log"):
        relay_session.process_telemetry(
            telemetry_payload(event_id="telemetry-recovery", timestamp=clock.value),
            adapter_principal,
        )

    monkeypatch.setattr(os, "close", real_close)
    reopened = SessionAuditLog(relay_session.audit_log.root, SESSION)
    replay = reopened.replay()
    assert [record["seq"] for record in replay] == list(range(1, len(replay) + 1))
    durable_events = [record["event"] for record in replay[-2:]]
    assert [event["type"] for event in durable_events] == ["telemetry", "membership"]
    assert durable_events[0]["event_id"] == "telemetry-recovery"
    with pytest.raises(AuditLogError, match="session is unusable"):
        relay_session.current_state()
    with pytest.raises(AuditLogError, match="session is unusable"):
        relay_session.replay()


def test_authenticated_source_cannot_impersonate_another_registered_source(
    relay_session: RelaySession, console_principal: Principal, keyboard_principal: Principal
) -> None:
    keyboard_intent = intent_payload(source="keyboard")
    keyboard_intent.update(name="estop", selection=[])
    keyboard_stop = intent_payload(source="keyboard", intent_id="intent-2")
    keyboard_stop.update(name="estop", selection=[])

    refused = relay_session.process_intent(keyboard_intent, console_principal)
    accepted = relay_session.process_intent(keyboard_stop, keyboard_principal)

    assert refused[0]["reason"] == "source_mismatch"
    assert accepted[0]["status"] == "accepted"


def test_webcam_source_is_gated_like_console_and_keyboard(
    relay_session: RelaySession,
    console_principal: Principal,
    webcam_principal: Principal,
    adapter_principal: Principal,
) -> None:
    accepted = relay_session.process_frame(intent_payload(source="webcam"), webcam_principal)
    impersonated = relay_session.process_frame(
        intent_payload(source="webcam", intent_id="intent-2"), console_principal
    )
    reversed_binding = relay_session.process_frame(
        intent_payload(source="console", intent_id="intent-3"), webcam_principal
    )
    adapter_authored = relay_session.process_frame(
        intent_payload(source="webcam", intent_id="intent-4"), adapter_principal
    )

    assert accepted[0]["status"] == "accepted"
    assert accepted[0]["intent_id"] == "intent-1"
    assert impersonated[0]["reason"] == "source_mismatch"
    assert reversed_binding[0]["reason"] == "source_mismatch"
    assert adapter_authored[0]["reason"] == "frame_not_allowed"


def test_source_allowlist_refuses_names_a_source_never_emits(
    relay_session: RelaySession,
    keyboard_principal: Principal,
    webcam_principal: Principal,
) -> None:
    webcam_takeoff = intent_payload(source="webcam")
    webcam_takeoff.update(name="takeoff", confirm=True)
    keyboard_hold = intent_payload(source="keyboard", intent_id="intent-2")

    refused_takeoff = relay_session.process_frame(webcam_takeoff, webcam_principal)
    refused_hold = relay_session.process_frame(keyboard_hold, keyboard_principal)
    accepted_hold = relay_session.process_frame(
        intent_payload(source="webcam", intent_id="intent-3"), webcam_principal
    )

    assert refused_takeoff[0]["type"] == "refusal"
    assert refused_takeoff[0]["reason"] == "source_not_allowed"
    assert refused_takeoff[0]["detail"] == "takeoff is not allowed from source webcam"
    assert refused_hold[0]["reason"] == "source_not_allowed"
    assert refused_hold[0]["detail"] == "hold is not allowed from source keyboard"
    assert accepted_hold[0]["status"] == "accepted"
    assert relay_session.current_state()["estop"] is False


def test_intent_timestamp_and_id_replay_checks(
    relay_session: RelaySession, console_principal: Principal, clock: MutableClock
) -> None:
    stale = relay_session.process_intent(
        intent_payload(timestamp=clock.value - 5_001), console_principal
    )
    future = relay_session.process_intent(
        intent_payload(timestamp=clock.value + 1_001, intent_id="intent-future"),
        console_principal,
    )
    accepted = relay_session.process_intent(
        intent_payload(intent_id="intent-fresh"), console_principal
    )
    duplicate = relay_session.process_intent(
        intent_payload(intent_id="intent-fresh"), console_principal
    )

    assert stale[0]["reason"] == "stale_timestamp"
    assert future[0]["reason"] == "future_timestamp"
    assert accepted[0]["status"] == "accepted"
    assert duplicate[0]["reason"] == "duplicate_intent"


def test_retry_requires_a_same_session_terminal_failure(
    relay_session: RelaySession, console_principal: Principal
) -> None:
    relay_session.process_intent(intent_payload(), console_principal)
    premature = relay_session.process_intent(
        intent_payload(intent_id="retry-early", retry_of="intent-1"), console_principal
    )
    relay_session.record_lifecycle(
        intent_id="intent-1",
        status=LifecycleStatus.FAILED,
        source="arbiter",
        reason="adapter_timeout",
    )
    retry = relay_session.process_intent(
        intent_payload(intent_id="retry-good", retry_of="intent-1"), console_principal
    )

    assert premature[0]["reason"] == "invalid_retry"
    assert retry[0]["status"] == "accepted"


def test_membership_signature_binding_and_order_are_enforced(
    relay_session: RelaySession, adapter_principal: Principal
) -> None:
    wrong_key = membership_payload(
        action="join",
        event_id="join-wrong-key",
        key=b"another-adapter-key-that-is-32-bytes",
    )
    bad_signature = relay_session.process_membership(wrong_key, adapter_principal)
    joined = _join(relay_session, adapter_principal)
    replayed = _join(relay_session, adapter_principal)
    other_identity = Principal(source="adapter", drone_id=2, signing_key=ADAPTER_KEY)
    mismatched = relay_session.process_membership(
        membership_payload(action="join", event_id="join-2", drone_id=1), other_identity
    )

    assert bad_signature[0]["reason"] == "invalid_signature"
    assert [event["type"] for event in joined] == ["membership", "state"]
    assert replayed[0]["reason"] == "replayed_event"
    assert mismatched[0]["reason"] == "drone_identity_mismatch"
    assert not _contains_key(relay_session.replay(), "signature")


def test_membership_then_state_is_atomic_and_shape_compatible(
    relay_session: RelaySession, adapter_principal: Principal
) -> None:
    events = _join(relay_session, adapter_principal)

    membership, state = events
    assert membership["type"] == "membership"
    assert membership["membership"] == "registered"
    assert membership["connection_epoch"] == 1
    assert state["type"] == "state"
    assert state["roster_version"] == membership["roster_version"]
    assert state["drones"][0]["drone_id"] == membership["drone_id"]


def test_signed_readiness_becomes_selectable_only_after_current_telemetry(
    relay_session: RelaySession, adapter_principal: Principal
) -> None:
    _join(relay_session, adapter_principal)
    relay_session.process_telemetry(telemetry_payload(event_id="telemetry-1"), adapter_principal)

    events = relay_session.process_membership(
        membership_payload(action="readiness", event_id="readiness-1"),
        adapter_principal,
    )

    assert events[0]["action"] == "readiness"
    assert events[0]["membership"] == "ready"
    assert events[0]["provenance"] == "adapter_signature"
    assert events[1]["drones"][0]["selectable"] is True


def test_regressive_membership_timestamp_is_refused(
    relay_session: RelaySession, adapter_principal: Principal
) -> None:
    _join(relay_session, adapter_principal)

    result = relay_session.process_membership(
        membership_payload(
            action="readiness",
            event_id="readiness-old",
            timestamp=1_756_699_999_999,
        ),
        adapter_principal,
    )

    assert result[0]["reason"] == "out_of_order_event"


def test_telemetry_remains_live_state_but_replays_as_canonical_raw_evidence(
    relay_session: RelaySession, adapter_principal: Principal
) -> None:
    _join(relay_session, adapter_principal)

    events = relay_session.process_telemetry(
        telemetry_payload(event_id="telemetry-1"), adapter_principal
    )

    assert [event["type"] for event in events] == ["telemetry", "state"]
    drone = events[-1]["drones"][0]
    assert drone["flight_state"] == "hovering"
    assert drone["telemetry"]["x"] == 1.0
    assert drone["last_seen_at"] == events[0]["t"]

    replay = relay_session.replay()["events"]
    assert [record["seq"] for record in replay] == list(range(1, len(replay) + 1))
    persisted = [record["event"] for record in replay]
    assert [event["type"] for event in persisted] == ["membership", "state", "telemetry"]
    assert persisted[-1] == events[0]


def test_reopened_audit_keeps_latest_telemetry_pose_and_last_seen_evidence(
    relay_session: RelaySession,
    adapter_principal: Principal,
    clock: MutableClock,
) -> None:
    _join(relay_session, adapter_principal)
    relay_session.process_telemetry(
        telemetry_payload(event_id="telemetry-earlier"), adapter_principal
    )
    clock.advance(1)
    latest = telemetry_payload(event_id="telemetry-latest", timestamp=clock.value, state="landing")
    latest.update(x=7.5, y=-3.0, z=1.25, vx=0.2, vy=-0.1, vz=0.0, battery=0.42)
    relay_session.process_telemetry(latest, adapter_principal)

    reopened = SessionAuditLog(relay_session.audit_log.root, SESSION)
    replay = reopened.replay()
    assert [record["seq"] for record in replay] == list(range(1, len(replay) + 1))
    telemetry = [record["event"] for record in replay if record["event"]["type"] == "telemetry"]
    assert telemetry[-1] == latest
    assert telemetry[-1]["t"] == clock.value
    assert (telemetry[-1]["x"], telemetry[-1]["y"], telemetry[-1]["z"]) == (7.5, -3.0, 1.25)
    assert telemetry[-1]["state"] == "landing"


def test_signed_graceful_leave_defaults_fail_closed_and_accepts_safety_hook(
    tmp_path: Path,
    clock: MutableClock,
    event_ids: EventIds,
    adapter_principal: Principal,
) -> None:
    denied_session = _new_session(tmp_path / "denied", clock, event_ids)
    _join(denied_session, adapter_principal)
    denied = denied_session.process_membership(
        membership_payload(action="graceful_leave", event_id="leave-denied"),
        adapter_principal,
    )

    authorized_session = _new_session(
        tmp_path / "authorized",
        clock,
        event_ids,
        leave_authorizer=lambda drone_id, epoch, state: (
            drone_id == 1 and epoch == 1 and state["drones"][0]["membership"] == "registered"
        ),
    )
    _join(authorized_session, adapter_principal, "join-authorized")
    authorized_session.update_control_projection(
        selection=(1,),
        pending={"intent_id": "pending-intent", "name": "takeoff"},
        accepted_plan={"intent_id": "plan-intent", "plan_id": "plan-1"},
    )
    allowed = authorized_session.process_membership(
        membership_payload(action="graceful_leave", event_id="leave-allowed"),
        adapter_principal,
    )
    completed = authorized_session.handle_adapter_disconnect(drone_id=1, connection_epoch=1)

    assert denied[0]["reason"] == "graceful_leave_not_authorized"
    assert allowed[0]["membership"] == "leaving"
    assert allowed[1]["selection"] == []
    assert allowed[1]["pending"] is None
    assert allowed[1]["accepted_plan"] is None
    assert allowed[1]["invalidated_intent_ids"] == ["pending-intent", "plan-intent"]
    assert allowed[1]["invalidation_reason"] == "graceful_leave_roster_change"
    assert allowed[1]["prior_roster_version"] == 1
    assert allowed[1]["cleared_control_fields"] == [
        "selection",
        "pending",
        "accepted_plan",
    ]
    assert "invalidation_reason" not in authorized_session.current_state()
    assert completed[0]["action"] == "graceful_leave_completed"
    assert completed[0]["membership"] == "disconnected"


def test_graceful_leave_authorizer_must_return_literal_true(
    tmp_path: Path,
    clock: MutableClock,
    event_ids: EventIds,
    adapter_principal: Principal,
) -> None:
    session = _new_session(
        tmp_path,
        clock,
        event_ids,
        leave_authorizer=lambda _drone_id, _epoch, _state: 1,
    )
    _join(session, adapter_principal)

    result = session.process_membership(
        membership_payload(action="graceful_leave", event_id="leave-not-bool"),
        adapter_principal,
    )

    assert result[0]["reason"] == "graceful_leave_not_authorized"


def test_unexpected_loss_remains_visible_and_rejoin_increments_epoch(
    relay_session: RelaySession, adapter_principal: Principal
) -> None:
    _join(relay_session, adapter_principal)
    lost = relay_session.handle_adapter_disconnect(drone_id=1, connection_epoch=1)
    rejoined = _join(relay_session, adapter_principal, "join-again")

    assert lost[0]["action"] == "unexpected_loss"
    assert lost[0]["provenance"] == "relay_transport_attestation"
    assert lost[1]["drones"][0]["membership"] == "disconnected"
    assert rejoined[0]["connection_epoch"] == 2
    assert rejoined[1]["drones"][0]["membership_history"][-2]["membership"] == "disconnected"


def test_prior_epoch_acknowledgement_is_refused(
    relay_session: RelaySession,
    adapter_principal: Principal,
    console_principal: Principal,
) -> None:
    _join(relay_session, adapter_principal)
    relay_session.process_intent(intent_payload(), console_principal)
    relay_session.handle_adapter_disconnect(drone_id=1, connection_epoch=1)
    _join(relay_session, adapter_principal, "join-2")

    result = relay_session.process_acknowledgement(
        acknowledgement_payload(event_id="ack-old", connection_epoch=1, roster_version=3),
        adapter_principal,
    )

    assert result[0]["type"] == "refusal"
    assert result[0]["reason"] == "stale_connection_epoch"


def test_acknowledgement_keeps_nullable_fields_and_command_id(
    relay_session: RelaySession,
    adapter_principal: Principal,
    console_principal: Principal,
) -> None:
    _join(relay_session, adapter_principal)
    relay_session.process_intent(intent_payload(), console_principal)
    relay_session.issue_command(
        command_id="command-1",
        intent_id="intent-1",
        roster_version=relay_session.registry.roster_version,
        drone_id=1,
        connection_epoch=1,
        operation=CommandOperation.HOVER,
        args={},
        signing_key=ADAPTER_KEY,
    )

    result = relay_session.process_acknowledgement(
        acknowledgement_payload(event_id="ack-1", command_id="command-1"),
        adapter_principal,
    )[0]

    assert set(result) == {
        "v",
        "t",
        "type",
        "event_id",
        "session",
        "intent_id",
        "command_id",
        "status",
        "source",
        "drone_id",
        "connection_epoch",
        "roster_version",
        "reason",
        "detail",
    }
    assert result["command_id"] == "command-1"
    assert result["reason"] is None
    assert result["detail"] is None


def test_command_completion_does_not_terminalize_estop_intent(
    relay_session: RelaySession,
    adapter_principal: Principal,
    console_principal: Principal,
) -> None:
    _join(relay_session, adapter_principal)
    estop = intent_payload()
    estop.update(name="estop", selection=[])
    relay_session.process_intent(estop, console_principal)

    relay_session.process_acknowledgement(
        acknowledgement_payload(
            event_id="command-completed",
            status="completed",
            command_id="estop-command-1",
        ),
        adapter_principal,
    )
    retry = relay_session.process_intent(
        intent_payload(intent_id="retry-too-soon", retry_of="intent-1"),
        console_principal,
    )

    assert retry[0]["reason"] == "invalid_retry"


def test_authenticated_terminal_acknowledgement_resumes_the_bound_execution(
    tmp_path: Path,
    clock: MutableClock,
    event_ids: EventIds,
    adapter_principal: Principal,
    console_principal: Principal,
) -> None:
    class ResumeSink:
        def __init__(self) -> None:
            self.acknowledgements: list[object] = []

        def __call__(self, _intent: object, _state: object) -> None:
            return None

        def resume_after_acknowledgement(
            self, session: RelaySession, acknowledgement: object
        ) -> object:
            assert session is relay
            self.acknowledgements.append(acknowledgement)
            return SimpleNamespace(relay_events=({"type": "state", "estop": False},))

    sink = ResumeSink()
    relay = _new_session(tmp_path, clock, event_ids, intent_sink=sink)
    _join(relay, adapter_principal)
    intent = intent_payload()
    relay.process_intent(intent, console_principal)
    relay.mark_pending_intent_delivered(intent["intent_id"])
    relay.execute_pending_intent(intent["intent_id"])
    relay.issue_command(
        command_id="command-1",
        intent_id="intent-1",
        roster_version=relay.registry.roster_version,
        drone_id=1,
        connection_epoch=1,
        operation=CommandOperation.HOVER,
        args={},
        signing_key=ADAPTER_KEY,
    )

    executing = relay.process_acknowledgement(
        acknowledgement_payload(event_id="ack-executing"), adapter_principal
    )
    completed = relay.process_acknowledgement(
        acknowledgement_payload(event_id="ack-completed", status="completed"),
        adapter_principal,
    )

    assert len(sink.acknowledgements) == 1
    assert asdict(sink.acknowledgements[0]) == {
        "v": 1,
        "t": clock.value,
        "type": "acknowledgement",
        "event_id": "ack-completed",
        "session": SESSION,
        "intent_id": "intent-1",
        "command_id": "command-1",
        "status": LifecycleStatus.COMPLETED,
        "drone_id": 1,
        "connection_epoch": 1,
        "roster_version": 1,
        "reason": None,
        "detail": None,
    }
    assert [event["type"] for event in executing] == ["acknowledgement"]
    assert [event["type"] for event in completed] == ["acknowledgement", "state"]


def test_estop_latches_when_dispatch_is_still_executing_or_failed(
    tmp_path: Path,
    clock: MutableClock,
    event_ids: EventIds,
    console_principal: Principal,
) -> None:
    class EstopPlan:
        estop_update = True
        selection_update = None
        armed_update = None

        @staticmethod
        def to_dict() -> dict[str, object]:
            return {"intent_id": "intent-1", "plan_id": "estop-plan"}

    for status in (LifecycleStatus.EXECUTING, LifecycleStatus.FAILED):
        plan = EstopPlan()
        result = SimpleNamespace(
            intent_id="intent-1",
            status=status,
            plan=plan,
            refusal=(
                None
                if status is LifecycleStatus.EXECUTING
                else SimpleNamespace(
                    reason=SimpleNamespace(value="adapter_failure"), detail="link lost"
                )
            ),
        )
        relay = _new_session(
            tmp_path / status.value,
            clock,
            event_ids,
            intent_sink=lambda _intent, _state, result=result: result,
        )
        estop = intent_payload()
        estop.update(name="estop", selection=[])

        admission = relay.process_intent(estop, console_principal)
        assert admission[0]["status"] == "accepted"
        assert relay.current_state()["estop"] is False
        relay.mark_pending_intent_delivered(estop["intent_id"])
        relay.execute_pending_intent(estop["intent_id"])

        assert relay.current_state()["estop"] is True


def test_control_projection_omissions_do_not_clear_plan_or_pending(
    relay_session: RelaySession,
) -> None:
    relay_session.update_control_projection(
        accepted_plan={"plan_id": "plan-1"},
        pending={"name": "takeoff"},
        armed=True,
    )

    state = relay_session.update_control_projection(selection=(1,))

    assert state["selection"] == [1]
    assert state["accepted_plan"] == {"plan_id": "plan-1"}
    assert state["pending"] == {"name": "takeoff"}
    assert state["armed"] is True


def test_control_projection_applies_completed_formation_and_spacing_updates(
    relay_session: RelaySession,
) -> None:
    state = relay_session.update_control_projection(formation="circle", spacing=1.2)

    assert state["formation"] == "circle"
    assert state["spacing"] == 1.2


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("formation", "unknown"),
        ("spacing", 0),
        ("spacing", float("inf")),
        ("spacing", True),
    ],
)
def test_control_projection_rejects_invalid_formation_or_spacing(
    relay_session: RelaySession, field: str, value: object
) -> None:
    with pytest.raises(ValueError):
        relay_session.update_control_projection(**{field: value})


def test_downstream_failure_has_terminal_refused_record(
    tmp_path: Path,
    clock: MutableClock,
    event_ids: EventIds,
    console_principal: Principal,
) -> None:
    def fail(_intent: object, _state: object) -> None:
        raise RuntimeError("do not expose this")

    session = _new_session(tmp_path, clock, event_ids, intent_sink=fail)

    accepted = session.process_intent(intent_payload(), console_principal)
    result = session.execute_pending_intent("intent-1")
    records = session.replay()["events"]
    intent_outcomes = [
        record["event"]["outcome"]
        for record in records
        if record["event"]["type"] == "intent_record"
    ]

    assert accepted[0]["status"] == "accepted"
    assert result[0]["reason"] == "downstream_error"
    assert intent_outcomes == ["accepted", "refused"]
    assert "do not expose this" not in str(records)


def test_network_estop_preempts_blocked_normal_execution(
    tmp_path: Path,
    clock: MutableClock,
    event_ids: EventIds,
    console_principal: Principal,
) -> None:
    first_started = Event()
    release_first = Event()
    second_started = Event()
    observed_selections: list[list[int]] = []

    def sink(intent: IntentV1, state: dict[str, object]) -> IntentSinkResult | None:
        observed_selections.append(state["selection"])
        if intent.intent_id == "intent-1":
            first_started.set()
            assert release_first.wait(timeout=2)
            return IntentSinkResult(
                status=LifecycleStatus.COMPLETED,
                source="test",
                selection_update=(2,),
            )
        else:
            second_started.set()
        return None

    session = _new_session(tmp_path, clock, event_ids, intent_sink=sink)
    session.process_intent(intent_payload(intent_id="intent-1"), console_principal)
    estop = intent_payload(intent_id="intent-2")
    estop.update(name="estop", selection=[])
    session.process_intent(estop, console_principal)

    first = Thread(target=session.execute_pending_intent, args=("intent-1",))
    second = Thread(target=session.execute_pending_intent, args=("intent-2",))
    first.start()
    assert first_started.wait(timeout=1)
    second.start()
    assert second_started.wait(timeout=0.2)
    release_first.set()
    first.join(timeout=1)
    second.join(timeout=1)

    assert not first.is_alive()
    assert not second.is_alive()
    assert observed_selections == [[], []]


def _contains_key(value: object, target: str) -> bool:
    if isinstance(value, dict):
        return target in value or any(_contains_key(item, target) for item in value.values())
    if isinstance(value, list):
        return any(_contains_key(item, target) for item in value)
    return False


@pytest.mark.parametrize("delivery_failed", [False, True], ids=["execution", "delivery"])
def test_pending_intent_audit_close_failure_preserves_complete_outcome_and_blocks_dispatch(
    tmp_path: Path,
    clock: MutableClock,
    event_ids: EventIds,
    console_principal: Principal,
    monkeypatch: pytest.MonkeyPatch,
    delivery_failed: bool,
) -> None:
    dispatched: list[str] = []

    def sink(intent: IntentV1, _state: dict[str, object]) -> IntentSinkResult:
        dispatched.append(intent.intent_id)
        return IntentSinkResult(
            status=LifecycleStatus.COMPLETED,
            source="test",
            selection_update=(2,),
        )

    session = _new_session(tmp_path, clock, event_ids, intent_sink=sink)
    session.process_intent(intent_payload(), console_principal)
    session.process_intent(intent_payload(intent_id="intent-2"), console_principal)
    accepted_sequence = session.replay()["last_sequence"]
    real_close = os.close

    def close_then_fail(descriptor: int) -> None:
        real_close(descriptor)
        raise OSError("injected close failure")

    with monkeypatch.context() as patch:
        patch.setattr(os, "close", close_then_fail)
        with pytest.raises(AuditLogError, match="cannot close session log"):
            if delivery_failed:
                session.fail_pending_intent(
                    "intent-1",
                    reason="acceptance_delivery_failed",
                    detail="accepting connection did not receive acknowledgement",
                )
            else:
                session.execute_pending_intent("intent-1")

    reopened = SessionAuditLog(tmp_path, SESSION)
    outcome = [record["event"] for record in reopened.replay(after_sequence=accepted_sequence)]
    if delivery_failed:
        assert [event["type"] for event in outcome] == ["intent_record", "refusal"]
        assert outcome[-1]["reason"] == "acceptance_delivery_failed"
    else:
        assert [event["type"] for event in outcome] == [
            "autonomy_result",
            "state",
            "acknowledgement",
        ]
        assert outcome[-1]["status"] == "completed"
        assert outcome[1]["selection"] == [2]
    with pytest.raises(AuditLogError, match="session is unusable"):
        session.current_state()
    with pytest.raises(AuditLogError, match="session is unusable"):
        session.replay()
    with pytest.raises(AuditLogError, match="session is unusable"):
        session.execute_pending_intent("intent-2")
    assert dispatched == ([] if delivery_failed else ["intent-1"])
