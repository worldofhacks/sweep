from __future__ import annotations

import json
import time
from urllib.request import Request, urlopen

from tools.loopback_demo_rehearsal import (
    LoopbackDemoRehearsal,
    _rehearsal_deployment,
    epoch_ms,
)
from tools.loopback_search_fixture import SYNTHETIC_SOURCE_ID


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
    artifact = deployment.artifact()
    assert [zone.zone_id for zone in artifact.zones] == ["demo-east", "demo-west", "lobby"]
    assert {
        (zone.zone_id, slot.slot_id)
        for zone in artifact.zones
        for slot in zone.arrival_slots
    } == {
        ("demo-west", "demo-west-slot"),
        ("lobby", "lobby-slot"),
        ("demo-east", "demo-east-slot"),
    }
    assert all(zone.owner_approved for zone in artifact.zones)


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
        request = Request(
            f"http://127.0.0.1:{rehearsal.relay_port}/api/sessions/"
            f"{rehearsal.session_id}/navigation/catalog",
            headers={"Authorization": f"Bearer {payload['relay']['token']}"},
        )
        with urlopen(request, timeout=3) as response:
            catalog = json.loads(response.read())["catalog"]
        assert [
            destination["zoneId"]
            for destination in catalog["destinations"]
            if not destination["excluded"]
        ] == [
            "demo-west",
            "lobby",
            "demo-east",
        ]
        with urlopen(
            Request(
                f"http://127.0.0.1:{rehearsal.relay_port}/session/{rehearsal.session_id}",
                headers={"Authorization": f"Bearer {payload['relay']['token']}"},
            ),
            timeout=3,
        ) as response:
            replay = json.loads(response.read())
        state = next(
            record["event"]
            for record in reversed(replay["events"])
            if record["event"]["type"] == "state"
        )
        assert state["armed"] is True
        assert state["drones"][0]["telemetry"]["state"] == "hovering"

        runtime = rehearsal._composition.runtime
        autonomy = rehearsal._composition.session(rehearsal.session_id)
        assert rehearsal._app is not None
        approved = rehearsal._app.state.platform_services.maps.approved_bundle(rehearsal.session_id)
        authoring_map_pin = rehearsal._composition.config.navigation.config.authoring_map_pin
        assert authoring_map_pin is not None
        assert authoring_map_pin.version == approved["bundle"]["manifest"]["mapVersion"]
        assert authoring_map_pin.content_sha256 == approved["reference"]["contentHash"]
        assert autonomy.search_runtime is not None
        assert autonomy.search_runtime.config.source_by_drone == {1: SYNTHETIC_SOURCE_ID}
        assert autonomy.search_detection is not None
        deadline = time.monotonic() + 3
        pose = None
        while time.monotonic() < deadline:
            pose = runtime.sessions[rehearsal.session_id].control_pose(1)
            if pose is not None:
                break
            time.sleep(0.05)
        assert pose is not None
        relay_state = runtime.sessions[rehearsal.session_id].current_state()
        assert "test:synthetic" in relay_state["drones"][0]["adapter_capabilities"]
    assert not bootstrap.exists()
