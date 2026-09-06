from __future__ import annotations

from planner.navigation import ArrivalSlot, NavigationDispatchAcceptance, NavigationPermission
from planner.navigation_runtime import NavigationExecutionConfig, NavigationRuntime
from planner.test_navigation import MOTION, artifact, pose
from relay.autonomy import AutonomyComposition, AutonomyConfig
from relay.capabilities import C1_CAPABILITY_PROFILE, IntentName
from tests.autonomy_fixtures import planning_config, safety_config


def _runtime() -> NavigationRuntime:
    map_artifact = artifact(slots=(ArrivalSlot("atrium-1", "atrium", pose(6.5, 1.5), 0.5, 0.5),))

    def accepted(plan, current):
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
        accepted,
    )


def test_navigation_deployment_binds_one_explicit_profile_and_runtime() -> None:
    runtime = _runtime()
    from planner.navigation_deployment import NavigationDeployment

    composition = AutonomyComposition(
        AutonomyConfig(
            planning=planning_config(),
            safety=safety_config(),
            navigation_deployment=NavigationDeployment(
                runtime, 1, "control-store", "synthetic", "navigation-config"
            ),
        )
    )
    try:
        owner = composition.session("navigation-binding")

        assert composition.capability_profile.name == "c1_basic_control.navigation"
        assert composition.capability_profile.enabled_intent_names == (
            C1_CAPABILITY_PROFILE.enabled_intent_names | {IntentName.NAVIGATE}
        )
        assert owner.planner.navigation is runtime
        assert composition.navigation_runtime is runtime
    finally:
        composition.close()


def test_default_autonomy_profile_has_no_navigation_runtime() -> None:
    composition = AutonomyComposition(
        AutonomyConfig(planning=planning_config(), safety=safety_config())
    )
    try:
        assert composition.capability_profile == C1_CAPABILITY_PROFILE
        assert composition.navigation_runtime is None
        assert composition.session("default-navigation-binding").planner.navigation is None
    finally:
        composition.close()
