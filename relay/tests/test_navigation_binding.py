from __future__ import annotations

from dataclasses import replace

from planner.models import Refusal
from planner.navigation import ArrivalSlot, NavigationDispatchAcceptance, NavigationPermission
from planner.navigation_runtime import NavigationExecutionConfig, NavigationRuntime
from planner.test_navigation import MOTION, artifact, pose
from relay.autonomy import AutonomyComposition, AutonomyConfig
from relay.capabilities import C1_CAPABILITY_PROFILE, IntentName
from tests.autonomy_fixtures import planning_config, safety_config


def _runtime() -> NavigationRuntime:
    map_artifact = artifact(slots=(ArrivalSlot("atrium-1", "atrium", pose(6.5, 1.5), 0.5, 0.5),))
    map_artifact = replace(
        map_artifact,
        zones=(replace(map_artifact.zones[0], aliases=("atrium", "main hall")),),
    )

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


def test_session_preview_returns_the_frozen_route_and_current_aliases(tmp_path) -> None:
    from planner.navigation_deployment import NavigationDeployment
    from relay.audit import SessionAuditLog
    from relay.auth import Principal
    from relay.session import RelayLimits, RelaySession
    from relay.tests.conftest import (
        ADAPTER_KEY,
        EventIds,
        MutableClock,
        membership_payload,
        telemetry_payload,
    )
    from tests.autonomy_fixtures import make_intent

    runtime = _runtime()
    composition = AutonomyComposition(
        AutonomyConfig(
            planning=planning_config(),
            safety=safety_config(),
            navigation_deployment=NavigationDeployment(
                runtime, 1, "control-store", "synthetic", "navigation-config"
            ),
        )
    )
    clock = MutableClock()
    owner = composition.session("preview-navigation")
    session = RelaySession(
        session_id="preview-navigation",
        limits=RelayLimits(5_000, 5_000, 1_000, 1_000),
        audit_log=SessionAuditLog(tmp_path, "preview-navigation"),
        clock=clock,
        event_ids=EventIds(),
        intent_sink=owner,
        capability_profile=composition.capability_profile,
    )
    adapter = Principal(source="adapter", drone_id=1, signing_key=ADAPTER_KEY)
    try:
        session.process_membership(
            membership_payload(action="join", event_id="join", session="preview-navigation"),
            adapter,
        )
        session.process_telemetry(
            {
                **telemetry_payload(
                    event_id="pose", state="hovering", session="preview-navigation"
                ),
                "x": 0.5,
                "y": 1.5,
                "z": 1.0,
            },
            adapter,
        )
        session.process_membership(
            membership_payload(action="readiness", event_id="ready", session="preview-navigation"),
            adapter,
        )
        session.update_control_projection(selection=(1,), armed=True)
        intent = make_intent(
            IntentName.NAVIGATE,
            selection=(1,),
            args={"zone_id": "atrium"},
            confirm=False,
            t=clock.value,
        )

        plan = owner.preview_navigation(intent, session.current_state())
        catalog = owner.navigation_catalog()

        assert not isinstance(plan, Refusal)
        assert plan.navigation is not None
        assert plan.navigation.route.destination_zone_id == "atrium"
        assert catalog is not None
        assert catalog["zones"][0]["aliases"] == ["atrium", "main hall"]
    finally:
        composition.close()
