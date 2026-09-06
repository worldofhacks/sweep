from dataclasses import replace

import pytest

from planner.models import (
    Command,
    CommandOperation,
    FlightState,
    HoldScope,
    LifecycleStatus,
    MembershipState,
    Plan,
    Position,
    RefusalReason,
)
from relay.capabilities import C2_CAPABILITY_PROFILE
from relay.intent_v1 import IntentName
from tests.autonomy_fixtures import (
    NOW_MS,
    make_intent,
    make_snapshot,
    make_stack,
    replace_aircraft,
)


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"membership": MembershipState.REGISTERED}, RefusalReason.AIRCRAFT_NOT_READY),
        ({"flight_state": FlightState.LANDED}, RefusalReason.INVALID_STATE),
        ({"battery": 0.1}, RefusalReason.BATTERY_CRITICAL),
        ({"battery": 0.2}, RefusalReason.BATTERY_RESERVE),
        ({"link_quality": 0.2}, RefusalReason.LINK_QUALITY),
        ({"link_last_seen_ms": NOW_MS - 2_000}, RefusalReason.LINK_STALE),
        ({"link_last_seen_ms": NOW_MS + 1_001}, RefusalReason.LINK_STALE),
        ({"position_quality": 0.2}, RefusalReason.POSITION_QUALITY),
        ({"position_last_seen_ms": NOW_MS - 2_000}, RefusalReason.POSITION_STALE),
        ({"position_last_seen_ms": NOW_MS + 1_001}, RefusalReason.POSITION_STALE),
        ({"position_loss_since_ms": NOW_MS + 1_001}, RefusalReason.POSITION_STALE),
        ({"control_authority": False}, RefusalReason.CONTROL_AUTHORITY),
        (
            {"rc_safety_operator_present": False},
            RefusalReason.RC_SAFETY_OPERATOR_ABSENT,
        ),
        ({"physical_rc_available": False}, RefusalReason.CONTROL_AUTHORITY),
    ],
)
def test_unsafe_translate_is_refused_without_adapter_io(
    change: dict[str, object], reason: RefusalReason
) -> None:
    snapshot = replace_aircraft(make_snapshot(1, selection=(1,)), 1, **change)
    controller, _, _, _, flight, _ = make_stack(snapshot)
    intent = make_intent(
        IntentName.TRANSLATE,
        selection=(1,),
        args={"dx": 1, "dy": 0},
    )

    result = controller.execute(intent, snapshot)

    assert result.status is LifecycleStatus.REFUSED
    assert result.refusal is not None
    assert result.refusal.reason is reason
    assert flight.calls == []


@pytest.mark.parametrize(
    ("snapshot_change", "reason"),
    [
        ({"estop_active": True}, RefusalReason.ESTOP_ACTIVE),
        ({"operator_present": False}, RefusalReason.OPERATOR_ABSENT),
        ({"operator_last_seen_ms": NOW_MS - 11_000}, RefusalReason.OPERATOR_ABSENT),
        ({"operator_last_seen_ms": NOW_MS + 1_001}, RefusalReason.OPERATOR_ABSENT),
    ],
)
def test_global_safety_state_refuses_motion_without_adapter_io(
    snapshot_change: dict[str, object], reason: RefusalReason
) -> None:
    snapshot = replace(make_snapshot(1, selection=(1,)), **snapshot_change)
    controller, _, _, _, flight, _ = make_stack(snapshot)
    intent = make_intent(
        IntentName.TRANSLATE,
        selection=(1,),
        args={"dx": 1, "dy": 0},
    )

    result = controller.execute(intent, snapshot)

    assert result.refusal is not None
    assert result.refusal.reason is reason
    assert flight.calls == []


@pytest.mark.parametrize("operation", tuple(CommandOperation))
def test_every_operation_has_an_explicit_stopped_state_classification(
    operation: CommandOperation,
) -> None:
    stopped_intents = {
        CommandOperation.TAKEOFF: None,
        CommandOperation.GOTO: None,
        CommandOperation.ROTATE_TO: None,
        CommandOperation.HOVER: IntentName.HOLD,
        CommandOperation.LAND: IntentName.LAND_ALL,
        CommandOperation.ESTOP: IntentName.ESTOP,
        CommandOperation.CAMERA_CAPABILITIES: None,
        CommandOperation.SET_GIMBAL_PITCH: None,
        CommandOperation.CAMERA_READY: None,
        CommandOperation.CAPTURE_PANORAMA: None,
        CommandOperation.CAPTURE_PHOTO: None,
        CommandOperation.RETRIEVE_MEDIA: None,
    }
    assert set(stopped_intents) == set(CommandOperation)
    stopped_intent = stopped_intents[operation]
    snapshot = replace(make_snapshot(1, selection=(1,)), estop_active=True)
    command = Command(
        command_id="plan:stopped:command:0001",
        intent_id="stopped",
        roster_version=snapshot.roster_version,
        drone_id=1,
        connection_epoch=1,
        operation=operation,
        safety_action=stopped_intent is not None,
    )
    plan = Plan(
        plan_id="plan:stopped",
        intent_id="stopped",
        intent_name=stopped_intent or IntentName.TRANSLATE,
        roster_version=snapshot.roster_version,
        selection=(1,),
        confirmed=True,
        commands=(command,),
        hold_scope=(HoldScope.OPERATOR_SELECTION if stopped_intent is IntentName.HOLD else None),
        estop_update=True if stopped_intent is IntentName.ESTOP else None,
    )
    _, _, arbiter, _, _, _ = make_stack(snapshot)

    refusal = arbiter.check_command(plan, command, snapshot)

    if stopped_intent is None:
        assert refusal is not None
        assert refusal.reason is RefusalReason.ESTOP_ACTIVE
    else:
        assert refusal is None


@pytest.mark.parametrize("intent_name", [IntentName.ARM, IntentName.SELECT])
def test_zero_command_state_update_plan_cannot_bypass_stop(intent_name: IntentName) -> None:
    snapshot = replace(make_snapshot(1, selection=(1,)), estop_active=True)
    _, planner, arbiter, dispatcher, flight, _ = make_stack(snapshot)
    intent = make_intent(
        intent_name,
        selection=() if intent_name is IntentName.ARM else (1,),
        args={} if intent_name is IntentName.ARM else {"ids": (1,)},
    )
    plan = planner.plan(intent, snapshot)
    assert isinstance(plan, Plan)
    assert plan.commands == ()

    result = dispatcher.dispatch(plan, snapshot)

    assert result.status is LifecycleStatus.REFUSED
    assert result.refusal is not None
    assert result.refusal.reason is RefusalReason.ESTOP_ACTIVE
    assert flight.calls == []


@pytest.mark.parametrize(
    "operation",
    [CommandOperation.HOVER, CommandOperation.LAND, CommandOperation.ESTOP],
)
def test_safe_operation_name_cannot_bypass_stop_under_an_unrelated_plan(
    operation: CommandOperation,
) -> None:
    snapshot = replace(make_snapshot(1, selection=(1,)), estop_active=True)
    command = Command(
        command_id="plan:mismatch:command:0001",
        intent_id="mismatch",
        roster_version=snapshot.roster_version,
        drone_id=1,
        connection_epoch=1,
        operation=operation,
    )
    plan = Plan(
        plan_id="plan:mismatch",
        intent_id="mismatch",
        intent_name=IntentName.TRANSLATE,
        roster_version=snapshot.roster_version,
        selection=(1,),
        confirmed=True,
        commands=(command,),
    )
    _, _, arbiter, _, _, _ = make_stack(snapshot)

    refusal = arbiter.check_command(plan, command, snapshot)

    assert refusal is not None
    assert refusal.reason is RefusalReason.ESTOP_ACTIVE


@pytest.mark.parametrize(
    ("intent_name", "operation"),
    [
        (IntentName.HOLD, CommandOperation.HOVER),
        (IntentName.LAND_ALL, CommandOperation.LAND),
        (IntentName.ESTOP, CommandOperation.ESTOP),
    ],
)
def test_unflagged_safety_command_cannot_bypass_stop(
    intent_name: IntentName, operation: CommandOperation
) -> None:
    snapshot = replace(make_snapshot(1, selection=(1,)), estop_active=True)
    command = Command(
        command_id="plan:unflagged:command:0001",
        intent_id="unflagged",
        roster_version=snapshot.roster_version,
        drone_id=1,
        connection_epoch=1,
        operation=operation,
    )
    plan = Plan(
        plan_id="plan:unflagged",
        intent_id="unflagged",
        intent_name=intent_name,
        roster_version=snapshot.roster_version,
        selection=(1,),
        confirmed=True,
        commands=(command,),
        hold_scope=(HoldScope.OPERATOR_SELECTION if intent_name is IntentName.HOLD else None),
        estop_update=True if intent_name is IntentName.ESTOP else None,
    )
    _, _, arbiter, _, _, _ = make_stack(snapshot)

    refusal = arbiter.check_command(plan, command, snapshot)

    assert refusal is not None
    assert refusal.reason is RefusalReason.ESTOP_ACTIVE


@pytest.mark.parametrize("operation", tuple(CommandOperation))
def test_safety_action_flag_never_authorizes_an_unrelated_stopped_command(
    operation: CommandOperation,
) -> None:
    snapshot = replace(make_snapshot(1, selection=(1,)), estop_active=True)
    command = Command(
        command_id="plan:flagged:command:0001",
        intent_id="flagged",
        roster_version=snapshot.roster_version,
        drone_id=1,
        connection_epoch=1,
        operation=operation,
        safety_action=True,
    )
    plan = Plan(
        plan_id="plan:flagged",
        intent_id="flagged",
        intent_name=IntentName.TRANSLATE,
        roster_version=snapshot.roster_version,
        selection=(1,),
        confirmed=True,
        commands=(command,),
    )
    _, _, arbiter, _, _, _ = make_stack(snapshot)

    refusal = arbiter.check_command(plan, command, snapshot)

    assert refusal is not None
    assert refusal.reason is RefusalReason.INVALID_PLAN


def test_configured_future_clock_skew_is_accepted_at_its_boundary() -> None:
    snapshot = replace(
        make_snapshot(1, selection=(1,)),
        operator_last_seen_ms=NOW_MS + 1_000,
    )
    snapshot = replace_aircraft(
        snapshot,
        1,
        link_last_seen_ms=NOW_MS + 1_000,
        position_last_seen_ms=NOW_MS + 1_000,
    )
    controller, _, _, _, flight, _ = make_stack(snapshot)

    result = controller.execute(
        make_intent(
            IntentName.TRANSLATE,
            selection=(1,),
            args={"dx": 1, "dy": 0},
        ),
        snapshot,
    )

    assert result.status is LifecycleStatus.COMPLETED
    assert [call.operation for call in flight.calls] == [CommandOperation.GOTO]


def test_takeoff_requires_confirmation_without_adapter_io() -> None:
    snapshot = make_snapshot(
        1,
        selection=(1,),
        flight_state=FlightState.DISARMED,
        armed=True,
    )
    controller, _, _, _, flight, _ = make_stack(snapshot)

    result = controller.execute(
        make_intent(IntentName.TAKEOFF, selection=(1,), confirm=False), snapshot
    )

    assert result.refusal is not None
    assert result.refusal.reason is RefusalReason.CONFIRMATION_REQUIRED
    assert flight.calls == []


@pytest.mark.parametrize("stopped", [False, True])
def test_selection_scoped_land_executes_only_selected_aircraft(stopped: bool) -> None:
    snapshot = replace(make_snapshot(2, selection=(1,)), estop_active=stopped)
    controller, _, _, _, flight, _ = make_stack(snapshot)
    result = controller.execute(
        make_intent(IntentName.LAND, selection=(1,), confirm=True), snapshot
    )
    assert result.status is LifecycleStatus.COMPLETED
    assert [call.drone_ids for call in flight.calls] == [(1,)]
    assert all(call.operation is CommandOperation.LAND for call in flight.calls)


def test_geofence_and_ceiling_are_checked_on_planned_command() -> None:
    snapshot = replace_aircraft(
        make_snapshot(1, selection=(1,)),
        1,
        pose=Position(9.8, 0.0, 1.0),
        home=Position(9.8, 0.0, 0.0),
    )
    controller, _, arbiter, dispatcher, flight, _ = make_stack(snapshot)
    geofence_result = controller.execute(
        make_intent(
            IntentName.TRANSLATE,
            selection=(1,),
            args={"dx": 1, "dy": 0},
        ),
        snapshot,
    )
    ceiling_command = Command(
        command_id="plan:ceiling:command:0001",
        intent_id="ceiling",
        roster_version=snapshot.roster_version,
        drone_id=1,
        connection_epoch=1,
        operation=CommandOperation.GOTO,
        parameters={"x": 9.0, "y": 0.0, "z": 4.5, "speed": 0.5},
    )
    ceiling_plan = Plan(
        plan_id="plan:ceiling",
        intent_id="ceiling",
        intent_name=IntentName.TRANSLATE,
        roster_version=snapshot.roster_version,
        selection=(1,),
        confirmed=True,
        commands=(ceiling_command,),
    )
    ceiling_result = dispatcher.dispatch(ceiling_plan, snapshot)

    assert geofence_result.refusal is not None
    assert geofence_result.refusal.reason is RefusalReason.GEOFENCE
    assert ceiling_result.refusal is not None
    assert ceiling_result.refusal.reason is RefusalReason.CEILING
    assert arbiter.check_plan(ceiling_plan, snapshot) is not None
    assert flight.calls == []


def test_requested_sweep_box_outside_mode_geofence_is_refused_before_planning() -> None:
    snapshot = make_snapshot(2, selection=(1, 2))
    controller, _, _, _, flight, _ = make_stack(snapshot, capability_profile=C2_CAPABILITY_PROFILE)

    result = controller.execute(
        make_intent(
            IntentName.SWEEP,
            selection=(1, 2),
            args={"box": {"min_x": -2.0, "max_x": 11.0, "min_y": -3.0, "max_y": 3.0}},
            confirm=True,
        ),
        snapshot,
    )

    assert result.refusal is not None
    assert result.refusal.reason is RefusalReason.GEOFENCE
    assert flight.calls == []


def test_spacing_includes_unselected_ready_aircraft() -> None:
    snapshot = make_snapshot(2, selection=(1,))
    snapshot = replace_aircraft(snapshot, 2, pose=Position(0.5, 0.0, 1.0))
    controller, _, _, _, flight, _ = make_stack(snapshot)

    result = controller.execute(
        make_intent(
            IntentName.TRANSLATE,
            selection=(1,),
            args={"dx": 0, "dy": 0},
        ),
        snapshot,
    )

    assert result.refusal is not None
    assert result.refusal.reason is RefusalReason.SPACING
    assert flight.calls == []


def test_motion_battery_budget_includes_outbound_and_return_distance() -> None:
    snapshot = replace_aircraft(
        make_snapshot(1, selection=(1,)),
        1,
        battery=0.3,
        pose=Position(0.0, 0.0, 1.0),
        home=Position(0.0, 0.0, 0.0),
    )
    controller, _, _, _, flight, _ = make_stack(snapshot)

    result = controller.execute(
        make_intent(
            IntentName.TRANSLATE,
            selection=(1,),
            args={"dx": 18, "dy": 0},
        ),
        snapshot,
    )

    assert result.status is LifecycleStatus.REFUSED
    assert result.refusal is not None
    assert result.refusal.reason is RefusalReason.BATTERY_RESERVE
    assert flight.calls == []


@pytest.mark.parametrize("flight_state", [FlightState.TAKING_OFF, FlightState.LANDING])
@pytest.mark.parametrize("intent_name", [IntentName.TRANSLATE, IntentName.COME_HOME])
def test_motion_refuses_transitional_flight_states_without_adapter_io(
    flight_state: FlightState,
    intent_name: IntentName,
) -> None:
    snapshot = make_snapshot(1, selection=(1,), flight_state=flight_state)
    controller, _, _, _, flight, camera = make_stack(snapshot)
    args = {"dx": 1, "dy": 0} if intent_name is IntentName.TRANSLATE else {}

    result = controller.execute(
        make_intent(intent_name, selection=(1,), args=args),
        snapshot,
    )

    assert result.status is LifecycleStatus.REFUSED
    assert result.refusal is not None
    assert result.refusal.reason is RefusalReason.INVALID_STATE
    assert flight.calls == []
    assert camera.calls == []


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"active_task_id": "task-1"}, RefusalReason.ACTIVE_TASK),
        ({"camera_ready": False}, RefusalReason.CAMERA_NOT_READY),
        ({"storage_remaining_bytes": 999_999}, RefusalReason.STORAGE),
    ],
)
def test_capture_preconditions_fail_before_camera_or_flight_io(
    change: dict[str, object], reason: RefusalReason
) -> None:
    snapshot = replace_aircraft(make_snapshot(1, selection=(1,)), 1, **change)
    controller, _, _, _, flight, camera = make_stack(snapshot)
    intent = make_intent(
        IntentName.CAPTURE_ROOM,
        selection=(1,),
        args={"room_id": "room", "capture_id": "capture", "pattern": "pano_360"},
        confirm=True,
    )

    result = controller.execute(intent, snapshot)

    assert result.refusal is not None
    assert result.refusal.reason is reason
    assert flight.calls == []
    assert camera.calls == []


def test_estop_ignores_stale_selection_and_operator_state() -> None:
    snapshot = make_snapshot(2, selection=(1,))
    snapshot = replace(snapshot, operator_present=False, estop_active=True)
    snapshot = replace_aircraft(snapshot, 2, membership=MembershipState.DEGRADED)
    controller, _, _, _, flight, _ = make_stack(snapshot)

    result = controller.execute(make_intent(IntentName.ESTOP, selection=(99,)), snapshot)

    assert result.status is LifecycleStatus.COMPLETED
    assert result.plan is not None and result.plan.estop_update is True
    assert len(result.acknowledgements) == 2
    assert [call.operation for call in flight.calls] == [CommandOperation.ESTOP]


def test_estop_latches_with_no_eligible_aircraft_and_performs_no_adapter_io() -> None:
    snapshot = make_snapshot(0, selection=(), armed=False)
    controller, _, _, _, flight, camera = make_stack(snapshot)

    result = controller.execute(make_intent(IntentName.ESTOP, selection=()), snapshot)

    assert result.status is LifecycleStatus.COMPLETED
    assert result.plan is not None
    assert result.plan.estop_update is True
    assert result.plan.commands == ()
    assert flight.calls == []
    assert camera.calls == []


@pytest.mark.parametrize(
    ("name", "confirm", "expected_operation"),
    [
        (IntentName.HOLD, False, CommandOperation.HOVER),
        (IntentName.LAND_ALL, True, CommandOperation.LAND),
    ],
)
def test_safe_hold_and_land_execute_for_degraded_aircraft_without_authority(
    name: IntentName,
    confirm: bool,
    expected_operation: CommandOperation,
) -> None:
    snapshot = replace_aircraft(
        make_snapshot(1, selection=(1,)),
        1,
        membership=MembershipState.DEGRADED,
        control_authority=False,
        rc_safety_operator_present=False,
    )
    controller, _, _, _, flight, _ = make_stack(snapshot)

    result = controller.execute(make_intent(name, selection=(1,), confirm=confirm), snapshot)

    assert result.status is LifecycleStatus.COMPLETED
    assert flight.calls[-1].operation is expected_operation


@pytest.mark.parametrize(
    ("name", "args", "confirm"),
    [
        (IntentName.TRANSLATE, {"dx": 1, "dy": 0}, False),
        (IntentName.COME_HOME, {}, False),
        (
            IntentName.CAPTURE_ROOM,
            {"room_id": "room", "capture_id": "capture", "pattern": "pano_360"},
            True,
        ),
    ],
)
def test_non_safety_flight_and_camera_intents_require_session_arm_authorization(
    name: IntentName, args: dict[str, object], confirm: bool
) -> None:
    snapshot = replace(make_snapshot(1, selection=(1,)), armed=False)
    controller, _, _, _, flight, camera = make_stack(snapshot)

    result = controller.execute(
        make_intent(name, selection=(1,), args=args, confirm=confirm), snapshot
    )

    assert result.status is LifecycleStatus.REFUSED
    assert result.refusal is not None
    assert result.refusal.reason is RefusalReason.ARMED_REQUIRED
    assert flight.calls == []
    assert camera.calls == []


@pytest.mark.parametrize(
    ("name", "args", "confirm"),
    [
        (IntentName.TRANSLATE, {"dx": 1, "dy": 0}, False),
        (
            IntentName.CAPTURE_ROOM,
            {"room_id": "room", "capture_id": "capture", "pattern": "pano_360"},
            True,
        ),
    ],
)
def test_non_safety_intents_require_physical_armed_evidence(
    name: IntentName, args: dict[str, object], confirm: bool
) -> None:
    snapshot = replace_aircraft(make_snapshot(1, selection=(1,)), 1, armed=False)
    controller, _, _, _, flight, camera = make_stack(snapshot)

    result = controller.execute(
        make_intent(name, selection=(1,), args=args, confirm=confirm), snapshot
    )

    assert result.status is LifecycleStatus.REFUSED
    assert result.refusal is not None
    assert result.refusal.reason is RefusalReason.ARMED_REQUIRED
    assert flight.calls == []
    assert camera.calls == []


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"flight_state": FlightState.LANDED}, RefusalReason.INVALID_STATE),
        ({"control_authority": False}, RefusalReason.CONTROL_AUTHORITY),
        ({"rc_safety_operator_present": False}, RefusalReason.RC_SAFETY_OPERATOR_ABSENT),
        ({"physical_rc_available": False}, RefusalReason.CONTROL_AUTHORITY),
        ({"link_last_seen_ms": NOW_MS - 2000}, RefusalReason.LINK_STALE),
        ({"membership": MembershipState.REGISTERED}, RefusalReason.AIRCRAFT_NOT_READY),
    ],
)
def test_selected_land_preflights_every_target_before_any_io(change, reason) -> None:
    snapshot = replace_aircraft(make_snapshot(3, selection=(1, 2)), 2, **change)
    controller, _, _, _, flight, _ = make_stack(snapshot)
    result = controller.execute(
        make_intent(IntentName.LAND, selection=(1, 2), confirm=True), snapshot
    )
    assert result.refusal is not None
    assert result.refusal.reason is reason
    assert flight.calls == []


@pytest.mark.parametrize(
    ("selection", "confirm", "reason"),
    [
        ((1,), False, RefusalReason.CONFIRMATION_REQUIRED),
        ((2,), True, RefusalReason.STALE_SELECTION),
        ((), True, RefusalReason.INVALID_SELECTION),
    ],
)
def test_selected_land_requires_confirmation_and_current_nonempty_selection(
    selection, confirm, reason
) -> None:
    snapshot = make_snapshot(2, selection=() if not selection else (1,))
    controller, _, _, _, flight, _ = make_stack(snapshot)
    result = controller.execute(
        make_intent(IntentName.LAND, selection=selection, confirm=confirm), snapshot
    )
    assert result.refusal is not None
    assert result.refusal.reason is reason
    assert flight.calls == []


def test_selected_land_can_descend_with_critical_battery_and_lost_position() -> None:
    snapshot = replace_aircraft(make_snapshot(1), 1, battery=0.01, position_quality=0.0)
    controller, _, _, _, flight, _ = make_stack(snapshot)
    result = controller.execute(
        make_intent(IntentName.LAND, selection=(1,), confirm=True), snapshot
    )
    assert result.status is LifecycleStatus.COMPLETED
    assert [call.operation for call in flight.calls] == [CommandOperation.LAND]


def test_degraded_selected_land_passes_intent_plan_and_command_preflight() -> None:
    snapshot = replace_aircraft(
        make_snapshot(2, selection=(1,)), 1, membership=MembershipState.DEGRADED
    )
    controller, planner, arbiter, _, flight, _ = make_stack(snapshot)
    intent = make_intent(IntentName.LAND, selection=(1,), confirm=True)

    assert arbiter.check_intent(intent, snapshot) is None
    plan = planner.plan(intent, snapshot)
    assert isinstance(plan, Plan)
    assert arbiter.check_plan(plan, snapshot) is None
    assert arbiter.check_command(plan, plan.commands[0], snapshot) is None

    result = controller.execute(intent, snapshot)

    assert result.status is LifecycleStatus.COMPLETED
    assert [(call.operation, call.drone_ids) for call in flight.calls] == [
        (CommandOperation.LAND, (1,))
    ]


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"control_authority": False}, RefusalReason.CONTROL_AUTHORITY),
        ({"rc_safety_operator_present": False}, RefusalReason.RC_SAFETY_OPERATOR_ABSENT),
        ({"link_last_seen_ms": NOW_MS - 2000}, RefusalReason.LINK_STALE),
        ({"connection_epoch": 2}, RefusalReason.STALE_CONNECTION_EPOCH),
    ],
)
def test_degraded_selected_land_rechecks_authority_link_and_epoch_before_io(change, reason) -> None:
    snapshot = replace_aircraft(make_snapshot(1), 1, membership=MembershipState.DEGRADED)
    _, planner, arbiter, dispatcher, flight, _ = make_stack(snapshot)
    intent = make_intent(IntentName.LAND, selection=(1,), confirm=True)
    assert arbiter.check_intent(intent, snapshot) is None
    plan = planner.plan(intent, snapshot)
    assert isinstance(plan, Plan)

    result = dispatcher.dispatch(plan, replace_aircraft(snapshot, 1, **change))

    assert result.refusal is not None
    assert result.refusal.reason is reason
    assert flight.calls == []


@pytest.mark.parametrize("corruption", ["missing", "extra", "duplicate", "epoch", "bypass"])
def test_selected_land_rejects_altered_plan_before_any_io(corruption: str) -> None:
    snapshot = make_snapshot(3, selection=(1, 2))
    _, planner, _, dispatcher, flight, _ = make_stack(snapshot)
    plan = planner.plan(make_intent(IntentName.LAND, selection=(1, 2), confirm=True), snapshot)
    assert isinstance(plan, Plan)
    first, second = plan.commands
    commands = {
        "missing": (first,),
        "extra": (first, second, replace(second, drone_id=3)),
        "duplicate": (first, first),
        "epoch": (first, replace(second, connection_epoch=2)),
        "bypass": (first, replace(second, safety_action=True)),
    }[corruption]
    result = dispatcher.dispatch(replace(plan, commands=commands), snapshot)
    assert result.refusal is not None
    assert result.refusal.reason is (
        RefusalReason.STALE_CONNECTION_EPOCH
        if corruption == "epoch"
        else RefusalReason.INVALID_PLAN
    )
    assert flight.calls == []
