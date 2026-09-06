import json
from dataclasses import replace

import pytest

from planner.models import (
    CommandOperation,
    FlightState,
    LifecycleStatus,
    Position,
    PreparedExecution,
)
from planner.navigation import ArrivalSlot, NavigationDispatchAcceptance, NavigationPermission
from planner.navigation_runtime import NavigationExecutionConfig, NavigationRuntime
from planner.test_navigation import MOTION, artifact, pose
from relay.capabilities import C1_IMPLEMENTED_INTENT_NAMES, CapabilityProfile
from relay.intent_v1 import AcceptedIntent, IntentName, RejectedIntent, validate_intent
from tests.autonomy_fixtures import make_intent, make_snapshot, make_stack, replace_aircraft

PROFILE = CapabilityProfile(
    "mapped_navigation", C1_IMPLEMENTED_INTENT_NAMES | {IntentName.NAVIGATE}
)


def stack(count=1, *, blocked=frozenset()):
    snapshot = make_snapshot(count)
    for drone_id in snapshot.selection:
        snapshot = replace_aircraft(
            snapshot, drone_id, pose=Position(0.5, 1.5 + 2 * (drone_id - 1), 1)
        )
    slots = tuple(
        ArrivalSlot(f"atrium-{i}", "atrium", pose(6.5, 1.5 + 2 * (i - 1)), 0.5, 0.5)
        for i in snapshot.selection
    )
    current_map = [artifact(blocked, slots=slots)]

    def accept(plan, map_artifact):
        return NavigationDispatchAcceptance(
            "test-runtime-acceptance",
            plan.map_pin,
            plan.geometry_pin,
            plan.navigation_pin,
            plan.plan_revision,
        )

    runtime = NavigationRuntime(
        lambda: current_map[0],
        NavigationExecutionConfig(
            "level_1",
            MOTION,
            0.5,
            0.05,
            500,
            0.5,
            5_000,
        ),
        NavigationPermission(frozenset({"atrium"})),
        dispatch_acceptance=accept,
    )
    controller, planner, _, dispatcher, flight, _ = make_stack(snapshot, capability_profile=PROFILE)
    planner.navigation = runtime
    dispatcher.navigation = runtime
    clock = [snapshot.now_ms]

    def current():
        clock[0] += 1
        drones = {
            drone_id: replace(
                snapshot.aircraft[drone_id],
                pose=drone.pose,
                flight_state=drone.flight_state,
                position_last_seen_ms=clock[0],
            )
            for drone_id, drone in flight.aircraft.items()
        }
        return replace(snapshot, now_ms=clock[0], aircraft=drones)

    intent = make_intent(
        IntentName.NAVIGATE, selection=snapshot.selection, args={"zone_id": "atrium"}, confirm=True
    )
    return controller, dispatcher, flight, snapshot, current, current_map, intent


@pytest.mark.parametrize("count", [1, 2])
def test_route_executes_sequentially_and_requires_observed_arrival_hold(count):
    controller, _, flight, snapshot, current, _, intent = stack(count)
    prepared = controller.prepare(intent, snapshot, current_snapshot=current)
    assert isinstance(prepared, PreparedExecution)
    frozen = prepared.plan.to_dict()["navigation"]
    assert json.loads(json.dumps(prepared.plan.to_dict()))["navigation"]
    result = controller.dispatch_prepared(prepared, current_snapshot=current)
    assert result.status is LifecycleStatus.COMPLETED, result.refusal
    assert frozen == result.plan.to_dict()["navigation"]
    assert [call.operation for call in flight.calls].count("hover") == count
    assert all(item.flight_state is FlightState.HOVERING for item in flight.aircraft.values())
    assert all(item.pose.x == 6.5 for item in flight.aircraft.values())
    ids = [call.drone_ids[0] for call in flight.calls]
    assert ids == sorted(ids)


def test_acknowledgement_without_new_position_evidence_cannot_advance_route():
    controller, _, flight, snapshot, _, _, intent = stack(blocked=frozenset({(3, 1)}))
    result = controller.execute(intent, snapshot, current_snapshot=lambda: snapshot)
    assert result.status is not LifecycleStatus.COMPLETED
    assert len([call for call in flight.calls if call.operation == "goto"]) == 1
    assert flight.calls[-1].operation == "hover"


@pytest.mark.parametrize("change", ["map", "selection", "epoch", "stale", "config"])
def test_changed_confirmation_inputs_dispatch_no_motion(change):
    controller, dispatcher, flight, snapshot, current, maps, intent = stack()
    prepared = controller.prepare(intent, snapshot, current_snapshot=current)
    assert isinstance(prepared, PreparedExecution)
    if change == "map":
        maps[0] = replace(maps[0], map_pin=replace(maps[0].map_pin, version="new-map"))
    elif change == "config":
        dispatcher.navigation.config = replace(dispatcher.navigation.config, speed_m_s=0.25)
    original = current

    def changed():
        value = original()
        if change == "selection":
            return replace(value, selection=())
        if change == "epoch":
            return replace_aircraft(value, 1, connection_epoch=2)
        if change == "stale":
            return replace_aircraft(value, 1, position_last_seen_ms=value.now_ms - 501)
        return value

    result = controller.dispatch_prepared(prepared, current_snapshot=changed)
    assert result.status is not LifecycleStatus.COMPLETED
    assert not any(call.operation == "goto" for call in flight.calls)


def test_cancelled_execution_owner_cannot_launch_later_segments():
    controller, dispatcher, flight, snapshot, current, _, intent = stack(
        blocked=frozenset({(3, 1)})
    )
    prepared = controller.prepare(intent, snapshot, current_snapshot=current)
    assert isinstance(prepared, PreparedExecution)
    result = dispatcher.dispatch(
        prepared.plan,
        snapshot,
        current_snapshot=current,
        owner_still_valid=lambda: not flight.calls,
    )
    assert result.status is LifecycleStatus.INVALIDATED
    assert len(flight.calls) == 1


def test_navigation_is_disabled_by_default_and_requires_confirmed_selection():
    raw = dict(
        v=1,
        t=100,
        type="intent",
        intent_id="navigate-1",
        source="console",
        session="test",
        name="navigate",
        args={"zone_id": "atrium"},
        selection=[1],
        mode="indoor",
        confirm=True,
    )
    assert isinstance(validate_intent(raw), RejectedIntent)
    assert isinstance(validate_intent(raw, capability_profile=PROFILE), AcceptedIntent)
    for changes in ({"confirm": False}, {"selection": []}, {"args": {"x": 1, "y": 2}}):
        assert isinstance(
            validate_intent(raw | changes, capability_profile=PROFILE), RejectedIntent
        )


def test_search_camera_prelude_is_checked_without_waypoint_arrival() -> None:
    from planner.navigation_runtime import SearchCameraPreparation

    _, _, _, snapshot, _, _, intent = stack()
    runtime = NavigationRuntime(
        lambda: artifact(slots=(ArrivalSlot("atrium-1", "atrium", pose(6.5, 1.5), 0.5, 0.5),)),
        NavigationExecutionConfig("level_1", MOTION, 0.5, 0.05, 500, 0.5, 5_000),
        NavigationPermission(frozenset({"atrium"})),
    )
    normal = runtime.prepare(intent, snapshot)
    assert normal.navigation is not None
    route = normal.navigation.route
    plan = runtime.prepare_route(
        intent,
        snapshot,
        route,
        search_camera_preparations=(SearchCameraPreparation(1, 1, -90),),
    )

    assert [command.operation for command in plan.commands[:2]] == [
        CommandOperation.SET_GIMBAL_PITCH,
        CommandOperation.CAMERA_READY,
    ]
    assert runtime.check(plan, plan.commands[0], snapshot) is None
    assert runtime.check(plan, plan.commands[1], snapshot, completed=True) is None


def test_navigation_watchdog_invalidates_expired_executing_command() -> None:
    from adapters.test_dispatch import ExecutingFlight

    controller, dispatcher, _, snapshot, current, _, intent = stack()
    flight = ExecutingFlight.from_snapshot(snapshot)
    dispatcher.flight = flight
    prepared = controller.prepare(intent, snapshot, current_snapshot=current)
    assert isinstance(prepared, PreparedExecution)
    pending = dispatcher.dispatch(prepared.plan, snapshot, current_snapshot=current)
    expired = replace(snapshot, now_ms=snapshot.now_ms + 5_010)

    result = dispatcher.expire_navigation(prepared.plan, pending, expired)

    assert result is not None
    assert result.status is LifecycleStatus.INVALIDATED
    assert result.refusal is not None
    assert result.refusal.reason.name == "ADAPTER_TIMEOUT"
    assert flight.calls[-1].operation == "hover"


def test_navigation_completion_callback_runs_after_each_checked_command() -> None:
    controller, dispatcher, _, snapshot, current, _, intent = stack()
    completed = []
    dispatcher.on_navigation_command_completed = lambda plan, command, state: completed.append(
        (plan.intent_id, command.command_id, state.now_ms)
    )
    prepared = controller.prepare(intent, snapshot, current_snapshot=current)
    assert isinstance(prepared, PreparedExecution)

    result = controller.dispatch_prepared(prepared, current_snapshot=current)

    assert result.status is LifecycleStatus.COMPLETED
    assert [command_id for _, command_id, _ in completed] == [
        command.command_id for command in prepared.plan.commands
    ]


def test_navigation_watchdog_does_not_hold_or_return_after_owner_is_retired() -> None:
    from adapters.test_dispatch import ExecutingFlight

    controller, dispatcher, _, snapshot, current, _, intent = stack()
    flight = ExecutingFlight.from_snapshot(snapshot)
    dispatcher.flight = flight
    prepared = controller.prepare(intent, snapshot, current_snapshot=current)
    assert isinstance(prepared, PreparedExecution)
    pending = dispatcher.dispatch(prepared.plan, snapshot, current_snapshot=current)
    expired = replace(snapshot, now_ms=snapshot.now_ms + 5_010)
    checks = 0

    def owner_still_valid() -> bool:
        nonlocal checks
        checks += 1
        return checks < 3

    result = dispatcher.expire_navigation(
        prepared.plan,
        pending,
        expired,
        owner_still_valid=owner_still_valid,
    )

    assert result is None
    assert [call.operation for call in flight.calls] == ["goto"]


def test_phone_authorization_binds_each_goto_to_the_frozen_route() -> None:
    controller, dispatcher, _, snapshot, current, _, intent = stack()
    dispatcher.navigation.require_phone_authorization = True

    prepared = controller.prepare(intent, snapshot, current_snapshot=current)

    assert isinstance(prepared, PreparedExecution)
    gotos = [
        command for command in prepared.plan.commands if command.operation is CommandOperation.GOTO
    ]
    assert gotos
    assert all(command.parameters["navigation_route_id"] == intent.intent_id for command in gotos)
