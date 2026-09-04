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


@pytest.mark.parametrize("enrichment_fails", [False, True])
@pytest.mark.parametrize("hold_status", [LifecycleStatus.COMPLETED, LifecycleStatus.ACCEPTED])
def test_public_hold_invalidates_retained_translate_before_late_completion(
    tmp_path, monkeypatch, hold_status, enrichment_fails
):
    snapshot = make_snapshot(2, roster_version=4)
    controller, _, _, _, flight, _ = make_stack(snapshot)
    goto = flight.goto
    hover = flight.hover
    monkeypatch.setattr(
        flight, "goto", lambda *a, **kw: replace(goto(*a, **kw), status=LifecycleStatus.ACCEPTED)
    )
    unavailable = False

    def holding(*args, **kwargs):
        nonlocal unavailable
        result = tuple(replace(ack, status=hold_status) for ack in hover(*args, **kwargs))
        unavailable = enrichment_fails
        return result

    def live_snapshot():
        if unavailable:
            raise RuntimeError("enrichment lost after HOLD I/O")
        return snapshot

    monkeypatch.setattr(flight, "hover", holding)
    router = PreparedExecutionRouter(controller, current_snapshot=live_snapshot)
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

    def emit(name, intent_id, args=None):
        intent = replace(
            make_intent(name, intent_id=intent_id, args=args, t=snapshot.now_ms),
            session=relay.session_id,
        )
        prepared = router.prepare(intent, snapshot)
        assert isinstance(prepared, PreparedExecution)
        router.bind(prepared)
        return emitter(intent)

    events = emit(IntentName.TRANSLATE, "old-motion", {"dx": 1, "dy": 0})
    assert events[-1]["status"] == "executing", events
    command = relay.current_state()["accepted_plan"]["commands"][0]
    assert [(call.operation.value, call.drone_ids) for call in flight.calls] == [
        ("goto", (command["drone_id"],))
    ]
    events = emit(IntentName.HOLD, "new-hold")
    invalidations = [
        event
        for event in events
        if event.get("source") == "autonomy" and event.get("intent_id") == "old-motion"
    ]
    assert len(invalidations) == 1, events
    assert invalidations[0]["status"] == "invalidated"
    assert invalidations[0]["reason"] == "conflicting_motion"
    calls_after_hold = list(flight.calls)
    assert [call.operation.value for call in calls_after_hold].count("hover") >= 1
    for index, status in enumerate(["completed", "failed", "invalidated"]):
        events = relay.process_frame(
            {
                "v": 1,
                "t": snapshot.now_ms,
                "type": "acknowledgement",
                "event_id": f"late-{index}",
                "session": relay.session_id,
                "intent_id": "old-motion",
                "command_id": command["command_id"],
                "status": status,
                "drone_id": command["drone_id"],
                "connection_epoch": command["connection_epoch"],
                "roster_version": snapshot.roster_version,
                "reason": None,
                "detail": None,
            },
            Principal(source="adapter", drone_id=command["drone_id"], signing_key=b"x" * 32),
        )
        assert flight.calls == calls_after_hold
        assert not any(event.get("source") == "autonomy" for event in events), events
    replay = relay.audit_log.replay()
    assert any(
        record["event"].get("source") == "autonomy"
        and record["event"].get("intent_id") == "old-motion"
        and record["event"].get("status") == "invalidated"
        for record in replay
    )


@pytest.mark.parametrize("conflicting", [True, False])
def test_public_motion_cannot_replace_an_executing_plan(tmp_path, monkeypatch, conflicting):
    snapshot = make_snapshot(2, roster_version=4)
    controller, _, _, _, flight, _ = make_stack(snapshot)
    goto = flight.goto
    monkeypatch.setattr(
        flight, "goto", lambda *a, **kw: replace(goto(*a, **kw), status=LifecycleStatus.ACCEPTED)
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
    first = replace(
        make_intent(IntentName.TRANSLATE, intent_id="first-motion", args={"dx": 1, "dy": 0}),
        session=relay.session_id,
    )
    second = replace(
        first,
        intent_id="second-motion",
        args={"dx": -1, "dy": 0},
        t=first.t
        if conflicting
        else first.t + controller.arbiter.config.motion_conflict_window_ms + 1,
    )
    for intent in (first, second):
        prepared = router.prepare(intent, snapshot)
        assert isinstance(prepared, PreparedExecution)
        router.bind(prepared)
        events = emitter(intent)
        if intent is first:
            assert events[-1]["status"] == "executing", events
            active_plan = relay.current_state()["accepted_plan"]
    assert events[-1]["status"] == "refused", events
    assert events[-1]["reason"] == ("conflicting_motion" if conflicting else "active_task"), events
    assert [call.operation.value for call in flight.calls].count("goto") == 1
    if conflicting:
        assert any(
            event.get("intent_id") == first.intent_id and event.get("status") == "invalidated"
            for event in events
        )
        assert [call.operation.value for call in flight.calls].count("hover") == 2
        assert relay.current_state()["accepted_plan"] is None
        command = active_plan["commands"][0]
        calls_after_conflict = list(flight.calls)
        events = relay.process_frame(
            {
                "v": 1,
                "t": snapshot.now_ms,
                "type": "acknowledgement",
                "event_id": "conflicted-motion-completion",
                "session": relay.session_id,
                "intent_id": first.intent_id,
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
        assert flight.calls == calls_after_conflict
        assert not any(event.get("source") == "autonomy" for event in events)
    else:
        assert not any(event.get("status") == "invalidated" for event in events)
        assert relay.current_state()["accepted_plan"] == active_plan


def test_selection_cannot_redirect_hold_away_from_active_motion(tmp_path, monkeypatch):
    snapshot = make_snapshot(3, selection=(1, 2), roster_version=6)
    controller, _, _, _, flight, _ = make_stack(snapshot)
    goto = flight.goto
    monkeypatch.setattr(
        flight, "goto", lambda *a, **kw: replace(goto(*a, **kw), status=LifecycleStatus.ACCEPTED)
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

    def emit(name, intent_id, args=None, selection=(1, 2)):
        intent = replace(
            make_intent(name, intent_id=intent_id, args=args, selection=selection),
            session=relay.session_id,
        )
        prepared = router.prepare(intent, snapshot)
        assert isinstance(prepared, PreparedExecution)
        router.bind(prepared)
        return emitter(intent)

    events = emit(IntentName.TRANSLATE, "active-motion", {"dx": 1, "dy": 0})
    assert events[-1]["status"] == "executing"
    active = relay.current_state()["accepted_plan"]
    command = active["commands"][0]
    events = emit(IntentName.SELECT, "unrelated-selection", {"ids": (3,)}, selection=(3,))
    assert events[-1]["status"] == "refused"
    assert events[-1]["reason"] == "active_task"
    assert relay.current_state()["selection"] == [1, 2]
    assert relay.current_state()["accepted_plan"] == active
    assert len(flight.calls) == 1

    events = emit(IntentName.HOLD, "hold-active-selection")
    assert events[-1]["status"] == "completed"
    assert [(call.operation.value, call.drone_ids) for call in flight.calls] == [
        ("goto", (command["drone_id"],)),
        ("hover", (1,)),
        ("hover", (2,)),
    ]
    calls_after_hold = list(flight.calls)
    relay.process_frame(
        {
            "v": 1,
            "t": snapshot.now_ms,
            "type": "acknowledgement",
            "event_id": "late-active-motion",
            "session": relay.session_id,
            "intent_id": "active-motion",
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
    assert flight.calls == calls_after_hold


def test_hold_cannot_retire_an_executing_estop(tmp_path, monkeypatch):
    snapshot = make_snapshot(2, roster_version=4)
    controller, _, _, _, flight, _ = make_stack(snapshot)
    estop = flight.estop
    monkeypatch.setattr(
        flight,
        "estop",
        lambda *a, **kw: tuple(
            replace(ack, status=LifecycleStatus.ACCEPTED) for ack in estop(*a, **kw)
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
    emitter = router.relay_emitter(
        relay, Principal(source="console", drone_id=None, signing_key=b"x" * 32)
    )
    for name in (IntentName.ESTOP, IntentName.HOLD):
        intent = make_intent(name, intent_id=name.value)
        prepared = router.prepare(intent, snapshot)
        assert isinstance(prepared, PreparedExecution)
        router.bind(prepared)
        events = emitter(intent)
        if name is IntentName.ESTOP:
            assert events[-1]["status"] == "executing", events
            active = relay.current_state()["accepted_plan"]
        else:
            assert events[-1]["reason"] == "active_task", events
            assert relay.current_state()["accepted_plan"] == active
    assert [call.operation.value for call in flight.calls] == ["estop"]
    for index, command in enumerate(active["commands"]):
        events = relay.process_frame(
            {
                "v": 1,
                "type": "acknowledgement",
                "t": snapshot.now_ms,
                "event_id": f"stop-complete-{index}",
                "session": relay.session_id,
                "intent_id": active["intent_id"],
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
    assert [(call.operation.value, call.drone_ids) for call in flight.calls] == [("estop", (1, 2))]
    assert relay.current_state()["accepted_plan"] is None
    assert relay.current_state()["estop"] is True
