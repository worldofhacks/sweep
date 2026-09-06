from __future__ import annotations

from planner.models import CommandOperation, Plan, Refusal
from planner.navigation import ArrivalSlot, NavigationDispatchAcceptance, NavigationPermission
from planner.navigation_runtime import NavigationExecutionConfig, NavigationRuntime
from planner.planner import DeterministicPlanner
from planner.test_navigation import MOTION, artifact, pose
from relay.capabilities import C1_IMPLEMENTED_INTENT_NAMES, CapabilityProfile
from relay.intent_v1 import IntentName
from tests.autonomy_fixtures import make_intent, make_snapshot, planning_config, replace_aircraft


def _runtime(*, acceptance: bool = True) -> NavigationRuntime:
    map_artifact = artifact(slots=(ArrivalSlot("atrium-1", "atrium", pose(6.5, 1.5), 0.5, 0.5),))

    def dispatch_acceptance(plan, current):
        if not acceptance:
            return None
        return NavigationDispatchAcceptance(
            "test-acceptance",
            plan.map_pin,
            plan.geometry_pin,
            plan.navigation_pin,
            plan.plan_revision,
        )

    return NavigationRuntime(
        lambda: map_artifact,
        NavigationExecutionConfig("level_1", MOTION, 0.5, 0.05, 500, 0.5, 5_000),
        NavigationPermission(frozenset({"atrium"})),
        dispatch_acceptance,
    )


def _intent():
    return make_intent(
        IntentName.NAVIGATE, selection=(1,), args={"zone_id": "atrium"}, confirm=True
    )


def _snapshot():
    return replace_aircraft(make_snapshot(1), 1, pose=pose_as_position())


def pose_as_position():
    from planner.models import Position

    return Position(0.5, 1.5, 1.0)


def test_navigation_runtime_builds_frozen_route_commands() -> None:
    runtime = _runtime()
    plan = runtime.prepare(_intent(), _snapshot())

    assert isinstance(plan, Plan)
    assert plan.navigation is not None
    assert plan.navigation.matches_commands(plan)
    assert plan.commands[-1].operation is CommandOperation.HOVER
    assert any(command.operation is CommandOperation.GOTO for command in plan.commands)


def test_navigation_runtime_revalidates_with_dispatch_acceptance() -> None:
    runtime = _runtime()
    snapshot = _snapshot()
    plan = runtime.prepare(_intent(), snapshot)
    assert isinstance(plan, Plan)

    assert runtime.check(plan, plan.commands[0], snapshot) is None


def test_navigation_runtime_refuses_without_runtime_acceptance() -> None:
    runtime = _runtime(acceptance=False)
    snapshot = _snapshot()
    plan = runtime.prepare(_intent(), snapshot)
    assert isinstance(plan, Plan)

    refusal = runtime.check(plan, plan.commands[0], snapshot)

    assert isinstance(refusal, Refusal)
    assert "artifact_not_dispatchable" in refusal.detail


def test_configured_planner_routes_only_when_profile_enables_navigation() -> None:
    profile = CapabilityProfile(
        "mapped_navigation", C1_IMPLEMENTED_INTENT_NAMES | {IntentName.NAVIGATE}
    )
    plan = DeterministicPlanner(planning_config(), profile, navigation=_runtime()).plan(
        _intent(), _snapshot()
    )

    assert isinstance(plan, Plan)
    assert plan.navigation is not None
