"""Safety gates across device classes: which bounds change, which stay, and what refuses."""

from dataclasses import replace

import pytest

from arbiter.safety import SafetyArbiter
from planner.models import (
    Command,
    CommandOperation,
    DriveState,
    FleetSnapshot,
    FlightState,
    HoldScope,
    Plan,
    Position,
    Refusal,
    RefusalReason,
)
from planner.planner import DeterministicPlanner
from relay.capabilities import C2_CAPABILITY_PROFILE
from relay.intent_v1 import IntentName
from tests.autonomy_fixtures import (
    NOW_MS,
    make_intent,
    make_mixed_snapshot,
    planning_config,
    replace_aircraft,
    safety_config,
)

GROUND_ID = 11


def _arbiter() -> SafetyArbiter:
    return SafetyArbiter(safety_config())


def _command_plan(
    snapshot: FleetSnapshot,
    drone_id: int,
    operation: CommandOperation,
    parameters: dict[str, object] | None = None,
    *,
    intent_name: IntentName = IntentName.TRANSLATE,
) -> tuple[Plan, Command]:
    command = Command(
        command_id="plan:class:command:0001",
        intent_id="class",
        roster_version=snapshot.roster_version,
        drone_id=drone_id,
        connection_epoch=snapshot.aircraft[drone_id].connection_epoch,
        operation=operation,
        parameters=parameters or {},
    )
    plan = Plan(
        plan_id="plan:class",
        intent_id="class",
        intent_name=intent_name,
        roster_version=snapshot.roster_version,
        selection=snapshot.selection,
        confirmed=True,
        commands=(command,),
    )
    return plan, command


def _goto(
    snapshot: FleetSnapshot,
    drone_id: int,
    *,
    x: float,
    y: float,
    z: float,
    speed: float = 0.3,
) -> Refusal | None:
    plan, command = _command_plan(
        snapshot,
        drone_id,
        CommandOperation.GOTO,
        {"x": x, "y": y, "z": z, "speed": speed},
    )
    return _arbiter().check_command(plan, command, snapshot)


@pytest.mark.parametrize(
    "intent_name",
    [
        IntentName.TAKEOFF,
        IntentName.LAND,
        IntentName.ALTITUDE,
        IntentName.SWEEP,
        IntentName.CAPTURE_ROOM,
    ],
)
def test_the_class_gate_decides_before_any_flight_state_gate(intent_name: IntentName) -> None:
    """A docked ground vehicle fails several aircraft gates; the class refusal is the one."""
    snapshot = make_mixed_snapshot(
        aircraft_ids=(), ground_ids=(GROUND_ID,), drive_state=DriveState.DOCKED
    )
    args: dict[str, object] = {}
    if intent_name is IntentName.ALTITUDE:
        args = {"delta": 1}
    elif intent_name is IntentName.CAPTURE_ROOM:
        args = {"capture_id": "c-1", "room_id": "r-1", "pattern": "pano_360"}
    intent = make_intent(intent_name, selection=snapshot.selection, args=args, confirm=True)

    refusal = _arbiter().check_intent(intent, snapshot)

    assert refusal is not None
    assert refusal.reason is RefusalReason.UNSUPPORTED_FOR_DEVICE_CLASS
    assert refusal.drone_id == GROUND_ID


def test_land_all_without_an_aircraft_in_the_roster_is_refused_by_class() -> None:
    snapshot = make_mixed_snapshot(aircraft_ids=())
    intent = make_intent(IntentName.LAND_ALL, selection=(), confirm=True)

    refusal = _arbiter().check_intent(intent, snapshot)

    assert refusal is not None
    assert refusal.reason is RefusalReason.UNSUPPORTED_FOR_DEVICE_CLASS


def test_land_all_in_a_mixed_roster_still_reaches_the_aircraft() -> None:
    snapshot = make_mixed_snapshot()
    intent = make_intent(IntentName.LAND_ALL, selection=(), confirm=True)

    assert _arbiter().check_intent(intent, snapshot) is None


@pytest.mark.parametrize("operation", sorted(set(CommandOperation) - {CommandOperation.ESTOP}))
def test_a_ground_vehicle_accepts_only_its_four_operations(
    operation: CommandOperation,
) -> None:
    snapshot = make_mixed_snapshot(aircraft_ids=(), ground_ids=(GROUND_ID,))
    supported = {CommandOperation.GOTO, CommandOperation.ROTATE_TO, CommandOperation.HOVER}
    parameters: dict[str, object] = {}
    if operation is CommandOperation.GOTO:
        parameters = {"x": 0.0, "y": 6.0, "z": 0.0, "speed": 0.3}
    elif operation is CommandOperation.ROTATE_TO:
        parameters = {"yaw": 90.0, "speed": 45.0, "tolerance": 1.0, "min_overlap": 10.0}
    elif operation is CommandOperation.TAKEOFF:
        parameters = {"z": 1.0}
    plan, command = _command_plan(snapshot, GROUND_ID, operation, parameters)

    refusal = _arbiter().check_command(plan, command, snapshot)

    if operation in supported:
        assert refusal is None or refusal.reason is not RefusalReason.UNSUPPORTED_FOR_DEVICE_CLASS
    else:
        assert refusal is not None
        assert refusal.reason is RefusalReason.UNSUPPORTED_FOR_DEVICE_CLASS
        assert refusal.detail == (
            f"{operation.value} is not supported for device class ground_vehicle"
        )


def test_the_ceiling_bounds_an_aircraft_and_not_a_ground_vehicle() -> None:
    snapshot = make_mixed_snapshot(ground_ids=(GROUND_ID,))
    above_ceiling = safety_config().ceiling_m + 0.5

    aircraft = _goto(snapshot, 1, x=0.0, y=0.0, z=above_ceiling, speed=0.5)
    ground = _goto(snapshot, GROUND_ID, x=0.0, y=6.0, z=above_ceiling)

    assert aircraft is not None and aircraft.reason is RefusalReason.CEILING
    assert ground is None, "a ground vehicle drives on the floor; z does not bound it"


def test_the_geofence_bounds_a_ground_vehicle_in_x_and_y_only() -> None:
    snapshot = make_mixed_snapshot(aircraft_ids=(), ground_ids=(GROUND_ID,))
    geofence = safety_config().geofence

    outside = _goto(snapshot, GROUND_ID, x=geofence.max_x + 1.0, y=6.0, z=0.0)
    below = _goto(snapshot, GROUND_ID, x=0.0, y=6.0, z=geofence.min_z - 1.0)
    inside = _goto(snapshot, GROUND_ID, x=1.0, y=6.0, z=0.0)

    assert outside is not None and outside.reason is RefusalReason.GEOFENCE
    assert below is None, "the z bound is an aircraft bound"
    assert inside is None


def test_spacing_is_checked_within_a_class_and_not_across_one() -> None:
    snapshot = make_mixed_snapshot(aircraft_ids=(1,), ground_ids=(11, 12))
    snapshot = replace_aircraft(snapshot, 1, pose=Position(0.0, 0.0, 0.5))
    snapshot = replace_aircraft(snapshot, 11, pose=Position(3.0, 0.0, 0.0))
    snapshot = replace_aircraft(snapshot, 12, pose=Position(0.0, 3.0, 0.0))
    minimum = safety_config().min_spacing_m

    under_the_aircraft = _goto(snapshot, 12, x=0.0, y=0.3, z=0.0)
    beside_the_robot = _goto(snapshot, 12, x=3.3, y=0.0, z=0.0)

    assert Position(0.0, 0.3, 0.0).distance_to(snapshot.aircraft[1].pose) < minimum
    assert under_the_aircraft is None, "an aircraft overhead does not bound a ground vehicle"
    assert beside_the_robot is not None
    assert beside_the_robot.reason is RefusalReason.SPACING
    assert "11" in beside_the_robot.detail


def test_a_planned_drive_speed_above_the_ground_maximum_is_refused() -> None:
    snapshot = make_mixed_snapshot(aircraft_ids=(), ground_ids=(GROUND_ID,))
    maximum = safety_config().ground_max_speed_m_s

    too_fast = _goto(snapshot, GROUND_ID, x=0.5, y=6.0, z=0.0, speed=maximum + 0.1)
    at_the_cap = _goto(snapshot, GROUND_ID, x=0.5, y=6.0, z=0.0, speed=maximum)

    assert too_fast is not None and too_fast.reason is RefusalReason.SPEED_LIMIT
    assert at_the_cap is None


def test_ground_spacing_ignores_height_just_like_the_ground_geofence() -> None:
    snapshot = make_mixed_snapshot(aircraft_ids=(), ground_ids=(11, 12))
    snapshot = replace_aircraft(snapshot, 12, pose=Position(2.0, 6.0, 4.0))

    refusal = _goto(snapshot, 11, x=2.0, y=6.0, z=0.0)

    assert refusal is not None and refusal.reason is RefusalReason.SPACING


def test_an_empty_operator_hold_cannot_claim_to_stop_a_ground_only_fleet() -> None:
    snapshot = make_mixed_snapshot(aircraft_ids=(), selection=())
    plan = Plan(
        plan_id="plan:empty-hold",
        intent_id="empty-hold",
        intent_name=IntentName.HOLD,
        roster_version=snapshot.roster_version,
        selection=(),
        confirmed=False,
        commands=(),
        hold_scope=HoldScope.OPERATOR_SELECTION,
    )

    refusal = _arbiter().check_plan(plan, snapshot)

    assert refusal is not None and refusal.reason is RefusalReason.INVALID_PLAN


def test_the_flight_speed_is_not_capped_by_the_ground_maximum() -> None:
    snapshot = make_mixed_snapshot(aircraft_ids=(1,), ground_ids=())
    fast = safety_config().ground_max_speed_m_s + 1.0

    assert _goto(snapshot, 1, x=0.5, y=0.0, z=1.0, speed=fast) is None


@pytest.mark.parametrize(
    ("drive_state", "refused"),
    [
        (DriveState.IDLE, False),
        (DriveState.MOVING, False),
        (DriveState.STOPPED, False),
        (DriveState.DOCKED, True),
        (DriveState.FAULT, True),
    ],
)
def test_motion_and_hold_gates_read_the_drive_state(drive_state: DriveState, refused: bool) -> None:
    snapshot = make_mixed_snapshot(
        aircraft_ids=(), ground_ids=(GROUND_ID,), drive_state=drive_state
    )
    plan, hover = _command_plan(
        snapshot, GROUND_ID, CommandOperation.HOVER, intent_name=IntentName.HOLD
    )
    hold = make_intent(IntentName.HOLD, selection=snapshot.selection)

    goto_refusal = _goto(snapshot, GROUND_ID, x=0.5, y=6.0, z=0.0)
    hover_refusal = _arbiter().check_command(plan, hover, snapshot)
    intent_refusal = _arbiter().check_intent(hold, snapshot)

    if refused:
        assert goto_refusal is not None and goto_refusal.reason is RefusalReason.INVALID_STATE
        assert goto_refusal.detail == "goto requires an undocked ground vehicle"
        assert hover_refusal is not None and hover_refusal.reason is RefusalReason.INVALID_STATE
        assert intent_refusal is not None and intent_refusal.reason is RefusalReason.INVALID_STATE
        assert intent_refusal.detail == "hold requires an undocked ground vehicle"
    else:
        assert goto_refusal is None
        assert hover_refusal is None
        assert intent_refusal is None


@pytest.mark.parametrize(
    ("drive_state", "refused"),
    [
        (DriveState.DOCKED, False),
        (DriveState.IDLE, False),
        (DriveState.STOPPED, False),
        (DriveState.MOVING, True),
        (DriveState.FAULT, True),
    ],
)
def test_arm_reads_the_drive_state_rather_than_a_flight_state(
    drive_state: DriveState, refused: bool
) -> None:
    snapshot = make_mixed_snapshot(
        aircraft_ids=(), ground_ids=(GROUND_ID,), drive_state=drive_state, armed=False
    )
    intent = make_intent(IntentName.ARM, selection=snapshot.selection)

    refusal = _arbiter().check_intent(intent, snapshot)

    if refused:
        assert refusal is not None and refusal.reason is RefusalReason.INVALID_STATE
        assert refusal.detail == "arm requires a docked, idle, or stopped ground vehicle"
    else:
        assert refusal is None


def test_arm_keeps_the_aircraft_wording_and_gate() -> None:
    snapshot = make_mixed_snapshot(ground_ids=(), flight_state=FlightState.HOVERING, armed=False)
    intent = make_intent(IntentName.ARM, selection=snapshot.selection)

    refusal = _arbiter().check_intent(intent, snapshot)

    assert refusal is not None
    assert refusal.detail == "arm requires a landed and disarmed aircraft"


@pytest.mark.parametrize(
    ("drive_state", "allowed"),
    [
        (DriveState.DOCKED, True),
        (DriveState.IDLE, True),
        (DriveState.STOPPED, True),
        (DriveState.MOVING, False),
        (DriveState.FAULT, False),
    ],
)
def test_disarm_checks_unselected_robots_without_requiring_a_flight_state(
    drive_state: DriveState, allowed: bool
) -> None:
    snapshot = make_mixed_snapshot(
        aircraft_ids=(1,),
        ground_ids=(11,),
        selection=(1,),
        flight_state=FlightState.LANDED,
        drive_state=drive_state,
    )
    planner = DeterministicPlanner(planning_config(), C2_CAPABILITY_PROFILE)
    intent = make_intent(IntentName.DISARM, selection=(1,))
    plan = planner.plan(intent, snapshot)
    assert isinstance(plan, Plan)

    intent_refusal = _arbiter().check_intent(intent, snapshot)
    plan_refusal = _arbiter().check_plan(plan, snapshot)

    if allowed:
        assert intent_refusal is None and plan_refusal is None
        assert plan.armed_update is False and plan.commands == ()
    else:
        for refusal in (intent_refusal, plan_refusal):
            assert refusal is not None and refusal.reason is RefusalReason.INVALID_STATE
            assert "ground vehicle" in refusal.detail


def test_disarm_requires_complete_observation_even_if_all_visible_robots_are_stopped() -> None:
    snapshot = make_mixed_snapshot(aircraft_ids=(), fleet_observation_complete=False)
    intent = make_intent(IntentName.DISARM, selection=snapshot.selection)
    planner = DeterministicPlanner(planning_config(), C2_CAPABILITY_PROFILE)
    plan = planner.plan(intent, snapshot)
    assert isinstance(plan, Plan)

    for refusal in (
        _arbiter().check_intent(intent, snapshot), _arbiter().check_plan(plan, snapshot)
    ):
        assert refusal is not None and refusal.reason is RefusalReason.AIRCRAFT_NOT_READY


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"battery": 0.05}, RefusalReason.BATTERY_CRITICAL),
        ({"link_quality": 0.2}, RefusalReason.LINK_QUALITY),
        ({"link_last_seen_ms": NOW_MS - 2_000}, RefusalReason.LINK_STALE),
        ({"position_quality": 0.2}, RefusalReason.POSITION_QUALITY),
        ({"position_last_seen_ms": NOW_MS - 2_000}, RefusalReason.POSITION_STALE),
        ({"control_authority": False}, RefusalReason.CONTROL_AUTHORITY),
        ({"rc_safety_operator_present": False}, RefusalReason.RC_SAFETY_OPERATOR_ABSENT),
    ],
)
def test_the_shared_gates_apply_unchanged_to_a_ground_vehicle(
    change: dict[str, object], reason: RefusalReason
) -> None:
    snapshot = make_mixed_snapshot(aircraft_ids=(), ground_ids=(GROUND_ID,))
    snapshot = replace_aircraft(snapshot, GROUND_ID, **change)
    intent = make_intent(
        IntentName.TRANSLATE, selection=snapshot.selection, args={"dx": 1, "dy": 0}
    )

    refusal = _arbiter().check_intent(intent, snapshot)

    assert refusal is not None
    assert refusal.reason is reason


def test_a_session_that_is_not_armed_still_refuses_ground_motion() -> None:
    snapshot = make_mixed_snapshot(aircraft_ids=(), ground_ids=(GROUND_ID,), armed=False)
    intent = make_intent(
        IntentName.TRANSLATE, selection=snapshot.selection, args={"dx": 1, "dy": 0}
    )

    refusal = _arbiter().check_intent(intent, snapshot)

    assert refusal is not None
    assert refusal.reason is RefusalReason.ARMED_REQUIRED


def test_a_mixed_translate_plan_passes_the_whole_plan_gate() -> None:
    snapshot = make_mixed_snapshot()
    planner = DeterministicPlanner(planning_config())
    intent = make_intent(
        IntentName.TRANSLATE, selection=snapshot.selection, args={"dx": 1, "dy": 0}
    )

    plan = planner.plan(intent, snapshot)

    assert isinstance(plan, Plan)
    assert _arbiter().check_intent(intent, snapshot) is None
    assert _arbiter().check_plan(plan, snapshot) is None


def test_a_mixed_formation_plan_passes_the_whole_plan_gate() -> None:
    snapshot = make_mixed_snapshot(spacing=1.2)
    snapshot = replace_aircraft(snapshot, 12, pose=Position(2.0, 6.0, 0.0))
    planner = DeterministicPlanner(planning_config(), C2_CAPABILITY_PROFILE)
    intent = make_intent(
        IntentName.FORMATION_SET, selection=snapshot.selection, args={"name": "line"}
    )

    plan = planner.plan(intent, snapshot)

    assert isinstance(plan, Plan)
    assert _arbiter().check_plan(plan, snapshot) is None


def test_formation_next_validates_the_shapes_each_selected_class_can_form() -> None:
    snapshot = make_mixed_snapshot(formation="column")
    planner = DeterministicPlanner(planning_config(), C2_CAPABILITY_PROFILE)
    intent = make_intent(IntentName.FORMATION_NEXT, selection=snapshot.selection)

    plan = planner.plan(intent, snapshot)

    assert isinstance(plan, Plan) and plan.formation_update == "line"
    assert _arbiter().check_plan(plan, snapshot) is None


def test_a_fleet_safety_hold_must_cover_every_mobile_device_of_both_classes() -> None:
    snapshot = make_mixed_snapshot()
    planner = DeterministicPlanner(planning_config())

    hold = planner.emergency_hold_plan(intent_id="safety-1", snapshot=snapshot)
    aircraft_only = replace(hold, commands=hold.commands[:2])

    assert _arbiter().check_plan(hold, snapshot) is None
    refusal = _arbiter().check_plan(aircraft_only, snapshot)
    assert refusal is not None
    assert refusal.reason is RefusalReason.INVALID_PLAN


def test_a_stop_reaches_every_ready_device_of_both_classes() -> None:
    snapshot = make_mixed_snapshot()
    planner = DeterministicPlanner(planning_config())
    intent = make_intent(IntentName.ESTOP, selection=())

    plan = planner.plan(intent, snapshot)

    assert isinstance(plan, Plan)
    assert [command.drone_id for command in plan.commands] == [1, 2, 11, 12]
    assert _arbiter().check_plan(plan, snapshot) is None
