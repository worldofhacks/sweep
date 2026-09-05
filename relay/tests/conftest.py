from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from relay.audit import SessionAuditLog
from relay.auth import Principal, sign_event
from relay.capabilities import C1_CAPABILITY_PROFILE
from relay.session import CapabilityBoundIntentSink, RelayLimits, RelaySession

SESSION = "session-test"
CONSOLE_KEY = b"console-key-that-is-at-least-32-bytes"
ADAPTER_KEY = b"adapter-one-key-that-is-at-least-32"


@dataclass(slots=True)
class MutableClock:
    value: int = 1_756_700_000_000

    def __call__(self) -> int:
        return self.value

    def advance(self, milliseconds: int) -> None:
        self.value += milliseconds


class EventIds:
    def __init__(self) -> None:
        self.value = 0

    def __call__(self) -> str:
        self.value += 1
        return f"server-event-{self.value}"


@pytest.fixture
def clock() -> MutableClock:
    return MutableClock()


@pytest.fixture
def event_ids() -> EventIds:
    return EventIds()


@pytest.fixture
def relay_session(tmp_path: Path, clock: MutableClock, event_ids: EventIds) -> RelaySession:
    return RelaySession(
        session_id=SESSION,
        audit_log=SessionAuditLog(tmp_path, SESSION),
        limits=RelayLimits(
            intent_max_age_ms=5_000,
            transport_event_max_age_ms=5_000,
            future_clock_skew_ms=1_000,
            telemetry_freshness_ms=1_000,
        ),
        clock=clock,
        event_ids=event_ids,
        intent_sink=CapabilityBoundIntentSink(lambda _intent, _state: None, C1_CAPABILITY_PROFILE),
    )


@pytest.fixture
def console_principal() -> Principal:
    return Principal(source="console", drone_id=None, signing_key=CONSOLE_KEY)


@pytest.fixture
def keyboard_principal() -> Principal:
    return Principal(source="keyboard", drone_id=None, signing_key=CONSOLE_KEY)


@pytest.fixture
def webcam_principal() -> Principal:
    return Principal(source="webcam", drone_id=None, signing_key=CONSOLE_KEY)


@pytest.fixture
def adapter_principal() -> Principal:
    return Principal(source="adapter", drone_id=1, signing_key=ADAPTER_KEY)


def intent_payload(
    *,
    timestamp: int = 1_756_700_000_000,
    intent_id: str = "intent-1",
    source: str = "console",
    session: str = SESSION,
    retry_of: str | None = None,
) -> dict[str, object]:
    return {
        "v": 1,
        "t": timestamp,
        "type": "intent",
        "intent_id": intent_id,
        "retry_of": retry_of,
        "source": source,
        "session": session,
        "name": "hold",
        "args": {},
        "selection": [1],
        "mode": "indoor",
        "confirm": False,
    }


def membership_payload(
    *,
    action: str,
    event_id: str,
    timestamp: int = 1_756_700_000_000,
    drone_id: int = 1,
    session: str = SESSION,
    connection_epoch: int = 1,
    key: bytes = ADAPTER_KEY,
    **overrides: object,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "v": 1,
        "t": timestamp,
        "type": "membership",
        "event_id": event_id,
        "session": session,
        "drone_id": drone_id,
        "action": action,
    }
    if action == "join":
        payload.update(adapter_id=f"adapter-{drone_id}", capabilities=["flight", "pano_360"])
    elif action == "readiness":
        payload.update(
            connection_epoch=connection_epoch,
            home_pose_confirmed=True,
            control_authority=True,
            rc_safety_operator_present=True,
        )
    elif action == "graceful_leave":
        payload["connection_epoch"] = connection_epoch
    payload.update(overrides)
    payload["signature"] = sign_event(payload, key)
    return payload


def telemetry_payload(
    *,
    event_id: str,
    timestamp: int = 1_756_700_000_000,
    drone_id: int = 1,
    session: str = SESSION,
    connection_epoch: int = 1,
    state: str = "hovering",
) -> dict[str, object]:
    return {
        "v": 1,
        "t": timestamp,
        "type": "telemetry",
        "event_id": event_id,
        "session": session,
        "drone": drone_id,
        "connection_epoch": connection_epoch,
        "x": 1.0,
        "y": 2.0,
        "z": 0.5,
        "vx": 0.0,
        "vy": 0.0,
        "vz": 0.0,
        "battery": 0.8,
        "state": state,
        "link": 0.9,
        "pos_quality": 0.95,
    }


def acknowledgement_payload(
    *,
    event_id: str,
    timestamp: int = 1_756_700_000_000,
    intent_id: str = "intent-1",
    command_id: str | None = "command-1",
    drone_id: int = 1,
    connection_epoch: int = 1,
    roster_version: int = 1,
    status: str = "executing",
    reason: str | None = None,
    detail: str | None = None,
) -> dict[str, object]:
    return {
        "v": 1,
        "t": timestamp,
        "type": "acknowledgement",
        "event_id": event_id,
        "session": SESSION,
        "intent_id": intent_id,
        "command_id": command_id,
        "status": status,
        "drone_id": drone_id,
        "connection_epoch": connection_epoch,
        "roster_version": roster_version,
        "reason": reason,
        "detail": detail,
    }


_COMMAND_ARGS: dict[str, dict[str, object]] = {
    "takeoff": {"z_mm": 1_000},
    "goto": {"x_mm": 1_000, "y_mm": -400, "z_mm": 1_000, "speed_mm_s": 500},
    "rotate_to": {"yaw_mdeg": 90_000, "speed_mdeg_s": 30_000},
    "set_gimbal_pitch": {"pitch_mdeg": -15_000},
    "capture_panorama": {"capture_id": "capture-1"},
    "capture_photo": {"capture_id": "capture-1"},
    "retrieve_media": {"file_id": "capture-1-pano-360"},
}


def command_payload(
    *,
    event_id: str,
    timestamp: int = 1_756_700_000_000,
    command_id: str = "command-1",
    intent_id: str = "intent-1",
    roster_version: int = 1,
    drone_id: int = 1,
    session: str = SESSION,
    connection_epoch: int = 1,
    seq: int = 1,
    ttl_ms: int = 2_000,
    operation: str = "goto",
    args: dict[str, object] | None = None,
    key: bytes = ADAPTER_KEY,
    **overrides: object,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "v": 1,
        "t": timestamp,
        "type": "command",
        "event_id": event_id,
        "session": session,
        "command_id": command_id,
        "intent_id": intent_id,
        "roster_version": roster_version,
        "drone_id": drone_id,
        "connection_epoch": connection_epoch,
        "seq": seq,
        "issued_at": timestamp,
        "ttl_ms": ttl_ms,
        "operation": operation,
        "args": dict(_COMMAND_ARGS.get(operation, {})) if args is None else args,
    }
    payload.update(overrides)
    payload["signature"] = sign_event(payload, key)
    return payload


def capabilities_payload(
    *,
    event_id: str,
    timestamp: int = 1_756_700_000_000,
    drone_id: int = 1,
    session: str = SESSION,
    connection_epoch: int = 1,
    **overrides: object,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "v": 1,
        "t": timestamp,
        "type": "capabilities",
        "event_id": event_id,
        "session": session,
        "drone_id": drone_id,
        "connection_epoch": connection_epoch,
        "native_panorama_modes": ["pano_360"],
        "photo_capture": True,
        "gimbal_pitch_min_deg": -90.0,
        "gimbal_pitch_max_deg": 30.0,
        "horizontal_fov_deg": 66.0,
        "storage_remaining_bytes": 50_000_000,
        "media_retrieval": True,
        "aircraft_model": "DJI Mini 3",
        "aircraft_firmware": "01.00.05.00",
        "rc_firmware": "04.16.05.00",
        "phone_model": "fake-node",
        "android_version": "14",
        "sdk_version": "5.18.0",
        "measured_hfov_deg": None,
    }
    payload.update(overrides)
    return payload


def media_record(
    *,
    timestamp: int = 1_756_700_000_000,
    drone_id: int = 1,
    connection_epoch: int = 1,
    capture_id: str = "capture-1",
    file_id: str = "capture-1-pano-360",
    **overrides: object,
) -> dict[str, object]:
    record: dict[str, object] = {
        "capture_id": capture_id,
        "file_id": file_id,
        "timestamp_ms": timestamp,
        "drone_id": drone_id,
        "connection_epoch": connection_epoch,
        "pose": {"x": 1.0, "y": 2.0, "z": 1.0},
        "actual_yaw_deg": 0.0,
        "gimbal_pitch_deg": 0.0,
        "intrinsics": {
            "width_px": 4_096,
            "height_px": 2_048,
            "horizontal_fov_deg": 360.0,
            "projection": "equirectangular",
        },
        "checksum_sha256": "0" * 64,
        "storage_ref": f"node://media/{drone_id}/{file_id}",
        "retrieval_status": "completed",
    }
    record.update(overrides)
    return record


def media_file_payload(
    *,
    event_id: str,
    timestamp: int = 1_756_700_000_000,
    drone_id: int = 1,
    session: str = SESSION,
    connection_epoch: int = 1,
    **overrides: object,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "v": 1,
        "t": timestamp,
        "type": "media_file",
        "event_id": event_id,
        "session": session,
        **media_record(timestamp=timestamp, drone_id=drone_id, connection_epoch=connection_epoch),
    }
    payload.update(overrides)
    return payload


def capture_bundle_payload(
    *,
    event_id: str,
    timestamp: int = 1_756_700_000_000,
    drone_id: int = 1,
    session: str = SESSION,
    connection_epoch: int = 1,
    **overrides: object,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "v": 1,
        "t": timestamp,
        "type": "capture_bundle",
        "event_id": event_id,
        "session": session,
        "room_id": "room-1",
        "capture_id": "capture-1",
        "drone_id": drone_id,
        "connection_epoch": connection_epoch,
        "pattern": "pano_360",
        "coverage": "full_equirectangular",
        "status": "completed",
        "media": [
            media_record(timestamp=timestamp, drone_id=drone_id, connection_epoch=connection_epoch)
        ],
        "reason": None,
        "detail": None,
    }
    payload.update(overrides)
    return payload


def capture_readiness_payload(
    *,
    event_id: str,
    timestamp: int = 1_756_700_000_000,
    drone_id: int = 1,
    session: str = SESSION,
    connection_epoch: int = 1,
    **overrides: object,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "v": 1,
        "t": timestamp,
        "type": "capture_readiness",
        "event_id": event_id,
        "session": session,
        "drone_id": drone_id,
        "connection_epoch": connection_epoch,
        "room_id": "room-1",
        "capture_id": "capture-1",
        "guidance_mode": "visual_advisory",
        "pose_source": "operator_approved",
        "pose_ok": True,
        "clearance_ok": True,
        "camera_ok": True,
        "storage_ok": True,
        "motion_ok": True,
        "image_quality_ok": True,
        "coverage_missing": [90, 135],
        "next_heading_deg": 90,
        "suggested_delta": {"kind": "yaw", "degrees": 12},
    }
    payload.update(overrides)
    return payload


def node_status_payload(
    *,
    event_id: str,
    timestamp: int = 1_756_700_000_000,
    drone_id: int = 1,
    session: str = SESSION,
    connection_epoch: int = 1,
    **overrides: object,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "v": 1,
        "t": timestamp,
        "type": "node_status",
        "event_id": event_id,
        "session": session,
        "drone_id": drone_id,
        "connection_epoch": connection_epoch,
        "virtual_stick_enabled": False,
        "control_authority": True,
        "authority_change_reason": None,
        "watchdog_state": "nominal",
        "video_publish_state": "stopped",
        "phone_battery_percent": 81,
        "phone_thermal_state": "none",
    }
    payload.update(overrides)
    return payload
