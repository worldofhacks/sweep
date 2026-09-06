from __future__ import annotations

from adapters.sim.navigation_demo import navigation_demo_runtime
from planner.models import FlightState, Plan, Position
from planner.planner import DeterministicPlanner
from relay.capabilities import CapabilityProfile, IntentName
from tests.autonomy_fixtures import make_intent, make_snapshot, planning_config, replace_aircraft


def test_navigation_demo_plans_an_approved_selected_subset() -> None:
    runtime = navigation_demo_runtime()
    profile = CapabilityProfile("demo", frozenset({IntentName.NAVIGATE}))
    planner = DeterministicPlanner(
        planning_config(),
        profile,
        navigation=runtime,
    )
    snapshot = make_snapshot(3, selection=(1,), flight_state=FlightState.HOVERING)
    snapshot = replace_aircraft(snapshot, 1, pose=Position(0.5, 1.5, 1.0))
    snapshot = replace_aircraft(snapshot, 2, pose=Position(3.5, 3.5, 1.0))
    snapshot = replace_aircraft(snapshot, 3, pose=Position(5.5, 3.5, 1.0))
    plan = planner.plan(
        make_intent(
            IntentName.NAVIGATE,
            selection=(1,),
            args={"zone_id": "atrium"},
            confirm=True,
            intent_id="demo-atrium",
        ),
        snapshot,
    )

    assert isinstance(plan, Plan)
    assert plan.selection == (1,)
    assert plan.navigation is not None
    assert plan.navigation.route.destination_zone_id == "atrium"
    assert [zone.zone_id for zone in runtime.artifact().zones] == [
        "lobby",
        "formation-one",
        "formation-two",
        "atrium",
        "kitchen",
    ]
