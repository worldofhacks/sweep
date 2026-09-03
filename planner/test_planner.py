import pytest

from planner.models import CommandOperation, Plan, Position, Refusal, RefusalReason
from planner.planner import DeterministicPlanner
from relay.intent_v1 import IntentName
from tests.autonomy_fixtures import (
    make_intent,
    make_snapshot,
    planning_config,
    replace_aircraft,
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
    planner = DeterministicPlanner(planning_config())

    formation = planner.plan(
        make_intent(
            IntentName.FORMATION_SET, selection=snapshot.selection, args={"name": "circle"}
        ),
        snapshot,
    )
    spacing = planner.plan(
        make_intent(IntentName.SPACING, selection=snapshot.selection, args={"delta": 1}),
        snapshot,
    )

    assert isinstance(formation, Plan)
    assert formation.formation_update == "circle"
    assert len(formation.commands) == 4
    assert isinstance(spacing, Plan)
    assert spacing.spacing_update == 1.0
    assert spacing.commands == ()


def test_altitude_and_confirmed_sweep_expand_for_six_simulated_aircraft() -> None:
    snapshot = make_snapshot(6)
    planner = DeterministicPlanner(planning_config())

    altitude = planner.plan(
        make_intent(IntentName.ALTITUDE, selection=snapshot.selection, args={"delta": 1}),
        snapshot,
    )
    sweep = planner.plan(
        make_intent(IntentName.SWEEP, selection=snapshot.selection, args={}, confirm=True),
        snapshot,
    )

    assert isinstance(altitude, Plan)
    assert [command.parameters["z"] for command in altitude.commands] == [1.5] * 6
    assert isinstance(sweep, Plan)
    assert len(sweep.commands) == 12
    assert {command.operation for command in sweep.commands} == {CommandOperation.GOTO}
