from __future__ import annotations

import asyncio
import json

import pytest

from media.streams import CameraStream, parse_camera_mapping
from planner.models import DeviceClass
from relay.audit import MAX_AUDIT_RECORD_BYTES, AuditLogError, SessionAuditLog
from relay.contracts import DeviceIdentity, Membership, parse_membership_request
from relay.media import MediaEvidence, MediaMonitor, MediaPathObservation, project_camera_video
from relay.session import _material_state_projection
from relay.settings import RelaySettings, SettingsError
from relay.state import FleetRegistry
from relay.tests.conftest import (
    ADAPTER_KEY,
    CONSOLE_KEY,
    SESSION,
    MutableClock,
    membership_payload,
)

T0 = 1_756_700_000_000
CAMERAS = (
    CameraStream("primary", "Front", "ground5"),
    CameraStream("rear", "Rear", "ground5_rear"),
)


def _mapping():
    return {"11": [camera.to_dict() for camera in CAMERAS]}


def test_settings_accept_explicit_camera_inventory_and_no_camera_override() -> None:
    env = {
        "SWEEP_RELAY_TOKEN": CONSOLE_KEY.decode(),
        "SWEEP_ADAPTER_KEYS_JSON": json.dumps({"11": ADAPTER_KEY.decode()}),
        "SWEEP_DEVICE_CLASSES_JSON": '{"11":"ground_vehicle"}',
        "SWEEP_MEDIA_CAMERAS_JSON": json.dumps(_mapping()),
    }
    settings = RelaySettings.from_env(env)
    assert settings.configured_cameras() == {11: CAMERAS}
    assert RelaySettings.from_env(
        {**env, "SWEEP_MEDIA_CAMERAS_JSON": '{"11":[]}'}
    ).configured_cameras() == {11: ()}
    legacy = RelaySettings.from_env({**env, "SWEEP_MEDIA_CAMERAS_JSON": "{}"})
    assert legacy.configured_cameras() == {11: (CameraStream("primary", "Primary", "ground1"),)}
    with pytest.raises(SettingsError):
        RelaySettings.from_env({**env, "SWEEP_MEDIA_CAMERAS_JSON": '{"12":[]}'})


@pytest.mark.parametrize(
    "stream",
    ["../camera", "a/b", "https://camera", "x?token=y", "a\n", "a\r", "a\u2028", "a" * 97, ""],
)
def test_camera_stream_paths_are_local_bounded_names(stream: str) -> None:
    with pytest.raises(ValueError):
        CameraStream("front", "Front", stream)


def test_camera_mapping_is_bounded_unique_and_configured() -> None:
    all_cameras = {
        str(i): [
            CameraStream(f"cam{j}", f"Camera {j}", f"unit{i}_cam{j}").to_dict() for j in range(8)
        ]
        for i in range(1, 65)
    }
    assert len(parse_camera_mapping(all_cameras, set(range(1, 65)))) == 64
    for invalid in (
        {**all_cameras, "65": []},
        {"11": [CAMERAS[0].to_dict()] * 2},
        {"11": [CameraStream(f"cam{i}", "Camera", f"cam{i}").to_dict() for i in range(9)]},
        {"11": [CAMERAS[0].to_dict()], "12": [CAMERAS[0].to_dict()]},
        {"011": []},
        {"0": []},
        {"11": [{"camera_id": "a", "label": "A", "stream": "a", "url": "x"}]},
    ):
        with pytest.raises(ValueError):
            parse_camera_mapping(invalid, set(range(1, 66)))


def test_camera_status_expires_independently_and_rejects_old_epoch_frames() -> None:
    clock = MutableClock(T0)

    class Paths:
        failed = False

        async def read_path(self, stream):
            if self.failed and stream == "ground5_rear":
                raise RuntimeError("camera unavailable")
            return MediaPathObservation(online=True, inbound_bytes=clock())

    paths = Paths()
    monitor = MediaMonitor(
        paths,
        clock=clock,
        devices={11: DeviceIdentity(DeviceClass.GROUND_VEHICLE, 5)},
        cameras={11: CAMERAS},
    )
    assert asyncio.run(monitor.poll_once()) is True
    paths.failed = True
    clock.advance(3001)
    assert asyncio.run(monitor.poll_once()) is False
    front, rear = (monitor.camera_evidence(11, camera.camera_id, clock()) for camera in CAMERAS)
    assert front.fresh is True and front.last_frame_at == clock()
    assert rear.fresh is False and rear.last_frame_at == T0
    kwargs = {"membership": Membership.REGISTERED, "epoch_started_at": T0, "now_ms": clock()}
    assert project_camera_video(**kwargs, evidence=front)["status"] == "live"
    assert project_camera_video(**kwargs, evidence=rear)["status"] == "unreported"
    assert project_camera_video(**{**kwargs, "epoch_started_at": clock()}, evidence=rear) == {
        "status": "unreported",
        "last_frame_at": None,
    }
    for membership in (Membership.DISCONNECTED, Membership.LEAVING):
        assert project_camera_video(**{**kwargs, "membership": membership}, evidence=front) == {
            "status": "offline",
            "last_frame_at": None,
        }
    stalled = MediaEvidence(True, T0, clock(), True)
    assert (
        project_camera_video(**{**kwargs, "now_ms": T0 + 5001}, evidence=stalled)["status"]
        == "unreported"
    )


def _registry():
    registry = FleetRegistry(
        telemetry_freshness_ms=1000,
        devices={11: DeviceIdentity(DeviceClass.AIRCRAFT, 5)},
        media_cameras={11: CAMERAS},
    )
    registry.apply_join(
        parse_membership_request(
            membership_payload(action="join", event_id="join-camera", drone_id=11, timestamp=T0)
        )
    )
    return registry


def test_registry_projects_only_declared_cameras_and_does_not_infer_a_sensor() -> None:
    state = _registry().state_event(session=SESSION, t=T0, event_id="camera-state")
    drone = state["drones"][0]
    assert drone["cameras"] == [
        {**camera.to_dict(), "status": "unreported", "last_frame_at": None} for camera in CAMERAS
    ]
    assert drone["sensor"] == {"kind": None, "last_scan_at": None}
    assert json.loads(_material_state_projection(state))["drones"][0]["cameras"] == [
        {**camera.to_dict(), "status": "unreported"} for camera in CAMERAS
    ]


@pytest.mark.parametrize(
    "change",
    [
        {"status": []},
        {"last_frame_at": True},
        {"last_frame_at": -1},
        {"last_frame_at": float("nan")},
        {"stream": "../x"},
    ],
)
def test_camera_audit_refuses_malformed_records(change) -> None:
    state = _registry().state_event(session=SESSION, t=T0, event_id="camera-state")
    state["drones"][0]["cameras"][0].update(change)
    with pytest.raises(AuditLogError):
        _material_state_projection(state)


def test_camera_audit_refuses_duplicate_identities() -> None:
    state = _registry().state_event(session=SESSION, t=T0, event_id="camera-state")
    state["drones"][0]["cameras"].append(state["drones"][0]["cameras"][0])
    with pytest.raises(AuditLogError):
        _material_state_projection(state)


def test_64_bounded_capability_reports_exceed_old_audit_limit_and_fit_new_bound() -> None:
    registry = FleetRegistry(telemetry_freshness_ms=1000)
    capabilities = ["flight", *(f"{index}" + "🚁" * 127 for index in range(15))]
    for device_id in range(1, 65):
        registry.apply_join(
            parse_membership_request(
                membership_payload(
                    action="join",
                    event_id=f"join-{device_id}",
                    drone_id=device_id,
                    timestamp=T0,
                    capabilities=capabilities,
                )
            )
        )
    state = registry.state_event(session=SESSION, t=T0, event_id="full-fleet-state")
    assert len(json.loads(_material_state_projection(state))["drones"]) == 64
    encoded = SessionAuditLog._encode_record({"seq": 1, "event": state})
    assert 1 << 20 < len(encoded) < MAX_AUDIT_RECORD_BYTES == 16 << 20


def test_camera_labels_use_unicode_code_points_and_printable_space_only() -> None:
    assert CameraStream("front", "🚁" * 64, "front").label == "🚁" * 64
    for label in ("🚁" * 65, "Front\u00a0camera", "Front\u2007camera", "Front\nCamera"):
        with pytest.raises(ValueError):
            CameraStream("front", label, "front")
