from dataclasses import replace

import pytest

from language.test_compiler import _hydrate_relay_from_snapshot
from planner.controller import PreparedExecutionRouter
from planner.models import LifecycleStatus, PreparedExecution
from relay.audit import SessionAuditLog
from relay.auth import Principal
from relay.intent_v1 import IntentName
from relay.session import RelayLimits, RelaySession
from relay.tests.conftest import membership_payload
from tests.autonomy_fixtures import make_aircraft, make_intent, make_snapshot, make_stack


@pytest.mark.parametrize("unenriched_newcomer", [False, True])
def test_rejoining_waiting_aircraft_retires_old_execution_without_resending_motion(
    tmp_path, monkeypatch, unenriched_newcomer
):
    snapshot = make_snapshot(2, roster_version=4)
    controller, _, _, _, flight, _ = make_stack(snapshot)
    goto = flight.goto
    monkeypatch.setattr(
        flight, "goto", lambda *a, **kw: replace(goto(*a, **kw), status=LifecycleStatus.ACCEPTED)
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
    intent = make_intent(IntentName.TRANSLATE, args={"dx": 1, "dy": 0})
    prepared = router.prepare(intent, snapshot)
    assert isinstance(prepared, PreparedExecution)
    router.bind(prepared)
    emitter = router.relay_emitter(
        relay, Principal(source="console", drone_id=None, signing_key=b"x" * 32)
    )
    assert emitter(intent)[-1]["status"] == "executing"
    if unenriched_newcomer:
        newcomer = Principal(source="adapter", drone_id=3, signing_key=b"x" * 32)
        relay.process_frame(
            membership_payload(
                action="join",
                event_id="unenriched-join",
                timestamp=snapshot.now_ms,
                drone_id=3,
                session=relay.session_id,
                key=newcomer.signing_key,
            ),
            newcomer,
        )
        assert intent.intent_id in router._running
    first = prepared.plan.commands[0]
    principal = Principal(source="adapter", drone_id=first.drone_id, signing_key=b"x" * 32)
    events = relay.handle_adapter_disconnect(
        drone_id=first.drone_id, connection_epoch=first.connection_epoch
    )
    assert any(
        event.get("intent_id") == intent.intent_id and event.get("status") == "invalidated"
        for event in events
    )
    assert intent.intent_id not in router._running
    assert relay.current_state()["accepted_plan"] is None
    relay.process_frame(
        membership_payload(
            action="join",
            event_id="rejoin",
            timestamp=snapshot.now_ms,
            drone_id=first.drone_id,
            session=relay.session_id,
            key=principal.signing_key,
        ),
        principal,
    )
    raw = {
        "v": 1,
        "type": "acknowledgement",
        "t": snapshot.now_ms,
        "event_id": "old-completion",
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
    assert relay.process_frame(raw, principal)[0]["reason"] == "stale_connection_epoch"
    relay.process_frame(
        {**raw, "event_id": "new-epoch-completion", "connection_epoch": 2}, principal
    )
    assert [call.operation.value for call in flight.calls] == ["goto", "hover"]
    assert not router._running
    assert relay.current_state()["accepted_plan"] is None
    if unenriched_newcomer:
        return
    state = relay.current_state()
    select = replace(
        make_intent(IntentName.SELECT, args={"ids": (1,)}, selection=(1,)),
        intent_id="select-after-rejoin",
    )
    snapshot = replace(
        snapshot,
        roster_version=state["roster_version"],
        selection=(1,),
        aircraft={
            drone_id: replace(aircraft, connection_epoch=2)
            if drone_id == first.drone_id
            else aircraft
            for drone_id, aircraft in snapshot.aircraft.items()
        },
    )
    snapshot = router._relay_snapshot(relay)
    prepared_select = router.prepare(select, snapshot)
    assert isinstance(prepared_select, PreparedExecution)
    router.bind(prepared_select)
    events = emitter(select)
    assert events[-1]["status"] == "completed", events[-1].get("detail")


def test_membership_change_replaces_waiting_estop_with_owned_current_roster_stop(
    tmp_path, monkeypatch
):
    snapshot = make_snapshot(2, roster_version=4)
    controller, _, _, _, flight, _ = make_stack(snapshot)
    estop = flight.estop
    monkeypatch.setattr(
        flight,
        "estop",
        lambda *args, **kwargs: tuple(
            replace(ack, status=LifecycleStatus.ACCEPTED) for ack in estop(*args, **kwargs)
        ),
    )
    unavailable = False

    def enrichment():
        if unavailable:
            raise RuntimeError("membership changed during enrichment outage")
        return snapshot

    router = PreparedExecutionRouter(controller, current_snapshot=enrichment)
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
    intent = make_intent(IntentName.ESTOP, intent_id="original-estop")
    prepared = router.prepare(intent, snapshot)
    assert isinstance(prepared, PreparedExecution)
    router.bind(prepared)
    assert emitter(intent)[-1]["status"] == "executing"
    unavailable = True
    events = relay.handle_adapter_disconnect(drone_id=1, connection_epoch=1)
    assert any(
        event.get("intent_id") == intent.intent_id and event.get("status") == "invalidated"
        for event in events
    )
    assert intent.intent_id not in router._running
    active = relay.current_state()["accepted_plan"]
    assert active["intent_name"] == "estop"
    command = active["commands"][0]
    assert command["drone_id"] == 2
    assert command["roster_version"] == relay.current_state()["roster_version"]
    assert active["intent_id"] in router._running
    assert [(call.operation.value, call.drone_ids) for call in flight.calls] == [
        ("estop", (1, 2)),
        ("estop", (1, 2)),
    ]
    unavailable = False
    events = relay.process_frame(
        {
            "v": 1,
            "type": "acknowledgement",
            "t": snapshot.now_ms,
            "event_id": "replacement-stop-completed",
            "session": relay.session_id,
            "intent_id": active["intent_id"],
            "command_id": command["command_id"],
            "status": "completed",
            "drone_id": command["drone_id"],
            "connection_epoch": command["connection_epoch"],
            "roster_version": command["roster_version"],
            "reason": None,
            "detail": None,
        },
        Principal(source="adapter", drone_id=2, signing_key=b"x" * 32),
    )
    assert events[-1]["status"] == "completed", events
    assert not router._running
    assert relay.current_state()["estop"] is True


@pytest.mark.parametrize("enrich_newcomer", [False, True])
def test_newcomer_join_preserves_running_motion_until_next_dispatch(
    tmp_path, monkeypatch, enrich_newcomer
):
    snapshot = make_snapshot(2, roster_version=4)
    controller, _, _, _, flight, _ = make_stack(snapshot)
    goto = flight.goto
    monkeypatch.setattr(
        flight, "goto", lambda *a, **kw: replace(goto(*a, **kw), status=LifecycleStatus.ACCEPTED)
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
    intent = make_intent(IntentName.TRANSLATE, args={"dx": 1, "dy": 0})
    prepared = router.prepare(intent, snapshot)
    assert isinstance(prepared, PreparedExecution)
    router.bind(prepared)
    emitter = router.relay_emitter(
        relay, Principal(source="console", drone_id=None, signing_key=b"x" * 32)
    )
    assert emitter(intent)[-1]["status"] == "executing"
    before = relay.current_state()
    if enrich_newcomer:
        snapshot = replace(snapshot, aircraft={**snapshot.aircraft, 3: make_aircraft(3)})
    newcomer = Principal(source="adapter", drone_id=3, signing_key=b"x" * 32)
    events = relay.process_frame(
        membership_payload(
            action="join",
            event_id="benign-join",
            timestamp=snapshot.now_ms,
            drone_id=3,
            session=relay.session_id,
            key=newcomer.signing_key,
        ),
        newcomer,
    )
    after = relay.current_state()
    assert after["roster_version"] > before["roster_version"]
    assert after["accepted_plan"] == before["accepted_plan"]
    assert after["selection"] == before["selection"]
    assert [call.operation.value for call in flight.calls] == ["goto"]
    assert not any(event.get("status") == "invalidated" for event in events)
    first = prepared.plan.commands[0]
    events = relay.process_frame(
        {
            "v": 1,
            "type": "acknowledgement",
            "t": snapshot.now_ms,
            "event_id": "completion-after-join",
            "session": relay.session_id,
            "intent_id": intent.intent_id,
            "command_id": first.command_id,
            "status": "completed",
            "drone_id": first.drone_id,
            "connection_epoch": first.connection_epoch,
            "roster_version": first.roster_version,
            "reason": None,
            "detail": None,
        },
        Principal(source="adapter", drone_id=first.drone_id, signing_key=b"x" * 32),
    )
    assert any(
        event.get("intent_id") == intent.intent_id and event.get("status") == "invalidated"
        for event in events
    )
    assert intent.intent_id not in router._running
    assert sum(call.operation.value == "goto" for call in flight.calls) == 1
