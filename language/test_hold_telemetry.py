from dataclasses import replace

import pytest

from evals.language_corpus import StaticResponseTransport
from language.compiler import (
    ConfirmationError,
    ConfirmedPlan,
    InMemoryAuditSink,
    TranscriptCompiler,
)
from language.test_compiler import _hydrate_relay_from_snapshot
from planner.controller import PreparedExecutionRouter
from planner.models import FlightState, LifecycleStatus, Position
from relay.audit import SessionAuditLog
from relay.auth import Principal
from relay.session import RelayLimits, RelaySession
from tests.autonomy_fixtures import make_snapshot, make_stack, replace_aircraft


@pytest.mark.parametrize("postcondition_matches", [True, False])
@pytest.mark.parametrize("async_completion", [False, True])
def test_hold_waits_for_telemetry_before_land_all(
    tmp_path, monkeypatch, postcondition_matches, async_completion
):
    snapshot = make_snapshot(2, flight_state=FlightState.AIRBORNE)
    for drone_id, aircraft in snapshot.aircraft.items():
        snapshot = replace_aircraft(snapshot, drone_id, pose=Position(aircraft.pose.x, 0.0, 1.0))
    current = [snapshot]
    now = [snapshot.now_ms]
    controller, _, _, _, flight, _ = make_stack(snapshot)
    if async_completion:
        hover = flight.hover

        def accepted_hover(ids):
            return tuple(replace(ack, status=LifecycleStatus.ACCEPTED) for ack in hover(ids))

        monkeypatch.setattr(flight, "hover", accepted_hover)
    router = PreparedExecutionRouter(controller, current_snapshot=lambda: current[0])
    relay = RelaySession(
        session_id="language-eval",
        audit_log=SessionAuditLog(tmp_path, "language-eval"),
        limits=RelayLimits(5000, 5000, 1000, 1000),
        clock=lambda: now[0],
        intent_sink=router,
    )
    _hydrate_relay_from_snapshot(relay, snapshot)
    _, plan = TranscriptCompiler(
        StaticResponseTransport(
            {
                "kind": "plan",
                "intents": [
                    {
                        "name": name,
                        "args": {},
                        "selection": [1, 2] if name == "hold" else [],
                        "mode": "indoor",
                    }
                    for name in ("hold", "land_all")
                ],
            }
        ),
        audit=InMemoryAuditSink(),
    ).compile(
        "Hold, then land all.",
        relay.current_state(),
        capability_version="test",
        rooms=(),
        now_ms=now[0],
    )
    assert plan is not None
    audit = InMemoryAuditSink()
    pending = ConfirmedPlan(plan, session=relay.session_id, audit=audit)
    emitter = router.relay_emitter(
        relay, Principal(source="console", drone_id=None, signing_key=b"x" * 32)
    )
    prepared = pending.prepare_next(
        relay.current_state(),
        capability_version="test",
        rooms=(),
        now_ms=now[0],
        intent_id="hold",
        router=router,
        snapshot=current[0],
    )
    pending.confirm_next(
        relay.current_state(),
        capability_version="test",
        rooms=(),
        now_ms=now[0],
        intent_id="hold",
        emit=emitter,
        prepared=prepared,
    )
    if async_completion:
        now[0] += 1
        current[0] = replace(current[0], now_ms=now[0])
        for drone in relay.current_state()["drones"]:
            drone_id = drone["drone_id"]
            telemetry = {
                **drone["telemetry"],
                "v": 1,
                "t": now[0],
                "type": "telemetry",
                "session": relay.session_id,
                "drone": drone_id,
                "connection_epoch": 1,
                "event_id": f"before-completion-{drone_id}",
            }
            relay.process_frame(
                telemetry, Principal(source="adapter", drone_id=drone_id, signing_key=b"x" * 32)
            )
            current[0] = replace_aircraft(
                current[0], drone_id, position_last_seen_ms=now[0], link_last_seen_ms=now[0]
            )
        now[0] += 1
        for command in prepared.execution.plan.commands:
            events = relay.process_frame(
                {
                    "v": 1,
                    "t": now[0],
                    "type": "acknowledgement",
                    "event_id": f"completion-{command.drone_id}",
                    "session": relay.session_id,
                    "intent_id": "hold",
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
        outcome = next(event for event in events if event.get("source") == "autonomy")
        assert outcome["status"] == "completed"
        pending.acknowledge(
            outcome, relay.current_state(), capability_version="test", rooms=(), now_ms=now[0]
        )
    assert [call.operation.value for call in flight.calls] == ["hover", "hover"]
    assert not any(event["event"] == "intent_accepted" for event in audit.records)
    with pytest.raises(ConfirmationError, match="awaiting"):
        pending.prepare_next(
            relay.current_state(),
            capability_version="test",
            rooms=(),
            now_ms=now[0],
            intent_id="land_all",
            router=router,
            snapshot=current[0],
        )
    now[0] += 1
    current[0] = replace(current[0], now_ms=now[0])
    for drone_id in snapshot.aircraft:
        telemetry = dict(
            next(
                drone for drone in relay.current_state()["drones"] if drone["drone_id"] == drone_id
            )["telemetry"]
        )
        state = "hovering" if postcondition_matches else "airborne"
        z = 1.0
        telemetry.update(
            v=1,
            t=now[0],
            type="telemetry",
            session=relay.session_id,
            drone=drone_id,
            connection_epoch=1,
            event_id=f"fresh-{drone_id}",
            state=state,
            z=z,
        )
        events = relay.process_frame(
            telemetry, Principal(source="adapter", drone_id=drone_id, signing_key=b"x" * 32)
        )
        assert not any(event["type"] == "refusal" for event in events)
        aircraft = current[0].aircraft[drone_id]
        current[0] = replace_aircraft(
            current[0],
            drone_id,
            flight_state=FlightState(state),
            pose=Position(aircraft.pose.x, 0.0, z),
            position_last_seen_ms=now[0],
            link_last_seen_ms=now[0],
        )
    if not postcondition_matches:
        with pytest.raises(ConfirmationError, match="flight state or position"):
            pending.prepare_next(
                relay.current_state(),
                capability_version="test",
                rooms=(),
                now_ms=now[0],
                intent_id="land_all",
                router=router,
                snapshot=current[0],
            )
        with pytest.raises(ConfirmationError, match="closed"):
            pending.prepare_next(
                relay.current_state(),
                capability_version="test",
                rooms=(),
                now_ms=now[0],
                intent_id="land_all",
                router=router,
                snapshot=current[0],
            )
        assert len(flight.calls) == 2
        return
    prepared = pending.prepare_next(
        relay.current_state(),
        capability_version="test",
        rooms=(),
        now_ms=now[0],
        intent_id="land_all",
        router=router,
        snapshot=current[0],
    )
    pending.confirm_next(
        relay.current_state(),
        capability_version="test",
        rooms=(),
        now_ms=now[0],
        intent_id="land_all",
        emit=emitter,
        prepared=prepared,
    )
    assert [call.operation.value for call in flight.calls] == [
        "hover",
        "hover",
        "land",
        "land",
    ]
    assert [
        event["intent_id"] for event in audit.records if event["event"] == "intent_accepted"
    ] == ["hold"]
