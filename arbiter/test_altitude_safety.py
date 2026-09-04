from dataclasses import replace

import pytest

from arbiter.safety import SafetyArbiter
from planner.models import (
    AltitudeGrounding,
    Command,
    CommandOperation,
    FleetSnapshot,
    FlightState,
    Geofence,
    Plan,
    Position,
    RefusalReason,
)
from relay.intent_v1 import IntentName
from tests.autonomy_fixtures import (
    NOW_MS,
    make_intent,
    make_snapshot,
    replace_aircraft,
    safety_config,
)


def altitude_plan(snapshot: FleetSnapshot, targets: tuple[tuple[int, float], ...]) -> Plan:
    commands = []
    for drone_id, z in targets:
        aircraft = snapshot.aircraft[drone_id]
        for operation, parameters in (
            (
                CommandOperation.GOTO,
                {"x": aircraft.pose.x, "y": aircraft.pose.y, "z": z, "speed": 0.5},
            ),
            (CommandOperation.HOVER, {}),
        ):
            commands.append(
                Command(
                    command_id=f"altitude:{len(commands)}",
                    intent_id="altitude",
                    roster_version=snapshot.roster_version,
                    drone_id=drone_id,
                    connection_epoch=aircraft.connection_epoch,
                    operation=operation,
                    parameters=parameters,
                )
            )
    return Plan(
        plan_id="altitude-plan",
        intent_id="altitude",
        intent_name=IntentName.ALTITUDE,
        roster_version=snapshot.roster_version,
        selection=snapshot.selection,
        altitude_grounding=AltitudeGrounding(0.3048, 0.0, "test-floor"),
        confirmed=False,
        commands=tuple(commands),
    )


def test_vertical_path_rejects_intermediate_unselected_aircraft() -> None:
    snapshot = replace_aircraft(make_snapshot(2, selection=(1,)), 2, pose=Position(0, 0, 2))
    plan = altitude_plan(snapshot, ((1, 3),))
    assert snapshot.aircraft[1].pose.distance_to(snapshot.aircraft[2].pose) > 0.8
    assert Position(0, 0, 3).distance_to(snapshot.aircraft[2].pose) > 0.8
    refusal = SafetyArbiter(safety_config()).check_plan(plan, snapshot)
    assert refusal is not None
    assert refusal.reason is RefusalReason.SPACING


def test_vertical_motion_uses_completed_positions_in_selected_order() -> None:
    snapshot = replace_aircraft(make_snapshot(2), 2, pose=Position(0, 0, 2))
    arbiter = SafetyArbiter(safety_config())
    assert arbiter.check_plan(altitude_plan(snapshot, ((2, 3), (1, 2))), snapshot) is None
    refusal = arbiter.check_plan(altitude_plan(snapshot, ((1, 2), (2, 3))), snapshot)
    assert refusal is not None
    assert refusal.reason is RefusalReason.SPACING


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"position_last_seen_ms": NOW_MS - 1001}, RefusalReason.POSITION_STALE),
        ({"position_quality": 0.1}, RefusalReason.POSITION_QUALITY),
        ({"link_last_seen_ms": NOW_MS - 1001}, RefusalReason.LINK_STALE),
    ],
)
def test_vertical_motion_rejects_stale_unselected_evidence(
    change: dict, reason: RefusalReason
) -> None:
    snapshot = replace_aircraft(make_snapshot(2, selection=(1,)), 2, **change)
    refusal = SafetyArbiter(safety_config()).check_plan(
        altitude_plan(snapshot, ((1, 2),)), snapshot
    )
    assert refusal is not None
    assert refusal.reason is reason


def test_altitude_does_not_take_off_grounded_aircraft() -> None:
    snapshot = make_snapshot(1, flight_state=FlightState.LANDED)
    arbiter = SafetyArbiter(safety_config())
    intent = make_intent(IntentName.ALTITUDE, selection=(1,), args={"delta": 1})
    for refusal in (
        arbiter.check_intent(intent, snapshot),
        arbiter.check_plan(altitude_plan(snapshot, ((1, 2),)), snapshot),
    ):
        assert refusal is not None
        assert refusal.reason is RefusalReason.INVALID_STATE


@pytest.mark.parametrize(
    "corruption",
    [
        "missing",
        "duplicate",
        "foreign",
        "reversed",
        "hover_parameters",
        "safety",
        "zero",
        "extra",
    ],
)
def test_altitude_rejects_malformed_pair(corruption: str) -> None:
    snapshot = make_snapshot(2, selection=(1,))
    plan = altitude_plan(snapshot, ((1, 2),))
    goto, hover = plan.commands
    commands = {
        "missing": (goto,),
        "duplicate": (
            goto,
            hover,
            replace(goto, command_id="duplicate"),
            replace(hover, command_id="duplicate-hover"),
        ),
        "foreign": (replace(goto, drone_id=2), replace(hover, drone_id=2)),
        "reversed": (hover, goto),
        "hover_parameters": (goto, replace(hover, parameters={"z": 2})),
        "safety": (goto, replace(hover, safety_action=True)),
        "zero": (replace(goto, parameters={**goto.parameters, "z": 0}), hover),
        "extra": (replace(goto, parameters={**goto.parameters, "unexpected": 1}), hover),
    }[corruption]
    refusal = SafetyArbiter(safety_config()).check_plan(replace(plan, commands=commands), snapshot)
    assert refusal is not None
    assert refusal.reason is RefusalReason.INVALID_PLAN


def test_horizontal_drift_is_refused_at_dispatch_recheck() -> None:
    snapshot = make_snapshot(1)
    plan = altitude_plan(snapshot, ((1, 2),))
    moved = replace_aircraft(snapshot, 1, pose=Position(0.01, 0, 1))
    refusal = SafetyArbiter(safety_config()).check_command(plan, plan.commands[0], moved)
    assert refusal is not None
    assert refusal.reason is RefusalReason.INVALID_PLAN


@pytest.mark.parametrize(
    "start,target,reason",
    [
        (Position(0, 0, 4.5), 2, RefusalReason.CEILING),
        (Position(0, 0, -0.1), 2, RefusalReason.GEOFENCE),
        (Position(0, 0, 1), 4.5, RefusalReason.CEILING),
        (Position(0, 0, 1), 6, RefusalReason.GEOFENCE),
    ],
)
def test_vertical_motion_checks_both_path_endpoints(
    start: Position, target: float, reason: RefusalReason
) -> None:
    snapshot = replace_aircraft(make_snapshot(1), 1, pose=start)
    refusal = SafetyArbiter(safety_config()).check_plan(
        altitude_plan(snapshot, ((1, target),)), snapshot
    )
    assert refusal is not None
    assert refusal.reason is reason


def test_altitude_requires_grounding() -> None:
    snapshot = make_snapshot(1)
    plan = replace(altitude_plan(snapshot, ((1, 2),)), altitude_grounding=None)
    refusal = SafetyArbiter(safety_config()).check_plan(plan, snapshot)
    assert refusal is not None
    assert refusal.reason is RefusalReason.INVALID_PLAN


@pytest.mark.parametrize("target,accepted", [(-0.476, True), (-2.0, False), (-2.1, False)])
def test_altitude_uses_signed_building_floor_reference(target: float, accepted: bool) -> None:
    snapshot = replace_aircraft(make_snapshot(1), 1, pose=Position(0, 0, -1))
    plan = replace(
        altitude_plan(snapshot, ((1, target),)),
        altitude_grounding=AltitudeGrounding(0.3048, -2.0, "lower-floor"),
    )
    arbiter = SafetyArbiter(replace(safety_config(), geofence=Geofence(-10, 10, -10, 10, -3, 5)))
    refusal = arbiter.check_plan(plan, snapshot)
    if accepted:
        assert refusal is None
    else:
        assert refusal is not None
        assert refusal.reason is RefusalReason.INVALID_PLAN
