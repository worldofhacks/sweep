from fastapi.testclient import TestClient

from adapters.sim.runtime import create_m14_sim_app
from relay.capabilities import C2_CAPABILITY_PROFILE
from relay.settings import CapabilityRelease, RelaySettings


def test_m14_sim_app_threads_the_explicit_c2_profile() -> None:
    settings = RelaySettings(
        relay_token=b"r" * 32,
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
