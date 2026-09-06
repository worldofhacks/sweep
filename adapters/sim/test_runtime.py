from __future__ import annotations

from fastapi.testclient import TestClient

from adapters.sim.runtime import create_m14_sim_app
from relay.capabilities import C2_CAPABILITY_PROFILE
from relay.settings import CapabilityRelease, RelaySettings


def test_m14_sim_arbiter_uses_configured_relay_freshness(tmp_path) -> None:
    app = create_m14_sim_app(
        RelaySettings(
            relay_token=b"m14-simulator-freshness-test-key",
            log_dir=tmp_path,
            telemetry_freshness_ms=250,
        )
    )

    safety = app.state.sim_bridge_factory.safety
    assert safety.max_link_age_ms == 250
    assert safety.max_position_age_ms == 250


def test_m14_sim_app_threads_the_explicit_c2_profile(tmp_path) -> None:
    settings = RelaySettings(
        relay_token=b"r" * 32,
        log_dir=tmp_path,
        capability_release=CapabilityRelease.C2,
    )
    app = create_m14_sim_app(settings, auto_start_nodes=False)

    with TestClient(app):
        runtime = app.state.relay_runtime
        relay_session = runtime.session("sim-c2")

        profile = app.state.sim_bridge_factory.capability_profile

        assert settings.capability_profile is C2_CAPABILITY_PROFILE
        assert profile.supports("disarm")
        assert profile.supports("sweep")
        assert runtime.capability_profile is profile
        assert relay_session.capability_profile is profile
        assert app.state.sim_bridge_factory.bridges["sim-c2"].capability_profile is profile
