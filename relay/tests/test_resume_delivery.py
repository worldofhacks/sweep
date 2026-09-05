from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event

import pytest

from planner.models import LifecycleStatus
from relay.auth import Principal
from relay.tests.test_router_delivery import _prepared_translation


@pytest.mark.parametrize("early_ack", [False, True])
def test_estop_does_not_wait_for_resumed_adapter_io(
    tmp_path, monkeypatch, console_principal, early_ack
):
    relay, prepared, flight, payload = _prepared_translation(tmp_path)
    first_entered, first_release = Event(), Event()
    resumed_entered, resumed_release = Event(), Event()
    original_goto = flight.goto

    def goto(*args):
        acknowledgement = original_goto(*args)
        if len(flight.calls) == 1:
            first_entered.set()
            if early_ack:
                assert first_release.wait(5)
            return replace(acknowledgement, status=LifecycleStatus.EXECUTING)
        resumed_entered.set()
        assert resumed_release.wait(5)
        return acknowledgement

    monkeypatch.setattr(flight, "goto", goto)
    relay.process_intent(payload, console_principal)
    relay.mark_pending_intent_delivered(prepared.intent.intent_id)
    command = prepared.plan.commands[0]
    ack = {
        "v": 1,
        "t": prepared.intent.t,
        "type": "acknowledgement",
        "event_id": "resume-ack",
        "session": prepared.intent.session,
        "intent_id": prepared.intent.intent_id,
        "command_id": command.command_id,
        "status": "completed",
        "drone_id": command.drone_id,
        "connection_epoch": command.connection_epoch,
        "roster_version": prepared.plan.roster_version,
        "reason": None,
        "detail": None,
    }

    def stop():
        intent = {
            **payload,
            "intent_id": "stop-during-resume",
            "name": "estop",
            "args": {},
            "selection": [],
        }
        assert relay.process_intent(intent, console_principal)[0]["status"] == "accepted"
        relay.mark_pending_intent_delivered(intent["intent_id"])
        return relay.execute_pending_intent(intent["intent_id"])

    with ThreadPoolExecutor(max_workers=3) as executor:
        execution = executor.submit(relay.execute_pending_intent, prepared.intent.intent_id)
        try:
            assert first_entered.wait(2)
            if not early_ack:
                execution.result(timeout=2)
            completion = executor.submit(
                relay.process_acknowledgement,
                ack,
                Principal(source="adapter", drone_id=command.drone_id, signing_key=b"x" * 32),
            )
            if early_ack:
                completion.result(timeout=2)
                first_release.set()
            assert resumed_entered.wait(2)
            stopped = executor.submit(stop)
            events = stopped.result(timeout=1)
            assert any(event.get("status") == "completed" for event in events)
            assert relay.current_state()["estop"] is True
        finally:
            first_release.set()
            resumed_release.set()
        execution.result(timeout=3)
        completion.result(timeout=3)
    assert [call.operation.value for call in flight.calls] == ["goto", "goto", "estop"]
    assert relay.current_state()["accepted_plan"] is None


def test_crash_after_resumed_io_keeps_durable_marker(tmp_path, monkeypatch, console_principal):
    from relay.audit import AuditLogError, SessionAuditLog

    relay, prepared, flight, payload = _prepared_translation(tmp_path)
    original_goto = flight.goto

    class AdapterCrash(BaseException):
        pass

    def goto(*args):
        acknowledgement = original_goto(*args)
        if len(flight.calls) == 1:
            return replace(acknowledgement, status=LifecycleStatus.EXECUTING)
        raise AdapterCrash

    monkeypatch.setattr(flight, "goto", goto)
    relay.process_intent(payload, console_principal)
    relay.mark_pending_intent_delivered(prepared.intent.intent_id)
    relay.execute_pending_intent(prepared.intent.intent_id)
    command = prepared.plan.commands[0]
    with pytest.raises(AdapterCrash):
        relay.process_acknowledgement(
            {
                "v": 1,
                "t": prepared.intent.t,
                "type": "acknowledgement",
                "event_id": "crash-resume-ack",
                "session": prepared.intent.session,
                "intent_id": prepared.intent.intent_id,
                "command_id": command.command_id,
                "status": "completed",
                "drone_id": command.drone_id,
                "connection_epoch": command.connection_epoch,
                "roster_version": prepared.plan.roster_version,
                "reason": None,
                "detail": None,
            },
            Principal(source="adapter", drone_id=command.drone_id, signing_key=b"x" * 32),
        )
    assert len(flight.calls) == 2
    with pytest.raises(AuditLogError):
        SessionAuditLog(tmp_path, relay.session_id).replay()
    with pytest.raises(AuditLogError):
        relay.current_state()
