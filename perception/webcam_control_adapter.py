"""Convert `webcam_localization` pose records into `control_publisher` tag sensor records.

`webcam_localization` emits observation-only JSONL: a map/world-frame body pose plus
provenance, with no notion of an aircraft identity, a relay connection epoch, or a
control-fusion frame. `control_publisher.enqueue()` requires an exact, fully-provenanced
`TagFix` record. This module is the translation between the two: it supplies the
identities and physical transforms that only deployment configuration or a physical
survey can provide, and passes through the measured pose and timing untouched.

This module never contacts the relay and grants no control authority.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from tools.map_common import parse_document, validate_transform

GIMBAL_ATTITUDE_CONVENTION = "intrinsic_zyx_degrees"
_MAX_IDENTIFIER_CHARS = 128


class AdapterError(ValueError):
    """A bounded refusal to translate a webcam localization record or its config."""


def _identifier(value: object, name: str) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or not value.isprintable()
        or len(value) > _MAX_IDENTIFIER_CHARS
    ):
        raise AdapterError(f"{name} must be canonical text of at most 128 characters")
    return value


def _finite(value: object, name: str) -> float:
    if isinstance(value, bool) or type(value) not in (int, float) or not np.isfinite(value):
        raise AdapterError(f"{name} must be finite")
    return float(value)


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or type(value) is not int or value <= 0:
        raise AdapterError(f"{name} must be a positive integer")
    return value


def _transform(value: object, name: str) -> np.ndarray:
    try:
        matrix = np.asarray(validate_transform(np.asarray(value).tolist()), dtype=float)
    except (TypeError, ValueError) as error:
        raise AdapterError(f"{name} must be a rigid 4x4 transform") from error
    return matrix


def _covariance(value: object, name: str) -> np.ndarray:
    try:
        matrix = np.asarray(value, dtype=float)
    except (TypeError, ValueError) as error:
        raise AdapterError(f"{name} must be a 3x3 positive definite matrix") from error
    if matrix.shape != (3, 3) or not np.isfinite(matrix).all() or not np.allclose(matrix, matrix.T):
        raise AdapterError(f"{name} must be a 3x3 positive definite matrix")
    try:
        if np.linalg.eigvalsh(matrix).min() <= 0:
            raise AdapterError(f"{name} must be a 3x3 positive definite matrix")
    except np.linalg.LinAlgError as error:
        raise AdapterError(f"{name} must be a 3x3 positive definite matrix") from error
    return matrix


def _pose_from_mapping(raw: object, parent: str, child: str, name: str) -> np.ndarray:
    if not isinstance(raw, Mapping) or (raw.get("parent_frame"), raw.get("child_frame")) != (
        parent,
        child,
    ):
        raise AdapterError(f"{name} must be a {parent}-to-{child} pose")
    fields = ("x_m", "y_m", "z_m", "qx", "qy", "qz", "qw")
    if set(raw) != {"parent_frame", "child_frame", *fields}:
        raise AdapterError(f"{name} fields do not match the pose contract")
    x, y, z, qx, qy, qz, qw = (_finite(raw[field], f"{name} {field}") for field in fields)
    quaternion = np.array([qx, qy, qz, qw])
    if not np.isclose(np.linalg.norm(quaternion), 1.0, atol=1e-6):
        raise AdapterError(f"{name} quaternion must be unit length")
    x2, y2, z2 = quaternion[:3] * 2
    xx, yy, zz = quaternion[:3] * np.array([x2, y2, z2])
    xy, xz, yz = qx * y2, qx * z2, qy * z2
    wx, wy, wz = qw * np.array([x2, y2, z2])
    matrix = np.eye(4)
    matrix[:3, :3] = (
        (1 - yy - zz, xy - wz, xz + wy),
        (xy + wz, 1 - xx - zz, yz - wx),
        (xz - wy, yz + wx, 1 - xx - yy),
    )
    matrix[:3, 3] = (x, y, z)
    return _transform(matrix.tolist(), name)


def _intrinsic_zyx_rotation(yaw_deg: float, pitch_deg: float, roll_deg: float) -> np.ndarray:
    """Body/gimbal rotation for `intrinsic_zyx_degrees`: R = Rz(yaw) @ Ry(pitch) @ Rx(roll)."""
    yaw, pitch, roll = np.deg2rad([yaw_deg, pitch_deg, roll_deg])
    cos, sin = np.cos, np.sin
    rz = np.array([[cos(yaw), -sin(yaw), 0], [sin(yaw), cos(yaw), 0], [0, 0, 1]])
    ry = np.array([[cos(pitch), 0, sin(pitch)], [0, 1, 0], [-sin(pitch), 0, cos(pitch)]])
    rx = np.array([[1, 0, 0], [0, cos(roll), -sin(roll)], [0, sin(roll), cos(roll)]])
    return rz @ ry @ rx


@dataclass(frozen=True, slots=True)
class GimbalAttitude:
    yaw_deg: float
    pitch_deg: float
    roll_deg: float

    def __post_init__(self) -> None:
        for name in ("yaw_deg", "pitch_deg", "roll_deg"):
            value = _finite(getattr(self, name), name)
            if abs(value) > 360:
                raise AdapterError(f"{name} exceeds angular bounds")
            object.__setattr__(self, name, value)

    @classmethod
    def from_mapping(cls, raw: object) -> GimbalAttitude:
        fields = {"yaw_deg", "pitch_deg", "roll_deg"}
        if not isinstance(raw, Mapping) or set(raw) != fields:
            raise AdapterError("gimbal attitude fields do not match the contract")
        return cls(raw["yaw_deg"], raw["pitch_deg"], raw["roll_deg"])  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class CaptureAlignmentDocument:
    """Static mount geometry: body-to-gimbal and gimbal-to-camera rigid poses.

    Live gimbal attitude is supplied separately, per `body_to_camera`, because it
    changes in flight while the two mounts do not.
    """

    alignment_id: str
    body_to_gimbal: np.ndarray
    gimbal_to_camera: np.ndarray

    @classmethod
    def from_document(cls, document: object) -> CaptureAlignmentDocument:
        fields = {"v", "alignment_id", "gimbal_attitude_convention", "body_to_gimbal", "gimbal_to_camera"}
        if not isinstance(document, Mapping) or set(document) != fields or document.get("v") != 1:
            raise AdapterError("capture alignment document does not match v1 schema")
        if document["gimbal_attitude_convention"] != GIMBAL_ATTITUDE_CONVENTION:
            raise AdapterError("capture alignment gimbal attitude convention is unsupported")
        alignment_id = _identifier(document["alignment_id"], "alignment_id")
        body_to_gimbal = _pose_from_mapping(document["body_to_gimbal"], "body", "gimbal", "body_to_gimbal")
        gimbal_to_camera = _pose_from_mapping(
            document["gimbal_to_camera"], "gimbal", "camera", "gimbal_to_camera"
        )
        return cls(alignment_id, body_to_gimbal, gimbal_to_camera)

    def body_to_camera(self, attitude: GimbalAttitude) -> np.ndarray:
        dynamic = np.eye(4)
        dynamic[:3, :3] = _intrinsic_zyx_rotation(attitude.yaw_deg, attitude.pitch_deg, attitude.roll_deg)
        return _transform(
            (self.body_to_gimbal @ dynamic @ self.gimbal_to_camera).tolist(), "body_to_camera"
        )


@dataclass(frozen=True, slots=True)
class WebcamControlAdapterConfig:
    """Every identity and transform the tag sensor-record schema needs that
    `webcam_localization` cannot supply on its own."""

    drone_id: int
    map_id: str
    geometry_id: str
    clock_id: str
    source_id: str
    camera_calibration_id: str
    body_extrinsics_id: str
    source_verified: bool
    timing_verified: bool
    map_to_map_enu: np.ndarray
    position_covariance_map_enu_m2: np.ndarray
    capture_alignment: CaptureAlignmentDocument
    gimbal_attitude: GimbalAttitude

    @classmethod
    def from_document(cls, document: object) -> WebcamControlAdapterConfig:
        fields = {
            "v",
            "drone_id",
            "map_id",
            "geometry_id",
            "clock_id",
            "source_id",
            "camera_calibration_id",
            "body_extrinsics_id",
            "source_verified",
            "timing_verified",
            "map_to_map_enu",
            "position_covariance_map_enu_m2",
            "gimbal_locked",
            "gimbal_attitude_deg",
            "capture_alignment",
        }
        if not isinstance(document, Mapping) or set(document) != fields or document.get("v") != 1:
            raise AdapterError("webcam control adapter config does not match v1 schema")
        alignment = document["map_to_map_enu"]
        if not isinstance(alignment, Mapping) or set(alignment) != {"measured", "matrix"}:
            raise AdapterError("map_to_map_enu must carry a measured rigid transform")
        if alignment["measured"] is not True:
            raise AdapterError("map_to_map_enu must be measured, not assumed")
        if document["gimbal_locked"] is not True:
            raise AdapterError(
                "this adapter assumes a mechanically locked gimbal; gimbal_locked must be true"
            )
        if not isinstance(document["source_verified"], bool) or not isinstance(
            document["timing_verified"], bool
        ):
            raise AdapterError("source_verified and timing_verified must be explicit booleans")
        return cls(
            drone_id=_positive_int(document["drone_id"], "drone_id"),
            map_id=_identifier(document["map_id"], "map_id"),
            geometry_id=_identifier(document["geometry_id"], "geometry_id"),
            clock_id=_identifier(document["clock_id"], "clock_id"),
            source_id=_identifier(document["source_id"], "source_id"),
            camera_calibration_id=_identifier(
                document["camera_calibration_id"], "camera_calibration_id"
            ),
            body_extrinsics_id=_identifier(document["body_extrinsics_id"], "body_extrinsics_id"),
            source_verified=document["source_verified"],
            timing_verified=document["timing_verified"],
            map_to_map_enu=_transform(alignment["matrix"], "map_to_map_enu"),
            position_covariance_map_enu_m2=_covariance(
                document["position_covariance_map_enu_m2"], "position_covariance_map_enu_m2"
            ),
            capture_alignment=CaptureAlignmentDocument.from_document(document["capture_alignment"]),
            gimbal_attitude=GimbalAttitude.from_mapping(document["gimbal_attitude_deg"]),
        )


def convert_pose_record(
    record: Mapping[str, object],
    *,
    config: WebcamControlAdapterConfig,
    event_id: str,
    connection_epoch: int,
) -> dict[str, object] | None:
    """Build one tag sensor record from a `webcam_localization` JSONL line.

    Returns `None` when the record carries no accepted tag pose (no detection, a
    rejected solve, or a stale/aged report) -- there is nothing to publish.
    """
    observation = record.get("pose_observation")
    if (
        not isinstance(observation, Mapping)
        or observation.get("accepted") is not True
        or observation.get("reason") != "pose"
    ):
        return None
    capture_time = _finite(observation.get("capture_time"), "capture_time")
    if capture_time < 0:
        raise AdapterError("capture_time must be nonnegative")
    body_key = "T_world_body" if "T_world_body" in observation else "T_map_body"
    if body_key not in observation:
        raise AdapterError("pose observation lacks a body pose")
    body = _transform(observation[body_key], body_key)

    position_map = np.append(body[:3, 3], 1.0)
    position_enu = (config.map_to_map_enu @ position_map)[:3]
    rotation = config.map_to_map_enu[:3, :3]
    covariance_enu = rotation @ config.position_covariance_map_enu_m2 @ rotation.T

    extrinsics_matrix = config.capture_alignment.body_to_camera(config.gimbal_attitude)

    return {
        "kind": "tag",
        "drone_id": config.drone_id,
        "event_id": _identifier(event_id, "event_id"),
        "connection_epoch": _positive_int(connection_epoch, "connection_epoch"),
        "map_id": config.map_id,
        "geometry_id": config.geometry_id,
        "clock_id": config.clock_id,
        "capture_time": capture_time,
        "position_map_enu_m": [float(value) for value in position_enu],
        "covariance_map_enu_m2": [[float(value) for value in row] for row in covariance_enu],
        "source_id": config.source_id,
        "camera_calibration_id": config.camera_calibration_id,
        "source_verified": config.source_verified,
        "timing_verified": config.timing_verified,
        "extrinsics": {
            "extrinsics_id": config.body_extrinsics_id,
            "source_id": config.source_id,
            "matrix": [[float(value) for value in row] for row in extrinsics_matrix],
            "capture_time": capture_time,
            "gimbal_time": capture_time,
            "attitude_time": capture_time,
            "measured": True,
        },
    }


def load_config(path: Path) -> WebcamControlAdapterConfig:
    path = Path(path)
    document = parse_document(path.read_bytes(), str(path))
    return WebcamControlAdapterConfig.from_document(document)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--input", type=Path, help="webcam_localization JSONL; defaults to stdin")
    parser.add_argument("--output", type=Path, help="sensor-record JSONL; defaults to stdout")
    parser.add_argument("--connection-epoch", type=int, required=True)
    parser.add_argument("--run-id", required=True, help="prefix for generated event_id values")
    args = parser.parse_args()
    if args.connection_epoch <= 0:
        raise SystemExit("--connection-epoch must be a positive integer")
    config = load_config(args.config)
    lines = (
        sys.stdin
        if args.input is None
        else args.input.open("r", encoding="utf-8")
    )
    output = sys.stdout if args.output is None else args.output.open("x", encoding="utf-8")
    try:
        sequence = 0
        for line in lines:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            sequence += 1
            sensor_record = convert_pose_record(
                record,
                config=config,
                event_id=f"{args.run_id}-{sequence:x}",
                connection_epoch=args.connection_epoch,
            )
            if sensor_record is None:
                continue
            output.write(json.dumps(sensor_record, sort_keys=True, separators=(",", ":")) + "\n")
            output.flush()
    finally:
        if args.input is not None:
            lines.close()
        if args.output is not None:
            output.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
