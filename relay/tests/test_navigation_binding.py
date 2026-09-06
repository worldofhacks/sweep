from __future__ import annotations

from dataclasses import replace

from planner.models import Refusal
from planner.navigation import ArrivalSlot, NavigationDispatchAcceptance, NavigationPermission
from planner.navigation_deployment import NavigationDeployment
from planner.navigation_runtime import NavigationExecutionConfig, NavigationRuntime
from planner.test_navigation import MOTION, artifact, pose
from relay.autonomy import AutonomyComposition, AutonomyConfig, create_autonomy_app
from relay.capabilities import C1_CAPABILITY_PROFILE, C2_CAPABILITY_PROFILE, IntentName
from relay.tests.test_search_runtime import _search_runtime
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


def test_c2_profile_keeps_disarm_without_unconfigured_navigation_or_search() -> None:
    profile = AutonomyConfig(
        planning=planning_config(), safety=safety_config()
    ).effective_capability_profile(C2_CAPABILITY_PROFILE)

    assert profile.supports(IntentName.DISARM)
    assert not profile.supports(IntentName.NAVIGATE)
    assert not profile.supports(IntentName.SEARCH)


def test_c2_profile_adds_configured_navigation_and_search() -> None:
    runtime = _runtime()
    profile = AutonomyConfig(
        planning=planning_config(),
        safety=safety_config(),
        navigation_deployment=NavigationDeployment(
            runtime, 1, "control-store", "synthetic", "navigation-config"
        ),
        search_runtime=_search_runtime(),
    ).effective_capability_profile(C2_CAPABILITY_PROFILE)

    assert profile.supports(IntentName.DISARM)
    assert profile.supports(IntentName.NAVIGATE)
    assert profile.supports(IntentName.SEARCH)


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

        # Stop workers so this assertion observes the job the relay would queue.
        owner.close(timeout_s=1)
        confirmation = replace(intent, confirm=True, t=clock.value + 1)
        owner.submit(confirmation, session.current_state())
        prepared = owner._normal.pending[-1].prepared
        assert prepared is not None
        assert prepared.plan is plan
        assert prepared.intent == confirmation

        changed = replace(
            intent,
            intent_id="preview-navigation-changed",
            args={"zone_id": "atrium"},
        )
        assert not isinstance(owner.preview_navigation(changed, session.current_state()), Refusal)
        owner.submit(
            replace(changed, confirm=True, args={"zone_id": "other"}),
            session.current_state(),
        )
        assert owner._normal.pending[-1].refusal_detail == (
            "navigation requires a current matching server preview"
        )

        stopped = replace(intent, intent_id="preview-navigation-stopped")
        assert not isinstance(owner.preview_navigation(stopped, session.current_state()), Refusal)
        owner.submit(
            make_intent(IntentName.HOLD, selection=(1,), t=clock.value), session.current_state()
        )
        owner.submit(replace(stopped, confirm=True), session.current_state())
        assert owner._normal.pending[-1].refusal_detail == (
            "navigation requires a current matching server preview"
        )
    finally:
        composition.close()


def test_navigation_catalog_endpoint_authenticates_and_returns_deployed_aliases(tmp_path) -> None:
    from fastapi.testclient import TestClient

    from planner.navigation_deployment import NavigationDeployment
    from relay.settings import AdapterBackend, RelaySettings
    from relay.tests.conftest import ADAPTER_KEY, CONSOLE_KEY

    runtime = _runtime()
    settings = RelaySettings(
        relay_token=CONSOLE_KEY,
        adapter_keys={1: ADAPTER_KEY},
        log_dir=tmp_path,
        adapter_backend=AdapterBackend.REMOTE,
    )
    app, composition = create_autonomy_app(
        settings,
        AutonomyConfig(
            planning=planning_config(),
            safety=safety_config(),
            navigation_deployment=NavigationDeployment(
                runtime, 1, "control-store", "synthetic", "navigation-config"
            ),
        ),
    )
    try:
        with TestClient(app) as client:
            assert client.get("/session/catalog/navigation/catalog").status_code == 401
            response = client.get(
                "/session/catalog/navigation/catalog",
                headers={"Authorization": f"Bearer {CONSOLE_KEY.decode()}"},
            )
        assert response.status_code == 200
        body = response.json()
        assert body["type"] == "navigation_catalog"
        assert body["session"] == "catalog"
        assert body["catalog"]["zones"][0]["aliases"] == ["atrium", "main hall"]
    finally:
        composition.close()


def test_future_dated_public_navigation_preview_does_not_extend_operator_presence(
    tmp_path,
) -> None:
    from fastapi.testclient import TestClient

    from relay.auth import Principal
    from relay.settings import AdapterBackend, RelaySettings
    from relay.tests.conftest import (
        ADAPTER_KEY,
        CONSOLE_KEY,
        EventIds,
        MutableClock,
        membership_payload,
        telemetry_payload,
    )

    clock = MutableClock()
    config = AutonomyConfig(
        planning=planning_config(),
        safety=replace(safety_config(), operator_timeout_ms=100),
        navigation_deployment=NavigationDeployment(
            _runtime(), 1, "control-store", "synthetic", "navigation-config"
        ),
    )
    app, composition = create_autonomy_app(
        RelaySettings(
            relay_token=CONSOLE_KEY,
            adapter_keys={1: ADAPTER_KEY},
            log_dir=tmp_path,
            adapter_backend=AdapterBackend.REMOTE,
        ),
        config,
        clock=clock,
        event_ids=EventIds(),
    )
    session_id = "future-preview"
    try:
        with TestClient(app) as client:
            runtime = app.state.relay_runtime
            session = runtime.session(session_id)
            adapter = Principal(source="adapter", drone_id=1, signing_key=ADAPTER_KEY)
            session.process_membership(
                membership_payload(action="join", event_id="join", session=session_id), adapter
            )
            session.process_telemetry(
                {
                    **telemetry_payload(event_id="pose", state="hovering", session=session_id),
                    "x": 0.5,
                    "y": 1.5,
                    "z": 1.0,
                },
                adapter,
            )
            session.process_membership(
                membership_payload(action="readiness", event_id="ready", session=session_id),
                adapter,
            )
            session.update_control_projection(selection=(1,), armed=True)

            response = client.post(
                f"/session/{session_id}/navigation/preview",
                headers={"Authorization": f"Bearer {CONSOLE_KEY.decode()}"},
                json={
                    "intent": {
                        "v": 1,
                        "t": clock.value + 1_000,
                        "type": "intent",
                        "intent_id": "future-preview",
                        "retry_of": None,
                        "source": "console",
                        "session": session_id,
                        "name": "navigate",
                        "args": {"zone_id": "atrium"},
                        "selection": [1],
                        "mode": "indoor",
                        "confirm": True,
                    }
                },
            )
            assert response.status_code == 200, response.text

            clock.advance(100)
            periodic = runtime.periodic_events(session)

        action = next(event for event in periodic if event["type"] == "safety_action")
        assert action["reason"] == "operator_presence_expired"
    finally:
        composition.close()
