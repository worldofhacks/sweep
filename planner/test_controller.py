from dataclasses import replace

import pytest

from adapters.sim.flight import SimFlightAdapter
from planner.controller import AutonomyController
from planner.models import (
    FleetSnapshot,
    FlightState,
    LifecycleStatus,
    Plan,
    Position,
    RefusalReason,
)
from planner.planner import DeterministicPlanner
from relay.intent_v1 import IntentName
from tests.autonomy_fixtures import (
    NOW_MS,
    make_intent,
    make_snapshot,
    make_stack,
    planning_config,
    replace_aircraft,
)


def test_arm_select_takeoff_documented_workflow() -> None:
    snapshot = make_snapshot(
        2,
        selection=(),
        flight_state=FlightState.DISARMED,
        armed=False,
    )
    controller, _, _, _, flight, _ = make_stack(snapshot)

    arm = controller.execute(make_intent(IntentName.ARM, selection=()), snapshot)
    assert arm.status is LifecycleStatus.COMPLETED
    assert arm.plan is not None and arm.plan.armed_update is True
    assert flight.calls == []
    snapshot = replace(snapshot, armed=arm.plan.armed_update)

    select = controller.execute(
        make_intent(IntentName.SELECT, selection=(), args={"ids": (1, 2)}),
        snapshot,
    )
    assert select.status is LifecycleStatus.COMPLETED
    assert select.plan is not None and select.plan.selection_update == (1, 2)
    snapshot = replace(snapshot, selection=select.plan.selection_update)

    takeoff = controller.execute(
        make_intent(IntentName.TAKEOFF, selection=(1, 2), confirm=True),
        snapshot,
    )

    assert takeoff.status is LifecycleStatus.COMPLETED
    assert [aircraft.flight_state for aircraft in flight.aircraft.values()] == [
        FlightState.HOVERING,
        FlightState.HOVERING,
    ]


def test_arm_is_global_and_does_not_depend_on_a_stale_selection() -> None:
    snapshot = make_snapshot(
        2,
        selection=(1,),
        flight_state=FlightState.DISARMED,
        armed=False,
    )
    controller, _, _, _, flight, _ = make_stack(snapshot)

    result = controller.execute(
        make_intent(IntentName.ARM, selection=(99,)),
        snapshot,
    )

    assert result.status is LifecycleStatus.COMPLETED
    assert result.plan is not None and result.plan.armed_update is True
    assert flight.calls == []


class BrokenPlanner(DeterministicPlanner):
    def plan(self, intent, snapshot):  # type: ignore[no-untyped-def]
        raise RuntimeError("injected planner bug")


def test_planner_exception_is_typed_and_only_emits_safety_hold() -> None:
    snapshot = make_snapshot(2)
    _, _, arbiter, dispatcher, flight, _ = make_stack(snapshot)
    controller = AutonomyController(
        planner=BrokenPlanner(planning_config()),
        arbiter=arbiter,
        dispatcher=dispatcher,
    )

    result = controller.execute(
        make_intent(
            IntentName.TRANSLATE,
            args={"dx": 1, "dy": 0},
        ),
        snapshot,
    )

    assert result.status is LifecycleStatus.FAILED
    assert result.refusal is not None
    assert result.refusal.reason is RefusalReason.PLANNER_FAILURE
    assert [call.operation.value for call in flight.calls] == ["hover", "hover"]


def test_two_motion_intents_within_window_are_both_dropped_and_fleet_holds() -> None:
    snapshot = make_snapshot(2)
    controller, _, _, _, flight, _ = make_stack(snapshot)
    first = make_intent(
        IntentName.TRANSLATE,
        args={"dx": 1, "dy": 0},
        intent_id="motion-one",
        t=NOW_MS,
    )
    second = make_intent(
        IntentName.COME_HOME,
        intent_id="motion-two",
        t=NOW_MS + 400,
    )

    result = controller.execute_pair(first, second, snapshot)

    assert result.resolution.accepted == ()
    assert len(result.resolution.refusals) == 2
    assert all(
        refusal.reason is RefusalReason.CONFLICTING_MOTION for refusal in result.resolution.refusals
    )
    assert result.safety_execution is not None
    assert result.safety_execution.status is LifecycleStatus.COMPLETED
    assert [call.operation.value for call in flight.calls] == ["hover", "hover"]


def test_later_selection_wins_without_adapter_io() -> None:
    snapshot = make_snapshot(2, selection=())
    controller, _, _, _, flight, _ = make_stack(snapshot)
    first = make_intent(
        IntentName.SELECT,
        selection=(),
        args={"ids": (1,)},
        intent_id="select-one",
        t=NOW_MS,
    )
    second = make_intent(
        IntentName.SELECT,
        selection=(),
        args={"ids": (2,)},
        intent_id="select-two",
        t=NOW_MS + 400,
    )

    result = controller.execute_pair(first, second, snapshot)

    assert [intent.intent_id for intent in result.resolution.accepted] == ["select-two"]
    assert result.resolution.invalidated_intent_ids == ("select-one",)
    assert len(result.executions) == 1
    assert result.executions[0].plan is not None
    assert result.executions[0].plan.selection_update == (2,)
    assert flight.calls == []


def test_positioning_loss_holds_all_then_lands_after_configured_dwell() -> None:
    initial = make_snapshot(2)
    initial = replace_aircraft(
        initial,
        1,
        position_quality=0.0,
        position_loss_since_ms=NOW_MS - 2_000,
    )
    controller, _, _, _, flight, _ = make_stack(initial)

    holding = controller.handle_positioning_loss(initial)
    assert holding.action == "hold"
    assert holding.execution is not None
    assert holding.execution.status is LifecycleStatus.COMPLETED
    assert [call.operation.value for call in flight.calls] == ["hover", "hover"]

    later = replace(initial, now_ms=NOW_MS + 1_001)
    landing = controller.handle_positioning_loss(later)
    assert landing.action == "land"
    assert landing.execution is not None
    assert landing.execution.status is LifecycleStatus.COMPLETED
    assert [call.operation.value for call in flight.calls[-2:]] == ["land", "land"]


def test_future_position_loss_timestamp_beyond_skew_fails_safe_to_land() -> None:
    snapshot = replace_aircraft(
        make_snapshot(1),
        1,
        position_quality=0.0,
        position_loss_since_ms=NOW_MS + 1_001,
    )
    controller, _, _, _, flight, _ = make_stack(snapshot)

    result = controller.handle_positioning_loss(snapshot)

    assert result.detected is True
    assert result.action == "land"
    assert result.execution is not None
    assert result.execution.status is LifecycleStatus.COMPLETED
    assert [call.operation.value for call in flight.calls] == ["land"]


def test_capability_gate_precedes_stale_selection_and_operator_checks() -> None:
    snapshot = replace(
        make_snapshot(1, selection=(1,)),
        operator_present=False,
    )
    controller, _, _, _, flight, _ = make_stack(snapshot)

    result = controller.execute(
        make_intent(
            IntentName.SURVEY_AREA,
            selection=(),
            args={"area_id": "area"},
            confirm=True,
        ),
        snapshot,
    )

    assert result.refusal is not None
    assert result.refusal.reason is RefusalReason.UNSUPPORTED
    assert flight.calls == []


def test_simulated_m15_formation_and_confirmed_sweep_dispatch_without_adapter_shortcuts() -> None:
    snapshot = make_snapshot(4)
    for drone_id, x in zip(snapshot.selection, (-8.0, -3.0, 3.0, 8.0), strict=True):
        snapshot = replace_aircraft(
            snapshot,
            drone_id,
            pose=Position(x, 0.0, 1.0),
            home=Position(x, 0.0, 0.0),
        )
    controller, _, _, _, flight, _ = make_stack(snapshot)

    formation = controller.execute(
        make_intent(
            IntentName.FORMATION_SET,
            selection=snapshot.selection,
            args={"name": "circle"},
        ),
        snapshot,
    )
    sweep = controller.execute(
        make_intent(IntentName.SWEEP, selection=snapshot.selection, confirm=True),
        snapshot,
    )

    assert formation.status is LifecycleStatus.COMPLETED
    assert formation.plan is not None and formation.plan.formation_update == "circle"
    assert sweep.status is LifecycleStatus.COMPLETED
    assert len(sweep.acknowledgements) == 8
    assert [call.operation.value for call in flight.calls].count("goto") == 12


@pytest.mark.parametrize("count", [4, 6])
def test_simulated_appendix_e_path_reaches_land_all_for_four_to_six_drones(count: int) -> None:
    snapshot = make_snapshot(
        count,
        selection=(),
        flight_state=FlightState.DISARMED,
        armed=False,
    )
    controller, _, _, _, flight, _ = make_stack(snapshot)

    for intent in (
        make_intent(IntentName.ARM, selection=()),
        make_intent(IntentName.SELECT, selection=(), args={"ids": tuple(range(1, count + 1))}),
        make_intent(IntentName.TAKEOFF, selection=tuple(range(1, count + 1)), confirm=True),
        make_intent(
            IntentName.FORMATION_SET,
            selection=tuple(range(1, count + 1)),
            args={"name": "circle"},
        ),
        make_intent(
            IntentName.TRANSLATE, selection=tuple(range(1, count + 1)), args={"dx": 1, "dy": 0}
        ),
        make_intent(
            IntentName.TRANSLATE, selection=tuple(range(1, count + 1)), args={"dx": 1, "dy": 0}
        ),
        make_intent(IntentName.ALTITUDE, selection=tuple(range(1, count + 1)), args={"delta": 1}),
        make_intent(IntentName.SWEEP, selection=tuple(range(1, count + 1)), confirm=True),
        make_intent(IntentName.COME_HOME, selection=tuple(range(1, count + 1))),
        make_intent(IntentName.LAND_ALL, selection=tuple(range(1, count + 1)), confirm=True),
    ):
        result = controller.execute(intent, snapshot)
        assert result.status is LifecycleStatus.COMPLETED
        assert result.plan is not None
        snapshot = _apply_simulated_plan(snapshot, result.plan, flight)

    assert all(aircraft.flight_state is FlightState.LANDED for aircraft in flight.aircraft.values())


def _apply_simulated_plan(
    snapshot: FleetSnapshot, plan: Plan, flight: SimFlightAdapter
) -> FleetSnapshot:
    aircraft = {
        drone_id: replace(
            state,
            pose=simulated.pose,
            flight_state=simulated.flight_state,
            armed=simulated.armed,
        )
        for drone_id, state in snapshot.aircraft.items()
        for simulated in (flight.aircraft[drone_id],)
    }
    return replace(
        snapshot,
        aircraft=aircraft,
        selection=(
            plan.selection_update if plan.selection_update is not None else snapshot.selection
        ),
        armed=plan.armed_update if plan.armed_update is not None else snapshot.armed,
        formation=(
            plan.formation_update if plan.formation_update is not None else snapshot.formation
        ),
        spacing=plan.spacing_update if plan.spacing_update is not None else snapshot.spacing,
    )
