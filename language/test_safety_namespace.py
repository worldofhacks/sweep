from dataclasses import replace

import pytest

from language.test_compiler import _hydrate_relay_from_snapshot
from planner.controller import PreparedExecutionRouter, _intent_payload
from planner.models import LifecycleStatus, PreparedExecution
from relay.audit import SessionAuditLog
from relay.auth import Principal
from relay.intent_v1 import IntentName
from relay.session import RelayLimits, RelaySession
from tests.autonomy_fixtures import make_intent, make_snapshot, make_stack


@pytest.mark.parametrize("malformed", [False, True])
@pytest.mark.parametrize("recovery", ["motion-conflict:second", "ambiguous:hold"])
def test_public_id_cannot_reserve_controller_safety_ownership(
    tmp_path, monkeypatch, malformed, recovery
):
    snapshot = make_snapshot(2, roster_version=4)
    controller, _, _, _, flight, _ = make_stack(snapshot)
    goto, hover = flight.goto, flight.hover
    monkeypatch.setattr(
        flight, "goto", lambda *a, **kw: replace(goto(*a, **kw), status=LifecycleStatus.ACCEPTED)
    )
    hover_calls = 0

    def hovering(*args, **kwargs):
        nonlocal hover_calls
        hover_calls += 1
        result = hover(*args, **kwargs)
        if recovery.startswith("ambiguous") and hover_calls <= 4:
            raise RuntimeError("stop acknowledgement lost after I/O")
        return tuple(replace(ack, status=LifecycleStatus.ACCEPTED) for ack in result)

    monkeypatch.setattr(flight, "hover", hovering)
    router = PreparedExecutionRouter(controller, current_snapshot=lambda: snapshot)
    relay = RelaySession(
        session_id="test-session",
        audit_log=SessionAuditLog(tmp_path, "test-session"),
        limits=RelayLimits(5000, 5000, 1000, 1000),
        clock=lambda: snapshot.now_ms,
        intent_sink=router,
    )
    _hydrate_relay_from_snapshot(relay, snapshot)
    principal = Principal(source="console", drone_id=None, signing_key=b"x" * 32)
    hostile = _intent_payload(
        make_intent(IntentName.SELECT, intent_id=f"safety:{recovery}", args={"ids": [1, 2]})
    )
    if malformed:
        hostile.pop("args")
    assert relay.process_intent(hostile, principal)[0]["reason"] == "reserved_intent_id"
    assert hostile["intent_id"] not in relay._intents
    emitter = router.relay_emitter(relay, principal)
    motion = make_intent(IntentName.TRANSLATE, intent_id="old", args={"dx": 1, "dy": 0})
    prepared = router.prepare(motion, snapshot)
    assert isinstance(prepared, PreparedExecution)
    router.bind(prepared)
    assert emitter(motion)[-1]["status"] == "executing"
    if recovery.startswith("motion-conflict"):
        next_intent = replace(motion, intent_id="second", args={"dx": -1, "dy": 0})
        next_prepared = router.prepare(next_intent, snapshot)
        assert isinstance(next_prepared, PreparedExecution)
        router.bind(next_prepared)
        emitter(next_intent)
    else:
        hold = make_intent(IntentName.HOLD, intent_id="hold")
        next_prepared = router.prepare(hold, snapshot)
        assert isinstance(next_prepared, PreparedExecution)
        router.bind(next_prepared)
        assert emitter(hold)[-1]["status"] == "failed"
    assert [call.operation.value for call in flight.calls].count("goto") == 1
    assert any(call.operation.value == "hover" for call in flight.calls)
    assert hostile["intent_id"] in router._running
    assert relay.current_state()["accepted_plan"]["intent_id"] == hostile["intent_id"]
    assert "old" not in router._running
    command = prepared.plan.commands[0]
    before = list(flight.calls)
    relay.process_frame(
        {
            "v": 1,
            "t": snapshot.now_ms,
            "type": "acknowledgement",
            "event_id": "late-old",
            "session": relay.session_id,
            "intent_id": "old",
            "command_id": command.command_id,
            "status": "completed",
            "drone_id": command.drone_id,
            "connection_epoch": command.connection_epoch,
            "roster_version": snapshot.roster_version,
            "reason": None,
            "detail": None,
        },
        Principal(source="adapter", drone_id=command.drone_id, signing_key=b"x" * 32),
    )
    assert flight.calls == before
