from __future__ import annotations

from adapters.sim.runtime import create_m14_sim_app
from relay.settings import RelaySettings


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
