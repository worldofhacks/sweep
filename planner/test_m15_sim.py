from dataclasses import replace

import pytest

from adapters.sim.flight import SimFlightAdapter
from planner.models import FleetSnapshot, FlightState, LifecycleStatus, Plan
from relay.capabilities import C2_CAPABILITY_PROFILE
from relay.intent_v1 import IntentName
from tests.autonomy_fixtures import make_intent, make_snapshot, make_stack, planning_config


@pytest.mark.parametrize("count", [4, 6])
def test_simulated_m15_path_reaches_confirmed_land_all(count: int) -> None:
    snapshot = make_snapshot(
        count,
        selection=(),
        flight_state=FlightState.DISARMED,
        armed=False,
    )
    controller, _, _, _, flight, _ = make_stack(
        snapshot,
        config=replace(
            planning_config(),
            altitude_step_m=0.5,
            altitude_floor_z_m=0.0,
            altitude_configuration_id="m15-sim-floor-v1",
            altitude_completion_tolerance_m=0.05,
        ),
        capability_profile=C2_CAPABILITY_PROFILE,
    )

    intents = (
        make_intent(IntentName.ARM, selection=()),
        make_intent(
            IntentName.SELECT,
            selection=(),
            args={"ids": tuple(range(1, count + 1))},
        ),
        make_intent(
            IntentName.TAKEOFF,
            selection=tuple(range(1, count + 1)),
            confirm=True,
        ),
        make_intent(
            IntentName.FORMATION_SET,
            selection=tuple(range(1, count + 1)),
            args={"name": "circle"},
        ),
        make_intent(
            IntentName.TRANSLATE,
            selection=tuple(range(1, count + 1)),
            args={"dx": 1, "dy": 0},
        ),
        make_intent(
            IntentName.TRANSLATE,
            selection=tuple(range(1, count + 1)),
            args={"dx": 1, "dy": 0},
        ),
        make_intent(
            IntentName.ALTITUDE,
            selection=tuple(range(1, count + 1)),
            args={"delta": 1},
        ),
        make_intent(
            IntentName.SWEEP,
            selection=tuple(range(1, count + 1)),
            args={"box": {"min_x": -4, "max_x": 4, "min_y": -3, "max_y": 3}},
            confirm=True,
        ),
        make_intent(IntentName.COME_HOME, selection=tuple(range(1, count + 1))),
        make_intent(
            IntentName.LAND_ALL,
            selection=tuple(range(1, count + 1)),
            confirm=True,
        ),
    )

    for intent in intents:
        result = controller.execute(
            intent,
            snapshot,
            current_snapshot=lambda current=snapshot: _live_sim_snapshot(current, flight),
        )
        assert result.status is LifecycleStatus.COMPLETED, result.to_dict()
        assert result.plan is not None
        snapshot = _apply_simulated_plan(snapshot, result.plan, flight)

    assert all(aircraft.flight_state is FlightState.LANDED for aircraft in flight.aircraft.values())


def _apply_simulated_plan(
    snapshot: FleetSnapshot, plan: Plan, flight: SimFlightAdapter
) -> FleetSnapshot:
    current = _live_sim_snapshot(snapshot, flight)
    return replace(
        current,
        selection=(
            plan.selection_update if plan.selection_update is not None else snapshot.selection
        ),
        armed=plan.armed_update if plan.armed_update is not None else snapshot.armed,
        formation=(
            plan.formation_update if plan.formation_update is not None else snapshot.formation
        ),
        spacing=plan.spacing_update if plan.spacing_update is not None else snapshot.spacing,
    )


def _live_sim_snapshot(snapshot: FleetSnapshot, flight: SimFlightAdapter) -> FleetSnapshot:
    now_ms = snapshot.now_ms + len(flight.calls) + 1
    aircraft = {
        drone_id: replace(
            state,
            pose=simulated.pose,
            flight_state=simulated.flight_state,
            armed=simulated.armed,
            link_last_seen_ms=now_ms,
            position_last_seen_ms=now_ms,
        )
        for drone_id, state in snapshot.aircraft.items()
        for simulated in (flight.aircraft[drone_id],)
    }
    return replace(snapshot, aircraft=aircraft, now_ms=now_ms, operator_last_seen_ms=now_ms)
