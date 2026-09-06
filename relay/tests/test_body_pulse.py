"""Bounded node-timed body motion keeps the normal authorization/safety path."""

from dataclasses import replace

import pytest

from adapters.dispatch import AdapterDispatcher
from adapters.dji_mini3.remote import RemoteBridgeAdapter
from adapters.dji_mini3.test_remote import ScriptedLink
from arbiter.safety import SafetyArbiter
from planner.controller import AutonomyController
from planner.models import (
    CommandOperation,
    FlightState,
    LifecycleStatus,
    Plan,
    Position,
    RefusalReason,
)
from planner.planner import DeterministicPlanner
from relay.auth import verify_event_signature
from relay.body_pulse import BODY_PULSE_CAPABILITY
from relay.contracts import ContractError, parse_command
from relay.intent_v1 import AcceptedIntent, IntentName, RejectedIntent, validate_intent
from relay.tests.conftest import ADAPTER_KEY, command_payload
from relay.tests.test_intent_v1 import _c1_payload
from tests.autonomy_fixtures import (
    NOW_MS,
    make_intent,
    make_snapshot,
    make_stack,
    planning_config,
    replace_aircraft,
    safety_config,
)

ARGS = {"forward_mm_s": 250, "duration_ms": 500}


def pulse_snapshot(count=2, selection=(1, 2)):
    snapshot = make_snapshot(count, selection=selection)
    for drone_id in snapshot.aircraft:
        snapshot = replace_aircraft(
            snapshot, drone_id, capabilities=frozenset({BODY_PULSE_CAPABILITY})
        )
    return snapshot


def pulse_intent(selection=(1, 2), *, args=None, confirm=True):
    return make_intent(
        IntentName.BODY_PULSE,
        selection=selection,
        args=ARGS if args is None else args,
        confirm=confirm,
    )


@pytest.mark.parametrize(
    "args",
    [
        {"forward_mm_s": 0, "duration_ms": 500},
        {"forward_mm_s": 251, "duration_ms": 500},
        {"forward_mm_s": -251, "duration_ms": 500},
        {"forward_mm_s": True, "duration_ms": 500},
        {"forward_mm_s": 250.0, "duration_ms": 500},
        {"forward_mm_s": 250, "duration_ms": 99},
        {"forward_mm_s": 250, "duration_ms": 501},
        {"forward_mm_s": 250, "duration_ms": True},
        {"forward_mm_s": 250, "duration_ms": 500.0},
        {"forward_mm_s": 250, "duration_ms": 500, "dy": 0},
    ],
)
def test_intent_and_signed_command_reject_outside_exact_pulse_contract(args):
    raw = {**_c1_payload("console", IntentName.BODY_PULSE), "args": args}
    assert isinstance(validate_intent(raw), RejectedIntent)
    with pytest.raises(ContractError):
        parse_command(command_payload(event_id="pulse-command", operation="body_pulse", args=args))


@pytest.mark.parametrize("forward,duration", [(1, 100), (-1, 100), (250, 500), (-250, 500)])
def test_signed_pulse_roundtrip_binds_parameters_and_target(forward, duration):
    raw = command_payload(
        event_id="pulse-command",
        operation="body_pulse",
        args={"forward_mm_s": forward, "duration_ms": duration},
    )
    frame = parse_command(raw)
    assert verify_event_signature(frame.unsigned_event(), frame.signature, ADAPTER_KEY)
    for field, value in (
        ("args", {"forward_mm_s": -forward, "duration_ms": duration}),
        ("drone_id", 2),
        ("connection_epoch", 2),
    ):
        tampered = parse_command({**raw, field: value})
        assert not verify_event_signature(
            tampered.unsigned_event(), tampered.signature, ADAPTER_KEY
        )


@pytest.mark.parametrize(
    "name", [IntentName.ARM, IntentName.TAKEOFF, IntentName.BODY_PULSE, IntentName.LAND]
)
def test_webcam_flight_actions_require_explicit_confirmation(name):
    raw = {**_c1_payload("webcam", name), "confirm": False}
    assert isinstance(validate_intent(raw), RejectedIntent)
    assert isinstance(validate_intent({**raw, "confirm": True}), AcceptedIntent)


@pytest.mark.parametrize("selection", [(1,), (2,), (1, 2)])
@pytest.mark.parametrize("forward", [-250, 250])
def test_pulse_moves_only_selected_nodes_in_each_body_direction(selection, forward):
    snapshot = replace_aircraft(pulse_snapshot(selection=selection), 2, heading_deg=180.0)
    controller, _, _, _, flight, _ = make_stack(snapshot)
    result = controller.execute(
        pulse_intent(selection, args={"forward_mm_s": forward, "duration_ms": 500}), snapshot
    )
    assert result.status is LifecycleStatus.COMPLETED, result.refusal
    assert {call.operation for call in flight.calls} == {CommandOperation.BODY_PULSE}
    assert {call.drone_ids for call in flight.calls} == {(item,) for item in selection}
    for drone_id, state in flight.aircraft.items():
        expected_y = (
            (forward / 2000 if drone_id == 1 else -forward / 2000) if drone_id in selection else 0
        )
        assert state.pose.y == pytest.approx(expected_y)
        assert state.pose.x == pytest.approx(snapshot.aircraft[drone_id].pose.x)
        assert state.pose.z == snapshot.aircraft[drone_id].pose.z


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"capabilities": frozenset()}, RefusalReason.UNSUPPORTED),
        ({"position_quality": 0.0}, RefusalReason.POSITION_QUALITY),
        ({"position_last_seen_ms": NOW_MS - 1001}, RefusalReason.POSITION_STALE),
        ({"link_last_seen_ms": NOW_MS - 1001}, RefusalReason.LINK_STALE),
        ({"control_authority": False}, RefusalReason.CONTROL_AUTHORITY),
        ({"rc_safety_operator_present": False}, RefusalReason.RC_SAFETY_OPERATOR_ABSENT),
        ({"armed": False}, RefusalReason.ARMED_REQUIRED),
        ({"flight_state": FlightState.LANDED}, RefusalReason.INVALID_STATE),
        ({"flight_state": FlightState.AIRBORNE}, RefusalReason.INVALID_STATE),
        ({"active_task_id": "other-motion"}, RefusalReason.INVALID_STATE),
        ({"pose": Position(9.9, 0, 1)}, RefusalReason.GEOFENCE),
    ],
)
def test_pulse_refuses_unsafe_selected_node_before_any_adapter_io(change, reason):
    snapshot = replace_aircraft(pulse_snapshot(), 2, **change)
    controller, _, _, _, flight, _ = make_stack(snapshot)
    result = controller.execute(pulse_intent(), snapshot)
    assert result.refusal is not None and result.refusal.reason is reason
    assert flight.calls == []


@pytest.mark.parametrize("selection,distance", [((1,), 0.90), ((1, 2), 1.0)])
def test_pulse_reserves_swept_spacing_from_selected_and_unselected_aircraft(selection, distance):
    snapshot = replace_aircraft(
        pulse_snapshot(selection=selection), 2, pose=Position(distance, 0, 1)
    )
    controller, _, _, _, flight, _ = make_stack(snapshot)
    result = controller.execute(pulse_intent(selection), snapshot)
    assert result.refusal is not None and result.refusal.reason is RefusalReason.SPACING
    assert flight.calls == []


def test_unselected_aircraft_with_unknown_position_cannot_clear_a_pulse():
    snapshot = replace_aircraft(pulse_snapshot(selection=(1,)), 2, position_quality=0.0)
    controller, _, _, _, flight, _ = make_stack(snapshot)
    result = controller.execute(pulse_intent((1,)), snapshot)
    assert result.refusal is not None and result.refusal.reason is RefusalReason.POSITION_QUALITY
    assert flight.calls == []


def test_pulse_does_not_require_or_invent_a_world_heading():
    snapshot = replace_aircraft(pulse_snapshot(1, (1,)), 1, heading_deg=None)
    planner = DeterministicPlanner(planning_config())
    plan = planner.plan(pulse_intent((1,)), snapshot)
    assert isinstance(plan, Plan)
    assert dict(plan.commands[0].parameters) == ARGS
    assert SafetyArbiter(safety_config()).check_plan(plan, snapshot) is None


def test_remote_pulse_preserves_exact_target_epoch_and_arguments():
    snapshot = pulse_snapshot()
    link = ScriptedLink(epochs={1: 1, 2: 1})
    remote = RemoteBridgeAdapter.from_snapshot(link, snapshot, acknowledgement_timeout_ms=50)
    arbiter = SafetyArbiter(safety_config())
    controller = AutonomyController(
        planner=DeterministicPlanner(planning_config()),
        arbiter=arbiter,
        dispatcher=AdapterDispatcher(flight=remote, camera=remote, arbiter=arbiter),
    )
    result = controller.execute(pulse_intent(), snapshot)
    assert result.status is LifecycleStatus.COMPLETED, result.refusal
    assert [
        (item.drone_id, item.connection_epoch, item.operation, dict(item.args))
        for item in link.sent
    ] == [(1, 1, CommandOperation.BODY_PULSE, ARGS), (2, 1, CommandOperation.BODY_PULSE, ARGS)]


def test_pulse_respects_lower_configured_flight_speed():
    snapshot = pulse_snapshot()
    controller, _, _, _, flight, _ = make_stack(
        snapshot, config=replace(planning_config(), flight_speed_m_s=0.1)
    )
    result = controller.execute(pulse_intent(), snapshot)
    assert result.refusal is not None and result.refusal.reason is RefusalReason.INVALID_PLAN
    assert flight.calls == []


def test_pulse_capability_is_rechecked_at_dispatch_after_preview():
    snapshot = pulse_snapshot()
    controller, _, _, _, flight, _ = make_stack(snapshot)
    prepared = controller.prepare(pulse_intent(), snapshot)
    changed = replace_aircraft(snapshot, 2, capabilities=frozenset())
    result = controller.dispatch_prepared(prepared, current_snapshot=lambda: changed)
    assert result.refusal is not None and result.refusal.reason is RefusalReason.UNSUPPORTED
    assert flight.calls == []


def test_pulse_without_confirmation_refuses_before_io():
    snapshot = pulse_snapshot()
    controller, _, _, _, flight, _ = make_stack(snapshot)
    result = controller.execute(pulse_intent(confirm=False), snapshot)
    assert result.refusal is not None
    assert flight.calls == []


def test_overlapping_pulse_and_translation_use_motion_conflict_hold():
    snapshot = pulse_snapshot()
    controller, _, _, _, flight, _ = make_stack(snapshot)
    second = make_intent(
        IntentName.TRANSLATE,
        selection=(1, 2),
        args={"dx": 1, "dy": 0},
        intent_id="translation",
        t=NOW_MS + 1,
    )
    result = controller.execute_pair(pulse_intent(), second, snapshot)
    assert result.resolution.accepted == ()
    assert len(result.resolution.refusals) == 2
    assert all(
        item.reason is RefusalReason.CONFLICTING_MOTION for item in result.resolution.refusals
    )
    assert [item.operation for item in flight.calls] == [CommandOperation.HOVER] * 2


@pytest.mark.parametrize(
    "change",
    [
        {"flight_state": FlightState.AIRBORNE},
        {"flight_state": FlightState.TAKING_OFF},
        {"flight_state": FlightState.LANDING},
        {"active_task_id": "other-motion"},
    ],
)
def test_pulse_requires_other_airborne_aircraft_to_hold_first(change):
    snapshot = replace_aircraft(pulse_snapshot(selection=(1,)), 2, **change)
    controller, _, _, _, flight, _ = make_stack(snapshot)
    result = controller.execute(pulse_intent((1,)), snapshot)
    assert result.refusal is not None and result.refusal.reason is RefusalReason.INVALID_STATE
    assert flight.calls == []


@pytest.mark.parametrize("selection,spacing", [((1,), 0.96), ((1, 2), 1.1)])
def test_pulse_spacing_reserves_worst_supported_controller_tick(selection, spacing):
    # These distances clear nominal .125 m travel, but not the .175 m tick envelope.
    snapshot = replace_aircraft(
        pulse_snapshot(selection=selection), 2, pose=Position(spacing, 0, 1)
    )
    controller, _, _, _, flight, _ = make_stack(snapshot)
    result = controller.execute(pulse_intent(selection), snapshot)
    assert result.refusal is not None and result.refusal.reason is RefusalReason.SPACING
    assert flight.calls == []


def test_pulse_fence_reserves_worst_supported_controller_tick():
    snapshot = replace_aircraft(pulse_snapshot(1, (1,)), 1, pose=Position(9.85, 0, 1))
    controller, _, _, _, flight, _ = make_stack(snapshot)
    result = controller.execute(pulse_intent((1,)), snapshot)
    assert result.refusal is not None and result.refusal.reason is RefusalReason.GEOFENCE
    assert flight.calls == []
