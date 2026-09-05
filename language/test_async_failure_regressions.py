from dataclasses import replace

import pytest

from evals.language_corpus import StaticResponseTransport
from language.compiler import (
    ConfirmationError,
    ConfirmedPlan,
    InMemoryAuditSink,
    TranscriptCompiler,
)
from language.test_compiler import _case, _hydrate_relay_from_snapshot, _snapshot_at
from planner.controller import PreparedExecutionRouter
from planner.models import FlightState, LifecycleStatus, Position
from relay.audit import SessionAuditLog
from relay.auth import Principal
from relay.session import RelayLimits, RelaySession
from tests.autonomy_fixtures import make_snapshot, make_stack, replace_aircraft


@pytest.mark.parametrize("status", ["failed", "invalidated"])
@pytest.mark.parametrize("failure_index", [0, 1])
def test_authenticated_failure_holds_affected_aircraft_and_closes_ordered_plan(
    tmp_path, monkeypatch, status, failure_index
):
    count = 2
    case = _case("ordered-select-and-takeoff")
    snapshot = _snapshot_at(
        make_snapshot(count, flight_state=FlightState.ARMED, roster_version=count * 2),
        case.now_ms,
    )
    for drone_id in snapshot.aircraft:
        aircraft = snapshot.aircraft[drone_id]
        snapshot = replace_aircraft(
            snapshot, drone_id, pose=Position(aircraft.pose.x, aircraft.pose.y, 0.0)
        )
    controller, _, _, _, flight, _ = make_stack(snapshot)
    takeoff = flight.takeoff

    def accepted_takeoff(ids, z):
        return tuple(replace(ack, status=LifecycleStatus.ACCEPTED) for ack in takeoff(ids, z))

    monkeypatch.setattr(flight, "takeoff", accepted_takeoff)
    router = PreparedExecutionRouter(controller, current_snapshot=lambda: snapshot)
    relay = RelaySession(
        session_id="language-eval",
        audit_log=SessionAuditLog(tmp_path, "language-eval"),
        limits=RelayLimits(5_000, 5_000, 1_000, 1_000),
        clock=lambda: case.now_ms,
        intent_sink=router,
    )
    _hydrate_relay_from_snapshot(relay, snapshot)
    state = relay.current_state()
    _, plan = TranscriptCompiler(
        StaticResponseTransport(
            {
                "kind": "plan",
                "intents": [
                    {
                        "name": "takeoff",
                        "args": {},
                        "selection": list(snapshot.selection),
                        "mode": "indoor",
                    },
                    {
                        "name": "hold",
                        "args": {},
                        "selection": list(snapshot.selection),
                        "mode": "indoor",
                    },
                ],
            }
        ),
        audit=InMemoryAuditSink(),
    ).compile(
        "Take off.",
        state,
        capability_version=case.capability_version,
        rooms=case.rooms,
        now_ms=case.now_ms,
    )
    assert plan is not None
    pending = ConfirmedPlan(plan, session="language-eval", audit=InMemoryAuditSink())
    prepared = pending.prepare_next(
        state,
        capability_version=case.capability_version,
        rooms=case.rooms,
        now_ms=case.now_ms,
        intent_id="async-takeoff",
        router=router,
        snapshot=snapshot,
    )
    pending.confirm_next(
        state,
        capability_version=case.capability_version,
        rooms=case.rooms,
        now_ms=case.now_ms,
        intent_id="async-takeoff",
        prepared=prepared,
        emit=router.relay_emitter(
            relay, Principal(source="console", drone_id=None, signing_key=b"x" * 32)
        ),
    )
    assert [call.drone_ids for call in flight.calls] == [(1,)]
    commands = relay.current_state()["accepted_plan"]["commands"]
    for index, command in enumerate(commands[: failure_index + 1]):
        drone_id = command["drone_id"]
        principal = Principal(source="adapter", drone_id=drone_id, signing_key=b"x" * 32)
        telemetry = dict(
            next(
                drone for drone in relay.current_state()["drones"] if drone["drone_id"] == drone_id
            )["telemetry"]
        )
        telemetry.update(
            v=1,
            type="telemetry",
            session=relay.session_id,
            drone=drone_id,
            connection_epoch=1,
            event_id=f"airborne-{drone_id}",
            state="airborne",
            z=1.0,
        )
        events = relay.process_frame(telemetry, principal)
        assert not any(event["type"] == "refusal" for event in events), events
        events = relay.process_frame(
            {
                "v": 1,
                "t": case.now_ms,
                "type": "acknowledgement",
                "event_id": f"complete-{drone_id}",
                "session": relay.session_id,
                "intent_id": "async-takeoff",
                "command_id": command["command_id"],
                "status": status if index == failure_index else "completed",
                "drone_id": drone_id,
                "connection_epoch": 1,
                "roster_version": snapshot.roster_version,
                "reason": "adapter_failure" if index == failure_index else None,
                "detail": None,
            },
            principal,
        )
        overall = [event for event in events if event.get("source") == "autonomy"]
        assert len(overall) == 1, events
        if index < failure_index:
            assert overall[0]["status"] == "executing", events
    assert overall[0]["status"] == status, events
    assert overall[0]["reason"] == "adapter_failure", events
    assert [(call.operation.value, call.drone_ids) for call in flight.calls] == [
        *[("takeoff", (drone_id,)) for drone_id in range(1, failure_index + 2)],
        *[("hover", (drone_id,)) for drone_id in range(1, failure_index + 2)],
    ]
    assert relay.current_state()["accepted_plan"] is None
    with pytest.raises(ConfirmationError, match=f"relay returned terminal status {status}"):
        pending.acknowledge(
            overall[0],
            relay.current_state(),
            capability_version=case.capability_version,
            rooms=case.rooms,
            now_ms=case.now_ms,
        )
    with pytest.raises(ConfirmationError):
        pending.prepare_next(
            relay.current_state(),
            capability_version=case.capability_version,
            rooms=case.rooms,
            now_ms=case.now_ms,
            intent_id="forbidden-hold-suffix",
            router=router,
            snapshot=snapshot,
        )


@pytest.mark.parametrize("status", ["failed", "invalidated"])
def test_async_stop_failure_preserves_terminal_status_and_latch(tmp_path, monkeypatch, status):
    snapshot = make_snapshot(2, roster_version=4)
    controller, _, _, _, flight, _ = make_stack(snapshot)
    stop = flight.estop

    def accepted_stop():
        return tuple(replace(ack, status=LifecycleStatus.ACCEPTED) for ack in stop())

    monkeypatch.setattr(flight, "estop", accepted_stop)
    router = PreparedExecutionRouter(controller, current_snapshot=lambda: snapshot)
    relay = RelaySession(
        session_id="language-eval",
        audit_log=SessionAuditLog(tmp_path, "language-eval"),
        limits=RelayLimits(5_000, 5_000, 1_000, 1_000),
        clock=lambda: snapshot.now_ms,
        intent_sink=router,
    )
    _hydrate_relay_from_snapshot(relay, snapshot)
    intent = {
        "v": 1,
        "t": snapshot.now_ms,
        "type": "intent",
        "intent_id": "async-stop",
        "retry_of": None,
        "source": "keyboard",
        "session": relay.session_id,
        "name": "estop",
        "args": {},
        "selection": [],
        "mode": "indoor",
        "confirm": False,
    }
    operator = Principal(source="keyboard", drone_id=None, signing_key=b"x" * 32)
    events = relay.process_frame(intent, operator)
    assert events[0]["status"] == "accepted"
    assert flight.calls == []
    relay.mark_pending_intent_delivered(intent["intent_id"])
    events.extend(relay.execute_pending_intent(intent["intent_id"]))
    assert events[-1]["status"] == "executing", events
    calls_after_stop = list(flight.calls)
    command = relay.current_state()["accepted_plan"]["commands"][0]
    events = relay.process_frame(
        {
            "v": 1,
            "t": snapshot.now_ms,
            "type": "acknowledgement",
            "event_id": "stop-failure",
            "session": relay.session_id,
            "intent_id": "async-stop",
            "command_id": command["command_id"],
            "status": status,
            "drone_id": command["drone_id"],
            "connection_epoch": command["connection_epoch"],
            "roster_version": snapshot.roster_version,
            "reason": "adapter_failure",
            "detail": "stop failed",
        },
        Principal(source="adapter", drone_id=command["drone_id"], signing_key=b"x" * 32),
    )
    overall = [event for event in events if event.get("source") == "autonomy"]
    assert len(overall) == 1, events
    assert overall[0]["status"] == status, events
    assert overall[0]["reason"] == "adapter_failure", events
    assert relay.current_state()["estop"] is True
    assert relay.current_state()["accepted_plan"] is None
    # The keyboard socket carries only the network stop; motion comes from the console.
    console = Principal(source="console", drone_id=None, signing_key=b"x" * 32)
    events = relay.process_frame(
        {
            **intent,
            "intent_id": "motion-after-stop",
            "source": "console",
            "name": "takeoff",
            "selection": [1, 2],
        },
        console,
    )
    assert events[0]["status"] == "accepted"
    relay.mark_pending_intent_delivered("motion-after-stop")
    events.extend(relay.execute_pending_intent("motion-after-stop"))
    assert events[-1]["status"] == "refused", events
    assert flight.calls == calls_after_stop
