"""Planning for two device classes: what a ground vehicle executes, and what it refuses."""

from dataclasses import replace

import pytest

from planner.models import (
    AircraftState,
    CommandOperation,
    DeviceClass,
    DriveState,
    FleetSnapshot,
    FlightState,
    HoldScope,
    MembershipState,
    Plan,
    Position,
    Refusal,
    RefusalReason,
)
from planner.planner import AIRCRAFT_ONLY_INTENTS, DeterministicPlanner
from planner.roster import authorize_graceful_removal
from relay.capabilities import C1_IMPLEMENTED_INTENT_NAMES
from relay.intent_v1 import IntentName
from tests.autonomy_fixtures import (
    make_ground_vehicle,
    make_intent,
    make_mixed_snapshot,
    make_snapshot,
    planning_config,
    replace_aircraft,
)

GROUND_IDS = (11, 12)
AIRCRAFT_IDS = (1, 2)


def _planner(**changes: object) -> DeterministicPlanner:
    return DeterministicPlanner(replace(planning_config(), **changes))


def _plan(intent_name: IntentName, snapshot: FleetSnapshot, **intent_changes: object) -> object:
    intent = make_intent(intent_name, selection=snapshot.selection, **intent_changes)
    return _planner().plan(intent, snapshot)


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
def test_an_aircraft_only_intent_on_a_ground_vehicle_is_refused_by_class(
    intent_name: IntentName,
) -> None:
    snapshot = make_mixed_snapshot(aircraft_ids=(), ground_ids=(11,))
    args: dict[str, object] = {}
    if intent_name is IntentName.ALTITUDE:
        args = {"delta": 1}
    elif intent_name is IntentName.CAPTURE_ROOM:
        args = {"capture_id": "c-1", "room_id": "r-1", "pattern": "pano_360"}

    result = _plan(intent_name, snapshot, args=args, confirm=True)

    assert isinstance(result, Refusal)
    assert result.reason is RefusalReason.UNSUPPORTED_FOR_DEVICE_CLASS
    assert result.detail == f"{intent_name.value} is not supported for device class ground_vehicle"
    assert result.drone_id == 11


@pytest.mark.parametrize("intent_name", [IntentName.SURVEY_AREA, IntentName.MAP_AREA])
def test_the_later_area_intents_stay_unsupported_and_are_listed_as_aircraft_only(
    intent_name: IntentName,
) -> None:
    """No capability profile can enable either yet, so the profile gate refuses first.

    Both are in ``AIRCRAFT_ONLY_INTENTS``, so a ground vehicle target earns the typed
    class refusal the moment a profile implements them.
    """
    snapshot = make_mixed_snapshot(aircraft_ids=(), ground_ids=(11,))
    intent = make_intent(intent_name, selection=snapshot.selection, args={"area_id": "floor-1"})

    result = DeterministicPlanner(planning_config()).plan(intent, snapshot)

    assert isinstance(result, Refusal)
    assert result.reason is RefusalReason.UNSUPPORTED
    assert intent_name in AIRCRAFT_ONLY_INTENTS
    assert intent_name not in C1_IMPLEMENTED_INTENT_NAMES


def test_one_ground_vehicle_in_a_mixed_selection_refuses_the_whole_aircraft_intent() -> None:
    snapshot = make_mixed_snapshot(ground_ids=(11,), flight_state=FlightState.LANDED, armed=True)

    result = _plan(IntentName.TAKEOFF, snapshot, confirm=True)

    assert isinstance(result, Refusal)
    assert result.reason is RefusalReason.UNSUPPORTED_FOR_DEVICE_CLASS
    assert result.drone_id == 11


def test_land_all_skips_ground_vehicles_in_a_mixed_roster() -> None:
    snapshot = make_mixed_snapshot()

    result = _plan(IntentName.LAND_ALL, snapshot, confirm=True)

    assert isinstance(result, Plan)
    assert [command.drone_id for command in result.commands] == list(AIRCRAFT_IDS)
    assert {command.operation for command in result.commands} == {CommandOperation.LAND}


def test_land_all_is_refused_by_class_when_the_roster_holds_no_aircraft() -> None:
    snapshot = make_mixed_snapshot(aircraft_ids=())

    result = _plan(IntentName.LAND_ALL, snapshot, confirm=True)

    assert isinstance(result, Refusal)
    assert result.reason is RefusalReason.UNSUPPORTED_FOR_DEVICE_CLASS
    assert result.detail == "land_all is not supported for device class ground_vehicle"
    assert result.drone_id is None


def test_translate_drives_a_ground_vehicle_across_the_floor_plane() -> None:
    snapshot = make_mixed_snapshot(aircraft_ids=(), ground_ids=(11,))

    result = _plan(IntentName.TRANSLATE, snapshot, args={"dx": 1, "dy": 0})

    assert isinstance(result, Plan)
    (command,) = result.commands
    assert command.operation is CommandOperation.GOTO
    assert command.parameters["z"] == 0.0
    assert command.parameters["speed"] == planning_config().drive_speed_m_s
    assert command.parameters["x"] == pytest.approx(0.5)


def test_translate_keeps_the_flight_speed_and_height_for_an_aircraft() -> None:
    snapshot = make_mixed_snapshot()

    result = _plan(IntentName.TRANSLATE, snapshot, args={"dx": 1, "dy": 0})

    assert isinstance(result, Plan)
    speeds = {
        command.drone_id: (command.parameters["speed"], command.parameters["z"])
        for command in result.commands
    }
    config = planning_config()
    assert speeds[1] == (config.flight_speed_m_s, 1.0)
    assert speeds[11] == (config.drive_speed_m_s, 0.0)


def test_aircraft_relative_translation_uses_the_ground_vehicle_heading() -> None:
    snapshot = make_mixed_snapshot(aircraft_ids=(), ground_ids=(11,))
    snapshot = replace_aircraft(snapshot, 11, heading_deg=90.0)
    planner = _planner(translation_frame="aircraft_relative")

    result = planner.plan(
        make_intent(IntentName.TRANSLATE, selection=(11,), args={"dx": 1, "dy": 0}), snapshot
    )

    assert isinstance(result, Plan)
    (command,) = result.commands
    assert command.parameters["x"] == pytest.approx(0.0, abs=1e-9)
    assert command.parameters["y"] == pytest.approx(6.5)
    assert command.parameters["z"] == 0.0


def test_come_home_returns_a_ground_vehicle_to_its_launch_spot_on_the_floor() -> None:
    snapshot = make_mixed_snapshot(aircraft_ids=(), ground_ids=(11,))
    snapshot = replace_aircraft(snapshot, 11, pose=Position(3.0, 6.0, 0.0))

    result = _plan(IntentName.COME_HOME, snapshot)

    assert isinstance(result, Plan)
    (command,) = result.commands
    assert command.parameters["z"] == 0.0, "no climb to the takeoff altitude"
    assert command.parameters["speed"] == planning_config().drive_speed_m_s


def test_hold_and_estop_reach_both_classes() -> None:
    snapshot = make_mixed_snapshot()

    hold = _plan(IntentName.HOLD, snapshot)
    stop = _plan(IntentName.ESTOP, snapshot)

    assert isinstance(hold, Plan) and isinstance(stop, Plan)
    assert hold.hold_scope is HoldScope.OPERATOR_SELECTION
    assert [command.drone_id for command in hold.commands] == [1, 2, 11, 12]
    assert {command.operation for command in hold.commands} == {CommandOperation.HOVER}
    assert [command.drone_id for command in stop.commands] == [1, 2, 11, 12]
    assert {command.operation for command in stop.commands} == {CommandOperation.ESTOP}


def test_a_formation_places_each_class_around_its_own_centre() -> None:
    snapshot = make_mixed_snapshot()

    result = _plan(IntentName.FORMATION_SET, snapshot, args={"name": "line"})

    assert isinstance(result, Plan)
    targets = {
        command.drone_id: (
            command.parameters["x"],
            command.parameters["y"],
            command.parameters["z"],
        )
        for command in result.commands
    }
    assert sorted(targets) == [1, 2, 11, 12]
    assert [targets[drone_id][2] for drone_id in GROUND_IDS] == [0.0, 0.0]
    assert [targets[drone_id][2] for drone_id in AIRCRAFT_IDS] == [1.0, 1.0]
    assert [targets[drone_id][1] for drone_id in GROUND_IDS] == [6.0, 6.0]
    assert abs(targets[11][0] - targets[12][0]) == pytest.approx(snapshot.spacing)
    assert abs(targets[1][0] - targets[2][0]) == pytest.approx(snapshot.spacing)


def test_a_single_selected_ground_vehicle_holds_its_place_in_a_mixed_formation() -> None:
    snapshot = make_mixed_snapshot(ground_ids=(11,))

    result = _plan(IntentName.FORMATION_SET, snapshot, args={"name": "line"})

    assert isinstance(result, Plan)
    ground = next(command for command in result.commands if command.drone_id == 11)
    pose = snapshot.aircraft[11].pose
    assert (ground.parameters["x"], ground.parameters["y"], ground.parameters["z"]) == (
        pose.x,
        pose.y,
        0.0,
    )


def test_spacing_and_select_change_state_for_a_ground_only_session() -> None:
    snapshot = make_mixed_snapshot(aircraft_ids=())

    spacing = _plan(IntentName.SPACING, snapshot, args={"delta": 1})
    selection = _planner().plan(
        make_intent(IntentName.SELECT, selection=(), args={"ids": (11,)}), snapshot
    )

    assert isinstance(spacing, Plan) and isinstance(selection, Plan)
    assert spacing.commands == () and spacing.spacing_update == pytest.approx(1.0)
    assert selection.selection_update == (11,)


def test_safety_plans_hold_every_mobile_device_and_land_only_aircraft() -> None:
    snapshot = make_mixed_snapshot()
    planner = _planner()

    hold = planner.emergency_hold_plan(intent_id="safety-1", snapshot=snapshot)
    stopped = planner.fleet_position_loss_plan(intent_id="loss-1", snapshot=snapshot, land=False)
    landed = planner.fleet_position_loss_plan(intent_id="loss-2", snapshot=snapshot, land=True)

    assert [command.drone_id for command in hold.commands] == [1, 2, 11, 12]
    assert [command.drone_id for command in stopped.commands] == [1, 2, 11, 12]
    assert [command.drone_id for command in landed.commands] == [1, 2]


def test_a_docked_or_faulted_ground_vehicle_is_not_mobile() -> None:
    snapshot = make_mixed_snapshot(aircraft_ids=(), drive_state=DriveState.DOCKED)
    faulted = replace_aircraft(snapshot, 12, drive_state=DriveState.FAULT, armed=False)
    planner = _planner()

    hold = planner.emergency_hold_plan(intent_id="safety-2", snapshot=faulted)

    assert hold.commands == ()


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
def test_graceful_removal_reads_the_ground_vehicle_drive_state(
    drive_state: DriveState, allowed: bool
) -> None:
    snapshot = make_mixed_snapshot(aircraft_ids=(), ground_ids=(11,), drive_state=drive_state)

    authorization = authorize_graceful_removal(snapshot, 11)

    assert authorization.allowed is allowed
    if not allowed:
        assert authorization.refusal is not None
        assert authorization.refusal.reason is RefusalReason.INVALID_STATE
        assert authorization.refusal.detail == (
            "graceful removal requires a docked, idle, or stopped ground vehicle"
        )


def test_graceful_removal_still_refuses_an_active_task_on_a_ground_vehicle() -> None:
    snapshot = make_mixed_snapshot(aircraft_ids=(), ground_ids=(11,))
    snapshot = replace_aircraft(snapshot, 11, active_task_id="task-1")

    authorization = authorize_graceful_removal(snapshot, 11)

    assert authorization.allowed is False
    assert authorization.refusal is not None
    assert authorization.refusal.reason is RefusalReason.ACTIVE_TASK


def test_graceful_removal_keeps_the_aircraft_disarmed_proof() -> None:
    snapshot = replace_aircraft(
        make_snapshot(1, selection=()), 1, flight_state=FlightState.LANDED, armed=True
    )

    authorization = authorize_graceful_removal(snapshot, 1)

    assert authorization.allowed is False
    assert authorization.refusal is not None
    assert authorization.refusal.detail == "graceful removal requires a disarmed aircraft"


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"flight_state": FlightState.HOVERING}, "a ground vehicle carries no flight_state"),
        ({"flight_state": None, "drive_state": None}, "a ground vehicle requires a DriveState"),
        ({"unit": 0}, "unit must be null or a positive integer"),
    ],
)
def test_a_malformed_ground_vehicle_state_fails_closed(
    changes: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        make_ground_vehicle(11, **changes)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"flight_state": None}, "an aircraft requires a FlightState"),
        ({"drive_state": DriveState.IDLE}, "an aircraft carries no drive_state"),
    ],
)
def test_a_malformed_aircraft_state_fails_closed(changes: dict[str, object], message: str) -> None:
    snapshot = make_snapshot(1)
    with pytest.raises(ValueError, match=message):
        replace(snapshot.aircraft[1], **changes)


def test_the_ground_vehicle_projection_round_trips_through_its_mapping() -> None:
    state = make_ground_vehicle(11, drive_state=DriveState.MOVING)

    projection = state.to_dict()
    restored = AircraftState.from_mapping(projection)

    assert projection["device_class"] == "ground_vehicle"
    assert projection["flight_state"] is None
    assert projection["drive_state"] == "moving"
    assert projection["unit"] == 1
    assert restored == state
    assert restored.telemetry_state == "moving"
    assert restored.mobile is True
    assert restored.airborne is False


def test_an_aircraft_mapping_without_a_class_stays_an_aircraft() -> None:
    projection = make_snapshot(1).aircraft[1].to_dict()
    del projection["device_class"]
    del projection["unit"]

    restored = AircraftState.from_mapping(projection)

    assert restored.device_class is DeviceClass.AIRCRAFT
    assert restored.unit is None
    assert restored.telemetry_state == "hovering"


def test_a_membership_that_is_not_ready_keeps_a_ground_vehicle_out_of_safety_plans() -> None:
    snapshot = make_mixed_snapshot(aircraft_ids=(), ground_ids=(11, 12))
    snapshot = replace_aircraft(snapshot, 12, membership=MembershipState.REGISTERED)

    hold = _planner().emergency_hold_plan(intent_id="safety-3", snapshot=snapshot)

    assert [command.drone_id for command in hold.commands] == [11]
