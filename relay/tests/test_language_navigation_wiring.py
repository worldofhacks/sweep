from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from evals.language_corpus import StaticResponseTransport
from language.contracts import intent_payload
from language.relay_compiler import RelayTranscriptCompiler
from planner.navigation import ArrivalSlot, NavigationDispatchAcceptance, NavigationPermission
from planner.navigation_deployment import NavigationDeployment
from planner.navigation_runtime import NavigationExecutionConfig, NavigationRuntime
from planner.test_navigation import MOTION, artifact, pose
from relay.auth import Principal
from relay.autonomy import AutonomyConfig, create_autonomy_app
from relay.main import build_transcript_service, transcript_service_factory
from relay.settings import AdapterBackend, CapabilityRelease, RelaySettings
from relay.tests.conftest import (
    ADAPTER_KEY,
    CONSOLE_KEY,
    EventIds,
    MutableClock,
    membership_payload,
    telemetry_payload,
)
from relay.tests.test_search_runtime import _search_runtime
from relay.voice import compiler_capability_version
from tests.autonomy_fixtures import camera_config, planning_config, safety_config


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


@pytest.mark.parametrize("search_enabled", [False, True])
@pytest.mark.parametrize("capability_release", [CapabilityRelease.C1, CapabilityRelease.C2])
def test_transcript_service_factory_resolves_configured_navigation_for_the_relay_compiler(
    tmp_path,
    search_enabled,
    capability_release,
) -> None:
    runtime = _runtime()
    config = AutonomyConfig(
        planning=planning_config(),
        safety=safety_config(),
        navigation_deployment=NavigationDeployment(
            runtime, 1, "control-store", "synthetic", "navigation-config"
        ),
        sim_camera=camera_config() if capability_release is CapabilityRelease.C2 else None,
    )
    if search_enabled:
        config = replace(config, search_runtime=_search_runtime())
    settings = RelaySettings(
        relay_token=CONSOLE_KEY,
        adapter_keys={1: ADAPTER_KEY},
        log_dir=tmp_path,
        adapter_backend=(
            AdapterBackend.SIM
            if capability_release is CapabilityRelease.C2
            else AdapterBackend.REMOTE
        ),
        capability_release=capability_release,
    )
    clock = MutableClock()
    app, composition = create_autonomy_app(
        settings,
        config,
        clock=clock,
        event_ids=EventIds(),
        transcript_service_factory=transcript_service_factory(
            config,
            {
                "ANTHROPIC_API_KEY": "test-key-never-sent",
                "SWEEP_QUALIFIED_VOICE_INTENTS": "navigate",
            },
        ),
    )
    try:
        with TestClient(app) as client:
            relay_runtime = app.state.relay_runtime
            session = relay_runtime.session("voice-navigation")
            adapter = Principal(source="adapter", drone_id=1, signing_key=ADAPTER_KEY)
            session.process_membership(
                membership_payload(action="join", event_id="join", session="voice-navigation"),
                adapter,
            )
            session.process_telemetry(
                {
                    **telemetry_payload(
                        event_id="pose", state="hovering", session="voice-navigation"
                    ),
                    "x": 0.5,
                    "y": 1.5,
                    "z": 1.0,
                },
                adapter,
            )
            session.process_membership(
                membership_payload(
                    action="readiness", event_id="ready", session="voice-navigation"
                ),
                adapter,
            )
            session.update_control_projection(selection=(1,), armed=True)
            state = session.current_state()

            factory_compiler = app.state.transcript_service._compiler
            assert isinstance(factory_compiler, RelayTranscriptCompiler)
            assert factory_compiler._capability_profile == composition.capability_profile
            grounding = factory_compiler._navigation(state)
            assert grounding is not None
            assert grounding.resolve("main hall")[0].zone_id == "atrium"

            service = build_transcript_service(
                relay_runtime,
                config=config,
                environ={"SWEEP_QUALIFIED_VOICE_INTENTS": "navigate"},
                transport=StaticResponseTransport(
                    {
                        "kind": "plan",
                        "intents": [
                            {
                                "name": "navigate",
                                "args": {"zone_id": "atrium"},
                                "selection": [1],
                                "mode": "indoor",
                            }
                        ],
                    }
                ),
            )
            compiler = service._compiler
            assert isinstance(compiler, RelayTranscriptCompiler)
            assert compiler._capability_profile == composition.capability_profile
            plan, compiled = compiler.compile(
                "fly to main hall",
                state,
                capability_version=compiler_capability_version(state),
                now_ms=clock.value,
                correlation_id="voice-navigation-plan",
                session_id="voice-navigation",
            )

            assert compiled is not None
            payload = intent_payload(
                compiled.intents[0],
                session=session.session_id,
                intent_id=plan.steps[0].intent_id,
                timestamp_ms=clock.value,
                source="language",
            )
            response = client.post(
                f"/session/{session.session_id}/navigation/preview",
                headers={"Authorization": f"Bearer {CONSOLE_KEY.decode()}"},
                json={"intent": payload},
            )
            assert response.status_code == 200, response.text
            assert response.json()["intent_id"] == plan.steps[0].intent_id
            accepted = session.process_intent(
                payload, Principal(source="language", drone_id=None, signing_key=CONSOLE_KEY)
            )
            assert accepted[0]["type"] == "acknowledgement"
            assert accepted[0]["status"] == "accepted"
            forged = session.process_intent(
                {**payload, "intent_id": "copied-unbound-navigation"},
                Principal(source="language", drone_id=None, signing_key=CONSOLE_KEY),
            )
            assert forged[0]["reason"] == "unbound_language_intent"

        assert compiled is not None
        assert plan.kind == "plan"
        assert plan.steps[0].name == "navigate"
        assert plan.steps[0].args == {"zone_id": "atrium"}
    finally:
        composition.close()
