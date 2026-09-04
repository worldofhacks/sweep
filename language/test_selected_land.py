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


@pytest.mark.parametrize(
    ("async_completion", "lagging_same_ms"), [(False, False), (True, False), (True, True)]
)
def test_selected_land_waits_for_landed_telemetry_and_leaves_other_aircraft_airborne(
    tmp_path, monkeypatch, async_completion, lagging_same_ms
):
    snapshot = replace(make_snapshot(3), selection=(1, 2))
    current = [snapshot]
    now = [snapshot.now_ms]
    controller, _, _, _, flight, _ = make_stack(snapshot)
    if async_completion:
        land = flight.land

        def accepted_land(ids):
            return tuple(replace(ack, status=LifecycleStatus.ACCEPTED) for ack in land(ids))

        monkeypatch.setattr(flight, "land", accepted_land)
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
                    {"name": "land", "args": {}, "selection": [1, 2], "mode": "indoor"},
                    {"name": "select", "args": {"ids": [3]}, "selection": [3], "mode": "indoor"},
                ],
            }
        ),
        audit=InMemoryAuditSink(),
    ).compile(
        "Land the selected drones, then select drone three.",
        relay.current_state(),
        capability_version="test",
        rooms=(),
        now_ms=now[0],
    )
    assert plan is not None
    pending = ConfirmedPlan(plan, session=relay.session_id, audit=InMemoryAuditSink())
    emitter = router.relay_emitter(
        relay, Principal(source="console", drone_id=None, signing_key=b"x" * 32)
    )

    def prepare(intent_id):
        return pending.prepare_next(
            relay.current_state(),
            capability_version="test",
            rooms=(),
            now_ms=now[0],
            intent_id=intent_id,
            router=router,
            snapshot=current[0],
        )

    def confirm(intent_id, prepared):
        return pending.confirm_next(
            relay.current_state(),
            capability_version="test",
            rooms=(),
            now_ms=now[0],
            intent_id=intent_id,
            emit=emitter,
            prepared=prepared,
        )

    prepared = prepare("selected-land")
    confirm("selected-land", prepared)
    if async_completion:
        assert [call.drone_ids for call in flight.calls] == [(1,)]
        now[0] += 1
        current[0] = replace(current[0], now_ms=now[0])
        if lagging_same_ms:
            for drone_id in (1, 2):
                drone = next(
                    d for d in relay.current_state()["drones"] if d["drone_id"] == drone_id
                )
                events = relay.process_frame(
                    {
                        **drone["telemetry"],
                        "v": 1,
                        "t": now[0],
                        "type": "telemetry",
                        "session": relay.session_id,
                        "drone": drone_id,
                        "connection_epoch": 1,
                        "event_id": f"lagging-{drone_id}",
                        "state": "hovering",
                    },
                    Principal(source="adapter", drone_id=drone_id, signing_key=b"x" * 32),
                )
                assert not any(event["type"] == "refusal" for event in events)
        for command in prepared.execution.plan.commands:
            events = relay.process_frame(
                {
                    "v": 1,
                    "t": now[0],
                    "type": "acknowledgement",
                    "event_id": f"land-complete-{command.drone_id}",
                    "session": relay.session_id,
                    "intent_id": "selected-land",
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
    assert [(call.operation.value, call.drone_ids) for call in flight.calls] == [
        ("land", (1,)),
        ("land", (2,)),
    ]
    with pytest.raises(ConfirmationError, match="awaiting"):
        prepare("select-three")
    now[0] += 1
    current[0] = replace(current[0], now_ms=now[0])
    for drone_id in (1, 2):
        drone = next(d for d in relay.current_state()["drones"] if d["drone_id"] == drone_id)
        events = relay.process_frame(
            {
                **drone["telemetry"],
                "v": 1,
                "t": now[0],
                "type": "telemetry",
                "session": relay.session_id,
                "drone": drone_id,
                "connection_epoch": 1,
                "event_id": f"landed-{drone_id}",
                "state": "landed",
                "z": 0.0,
            },
            Principal(source="adapter", drone_id=drone_id, signing_key=b"x" * 32),
        )
        assert not any(event["type"] == "refusal" for event in events)
        aircraft = current[0].aircraft[drone_id]
        current[0] = replace_aircraft(
            current[0],
            drone_id,
            flight_state=FlightState.LANDED,
            pose=Position(aircraft.pose.x, 0.0, 0.0),
            position_last_seen_ms=now[0],
            link_last_seen_ms=now[0],
        )
    confirm("select-three", prepare("select-three"))
    assert relay.current_state()["selection"] == [3]
    assert (
        next(d for d in relay.current_state()["drones"] if d["drone_id"] == 3)["flight_state"]
        == "hovering"
    )
    assert [(call.operation.value, call.drone_ids) for call in flight.calls] == [
        ("land", (1,)),
        ("land", (2,)),
    ]
