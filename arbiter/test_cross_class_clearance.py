"""Other-class bodies and missing pose evidence cannot disappear from motion clearance."""

from dataclasses import replace

import pytest

from arbiter.safety import SafetyArbiter
from arbiter.test_device_class_safety import _command_plan, _goto
from planner.models import (
    CommandOperation,
    DeviceClass,
    DriveState,
    FleetSnapshot,
    FlightState,
    MembershipState,
    Position,
    RefusalReason,
)
from relay.intent_v1 import IntentName
from tests.autonomy_fixtures import NOW_MS, make_mixed_snapshot, replace_aircraft, safety_config


@pytest.mark.parametrize("flight_state", [FlightState.LANDED, FlightState.HOVERING])
def test_drive_path_cannot_cross_a_stationary_aircraft_body_at_any_height(flight_state):
    snapshot = make_mixed_snapshot(aircraft_ids=(1,), ground_ids=(11,), selection=(11,))
    snapshot = replace_aircraft(snapshot, 1, pose=Position(0, 0, 3), flight_state=flight_state)
    snapshot = replace_aircraft(snapshot, 11, pose=Position(-2, 0, 0))
    refusal = _goto(snapshot, 11, x=2, y=0, z=0)
    assert refusal is not None and refusal.reason is RefusalReason.SPACING


@pytest.mark.parametrize(
    "membership", [MembershipState.READY, MembershipState.DEGRADED, MembershipState.DISCONNECTED]
)
def test_aircraft_path_keeps_clear_of_docked_robot_even_when_it_is_not_selectable(membership):
    snapshot = make_mixed_snapshot(aircraft_ids=(1,), ground_ids=(11,), selection=(1,))
    snapshot = replace_aircraft(snapshot, 1, pose=Position(-2, 0, 3))
    snapshot = replace_aircraft(
        snapshot,
        11,
        pose=Position(0, 0, 0),
        drive_state=DriveState.DOCKED,
        membership=membership,
        armed=False,
    )
    refusal = _goto(snapshot, 1, x=2, y=0, z=3)
    assert refusal is not None and refusal.reason is RefusalReason.SPACING


def test_crossing_planned_paths_refuse_even_when_every_endpoint_has_clearance():
    snapshot = make_mixed_snapshot(aircraft_ids=(1,), ground_ids=(11,))
    snapshot = replace_aircraft(snapshot, 1, pose=Position(-2, 0, 1))
    snapshot = replace_aircraft(snapshot, 11, pose=Position(0, -2, 0))
    plan, aircraft_command = _command_plan(
        snapshot, 1, CommandOperation.GOTO, {"x": 2, "y": 0, "z": 1, "speed": 0.3}
    )
    ground_command = replace(
        aircraft_command,
        command_id="ground-command",
        drone_id=11,
        parameters={"x": 0, "y": 2, "z": 0, "speed": 0.3},
    )
    plan = replace(plan, commands=(aircraft_command, ground_command))
    refusal = SafetyArbiter(safety_config()).check_plan(plan, snapshot)
    assert refusal is not None and refusal.reason is RefusalReason.SPACING


@pytest.mark.parametrize("distance,refused", [(0.95, True), (1.1, False)])
def test_pulse_cross_class_clearance_includes_its_tick_quantized_displacement(distance, refused):
    snapshot = make_mixed_snapshot(aircraft_ids=(1,), ground_ids=(11,), selection=(1,))
    snapshot = replace_aircraft(
        snapshot, 1, pose=Position(0, 0, 3), capabilities=frozenset({"body_pulse_v1"})
    )
    snapshot = replace_aircraft(snapshot, 11, pose=Position(distance, 0, 0))
    plan, command = _command_plan(
        snapshot,
        1,
        CommandOperation.BODY_PULSE,
        {"forward_mm_s": 250, "duration_ms": 500},
        intent_name=IntentName.BODY_PULSE,
    )
    outcome = SafetyArbiter(safety_config()).check_command(plan, command, snapshot)
    if refused:
        assert outcome is not None and outcome.reason is RefusalReason.SPACING
    else:
        assert outcome is None


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"position_quality": 0.0}, RefusalReason.POSITION_QUALITY),
        ({"position_quality": 0.49}, RefusalReason.POSITION_QUALITY),
        ({"position_last_seen_ms": NOW_MS - 1_001}, RefusalReason.POSITION_STALE),
        ({"position_last_seen_ms": NOW_MS + 1_001}, RefusalReason.POSITION_STALE),
        ({"drive_state": DriveState.MOVING}, RefusalReason.INVALID_STATE),
        ({"active_task_id": "unrelated-motion"}, RefusalReason.INVALID_STATE),
    ],
)
def test_unbounded_or_unusable_other_class_pose_refuses_motion_even_far_away(changes, reason):
    snapshot = make_mixed_snapshot(aircraft_ids=(1,), ground_ids=(11,), selection=(1,))
    snapshot = replace_aircraft(snapshot, 11, **changes)
    refusal = _goto(snapshot, 1, x=0.5, y=0, z=1)
    assert refusal is not None and refusal.reason is reason


@pytest.mark.parametrize("known_class", [DeviceClass.GROUND_VEHICLE, None])
def test_unobserved_other_class_blocks_motion_and_survives_snapshot_roundtrip(known_class):
    snapshot = make_mixed_snapshot(
        aircraft_ids=(1,), ground_ids=(), selection=(1,), unobserved_devices={11: known_class}
    )
    restored = FleetSnapshot.from_mapping(snapshot.to_dict())
    assert dict(restored.unobserved_devices) == {11: known_class}
    with pytest.raises(TypeError):
        restored.unobserved_devices[11] = DeviceClass.AIRCRAFT
    refusal = _goto(restored, 1, x=0.5, y=0, z=1)
    assert refusal is not None and refusal.reason is RefusalReason.POSITION_QUALITY


def test_unobserved_aircraft_blocks_ground_motion():
    snapshot = make_mixed_snapshot(
        aircraft_ids=(),
        ground_ids=(11,),
        selection=(11,),
        unobserved_devices={1: DeviceClass.AIRCRAFT},
    )
    refusal = _goto(snapshot, 11, x=0.5, y=6, z=0)
    assert refusal is not None and refusal.reason is RefusalReason.POSITION_QUALITY


def test_stops_and_aircraft_landing_remain_available_with_unknown_cross_class_pose():
    snapshot = make_mixed_snapshot(
        aircraft_ids=(1,),
        ground_ids=(11,),
        selection=(1, 11),
        unobserved_devices={12: DeviceClass.GROUND_VEHICLE},
    )
    from planner.planner import DeterministicPlanner
    from tests.autonomy_fixtures import make_intent, planning_config

    planner = DeterministicPlanner(planning_config())
    arbiter = SafetyArbiter(safety_config())
    for intent_name in (IntentName.HOLD, IntentName.ESTOP, IntentName.LAND_ALL):
        selection = snapshot.selection if intent_name is IntentName.HOLD else ()
        plan = planner.plan(make_intent(intent_name, selection=selection, confirm=True), snapshot)
        assert arbiter.check_plan(plan, snapshot) is None
