from dataclasses import replace
from math import inf, nan

import pytest

from planner.models import (
    AltitudeGrounding,
    CommandOperation,
    FlightState,
    LifecycleStatus,
    Position,
    PreparedExecution,
    RefusalReason,
)
from planner.planner import DeterministicPlanner
from relay.capabilities import C1_CAPABILITY_PROFILE, CapabilityProfile
from relay.intent_v1 import AcceptedIntent, IntentName, RejectedIntent, validate_intent
from tests.autonomy_fixtures import (
    make_intent,
    make_snapshot,
    make_stack,
    planning_config,
    replace_aircraft,
)

NO_ALTITUDE_PROFILE = CapabilityProfile(
    "c1_without_altitude",
    C1_CAPABILITY_PROFILE.enabled_intent_names - {IntentName.ALTITUDE},
)


def config(**changes):
    configured = replace(
        planning_config(),
        altitude_step_m=0.5,
        altitude_floor_z_m=0,
        altitude_configuration_id="survey-floor-1-v1",
        altitude_completion_tolerance_m=0.05,
    )
    return replace(configured, **changes)


def wire(args, selection=(1, 2)):
    return {
        "v": 1,
        "t": 100_000,
        "type": "intent",
        "intent_id": "altitude-test",
        "source": "console",
        "session": "test-session",
        "name": "altitude",
        "args": args,
        "selection": list(selection),
        "mode": "indoor",
        "confirm": True,
    }


def sim_snapshot(snapshot, flight):
    """Project the simulator's terminal state as fresh authoritative telemetry."""
    now_ms = snapshot.now_ms + len(flight.calls)
    current = replace(snapshot, now_ms=now_ms)
    for drone_id, simulated in flight.aircraft.items():
        current = replace_aircraft(
            current,
            drone_id,
            pose=simulated.pose,
            flight_state=simulated.flight_state,
            armed=simulated.armed,
            position_last_seen_ms=now_ms,
            link_last_seen_ms=now_ms,
        )
    return current


def execute_with_sim_state(controller, flight, intent, snapshot):
    return controller.execute(
        intent,
        snapshot,
        current_snapshot=lambda: sim_snapshot(snapshot, flight),
    )


@pytest.mark.parametrize("args", [{"delta": -1}, {"delta": 0.6096}, {"delta": 0}])
def test_altitude_forms_validate_without_changing_step_units(args):
    result = validate_intent(wire(args))
    assert isinstance(result, AcceptedIntent)
    assert dict(result.intent.args) == args


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"delta": 1, "height_m": 1},
        {"height_m": 0},
        {"height_m": -1},
        {"height_m": True},
        {"delta": False},
        {"delta": nan},
        {"delta": 10**400},
        {"height_m": inf},
        {"height_m": "5"},
        {"meters": 1},
        {"delta": 1, "floor_z_m": 99},
    ],
)
def test_malformed_altitude_forms_never_reach_planning(args):
    assert isinstance(validate_intent(wire(args)), RejectedIntent)


def test_disabled_deployment_refuses_without_adapter_calls():
    snapshot = make_snapshot()
    disabled = replace(
        planning_config(),
        altitude_step_m=None,
        altitude_floor_z_m=None,
        altitude_configuration_id=None,
        altitude_completion_tolerance_m=None,
    )
    controller, planner, _, _, flight, _ = make_stack(
        snapshot,
        config=disabled,
        capability_profile=NO_ALTITUDE_PROFILE,
    )
    result = controller.execute(make_intent(IntentName.ALTITUDE, args={"delta": 1}), snapshot)
    assert result.status is LifecycleStatus.REFUSED
    assert result.refusal.reason is RefusalReason.UNSUPPORTED
    assert planner.config.altitude_grounding() is None
    assert flight.calls == []


def test_missing_floor_allows_relative_motion():
    snapshot = make_snapshot()
    enabled = replace(config(), altitude_floor_z_m=None)
    controller, _, _, _, flight, _ = make_stack(snapshot, config=enabled)
    relative = execute_with_sim_state(
        controller, flight, make_intent(IntentName.ALTITUDE, args={"delta": 1}), snapshot
    )
    assert relative.status is LifecycleStatus.COMPLETED
    assert all(a.pose.z == 1.5 for a in flight.aircraft.values())


@pytest.mark.parametrize(
    "delta,expected", [(0.6096, [1.3048, 2.3048]), (-0.6096, [0.6952, 1.6952])]
)
def test_one_foot_relative_motion_preserves_starting_offsets_and_holds(delta, expected):
    snapshot = replace_aircraft(make_snapshot(3, selection=(1, 2)), 2, pose=Position(2, 0, 2))
    accepted = validate_intent(wire({"delta": delta}))
    assert isinstance(accepted, AcceptedIntent)
    controller, _, _, _, flight, _ = make_stack(snapshot, config=config())
    result = execute_with_sim_state(controller, flight, accepted.intent, snapshot)
    assert result.status is LifecycleStatus.COMPLETED
    for drone_id, height in zip((1, 2), expected, strict=True):
        assert flight.aircraft[drone_id].pose == Position(
            snapshot.aircraft[drone_id].pose.x, 0, height
        )
        assert flight.aircraft[drone_id].flight_state is FlightState.HOVERING
    assert flight.aircraft[3].pose == snapshot.aircraft[3].pose
    assert [call.operation for call in flight.calls] == [
        CommandOperation.GOTO,
        CommandOperation.HOVER,
    ] * 2


def test_relative_height_preserves_different_starting_heights():
    snapshot = replace_aircraft(make_snapshot(), 2, pose=Position(2, 0, 3))
    controller, _, _, _, flight, _ = make_stack(
        snapshot, config=replace(config(), altitude_floor_z_m=1)
    )
    accepted = validate_intent(wire({"delta": 1}))
    result = execute_with_sim_state(controller, flight, accepted.intent, snapshot)
    assert result.status is LifecycleStatus.COMPLETED
    assert [a.pose.z for a in flight.aircraft.values()] == [1.5, 3.5]
    assert result.plan.to_dict()["altitude_grounding"] == {
        "step_m": 0.5,
        "floor_z_m": 1,
        "configuration_id": "survey-floor-1-v1",
        "completion_tolerance_m": 0.05,
    }


@pytest.mark.parametrize("delta,heights,order", [(1, [1, 2], (2, 1)), (-1, [1, 2], (1, 2))])
def test_vertical_column_moves_leading_aircraft_first(delta, heights, order):
    snapshot = make_snapshot()
    for drone_id, height in zip((1, 2), heights, strict=True):
        snapshot = replace_aircraft(snapshot, drone_id, pose=Position(0, 0, height))
    controller, _, _, _, flight, _ = make_stack(snapshot, config=config())
    result = execute_with_sim_state(
        controller,
        flight,
        make_intent(IntentName.ALTITUDE, args={"delta": delta}),
        snapshot,
    )
    assert result.status is LifecycleStatus.COMPLETED
    assert (
        tuple(call.drone_ids[0] for call in flight.calls if call.operation is CommandOperation.GOTO)
        == order
    )


def test_intermediate_stationary_aircraft_blocks_vertical_path_before_io():
    snapshot = replace_aircraft(make_snapshot(2, selection=(1,)), 2, pose=Position(0, 0, 2))
    controller, _, _, _, flight, _ = make_stack(snapshot, config=config())
    result = controller.execute(
        make_intent(IntentName.ALTITUDE, selection=(1,), args={"delta": 4}), snapshot
    )
    assert result.status is LifecycleStatus.REFUSED
    assert result.refusal.reason is RefusalReason.SPACING
    assert flight.calls == []


@pytest.mark.parametrize(
    "change",
    [
        {"altitude_step_m": None},
        {"altitude_step_m": 1},
        {"altitude_floor_z_m": 1},
        {"altitude_configuration_id": "resurvey-v2"},
        {"altitude_completion_tolerance_m": 0.1},
    ],
)
def test_grounding_change_invalidates_prepared_altitude_before_io(change):
    snapshot = make_snapshot()
    controller, _, _, _, flight, _ = make_stack(snapshot, config=config())
    prepared = controller.prepare(make_intent(IntentName.ALTITUDE, args={"delta": 1}), snapshot)
    assert isinstance(prepared, PreparedExecution)
    profile = NO_ALTITUDE_PROFILE if change == {"altitude_step_m": None} else C1_CAPABILITY_PROFILE
    controller.planner = DeterministicPlanner(replace(config(), **change), profile)
    result = controller.dispatch_prepared(prepared)
    assert result.status is LifecycleStatus.REFUSED
    assert "configuration changed" in result.refusal.detail
    assert flight.calls == []


@pytest.mark.parametrize("state", [FlightState.LANDED, FlightState.DISARMED, FlightState.ARMED])
def test_grounded_selected_aircraft_never_implicitly_take_off(state):
    snapshot = replace_aircraft(make_snapshot(), 2, flight_state=state, pose=Position(2, 0, 0))
    controller, _, _, _, flight, _ = make_stack(snapshot, config=config())
    result = controller.execute(make_intent(IntentName.ALTITUDE, args={"delta": 1}), snapshot)
    assert result.status is LifecycleStatus.REFUSED
    assert flight.calls == []


@pytest.mark.parametrize("target", [0, -1, 5])
def test_floor_or_ceiling_violation_refuses_entire_move(target):
    snapshot = make_snapshot()
    controller, _, _, _, flight, _ = make_stack(snapshot, config=config())
    result = controller.execute(
        make_intent(IntentName.ALTITUDE, args={"delta": (target - 1) / 0.5}), snapshot
    )
    assert result.status is LifecycleStatus.REFUSED
    assert flight.calls == []


def test_stale_selected_and_unselected_positions_are_refused():
    for stale_id in (1, 2):
        snapshot = replace_aircraft(
            make_snapshot(2, selection=(1,)), stale_id, position_last_seen_ms=0
        )
        controller, _, _, _, flight, _ = make_stack(snapshot, config=config())
        result = controller.execute(
            make_intent(IntentName.ALTITUDE, selection=(1,), args={"delta": 1}), snapshot
        )
        assert result.status is LifecycleStatus.REFUSED
        assert result.refusal.reason is RefusalReason.POSITION_STALE
        assert flight.calls == []


def test_hold_waits_for_terminal_movement_ack(monkeypatch):
    snapshot = make_snapshot(1)
    controller, _, _, dispatcher, flight, _ = make_stack(snapshot, config=config())
    original = flight.goto
    completed = []

    def moving(*args, **kwargs):
        ack = original(*args, **kwargs)
        completed.append(ack)
        return replace(ack, status=LifecycleStatus.EXECUTING)

    monkeypatch.setattr(flight, "goto", moving)
    intent = make_intent(IntentName.ALTITUDE, selection=(1,), args={"delta": 1})

    def provider():
        return sim_snapshot(snapshot, flight)

    pending = controller.execute(intent, snapshot, current_snapshot=provider)
    assert pending.status is LifecycleStatus.EXECUTING
    assert [call.operation for call in flight.calls] == [CommandOperation.GOTO]
    terminal = replace(pending.acknowledgements[-1], status=LifecycleStatus.COMPLETED)
    result = dispatcher.resume_after_completion(
        pending.plan, pending, terminal, snapshot, current_snapshot=provider
    )
    assert result.status is LifecycleStatus.COMPLETED
    assert [call.operation for call in flight.calls] == [
        CommandOperation.GOTO,
        CommandOperation.HOVER,
    ]


def test_altitude_and_horizontal_motion_conflict_holds_without_moving():
    snapshot = make_snapshot()
    controller, _, _, _, flight, _ = make_stack(snapshot, config=config())
    result = controller.execute_pair(
        make_intent(IntentName.ALTITUDE, args={"delta": 1}),
        make_intent(IntentName.TRANSLATE, args={"dx": 1, "dy": 0}, intent_id="horizontal"),
        snapshot,
    )
    assert result.resolution.hold_required is True
    assert all(call.operation is CommandOperation.HOVER for call in flight.calls)


@pytest.mark.parametrize(
    "step,floor,identity,tolerance",
    [
        (0, 0, "v1", 0.05),
        (nan, 0, "v1", 0.05),
        (True, 0, "v1", 0.05),
        (0.5, inf, "v1", 0.05),
        (0.5, False, "v1", 0.05),
        (0.5, 0, "", 0.05),
        (0.5, 0, "v1", 0),
        (0.5, 0, "v1", inf),
    ],
)
def test_invalid_grounding_configuration_is_rejected(step, floor, identity, tolerance):
    with pytest.raises(ValueError):
        AltitudeGrounding(step, floor, identity, tolerance)


@pytest.mark.parametrize(
    "change",
    [
        {"altitude_step_m": 10**400},
        {"altitude_floor_z_m": 10**400},
        {"altitude_completion_tolerance_m": 10**400},
    ],
)
def test_oversized_grounding_configuration_is_a_value_error(change):
    with pytest.raises(ValueError):
        config(**change)


@pytest.mark.parametrize("kind", ["xy_drift", "stale_position", "configuration"])
def test_changes_after_first_move_stop_later_dispatch_without_horizontal_correction(
    monkeypatch, kind
):
    snapshot = make_snapshot()
    controller, _, _, _, flight, _ = make_stack(snapshot, config=config())
    current = snapshot
    original = flight.goto

    def first_move(*args, **kwargs):
        nonlocal current
        ack = original(*args, **kwargs)
        if kind == "configuration":
            controller.planner = DeterministicPlanner(
                replace(config(), altitude_configuration_id="v2")
            )
        elif kind == "xy_drift":
            current = replace_aircraft(current, 2, pose=Position(2.1, 0, 1))
        else:
            current = replace_aircraft(current, 2, position_last_seen_ms=0)
        return ack

    monkeypatch.setattr(flight, "goto", first_move)
    result = controller.execute(
        make_intent(IntentName.ALTITUDE, args={"delta": 1}),
        snapshot,
        current_snapshot=lambda: current,
    )
    assert result.status is LifecycleStatus.REFUSED
    moves = [call for call in flight.calls if call.operation is CommandOperation.GOTO]
    assert len(moves) == 1 and moves[0].drone_ids == (1,)
    assert all(
        call.operation in (CommandOperation.GOTO, CommandOperation.HOVER) for call in flight.calls
    )


def test_configuration_change_while_waiting_for_completion_blocks_resume(monkeypatch):
    snapshot = make_snapshot()
    controller, _, _, dispatcher, flight, _ = make_stack(snapshot, config=config())
    original = flight.goto

    def moving(*args, **kwargs):
        return replace(original(*args, **kwargs), status=LifecycleStatus.EXECUTING)

    monkeypatch.setattr(flight, "goto", moving)
    pending = controller.execute(make_intent(IntentName.ALTITUDE, args={"delta": 1}), snapshot)
    assert pending.status is LifecycleStatus.EXECUTING
    controller.planner = DeterministicPlanner(
        replace(config(), altitude_step_m=None), NO_ALTITUDE_PROFILE
    )
    terminal = replace(pending.acknowledgements[-1], status=LifecycleStatus.COMPLETED)
    result = dispatcher.resume_after_completion(pending.plan, pending, terminal, snapshot)
    assert result.status is LifecycleStatus.INVALIDATED
    assert "configuration" in result.refusal.detail
    assert sum(call.operation is CommandOperation.GOTO for call in flight.calls) == 1


def test_relative_altitude_respects_signed_building_floor(monkeypatch):
    from planner.models import Geofence

    snapshot = replace_aircraft(make_snapshot(1), 1, pose=Position(0, 0, -1))
    controller, _, arbiter, _, flight, _ = make_stack(
        snapshot, config=replace(config(), altitude_floor_z_m=-2)
    )
    arbiter.config = replace(arbiter.config, geofence=Geofence(-10, 10, -10, 10, -3, 5))
    result = execute_with_sim_state(
        controller,
        flight,
        make_intent(IntentName.ALTITUDE, selection=(1,), args={"delta": 1}),
        snapshot,
    )
    assert result.status is LifecycleStatus.COMPLETED
    assert flight.aircraft[1].pose.z == pytest.approx(-0.5)


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("change", ["configuration", "stale_position"])
def test_final_hover_cannot_complete_with_changed_grounding_or_stale_position(
    monkeypatch, asynchronous, change
):
    snapshot = make_snapshot(1)
    current = snapshot
    controller, _, _, dispatcher, flight, _ = make_stack(snapshot, config=config())
    original = flight.hover

    def change_state():
        nonlocal current
        if change == "configuration":
            controller.planner = DeterministicPlanner(
                replace(config(), altitude_configuration_id="v2")
            )
        else:
            current = replace_aircraft(current, 1, position_last_seen_ms=0)

    def final_hover(*args, **kwargs):
        acknowledgements = original(*args, **kwargs)
        if asynchronous:
            return tuple(replace(ack, status=LifecycleStatus.EXECUTING) for ack in acknowledgements)
        change_state()
        return acknowledgements

    monkeypatch.setattr(flight, "hover", final_hover)
    intent = make_intent(IntentName.ALTITUDE, selection=(1,), args={"delta": 1})
    result = controller.execute(intent, snapshot, current_snapshot=lambda: current)
    if asynchronous:
        assert result.status is LifecycleStatus.EXECUTING
        change_state()
        terminal = replace(result.acknowledgements[-1], status=LifecycleStatus.COMPLETED)
        result = dispatcher.resume_after_completion(
            result.plan, result, terminal, snapshot, current_snapshot=lambda: current
        )
    assert result.status in {LifecycleStatus.REFUSED, LifecycleStatus.INVALIDATED}
    assert result.refusal is not None
    assert sum(call.operation is CommandOperation.GOTO for call in flight.calls) == 1


def test_final_hover_requires_fresh_measured_target_attainment():
    snapshot = make_snapshot(1)
    controller, _, _, _, flight, _ = make_stack(snapshot, config=config())

    def wrong_altitude_after_hover():
        current = sim_snapshot(snapshot, flight)
        if any(call.operation is CommandOperation.HOVER for call in flight.calls):
            current = replace_aircraft(
                current,
                1,
                pose=Position(0, 0, 1.2),
                position_last_seen_ms=current.now_ms,
            )
        return current

    result = controller.execute(
        make_intent(IntentName.ALTITUDE, selection=(1,), args={"delta": 1}),
        snapshot,
        current_snapshot=wrong_altitude_after_hover,
    )

    assert result.status is LifecycleStatus.REFUSED
    assert result.refusal is not None
    assert result.refusal.reason is RefusalReason.INVALID_STATE
    assert "has not reached" in result.refusal.detail
    assert sum(call.operation is CommandOperation.GOTO for call in flight.calls) == 1


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize(
    ("violation", "expected_reason"),
    [
        ("ceiling", RefusalReason.CEILING),
        ("floor", RefusalReason.INVALID_STATE),
        ("spacing", RefusalReason.SPACING),
    ],
)
def test_final_hover_rejects_hard_attained_geometry(
    monkeypatch, asynchronous, violation, expected_reason
):
    count = 2 if violation == "spacing" else 1
    snapshot = make_snapshot(count, selection=(1,))
    altitude_config = (
        replace(config(), altitude_floor_z_m=0.5) if violation == "floor" else config()
    )
    controller, _, _, dispatcher, flight, _ = make_stack(snapshot, config=altitude_config)
    original = flight.hover

    def hover(*args, **kwargs):
        acknowledgements = original(*args, **kwargs)
        if asynchronous:
            return tuple(replace(ack, status=LifecycleStatus.EXECUTING) for ack in acknowledgements)
        return acknowledgements

    monkeypatch.setattr(flight, "hover", hover)
    delta = {"ceiling": 5.98, "floor": -0.98, "spacing": 1.0}[violation]

    def provider():
        current = sim_snapshot(snapshot, flight)
        if any(call.operation is CommandOperation.HOVER for call in flight.calls):
            target_z = {"ceiling": 4.01, "floor": 0.49, "spacing": 1.5}[violation]
            current = replace_aircraft(current, 1, pose=Position(0, 0, target_z))
            if violation == "spacing":
                current = replace_aircraft(current, 2, pose=Position(0.79, 0, target_z))
        return current

    result = controller.execute(
        make_intent(IntentName.ALTITUDE, selection=(1,), args={"delta": delta}),
        snapshot,
        current_snapshot=provider,
    )
    if asynchronous:
        assert result.status is LifecycleStatus.EXECUTING
        terminal = replace(result.acknowledgements[-1], status=LifecycleStatus.COMPLETED)
        result = dispatcher.resume_after_completion(
            result.plan,
            result,
            terminal,
            snapshot,
            current_snapshot=provider,
        )

    assert result.status in {LifecycleStatus.REFUSED, LifecycleStatus.INVALIDATED}
    assert result.refusal is not None
    assert result.refusal.reason is expected_reason


def test_altitude_completion_without_new_position_evidence_fails_closed():
    snapshot = make_snapshot(1)
    controller, _, _, _, flight, _ = make_stack(snapshot, config=config())

    result = controller.execute(
        make_intent(IntentName.ALTITUDE, selection=(1,), args={"delta": 1}), snapshot
    )

    assert result.status is LifecycleStatus.REFUSED
    assert result.refusal is not None
    assert result.refusal.reason is RefusalReason.POSITION_STALE
    assert "post-command position evidence" in result.refusal.detail
    assert sum(call.operation is CommandOperation.GOTO for call in flight.calls) == 1


def test_newer_peer_measurement_supersedes_a_completed_position_projection():
    baseline = make_snapshot(2)
    controller, _, arbiter, dispatcher, _, _ = make_stack(baseline, config=config())
    prepared = controller.prepare(make_intent(IntentName.ALTITUDE, args={"delta": 1}), baseline)
    assert isinstance(prepared, PreparedExecution)
    second_move = next(
        command
        for command in prepared.plan.commands
        if command.drone_id == 2 and command.operation is CommandOperation.GOTO
    )
    current = replace(
        replace_aircraft(
            baseline,
            1,
            pose=Position(2, 0, 1.25),
            position_last_seen_ms=baseline.now_ms + 1,
        ),
        now_ms=baseline.now_ms + 1,
    )
    projected = {1: Position(0, 0, 1.5)}

    effective = dispatcher._effective_projected_positions(projected, baseline, current)
    refusal = arbiter.check_command(
        prepared.plan, second_move, current, projected_positions=effective
    )

    assert effective == {}
    assert refusal is not None
    assert refusal.reason is RefusalReason.SPACING


def test_roster_change_cannot_shortcut_final_altitude_revalidation(monkeypatch):
    snapshot = make_snapshot(1)
    controller, _, _, dispatcher, flight, _ = make_stack(snapshot, config=config())
    original = flight.hover

    def waiting_hover(*args, **kwargs):
        acknowledgements = original(*args, **kwargs)
        return tuple(replace(ack, status=LifecycleStatus.EXECUTING) for ack in acknowledgements)

    monkeypatch.setattr(flight, "hover", waiting_hover)

    def provider():
        return sim_snapshot(snapshot, flight)

    pending = controller.execute(
        make_intent(IntentName.ALTITUDE, selection=(1,), args={"delta": 1}),
        snapshot,
        current_snapshot=provider,
    )
    assert pending.status is LifecycleStatus.EXECUTING
    controller.planner = DeterministicPlanner(
        replace(config(), altitude_configuration_id="resurvey-v2")
    )
    terminal = replace(pending.acknowledgements[-1], status=LifecycleStatus.COMPLETED)

    result = dispatcher.resume_after_completion(
        pending.plan,
        pending,
        terminal,
        snapshot,
        current_snapshot=lambda: replace(provider(), roster_version=snapshot.roster_version + 1),
    )

    assert result.status is LifecycleStatus.INVALIDATED
    assert result.refusal is not None
    assert result.refusal.reason is RefusalReason.STALE_ROSTER
