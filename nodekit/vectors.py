"""Wire vectors for the node kit, rendered from the relay's own code.

``nodekit`` may not import ``relay`` at runtime: it is copied onto a robot on its own. The
price of that independence is a second implementation of the canonical JSON, the HMAC, and
every frame shape, which could drift. This module pays it by generating
``nodekit/tests/vectors/*.json`` **from the relay contracts and signer**, reusing the
Android bridge's generator (``adapters/dji_mini3/vectors.py``) wherever the shape is the
same; ``nodekit/tests/test_vectors.py`` then holds the kit to those bytes and fails when
the committed files are stale.

Refresh with ``uv run python -m nodekit.vectors`` from the repository root.
"""

from __future__ import annotations

import json
from pathlib import Path

from adapters.dji_mini3 import vectors as bridge_vectors
from planner.models import CommandOperation, DeviceClass
from relay.auth import sign_event
from relay.contracts import (
    COMMAND_ARGUMENT_FIELDS,
    DEVICE_CLASS_CAPABILITY_PREFIX,
    MAX_SENSOR_FRAME_CANONICAL_BYTES,
    MAX_SENSOR_RANGE_CM,
    MAX_SENSOR_RANGES,
    SENSOR_ANGLE_INCREMENTS_DEG,
    MembershipAction,
    MembershipRequest,
    NodeAcknowledgementReason,
    SensorFrame,
    SensorKind,
    SensorPose,
    TelemetryV1,
)

VECTOR_DIR = Path(__file__).resolve().parent / "tests" / "vectors"

NODE_KEY = bridge_vectors.NODE_KEY
SESSION = bridge_vectors.SESSION

# A ground vehicle in the demo's id range (11 to 13), mid-session.
GROUND_ID = 11
GROUND_EPOCH = 3
GROUND_CAPABILITIES = ("ground_drive", "lidar", "camera")
GROUND_ADAPTER_ID = "ohmni-1"


def ground_join() -> MembershipRequest:
    return MembershipRequest(
        1,
        8000,
        "membership",
        "evt-ground-join-1",
        SESSION,
        GROUND_ID,
        MembershipAction.JOIN,
        "",
        adapter_id=GROUND_ADAPTER_ID,
        capabilities=(
            *GROUND_CAPABILITIES,
            DEVICE_CLASS_CAPABILITY_PREFIX + DeviceClass.GROUND_VEHICLE.value,
        ),
    )


def ground_readiness() -> MembershipRequest:
    return MembershipRequest(
        1,
        8001,
        "membership",
        "evt-ground-ready-1",
        SESSION,
        GROUND_ID,
        MembershipAction.READINESS,
        "",
        connection_epoch=GROUND_EPOCH,
        home_pose_confirmed=True,
        control_authority=True,
        rc_safety_operator_present=True,
    )


def ground_leave() -> MembershipRequest:
    return MembershipRequest(
        1,
        8002,
        "membership",
        "evt-ground-leave-1",
        SESSION,
        GROUND_ID,
        MembershipAction.GRACEFUL_LEAVE,
        "",
        connection_epoch=GROUND_EPOCH,
    )


def ground_telemetry() -> TelemetryV1:
    """A driving robot: z and vz are zero and the state is from the drive vocabulary."""
    return TelemetryV1(
        1,
        8003,
        "telemetry",
        "evt-ground-telemetry-1",
        SESSION,
        GROUND_ID,
        GROUND_EPOCH,
        1.2,
        -0.4,
        0.0,
        0.25,
        0.1,
        0.0,
        0.86,
        "moving",
        0.95,
        0.6,
    )


def ground_scan_ranges() -> list[int]:
    """A synthetic room seen at 2 degrees per bin: walls, with a few no-return bins."""
    return [0 if index % 13 == 0 else 150 + (index % 7) * 10 for index in range(180)]


def ground_sensor() -> SensorFrame:
    return SensorFrame(
        1,
        8004,
        "sensor",
        "evt-ground-sensor-1",
        SESSION,
        GROUND_ID,
        GROUND_EPOCH,
        SensorKind.LIDAR_SCAN,
        SensorPose(1.2, -0.4, 87.5),
        0.0,
        2.0,
        0.15,
        12.0,
        tuple(ground_scan_ranges()),
    )


def ground_capabilities() -> dict[str, object]:
    """``nodekit.fake.FakeGroundVehicle.hardware_profile`` through the aircraft-shaped frame."""
    return {
        "v": 1,
        "t": 8005,
        "type": "capabilities",
        "event_id": "evt-ground-capabilities-1",
        "session": SESSION,
        "drone_id": GROUND_ID,
        "connection_epoch": GROUND_EPOCH,
        "native_panorama_modes": [],
        "photo_capture": False,
        "gimbal_pitch_min_deg": -1.0,
        "gimbal_pitch_max_deg": 1.0,
        "horizontal_fov_deg": 78.0,
        "storage_remaining_bytes": 0,
        "media_retrieval": False,
        "aircraft_model": "fake-ground-vehicle",
        "aircraft_firmware": "fake",
        "rc_firmware": "fake",
        "phone_model": "fake-node",
        "android_version": "fake",
        "sdk_version": "fake",
        "measured_hfov_deg": None,
    }


def ground_node_status() -> dict[str, object]:
    return {
        "v": 1,
        "t": 8006,
        "type": "node_status",
        "event_id": "evt-ground-status-1",
        "session": SESSION,
        "drone_id": GROUND_ID,
        "connection_epoch": GROUND_EPOCH,
        "virtual_stick_enabled": False,
        "control_authority": True,
        "authority_change_reason": None,
        "watchdog_state": "hold",
        "video_publish_state": "publishing",
        "phone_battery_percent": 86,
        "phone_thermal_state": "none",
    }


def ground_acknowledgement() -> dict[str, object]:
    """The refusal a ground vehicle returns for an operation its class does not have."""
    return {
        "v": 1,
        "t": 8007,
        "type": "acknowledgement",
        "event_id": "evt-ground-ack-1",
        "session": SESSION,
        "intent_id": "intent-9",
        "command_id": "cmd-9",
        "status": "failed",
        "drone_id": GROUND_ID,
        "connection_epoch": GROUND_EPOCH,
        "roster_version": 5,
        "reason": NodeAcknowledgementReason.UNSUPPORTED_OPERATION.value,
        "detail": "takeoff is not available for a ground_vehicle",
    }


def control_heartbeat_unsigned() -> dict[str, object]:
    """The relay's control lease exactly as ``RelayRuntime._publish_control_heartbeats``
    builds it (``relay/app.py``); the node's deadman accepts nothing else."""
    return {
        "v": 1,
        "t": 8008,
        "type": "control_heartbeat",
        "event_id": "evt-ground-heartbeat-1",
        "session": SESSION,
        "source": "relay",
        "drone_id": GROUND_ID,
        "connection_epoch": GROUND_EPOCH,
        "roster_version": 5,
        "seq": 12,
    }


def _signed(unsigned: dict[str, object], key: str) -> dict[str, object]:
    return {**unsigned, "signature": sign_event(unsigned, key)}


def node_protocol() -> dict[str, object]:
    return {
        "session": SESSION,
        "key": NODE_KEY,
        "device_classes": sorted(member.value for member in DeviceClass),
        "device_class_capability_prefix": DEVICE_CLASS_CAPABILITY_PREFIX,
        "command_operations": sorted(member.value for member in CommandOperation),
        "command_argument_fields": {
            operation.value: dict(fields) for operation, fields in COMMAND_ARGUMENT_FIELDS.items()
        },
        "command_args": bridge_vectors.command_args(),
        "acknowledgement_reasons": sorted(member.value for member in NodeAcknowledgementReason),
        "node_settings": bridge_vectors.node_settings(),
        "sensor_bounds": {
            "angle_increments_deg": list(SENSOR_ANGLE_INCREMENTS_DEG),
            "max_ranges": MAX_SENSOR_RANGES,
            "max_range_cm": MAX_SENSOR_RANGE_CM,
            "max_canonical_bytes": MAX_SENSOR_FRAME_CANONICAL_BYTES,
        },
        "frames": {
            "command": {
                "key": NODE_KEY,
                "wire": _signed(bridge_vectors.command_unsigned(), NODE_KEY),
            },
            "control_heartbeat": {
                "key": NODE_KEY,
                "wire": _signed(control_heartbeat_unsigned(), NODE_KEY),
            },
            "ground_join": {
                "key": NODE_KEY,
                "wire": _signed(ground_join().unsigned_event(), NODE_KEY),
            },
            "ground_readiness": {
                "key": NODE_KEY,
                "wire": _signed(ground_readiness().unsigned_event(), NODE_KEY),
            },
            "ground_graceful_leave": {
                "key": NODE_KEY,
                "wire": _signed(ground_leave().unsigned_event(), NODE_KEY),
            },
            "ground_telemetry": {"wire": ground_telemetry().to_event()},
            "ground_sensor": {"wire": ground_sensor().to_event()},
            "ground_capabilities": {"wire": ground_capabilities()},
            "ground_node_status": {"wire": ground_node_status()},
            "ground_acknowledgement": {"wire": ground_acknowledgement()},
        },
    }


def render() -> dict[str, str]:
    documents = {
        # The kit signs the same bytes the Android bridge does; both are held to the relay.
        "canonical_json.json": {"cases": bridge_vectors.canonical_json_cases()},
        "hmac_sha256.json": {"cases": bridge_vectors.hmac_cases()},
        "node_protocol.json": node_protocol(),
    }
    return {
        name: json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        for name, document in documents.items()
    }


def write(directory: Path = VECTOR_DIR) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for name, text in render().items():
        path = directory / name
        path.write_text(text, encoding="utf-8")
        written.append(path)
    return written


if __name__ == "__main__":
    for path in write():
        print(path)
