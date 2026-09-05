from dataclasses import replace
from sys import float_info

import pytest

from planner.models import ExecutionResult, LifecycleStatus, Plan, Position, RefusalReason
from planner.planner import DeterministicPlanner
from relay.intent_v1 import IntentName
from tests.autonomy_fixtures import (
    make_intent,
    make_snapshot,
    make_stack,
    planning_config,
    replace_aircraft,
)


@pytest.mark.parametrize(
    ("dx", "dy", "step", "heading", "position"),
    [
        (10**400, 0.0, 1.0, 0.0, 0.0),
        (float_info.max, 0.0, 2.0, 0.0, 0.0),
        (float_info.max, -float_info.max, 1.0, 45.0, 0.0),
        (float_info.max, 0.0, 1.0, 0.0, float_info.max),
    ],
    ids=["integer-conversion", "scaled", "rotated", "target-addition"],
)
def test_translation_overflow_refuses_without_recovery_hover(dx, dy, step, heading, position):
    snapshot = replace_aircraft(
        make_snapshot(1),
        1,
        pose=Position(position, 0.0, 1.0),
        home=Position(position, 0.0, 0.0),
        heading_deg=heading,
    )
    controller, _, _, _, flight, camera = make_stack(snapshot)
    controller.planner = DeterministicPlanner(
        replace(planning_config(), translation_frame="aircraft_relative", translation_step_m=step)
    )
    result = controller.execute(
        make_intent(IntentName.TRANSLATE, selection=(1,), args={"dx": dx, "dy": dy}), snapshot
    )

    assert isinstance(result, ExecutionResult)
    assert result.status is LifecycleStatus.REFUSED
    assert result.refusal.reason is RefusalReason.INVALID_PLAN
    assert not flight.calls
    assert not camera.calls


def test_large_finite_translation_is_left_for_geofence_arbitration():
    snapshot = make_snapshot(1)
    planner = DeterministicPlanner(replace(planning_config(), translation_step_m=1.0))
    result = planner.plan(
        make_intent(IntentName.TRANSLATE, selection=(1,), args={"dx": float_info.max / 2, "dy": 0}),
        snapshot,
    )

    assert isinstance(result, Plan)
    assert result.commands[0].parameters["x"] == float_info.max / 2
