from dataclasses import replace
from itertools import combinations, permutations
from math import fsum

import pytest

from arbiter.safety import SafetyArbiter
from planner.models import (
    CommandOperation,
    FleetSnapshot,
    FlightState,
    Plan,
    Position,
    Refusal,
    RefusalReason,
)
from planner.planner import (
    DeterministicPlanner,
    _formation_offsets,
    _formation_targets,
    _formation_transitions_cross,
    _minimum_cost_formation_assignment,
    _segments_cross_xy,
)
from relay.capabilities import C1_CAPABILITY_PROFILE, C2_CAPABILITY_PROFILE
from relay.intent_v1 import FORMATION_NAMES, IntentName
from tests.autonomy_fixtures import (
    make_intent,
    make_snapshot,
    planning_config,
    replace_aircraft,
    safety_config,
)


def altitude_config():
    return replace(
        planning_config(),
        altitude_step_m=0.5,
        altitude_floor_z_m=0.0,
        altitude_configuration_id="planner-test-floor-v1",
        altitude_completion_tolerance_m=0.05,
    )


@pytest.mark.parametrize("count", [1, 2, 3, 4])
def test_planner_iterates_registered_selection_sizes(count: int) -> None:
    snapshot = make_snapshot(count)
    intent = make_intent(
        IntentName.TRANSLATE,
        selection=snapshot.selection,
        args={"dx": 2, "dy": -1},
    )

    result = DeterministicPlanner(planning_config()).plan(intent, snapshot)

    assert isinstance(result, Plan)
    assert result.roster_version == snapshot.roster_version
    assert [command.drone_id for command in result.commands] == list(range(count, 0, -1))
    assert all(command.connection_epoch == 1 for command in result.commands)
    assert [command.command_id for command in result.commands] == [
        f"plan:{intent.intent_id}:command:{index:04d}" for index in range(1, count + 1)
    ]


def test_arm_is_session_authorization_without_adapter_command() -> None:
    snapshot = make_snapshot(2, selection=(), armed=False)
    intent = make_intent(IntentName.ARM, selection=())

    result = DeterministicPlanner(planning_config()).plan(intent, snapshot)

    assert isinstance(result, Plan)
    assert result.commands == ()
    assert result.armed_update is True


def test_select_is_state_update_without_adapter_command() -> None:
    snapshot = make_snapshot(2, selection=())
    intent = make_intent(IntentName.SELECT, selection=(), args={"ids": (2, 1)})

    result = DeterministicPlanner(planning_config()).plan(intent, snapshot)

    assert isinstance(result, Plan)
    assert result.commands == ()
    assert result.selection_update == (1, 2)


def test_come_home_expands_only_to_existing_goto_operation() -> None:
    snapshot = make_snapshot(2)
    intent = make_intent(IntentName.COME_HOME)

    result = DeterministicPlanner(planning_config()).plan(intent, snapshot)

    assert isinstance(result, Plan)
    assert {command.operation for command in result.commands} == {CommandOperation.GOTO}
    assert all(command.parameters["z"] == 1.0 for command in result.commands)


def test_land_all_and_estop_ignore_stale_selection_and_target_fleet() -> None:
    snapshot = make_snapshot(3, selection=(1,))
    planner = DeterministicPlanner(planning_config())

    land = planner.plan(make_intent(IntentName.LAND_ALL, selection=(99,), confirm=True), snapshot)
    stop = planner.plan(make_intent(IntentName.ESTOP, selection=(99,)), snapshot)

    assert isinstance(land, Plan)
    assert isinstance(stop, Plan)
    assert [command.drone_id for command in land.commands] == [1, 2, 3]
    assert [command.drone_id for command in stop.commands] == [1, 2, 3]
    assert all(command.safety_action for command in (*land.commands, *stop.commands))
    assert stop.estop_update is True
    assert stop.to_dict()["estop_update"] is True


def test_land_targets_only_selected_aircraft() -> None:
    snapshot = replace_aircraft(
        make_snapshot(3, selection=(1, 3)), 2, flight_state=FlightState.LANDED
    )
    planner = DeterministicPlanner(planning_config())

    result = planner.plan(make_intent(IntentName.LAND, selection=(1, 3), confirm=True), snapshot)

    assert isinstance(result, Plan)
    assert [command.drone_id for command in result.commands] == [1, 3]
    assert all(command.operation is CommandOperation.LAND for command in result.commands)
    assert all(not command.safety_action for command in result.commands)


def test_aircraft_relative_translation_rotates_each_aircraft_vector_by_heading() -> None:
    snapshot = replace_aircraft(make_snapshot(2), 2, heading_deg=90.0)
    intent = make_intent(
        IntentName.TRANSLATE,
        selection=(1, 2),
        args={"dx": 1, "dy": 0},
    )

    result = DeterministicPlanner(planning_config(translation_frame="aircraft_relative")).plan(
        intent, snapshot
    )

    assert isinstance(result, Plan)
    targets = {
        command.drone_id: (command.parameters["x"], command.parameters["y"])
        for command in result.commands
    }
    assert targets == {1: (0.5, 0.0), 2: (2.0, 0.5)}


def test_world_translation_keeps_the_shared_world_vector() -> None:
    snapshot = replace_aircraft(make_snapshot(2), 2, heading_deg=90.0)
    intent = make_intent(
        IntentName.TRANSLATE,
        selection=(1, 2),
        args={"dx": 1, "dy": 0},
    )

    result = DeterministicPlanner(planning_config(translation_frame="world")).plan(intent, snapshot)

    assert isinstance(result, Plan)
    targets = {
        command.drone_id: (command.parameters["x"], command.parameters["y"])
        for command in result.commands
    }
    assert targets == {1: (0.5, 0.0), 2: (2.5, 0.0)}


def test_panorama_plan_preserves_room_association_and_protocol_order() -> None:
    snapshot = make_snapshot(1)
    intent = make_intent(
        IntentName.CAPTURE_ROOM,
        selection=(1,),
        args={"room_id": "room-a", "capture_id": "capture-a", "pattern": "pano_360"},
        confirm=True,
    )

    result = DeterministicPlanner(planning_config()).plan(intent, snapshot)

    assert isinstance(result, Plan)
    assert [command.operation for command in result.commands] == [
        CommandOperation.CAMERA_CAPABILITIES,
        CommandOperation.SET_GIMBAL_PITCH,
        CommandOperation.CAMERA_READY,
        CommandOperation.CAPTURE_PANORAMA,
        CommandOperation.RETRIEVE_MEDIA,
    ]
    capture = result.commands[3]
    assert capture.parameters["room_id"] == "room-a"
    assert capture.parameters["capture_id"] == "capture-a"


def test_reconstruct_plan_has_eight_ordered_acknowledged_frames() -> None:
    snapshot = make_snapshot(1)
    intent = make_intent(
        IntentName.CAPTURE_ROOM,
        selection=(1,),
        args={
            "room_id": "room-a",
            "capture_id": "capture-eight",
            "pattern": "reconstruct_8",
        },
        confirm=True,
    )

    result = DeterministicPlanner(planning_config()).plan(intent, snapshot)

    assert isinstance(result, Plan)
    assert len(result.commands) == 34
    assert (
        sum(command.operation is CommandOperation.CAPTURE_PHOTO for command in result.commands) == 8
    )
    assert (
        sum(command.operation is CommandOperation.RETRIEVE_MEDIA for command in result.commands)
        == 8
    )
    rotations = [
        command for command in result.commands if command.operation is CommandOperation.ROTATE_TO
    ]
    assert [command.parameters["tolerance"] for command in rotations] == [1.0] * 8


def test_unearned_intent_is_unsupported_before_selection_checks() -> None:
    snapshot = make_snapshot(1, selection=(1,))
    intent = make_intent(
        IntentName.SURVEY_AREA,
        selection=(),
        args={"area_id": "area-a"},
        confirm=True,
    )

    result = DeterministicPlanner(planning_config()).plan(intent, snapshot)

    assert isinstance(result, Refusal)
    assert result.reason is RefusalReason.UNSUPPORTED


def test_translate_orders_leading_aircraft_first_for_sequential_spacing() -> None:
    snapshot = replace_aircraft(
        make_snapshot(2),
        2,
        pose=Position(1.0, 0.0, 1.0),
        home=Position(1.0, 0.0, 0.0),
    )
    intent = make_intent(
        IntentName.TRANSLATE,
        selection=(1, 2),
        args={"dx": 1, "dy": 0},
    )

    result = DeterministicPlanner(planning_config()).plan(intent, snapshot)

    assert isinstance(result, Plan)
    assert [command.drone_id for command in result.commands] == [2, 1]


def test_formation_and_spacing_plans_carry_authoritative_projection_updates() -> None:
    snapshot = make_snapshot(4)
    planner = DeterministicPlanner(planning_config(), C2_CAPABILITY_PROFILE)

    formation = planner.plan(
        make_intent(
            IntentName.FORMATION_SET, selection=snapshot.selection, args={"name": "diamond"}
        ),
        snapshot,
    )
    spacing = planner.plan(
        make_intent(IntentName.SPACING, selection=snapshot.selection, args={"delta": 1}),
        snapshot,
    )

    assert isinstance(formation, Plan)
    assert formation.formation_update == "diamond"
    assert len(formation.commands) == 4
    assert isinstance(spacing, Plan)
    assert spacing.spacing_update == 1.0
    assert spacing.commands == ()


def test_arbiter_rejects_a_tampered_formation_projection() -> None:
    snapshot = make_snapshot(4)
    plan = DeterministicPlanner(planning_config(), C2_CAPABILITY_PROFILE).plan(
        make_intent(
            IntentName.FORMATION_SET,
            selection=snapshot.selection,
            args={"name": "diamond"},
        ),
        snapshot,
    )
    assert isinstance(plan, Plan)

    refusal = SafetyArbiter(safety_config()).check_plan(
        replace(plan, formation_update="circle"), snapshot
    )

    assert refusal is not None
    assert refusal.reason is RefusalReason.INVALID_PLAN


def test_altitude_and_confirmed_sweep_expand_for_six_simulated_aircraft() -> None:
    snapshot = make_snapshot(6)
    planner = DeterministicPlanner(altitude_config(), C2_CAPABILITY_PROFILE)

    altitude = planner.plan(
        make_intent(IntentName.ALTITUDE, selection=snapshot.selection, args={"delta": 1}),
        snapshot,
    )
    sweep = planner.plan(
        make_intent(IntentName.SWEEP, selection=snapshot.selection, args={}, confirm=True),
        snapshot,
    )

    assert isinstance(altitude, Plan)
    assert [
        command.parameters["z"]
        for command in altitude.commands
        if command.operation is CommandOperation.GOTO
    ] == [1.5] * 6
    assert isinstance(sweep, Plan)
    assert len(sweep.commands) == 12
    assert {command.operation for command in sweep.commands} == {CommandOperation.GOTO}


def test_requested_sweep_box_is_the_exact_source_of_lane_coordinates() -> None:
    snapshot = make_snapshot(4)
    box = {"min_x": -2.0, "max_x": 2.0, "min_y": -3.0, "max_y": 3.0}

    result = DeterministicPlanner(planning_config(), C2_CAPABILITY_PROFILE).plan(
        make_intent(
            IntentName.SWEEP,
            selection=snapshot.selection,
            args={"box": box},
            confirm=True,
        ),
        snapshot,
    )

    assert isinstance(result, Plan)
    endpoints = [command.parameters for command in result.commands]
    assert {point["x"] for point in endpoints} == {-1.5, -0.5, 0.5, 1.5}
    assert {point["y"] for point in endpoints} == {-3.0, 3.0}
    assert all(point["z"] == 1.0 for point in endpoints)


@pytest.mark.parametrize("name", [IntentName.ALTITUDE, IntentName.SPACING])
def test_m15_delta_overflow_is_a_typed_refusal(name: IntentName) -> None:
    snapshot = make_snapshot(2)
    config = altitude_config() if name is IntentName.ALTITUDE else planning_config()

    result = DeterministicPlanner(
        config, C2_CAPABILITY_PROFILE if name is IntentName.SPACING else C1_CAPABILITY_PROFILE
    ).plan(
        make_intent(name, selection=snapshot.selection, args={"delta": 10**400}),
        snapshot,
    )

    assert isinstance(result, Refusal)
    assert result.reason is RefusalReason.INVALID_PLAN


@pytest.mark.parametrize("count", [4, 5, 6])
@pytest.mark.parametrize("name", FORMATION_NAMES)
def test_mvp_formations_are_separated_non_crossing_and_safe(name: str, count: int) -> None:
    snapshot = make_snapshot(count)

    result = DeterministicPlanner(planning_config(), C2_CAPABILITY_PROFILE).plan(
        make_intent(
            IntentName.FORMATION_SET,
            selection=snapshot.selection,
            args={"name": name},
        ),
        snapshot,
    )

    assert isinstance(result, Plan)
    assert result.formation_update == name
    assert len(result.commands) == count
    assert {command.drone_id for command in result.commands} == set(snapshot.selection)
    assert {command.operation for command in result.commands} == {CommandOperation.GOTO}
    assignments = tuple(
        (
            command.drone_id,
            Position(
                float(command.parameters["x"]),
                float(command.parameters["y"]),
                float(command.parameters["z"]),
            ),
        )
        for command in result.commands
    )
    targets = tuple(target for _, target in assignments)
    assert sum(target.x for target in targets) / count == pytest.approx(
        sum(aircraft.pose.x for aircraft in snapshot.aircraft.values()) / count
    )
    assert sum(target.y for target in targets) / count == pytest.approx(0.0)
    assert min(first.distance_to(second) for first, second in combinations(targets, 2)) > (
        snapshot.spacing
    )
    assert not _formation_transitions_cross(assignments, snapshot)
    assert SafetyArbiter(safety_config()).check_plan(result, snapshot) is None
    occupied = {drone_id: aircraft.pose for drone_id, aircraft in snapshot.aircraft.items()}
    for drone_id, target in assignments:
        occupied.pop(drone_id)
        assert all(target.distance_to(other) >= snapshot.spacing for other in occupied.values())
        occupied[drone_id] = target


@pytest.mark.parametrize("name", ["circle", "grid", "V", "unknown"])
def test_planner_refuses_names_outside_the_exact_mvp_library(name: str) -> None:
    snapshot = make_snapshot(4)

    result = DeterministicPlanner(planning_config(), C2_CAPABILITY_PROFILE).plan(
        make_intent(
            IntentName.FORMATION_SET,
            selection=snapshot.selection,
            args={"name": name},
        ),
        snapshot,
    )

    assert isinstance(result, Refusal)
    assert result.reason is RefusalReason.PLANNER_FAILURE


@pytest.mark.parametrize("name", ["wedge", "diamond"])
@pytest.mark.parametrize("count", [2, 3])
def test_four_aircraft_shapes_refuse_smaller_selections(name: str, count: int) -> None:
    snapshot = make_snapshot(count)

    result = DeterministicPlanner(planning_config(), C2_CAPABILITY_PROFILE).plan(
        make_intent(
            IntentName.FORMATION_SET,
            selection=snapshot.selection,
            args={"name": name},
        ),
        snapshot,
    )

    assert isinstance(result, Refusal)
    assert result.reason is RefusalReason.PLANNER_FAILURE


def test_formation_next_uses_only_shapes_available_to_the_selection() -> None:
    planner = DeterministicPlanner(planning_config(), C2_CAPABILITY_PROFILE)
    snapshot = make_snapshot(3)

    first = planner.plan(
        make_intent(IntentName.FORMATION_NEXT, selection=snapshot.selection), snapshot
    )
    second = planner.plan(
        make_intent(IntentName.FORMATION_NEXT, selection=snapshot.selection),
        replace(snapshot, formation="line"),
    )
    wrapped = planner.plan(
        make_intent(IntentName.FORMATION_NEXT, selection=snapshot.selection),
        replace(snapshot, formation="column"),
    )

    assert isinstance(first, Plan) and first.formation_update == "line"
    assert isinstance(second, Plan) and second.formation_update == "column"
    assert isinstance(wrapped, Plan) and wrapped.formation_update == "line"


def test_assignment_minimizes_3d_cost_among_non_crossing_matches() -> None:
    positions = (
        Position(3.4, 0.3, 3.1),
        Position(1.6, -2.8, 2.2),
        Position(-3.8, 0.1, 1.0),
        Position(-3.5, 0.3, 2.3),
    )
    snapshot = make_snapshot(4)
    for drone_id, pose in enumerate(positions, start=1):
        snapshot = replace_aircraft(snapshot, drone_id, pose=pose)
    offsets = _formation_offsets("line", 4)
    assert offsets is not None
    center = Position(
        sum(position.x / 4 for position in positions),
        sum(position.y / 4 for position in positions),
        sum(position.z / 4 for position in positions),
    )
    targets = tuple(
        Position(
            center.x + x * snapshot.spacing,
            center.y + y * snapshot.spacing,
            center.z,
        )
        for x, y in offsets
    )

    candidates = []
    for target_indices in permutations(range(4)):
        assignment = tuple(
            (drone_id, targets[target_index])
            for drone_id, target_index in zip(
                tuple(sorted(snapshot.selection)), target_indices, strict=True
            )
        )
        cost = fsum(
            snapshot.aircraft[drone_id].pose.distance_to(target) for drone_id, target in assignment
        )
        candidates.append((cost, target_indices, assignment))
    unconstrained = min(candidates, key=lambda candidate: (candidate[0], candidate[1]))
    feasible = min(
        (
            candidate
            for candidate in candidates
            if not _formation_transitions_cross(candidate[2], snapshot)
            and _has_sequential_clearance(candidate[2], snapshot, snapshot.spacing)
        ),
        key=lambda candidate: (candidate[0], candidate[1]),
    )
    actual = _formation_targets("line", snapshot.selection, snapshot, snapshot.spacing)
    assert actual is not None
    actual_by_drone = dict(actual)
    actual_indices = tuple(targets.index(actual_by_drone[drone_id]) for drone_id in range(1, 5))

    assert _formation_transitions_cross(unconstrained[2], snapshot)
    assert actual_indices == feasible[1]
    assert feasible[0] > unconstrained[0]


def test_assignment_ties_are_stable_by_drone_and_slot_index() -> None:
    snapshot = make_snapshot(4, selection=(4, 3, 2, 1))
    for drone_id in snapshot.aircraft:
        snapshot = replace_aircraft(snapshot, drone_id, pose=Position(0.0, 0.0, 1.0))
    offsets = _formation_offsets("diamond", 4)
    assert offsets is not None
    targets = tuple(Position(x, y, 1.0) for x, y in offsets)

    assignment = _minimum_cost_formation_assignment(
        snapshot.selection,
        targets,
        snapshot,
        minimum_clearance=0.0,
    )

    assert assignment is not None
    by_drone = dict(assignment)
    assert tuple(by_drone[drone_id] for drone_id in range(1, 5)) == targets


def test_transition_crossing_uses_xy_geometry_and_allows_sequential_collinear_following() -> None:
    assert _segments_cross_xy(
        Position(-1.0, -1.0, 1.0),
        Position(1.0, 1.0, 1.0),
        Position(-1.0, 1.0, 3.0),
        Position(1.0, -1.0, 3.0),
    )
    assert not _segments_cross_xy(
        Position(0.0, 0.0, 1.0),
        Position(3.0, 0.0, 1.0),
        Position(2.0, 0.0, 1.0),
        Position(4.0, 0.0, 1.0),
    )


def _has_sequential_clearance(
    assignment: tuple[tuple[int, Position], ...],
    snapshot: FleetSnapshot,
    minimum_clearance: float,
) -> bool:
    by_drone = dict(assignment)
    for order in permutations(sorted(by_drone)):
        occupied = {
            drone_id: aircraft.pose
            for drone_id, aircraft in snapshot.aircraft.items()
            if aircraft.airborne
        }
        for drone_id in order:
            occupied.pop(drone_id)
            target = by_drone[drone_id]
            if any(target.distance_to(other) < minimum_clearance for other in occupied.values()):
                break
            occupied[drone_id] = target
        else:
            return True
    return False
