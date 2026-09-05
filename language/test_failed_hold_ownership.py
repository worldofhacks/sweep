from dataclasses import replace

import pytest

from language.test_compiler import _hydrate_relay_from_snapshot
from planner.controller import PreparedExecutionRouter
from planner.models import LifecycleStatus, PreparedExecution
from relay.audit import SessionAuditLog
from relay.auth import Principal
from relay.intent_v1 import IntentName
from relay.session import RelayLimits, RelaySession
from tests.autonomy_fixtures import make_intent, make_snapshot, make_stack


@pytest.mark.parametrize("failure_status", ["failed", "invalidated"])
def test_failed_first_hover_keeps_safety_ownership_until_moving_aircraft_is_stopped(
    tmp_path, monkeypatch, failure_status
):
    snapshot = make_snapshot(2, roster_version=4)
    controller, _, _, _, flight, _ = make_stack(snapshot)
    goto, hover = flight.goto, flight.hover
    monkeypatch.setattr(
        flight,
        "goto",
        lambda *args, **kwargs: replace(goto(*args, **kwargs), status=LifecycleStatus.ACCEPTED),
    )
    monkeypatch.setattr(
        flight,
        "hover",
        lambda *args, **kwargs: tuple(
            replace(ack, status=LifecycleStatus.ACCEPTED) for ack in hover(*args, **kwargs)
        ),
    )
    router = PreparedExecutionRouter(controller, current_snapshot=lambda: snapshot)
    relay = RelaySession(
        session_id="test-session",
        audit_log=SessionAuditLog(tmp_path, "test-session"),
        limits=RelayLimits(5_000, 5_000, 1_000, 1_000),
        clock=lambda: snapshot.now_ms,
        intent_sink=router,
    )
    _hydrate_relay_from_snapshot(relay, snapshot)
    emit = router.relay_emitter(
        relay, Principal(source="console", drone_id=None, signing_key=b"x" * 32)
    )
    for name in (IntentName.TRANSLATE, IntentName.HOLD):
        intent = make_intent(
            name,
            intent_id=name.value,
            args={"dx": 1, "dy": 0} if name is IntentName.TRANSLATE else {},
        )
        prepared = router.prepare(intent, snapshot)
        assert isinstance(prepared, PreparedExecution)
        router.bind(prepared)
        assert emit(intent)[-1]["status"] == "executing"
        if name is IntentName.TRANSLATE:
            motion_command = relay.current_state()["accepted_plan"]["commands"][0]

    assert [(call.operation.value, call.drone_ids) for call in flight.calls] == [
        ("goto", (2,)),
        ("hover", (1,)),
    ]

    def acknowledge(command, status, event_id):
        return relay.process_frame(
            {
                "v": 1,
                "t": snapshot.now_ms,
                "type": "acknowledgement",
                "event_id": event_id,
                "session": relay.session_id,
                "intent_id": command["intent_id"],
                "command_id": command["command_id"],
                "status": status,
                "drone_id": command["drone_id"],
                "connection_epoch": command["connection_epoch"],
                "roster_version": snapshot.roster_version,
                "reason": None if status == "completed" else "adapter_failure",
                "detail": None,
            },
            Principal(source="adapter", drone_id=command["drone_id"], signing_key=b"x" * 32),
        )

    failed_command = relay.current_state()["accepted_plan"]["commands"][0]
    acknowledge(failed_command, failure_status, "original-hold-failed")
    safety = relay.current_state()["accepted_plan"]
    assert safety is not None
    assert safety["intent_id"] == "safety:ambiguous:hold"
    assert set(router._running) == {safety["intent_id"]}
    assert router._running[safety["intent_id"]][1].status is LifecycleStatus.EXECUTING
    calls_before_late_ack = list(flight.calls)
    acknowledge(motion_command, "completed", "late-old-motion")
    assert flight.calls == calls_before_late_ack
    assert relay.current_state()["accepted_plan"] == safety

    for index, command in enumerate(safety["commands"]):
        events = acknowledge(command, "completed", f"safety-completed-{index}")
        assert not any(event.get("reason") == "unknown_intent_id" for event in events)
        terminal = [
            event
            for event in events
            if event.get("source") == "autonomy" and event.get("intent_id") == safety["intent_id"]
        ]
        assert terminal[-1]["status"] == ("executing" if index == 0 else "completed")
    assert [(call.operation.value, call.drone_ids) for call in flight.calls] == [
        ("goto", (2,)),
        ("hover", (1,)),
        ("hover", (1,)),
        ("hover", (2,)),
    ]
    assert not router._running
    assert relay.current_state()["accepted_plan"] is None
