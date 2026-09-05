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


@pytest.mark.parametrize("failure_stage", ["enrichment", "after_dispatch"])
@pytest.mark.parametrize("publication_fails", [False, True])
@pytest.mark.parametrize("status", ["completed", "failed", "invalidated"])
def test_terminal_ack_cannot_redispatch_after_resume_failure(
    tmp_path, monkeypatch, status, publication_fails, failure_stage
):
    snapshot = make_snapshot(2, roster_version=4)
    controller, _, _, _, flight, _ = make_stack(snapshot)
    hover = flight.hover

    def accepted_hover(ids):
        return tuple(replace(ack, status=LifecycleStatus.ACCEPTED) for ack in hover(ids))

    monkeypatch.setattr(flight, "hover", accepted_hover)
    unavailable = False

    def live_snapshot():
        if unavailable:
            raise RuntimeError("enrichment offline")
        return snapshot

    router = PreparedExecutionRouter(controller, current_snapshot=live_snapshot)
    relay = RelaySession(
        session_id="test-session",
        audit_log=SessionAuditLog(tmp_path, "test-session"),
        limits=RelayLimits(5_000, 5_000, 1_000, 1_000),
        clock=lambda: snapshot.now_ms,
        intent_sink=router,
    )
    _hydrate_relay_from_snapshot(relay, snapshot)
    intent = make_intent(IntentName.HOLD)
    prepared = router.prepare(intent, snapshot)
    assert isinstance(prepared, PreparedExecution)
    router.bind(prepared)
    events = router.relay_emitter(
        relay, Principal(source="console", drone_id=None, signing_key=b"x" * 32)
    )(intent)
    assert events[-1]["status"] == "executing"
    assert len(flight.calls) == 1
    command = prepared.plan.commands[0]
    unavailable = failure_stage == "enrichment"
    if failure_stage == "after_dispatch":
        resume = controller.dispatcher.resume_after_completion

        def resume_then_raise(*args, **kwargs):
            resume(*args, **kwargs)
            raise RuntimeError("dispatcher failed after adapter I/O")

        monkeypatch.setattr(controller.dispatcher, "resume_after_completion", resume_then_raise)
    expected_calls = 3 if failure_stage == "after_dispatch" and status == "completed" else 2
    safety_id = f"safety:resume:{intent.intent_id}"
    raw = {
        "v": 1,
        "t": snapshot.now_ms,
        "type": "acknowledgement",
        "event_id": "terminal-ack",
        "session": relay.session_id,
        "intent_id": intent.intent_id,
        "command_id": command.command_id,
        "status": status,
        "drone_id": command.drone_id,
        "connection_epoch": command.connection_epoch,
        "roster_version": snapshot.roster_version,
        "reason": None if status == "completed" else "adapter_failure",
        "detail": None,
    }
    principal = Principal(source="adapter", drone_id=command.drone_id, signing_key=b"x" * 32)
    if publication_fails:

        def fail_publication(*args):
            raise OSError("audit unavailable")

        monkeypatch.setattr(relay, "record_execution_result", fail_publication)
        with pytest.raises(OSError, match="audit unavailable"):
            relay.process_frame(raw, principal)
        assert intent.intent_id in router._running
        assert len(flight.calls) == expected_calls
        safety_prepared, safety_pending, safety_owner = router._running[safety_id]
        assert safety_owner is relay
        assert safety_pending.status is LifecycleStatus.EXECUTING
        assert safety_prepared.intent.name is IntentName.HOLD
        assert flight.calls[-1].operation.value == "hover"
        assert flight.calls[-1].drone_ids == (safety_prepared.plan.commands[0].drone_id,)
        return

    events = relay.process_frame(raw, principal)
    terminal = [
        event
        for event in events
        if event.get("source") == "autonomy" and event.get("intent_id") == intent.intent_id
    ]
    assert len(terminal) == 1
    assert terminal[0]["status"] == ("invalidated" if status == "completed" else status)
    detail = (
        "live safety state unavailable"
        if failure_stage == "enrichment"
        else "resume raised after possible adapter I/O"
    )
    assert detail in terminal[0]["detail"]
    safety_prepared, safety_pending, safety_owner = router._running[safety_id]
    assert safety_owner is relay
    assert safety_pending.status is LifecycleStatus.EXECUTING
    assert safety_prepared.intent.name is IntentName.HOLD
    assert relay.current_state()["accepted_plan"]["intent_id"] == safety_id
    assert intent.intent_id not in router._running
    assert all(call.operation.value == "hover" for call in flight.calls)
    assert flight.calls[-1].drone_ids == (safety_prepared.plan.commands[0].drone_id,)
    assert events[0]["status"] == status
    assert len(flight.calls) == expected_calls
    retained_safety = router._running[safety_id]

    unavailable = False
    repeated = relay.process_frame({**raw, "event_id": "terminal-ack-retry"}, principal)
    assert not any(event.get("source") == "autonomy" for event in repeated)
    assert len(flight.calls) == expected_calls
    assert router._running[safety_id] == retained_safety
