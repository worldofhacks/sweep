from dataclasses import replace

from language.test_compiler import _hydrate_relay_from_snapshot
from planner.controller import PreparedExecutionRouter
from planner.models import LifecycleStatus, PreparedExecution
from relay.audit import SessionAuditLog
from relay.auth import Principal
from relay.intent_v1 import IntentName
from relay.session import RelayLimits, RelaySession
from tests.autonomy_fixtures import make_intent, make_snapshot, make_stack


def test_post_goto_enrichment_failure_stops_aircraft_before_releasing_ownership(
    tmp_path, monkeypatch
):
    snapshot = make_snapshot(2, roster_version=4)
    controller, _, _, _, flight, _ = make_stack(snapshot)
    goto = flight.goto
    unavailable = False
    goto_count = 0

    def asynchronous_goto(*args, **kwargs):
        nonlocal unavailable, goto_count
        ack = goto(*args, **kwargs)
        goto_count += 1
        if goto_count == 2:
            unavailable = True
        return replace(ack, status=LifecycleStatus.ACCEPTED)

    def live_snapshot():
        if unavailable:
            raise RuntimeError("enrichment unavailable after adapter I/O")
        return snapshot

    monkeypatch.setattr(flight, "goto", asynchronous_goto)
    router = PreparedExecutionRouter(controller, current_snapshot=live_snapshot)
    relay = RelaySession(
        session_id="test-session",
        audit_log=SessionAuditLog(tmp_path, "test-session"),
        limits=RelayLimits(5_000, 5_000, 1_000, 1_000),
        clock=lambda: snapshot.now_ms,
        intent_sink=router,
    )
    _hydrate_relay_from_snapshot(relay, snapshot)
    intent = make_intent(IntentName.TRANSLATE, args={"dx": 1, "dy": 0})
    prepared = router.prepare(intent, snapshot)
    assert isinstance(prepared, PreparedExecution)
    router.bind(prepared)
    events = router.relay_emitter(
        relay, Principal(source="console", drone_id=None, signing_key=b"x" * 32)
    )(intent)
    assert events[-1]["status"] == "executing"
    first, second = prepared.plan.commands
    raw = {
        "v": 1,
        "t": snapshot.now_ms,
        "type": "acknowledgement",
        "event_id": "terminal-first",
        "session": relay.session_id,
        "intent_id": intent.intent_id,
        "command_id": first.command_id,
        "status": "completed",
        "drone_id": first.drone_id,
        "connection_epoch": first.connection_epoch,
        "roster_version": snapshot.roster_version,
        "reason": None,
        "detail": None,
    }
    principal = Principal(source="adapter", drone_id=first.drone_id, signing_key=b"x" * 32)
    events = relay.process_frame(raw, principal)
    calls = [(call.operation.value, call.drone_ids) for call in flight.calls]
    assert calls[:2] == [("goto", (first.drone_id,)), ("goto", (second.drone_id,))]
    assert all(operation == "hover" for operation, _ in calls[2:])
    assert {ids[0] for _, ids in calls[2:]} == {first.drone_id, second.drone_id}
    assert any(
        event.get("source") == "autonomy" and event["status"] == "failed" for event in events
    )
    assert relay.current_state()["accepted_plan"] is None
    assert intent.intent_id not in router._running
    unavailable = False
    calls = list(flight.calls)
    relay.process_frame({**raw, "event_id": "terminal-retry"}, principal)
    assert flight.calls == calls


def test_initial_ambiguous_goto_keeps_async_safety_hold_owned_and_rejects_retry(
    tmp_path, monkeypatch
):
    snapshot = make_snapshot(2, roster_version=4)
    controller, _, _, _, flight, _ = make_stack(snapshot)
    unavailable = False
    goto, hover = flight.goto, flight.hover

    def interrupted_goto(*args, **kwargs):
        nonlocal unavailable
        ack = goto(*args, **kwargs)
        unavailable = True
        return replace(ack, status=LifecycleStatus.ACCEPTED)

    def live_snapshot():
        if unavailable:
            raise RuntimeError("enrichment unavailable after I/O")
        return snapshot

    monkeypatch.setattr(flight, "goto", interrupted_goto)
    monkeypatch.setattr(
        flight,
        "hover",
        lambda *a, **kw: tuple(
            replace(ack, status=LifecycleStatus.ACCEPTED) for ack in hover(*a, **kw)
        ),
    )
    router = PreparedExecutionRouter(controller, current_snapshot=live_snapshot)
    relay = RelaySession(
        session_id="test-session",
        audit_log=SessionAuditLog(tmp_path, "test-session"),
        limits=RelayLimits(5_000, 5_000, 1_000, 1_000),
        clock=lambda: snapshot.now_ms,
        intent_sink=router,
    )
    _hydrate_relay_from_snapshot(relay, snapshot)
    emitter = router.relay_emitter(
        relay, Principal(source="console", drone_id=None, signing_key=b"x" * 32)
    )
    intent = make_intent(IntentName.TRANSLATE, args={"dx": 1, "dy": 0})
    prepared = router.prepare(intent, snapshot)
    assert isinstance(prepared, PreparedExecution)
    router.bind(prepared)
    events = emitter(intent)
    assert events[-1]["status"] == "failed"
    safety_id = f"safety:ambiguous:{intent.intent_id}"
    assert safety_id in router._running
    assert relay.current_state()["accepted_plan"]["intent_id"] == safety_id
    unavailable = False
    retry = replace(intent, intent_id="retry-motion", retry_of=intent.intent_id)
    retry_prepared = router.prepare(retry, snapshot)
    assert isinstance(retry_prepared, PreparedExecution)
    router.bind(retry_prepared)
    emitter(retry)
    assert [call.operation.value for call in flight.calls].count("goto") == 1
    safety, _, _ = router._running[safety_id]
    for index, command in enumerate(safety.plan.commands):
        events = relay.process_frame(
            {
                "v": 1,
                "t": snapshot.now_ms,
                "type": "acknowledgement",
                "event_id": f"safety-complete-{index}",
                "session": relay.session_id,
                "intent_id": safety_id,
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
        assert not any(event.get("reason") == "unknown_intent_id" for event in events)
    assert safety_id not in router._running
    assert relay.current_state()["accepted_plan"] is None
    assert [call.operation.value for call in flight.calls].count("goto") == 1
