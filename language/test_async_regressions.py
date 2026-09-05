from dataclasses import replace

import pytest

from evals.language_corpus import StaticResponseTransport
from language.compiler import ConfirmedPlan, InMemoryAuditSink, TranscriptCompiler
from language.test_compiler import _case, _hydrate_relay_from_snapshot, _snapshot_at
from planner.controller import PreparedExecutionRouter
from planner.models import FlightState, LifecycleStatus, Position
from relay.audit import SessionAuditLog
from relay.auth import Principal
from relay.session import RelayLimits, RelaySession
from tests.autonomy_fixtures import make_snapshot, make_stack, replace_aircraft


@pytest.mark.parametrize("count", [1, 2])
def test_authenticated_takeoff_completions_resume_dispatch_and_confirm_airborne(
    tmp_path, monkeypatch, count
):
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
                    }
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
    for index, command in enumerate(commands):
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
                "status": "completed",
                "drone_id": drone_id,
                "connection_epoch": 1,
                "roster_version": snapshot.roster_version,
                "reason": None,
                "detail": None,
            },
            principal,
        )
        overall = [event for event in events if event.get("source") == "autonomy"]
        assert len(overall) == 1, events
        assert overall[0]["status"] == ("completed" if index == count - 1 else "executing"), events
        assert len(flight.calls) == min(index + 2, count)
    pending.acknowledge(
        overall[0],
        relay.current_state(),
        capability_version=case.capability_version,
        rooms=case.rooms,
        now_ms=case.now_ms,
    )
    assert relay.current_state()["accepted_plan"] is None
