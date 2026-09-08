from __future__ import annotations

import json
import time

from tools.loopback_demo_rehearsal import (
    LoopbackDemoRehearsal,
    _rehearsal_deployment,
    epoch_ms,
)


def test_rehearsal_deployment_reloads_a_signed_fresh_session(tmp_path) -> None:
    now = epoch_ms()
    deployment, projector, pins = _rehearsal_deployment(
        tmp_path, session_id="loopback-fixture", now_ms=now, lifetime_ms=60_000
    )

    deployment.approval.check(
        session="loopback-fixture",
        configuration_sha256=deployment.approval.configuration_sha256,
        now_ms=now,
        epochs=((1, 1),),
    )
    deployment.validate_projector(projector)
    assert pins.clock_mapping.relay_reference_ms == now
    assert deployment.wire_profiles[1].clock_lease_expires_at_ms == now + 60_000


def test_loopback_rehearsal_publishes_a_fresh_signed_pose_and_private_bootstrap(tmp_path) -> None:
    bootstrap = tmp_path / "bootstrap.json"
    with LoopbackDemoRehearsal(
        start_console=False,
        bootstrap_path=bootstrap,
        console_port=47767,
    ) as rehearsal:
        assert bootstrap.stat().st_mode & 0o077 == 0
        payload = json.loads(bootstrap.read_text())
        assert payload["relay"]["origin"] == rehearsal.relay_url
        assert payload["relay"]["session"] == rehearsal.session_id

        runtime = rehearsal._composition.runtime
        deadline = time.monotonic() + 3
        pose = None
        while time.monotonic() < deadline:
            pose = runtime.sessions[rehearsal.session_id].control_pose(1)
            if pose is not None:
                break
            time.sleep(0.05)
        assert pose is not None
        state = runtime.sessions[rehearsal.session_id].current_state()
        assert "test:synthetic" in state["drones"][0]["adapter_capabilities"]
    assert not bootstrap.exists()
