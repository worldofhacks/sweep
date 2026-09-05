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


@pytest.mark.parametrize("pending_status", [LifecycleStatus.ACCEPTED, LifecycleStatus.EXECUTING])
def test_conflict_safety_hold_resumes_through_authenticated_completion(
    tmp_path, monkeypatch, pending_status
):
    snapshot = make_snapshot(2, roster_version=4)
    controller, _, _, _, flight, _ = make_stack(snapshot)
    goto, hover = flight.goto, flight.hover
    monkeypatch.setattr(
        flight, "goto", lambda *a, **kw: replace(goto(*a, **kw), status=pending_status)
    )
    monkeypatch.setattr(
        flight,
        "hover",
        lambda *a, **kw: tuple(replace(ack, status=pending_status) for ack in hover(*a, **kw)),
    )
    router = PreparedExecutionRouter(controller, current_snapshot=lambda: snapshot)
    relay = RelaySession(
        session_id="language-eval",
        audit_log=SessionAuditLog(tmp_path, "language-eval"),
        limits=RelayLimits(5_000, 5_000, 1_000, 1_000),
        clock=lambda: snapshot.now_ms,
        intent_sink=router,
    )
    _hydrate_relay_from_snapshot(relay, snapshot)
    emitter = router.relay_emitter(
        relay, Principal(source="console", drone_id=None, signing_key=b"x" * 32)
    )
    for index, dx in enumerate((1, -1)):
        intent = replace(
            make_intent(
                IntentName.TRANSLATE, intent_id=f"motion-{index}", args={"dx": dx, "dy": 0}
            ),
            session=relay.session_id,
        )
        prepared = router.prepare(intent, snapshot)
        assert isinstance(prepared, PreparedExecution)
        router.bind(prepared)
        events = emitter(intent)
    assert events[-1]["reason"] == "conflicting_motion"
    assert [(call.operation.value, call.drone_ids) for call in flight.calls] == [
        ("goto", (2,)),
        ("hover", (1,)),
    ]
    safety = relay.current_state()["accepted_plan"]
    assert safety["intent_id"] == "safety:motion-conflict:motion-1"
    assert any(
        event.get("intent_id") == safety["intent_id"] and event.get("status") == "executing"
        for event in events
    )
    for index, command in enumerate(safety["commands"]):
        events = relay.process_frame(
            {
                "v": 1,
                "type": "acknowledgement",
                "t": snapshot.now_ms,
                "event_id": f"hover-complete-{index}",
                "session": relay.session_id,
                "intent_id": safety["intent_id"],
                "command_id": command["command_id"],
                "status": "completed",
                "drone_id": command["drone_id"],
                "connection_epoch": command["connection_epoch"],
                "roster_version": snapshot.roster_version,
                "reason": None,
                "detail": None,
            },
            Principal(source="adapter", drone_id=command["drone_id"], signing_key=b"x" * 32),
        )
        assert events[-1]["status"] == ("executing" if index == 0 else "completed"), events
        if index == 0:
            assert relay.current_state()["accepted_plan"]["intent_id"] == safety["intent_id"]
    assert [(call.operation.value, call.drone_ids) for call in flight.calls] == [
        ("goto", (2,)),
        ("hover", (1,)),
        ("hover", (2,)),
    ]
    assert relay.current_state()["accepted_plan"] is None
    assert safety["intent_id"] not in router._running
    records = [record["event"] for record in relay.audit_log.replay()]
    assert any(
        event.get("type") == "intent_record" and event.get("intent_id") == safety["intent_id"]
        for event in records
    )
