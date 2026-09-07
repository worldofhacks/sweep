from __future__ import annotations

import asyncio
import json

import pytest

from relay.app import RelayRuntime
from relay.auth import Principal
from relay.contracts import NodeType
from relay.media import MediaMonitor, MediaPathObservation
from relay.settings import RelaySettings, SettingsError
from relay.tests.conftest import ADAPTER_KEY, CONSOLE_KEY, SESSION, MutableClock, membership_payload
from relay.tests.test_media import FakePathClient


def test_explicit_ground_unit_preserves_wire_identity_and_disabled_control(tmp_path):
    settings = RelaySettings.from_env(
        {
            "SWEEP_RELAY_TOKEN": CONSOLE_KEY.decode(),
            "SWEEP_ADAPTER_KEYS_JSON": json.dumps({"11": ADAPTER_KEY.decode()}),
            "SWEEP_NODE_TYPES_JSON": '{"11":"ground"}',
            "SWEEP_DEVICE_UNITS_JSON": '{"11":1}',
            "SWEEP_MEDIA_STREAMS_JSON": '{"11":"ground1"}',
            "SWEEP_SESSION_LOG_DIR": str(tmp_path),
            "SWEEP_ADAPTER_BACKEND": "remote",
        }
    )
    runtime = RelayRuntime(settings, clock=MutableClock())
    session = runtime.session(SESSION)
    session.process_frame(
        membership_payload(
            action="join",
            event_id="g01-join",
            drone_id=11,
            node_type="ground",
            capabilities=["ground_drive"],
        ),
        Principal("adapter", 11, ADAPTER_KEY),
    )
    state = session.current_state()
    assert [(d["drone_id"], d["device_class"], d["unit"]) for d in state["drones"]] == [
        (11, "ground_vehicle", 1)
    ]
    assert state["armed"] is False
    assert state["selection"] == []
    assert session.intent_sink is None
    assert settings.media_streams == {11: "ground1"}
    assert state["drones"][0]["cameras"] == [
        {
            "camera_id": "primary",
            "label": "Primary camera",
            "stream": "ground1",
            **state["drones"][0]["video"],
        }
    ]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"device_units": {12: 1}},
        {"device_units": {11: True}},
        {"device_units": {11: 0}},
        {"media_streams": {12: "ground1"}},
        {"media_streams": {11: "../other"}},
        {"media_streams": {11: "ground1?token=wrong"}},
    ],
)
def test_device_mapping_rejects_ambiguous_or_unconfigured_identity(kwargs):
    with pytest.raises(SettingsError):
        RelaySettings(relay_token=CONSOLE_KEY, adapter_keys={11: ADAPTER_KEY}, **kwargs)


def test_duplicate_units_within_class_cannot_alias_two_robots():
    with pytest.raises(SettingsError, match="unique"):
        RelaySettings(
            relay_token=CONSOLE_KEY,
            adapter_keys={11: ADAPTER_KEY, 12: ADAPTER_KEY + b"different"},
            node_types={11: NodeType.GROUND, 12: NodeType.GROUND},
            device_units={11: 1, 12: 1},
        )


def test_media_evidence_stays_bound_to_configured_device_and_expires():
    clock = MutableClock()
    client = FakePathClient()
    monitor = MediaMonitor(client, clock=clock, drone_ids=(11,), streams={11: "ground1"})
    client.paths["ground1"] = MediaPathObservation(online=True, inbound_bytes=100)
    asyncio.run(monitor.poll_once())
    assert client.calls == ["ground1"]
    assert monitor.evidence(1, clock()) is None
    assert monitor.evidence(11, clock()).last_frame_at is None
    clock.advance(1000)
    client.paths["ground1"] = MediaPathObservation(online=True, inbound_bytes=200)
    asyncio.run(monitor.poll_once())
    assert monitor.evidence(11, clock()).last_frame_at == clock()
    clock.advance(3001)
    assert monitor.evidence(11, clock()).fresh is False


@pytest.mark.parametrize(
    "kwargs",
    [
        {"device_units": {11: 1}},
        {"media_streams": {11: "drone1"}},
    ],
)
def test_override_cannot_alias_another_devices_default_identity(kwargs):
    with pytest.raises(SettingsError, match="unique"):
        RelaySettings(
            relay_token=CONSOLE_KEY,
            adapter_keys={1: ADAPTER_KEY, 11: ADAPTER_KEY + b"different"},
            node_types={1: NodeType.GROUND, 11: NodeType.GROUND},
            **kwargs,
        )


def test_two_onboard_cameras_keep_independent_evidence_and_disconnect(tmp_path):
    from media.streams import CameraStream

    clock = MutableClock()
    cameras = {
        11: (CameraStream("front", "Front", "g01-front"), CameraStream("rear", "Rear", "g01-rear"))
    }
    client = FakePathClient()
    monitor = MediaMonitor(client, clock=clock, drone_ids=(11,), cameras=cameras)
    settings = RelaySettings(
        relay_token=CONSOLE_KEY,
        adapter_keys={11: ADAPTER_KEY},
        node_types={11: NodeType.GROUND},
        device_units={11: 1},
        media_cameras=cameras,
        log_dir=tmp_path,
    )
    runtime = RelayRuntime(settings, clock=clock, media_monitor=monitor)
    session = runtime.session(SESSION)
    principal = Principal("adapter", 11, ADAPTER_KEY)
    session.process_frame(
        membership_payload(
            action="join",
            event_id="camera-join",
            drone_id=11,
            node_type="ground",
            capabilities=["ground_drive"],
        ),
        principal,
    )
    client.paths["g01-front"] = MediaPathObservation(online=True, inbound_bytes=100)
    asyncio.run(monitor.poll_once())
    assert session.current_state()["drones"][0]["cameras"][0]["status"] == "unreported"
    clock.advance(1000)
    client.paths["g01-front"] = MediaPathObservation(online=True, inbound_bytes=200)
    asyncio.run(monitor.poll_once())
    projected = session.current_state()["drones"][0]["cameras"]
    assert [(camera["camera_id"], camera["status"]) for camera in projected] == [
        ("front", "live"),
        ("rear", "offline"),
    ]
    clock.advance(1000)
    client.paths["g01-rear"] = MediaPathObservation(online=True, inbound_bytes=250)
    asyncio.run(monitor.poll_once())
    assert session.current_state()["drones"][0]["cameras"][1]["status"] == "unreported"
    clock.advance(1000)
    client.paths["g01-rear"] = MediaPathObservation(online=True, inbound_bytes=350)
    asyncio.run(monitor.poll_once())
    projected = session.current_state()["drones"][0]["cameras"]
    assert projected[1]["last_frame_at"] > projected[0]["last_frame_at"]
    session.handle_adapter_disconnect(drone_id=11, connection_epoch=1)
    assert all(
        camera["status"] == "offline" for camera in session.current_state()["drones"][0]["cameras"]
    )


def test_camera_config_rejects_alias_of_another_devices_primary():
    from media.streams import CameraStream

    with pytest.raises(SettingsError, match="unique"):
        RelaySettings(
            relay_token=CONSOLE_KEY,
            adapter_keys={1: ADAPTER_KEY, 11: ADAPTER_KEY + b"different"},
            media_cameras={11: (CameraStream("front", "Front", "drone1"),)},
        )


def test_one_camera_timeout_does_not_freeze_another_camera():
    from media.streams import CameraStream
    from relay.media import MediaUnreachable

    class PartialFailure(FakePathClient):
        async def read_path(self, name):
            if name == "rear":
                raise MediaUnreachable("secondary offline")
            return await super().read_path(name)

    clock = MutableClock()
    client = PartialFailure()
    monitor = MediaMonitor(
        client,
        clock=clock,
        drone_ids=(11,),
        cameras={
            11: (
                CameraStream("front", "Front", "front"),
                CameraStream("rear", "Rear", "rear"),
            )
        },
    )
    client.paths["front"] = MediaPathObservation(True, 100)
    assert asyncio.run(monitor.poll_once()) is False
    assert monitor.camera_evidence(11, "front", clock()).last_frame_at is None
    clock.advance(1000)
    client.paths["front"] = MediaPathObservation(True, 200)
    assert asyncio.run(monitor.poll_once()) is False
    assert monitor.camera_evidence(11, "front", clock()).last_frame_at == clock()
    assert monitor.camera_evidence(11, "rear", clock()) is None


def test_camera_evidence_cannot_cross_a_connection_epoch():
    from relay.contracts import Membership
    from relay.media import MediaEvidence, project_camera_video

    result = project_camera_video(
        membership=Membership.REGISTERED,
        epoch_started_at=2000,
        now_ms=2500,
        evidence=MediaEvidence(True, 1500, 2500, True),
    )
    assert result == {"status": "unreported", "last_frame_at": None}


@pytest.mark.parametrize(
    "evidence,epoch,now",
    [
        ((True, None, 2000, True), 1000, 2000),
        ((True, 1900, 2100, True), 1000, 2000),
        ((True, 2100, 2000, True), 1000, 2000),
        ((True, 1900, 2000, False), 1000, 2000),
        ((True, 2000, 8000, True), 1000, 8000),
        ((True, 1500, 2500, True), 2000, 2500),
    ],
)
def test_camera_projection_rejects_missing_future_stale_or_pre_epoch_progress(evidence, epoch, now):
    from relay.contracts import Membership
    from relay.media import MediaEvidence, project_camera_video

    result = project_camera_video(
        membership=Membership.READY,
        epoch_started_at=epoch,
        now_ms=now,
        evidence=MediaEvidence(*evidence),
    )
    assert result["status"] == "unreported"


def test_legacy_runtime_mapping_uses_same_progress_epoch_and_stall_guards(tmp_path):
    clock = MutableClock()
    client = FakePathClient()
    monitor = MediaMonitor(client, clock=clock, drone_ids=(11,), streams={11: "ground1"})
    settings = RelaySettings(
        relay_token=CONSOLE_KEY,
        adapter_keys={11: ADAPTER_KEY},
        node_types={11: NodeType.GROUND},
        media_streams={11: "ground1"},
        log_dir=tmp_path,
    )
    runtime = RelayRuntime(settings, clock=clock, media_monitor=monitor)
    session = runtime.session(SESSION)
    principal = Principal("adapter", 11, ADAPTER_KEY)

    def join(event_id):
        session.process_frame(
            membership_payload(
                action="join",
                event_id=event_id,
                drone_id=11,
                timestamp=clock(),
                node_type="ground",
                capabilities=["ground_drive"],
            ),
            principal,
        )

    def camera():
        return session.current_state()["drones"][0]["cameras"][0]

    join("legacy-camera-join")
    client.paths["ground1"] = MediaPathObservation(True, 100)
    asyncio.run(monitor.poll_once())
    assert camera()["stream"] == "ground1"
    assert camera()["status"] == "unreported"
    clock.advance(1000)
    client.paths["ground1"] = MediaPathObservation(True, 200)
    asyncio.run(monitor.poll_once())
    assert camera()["status"] == "live"
    clock.advance(5001)
    asyncio.run(monitor.poll_once())
    assert camera()["status"] == "unreported"
    session.handle_adapter_disconnect(drone_id=11, connection_epoch=1)
    assert camera()["status"] == "offline"
    join("legacy-camera-rejoin")
    assert camera()["status"] == "unreported"
    clock.advance(1000)
    client.paths["ground1"] = MediaPathObservation(True, 300)
    asyncio.run(monitor.poll_once())
    assert camera()["status"] == "live"
