"""Turn pinned Android evidence and decoded frames into verified localization input."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import time
from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

import cv2
import numpy as np

from perception.control_localization import BodyExtrinsics
from perception.control_publisher import (
    ControlPublisher,
    ControlPublisherConfig,
    WebSocketPublisherTransport,
)
from perception.sensor_records import SensorRecordAdapter
from perception.tag_localization import TagLocalizer

_ANDROID_CLOCK = "android_elapsed_realtime"
_RAW_IDENTITY_FIELDS = (
    "recording_run_id",
    "session",
    "product_id",
    "drone_id",
    "connection_generation",
    "connection_epoch",
    "product_type",
    "aircraft_firmware",
    "rc_firmware",
    "sdk_version",
    "recorder_config_sha256",
)


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip() or not value.isprintable():
        raise ValueError(f"{name} must be canonical text")
    return value


def _integer(value: object, name: str, *, positive: bool = False) -> int:
    if type(value) is not int or value < 0 or (positive and value == 0):
        raise ValueError(f"{name} must be a {'positive' if positive else 'nonnegative'} integer")
    return value


def _number(value: object, name: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    result = float(value)
    if result < 0 or (positive and result == 0):
        raise ValueError(f"{name} must be {'positive' if positive else 'nonnegative'}")
    return result


def _sha256(value: object, name: str) -> str:
    value = _text(value, name)
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{name} must be lowercase SHA-256 hex")
    return value


def _matrix(value: object, name: str, size: int) -> tuple[tuple[float, ...], ...]:
    array = np.asarray(value, dtype=float)
    if array.shape != (size, size) or not np.isfinite(array).all():
        raise ValueError(f"{name} must be a finite {size}x{size} matrix")
    return tuple(tuple(float(item) for item in row) for row in array)


def _rotation(value: object, name: str) -> tuple[tuple[float, ...], ...]:
    matrix = np.asarray(_matrix(value, name, 3))
    if not np.allclose(matrix.T @ matrix, np.eye(3), atol=1e-6) or not np.isclose(
        np.linalg.det(matrix), 1, atol=1e-6
    ):
        raise ValueError(f"{name} must be a proper rotation")
    return tuple(tuple(float(item) for item in row) for row in matrix)


def _rigid(value: object, name: str) -> tuple[tuple[float, ...], ...]:
    matrix = np.asarray(_matrix(value, name, 4))
    if not np.allclose(matrix[3], [0, 0, 0, 1]):
        raise ValueError(f"{name} must be a rigid transform")
    _rotation(matrix[:3, :3], name)
    return tuple(tuple(float(item) for item in row) for row in matrix)


def _covariance(value: object, name: str) -> tuple[tuple[float, ...], ...]:
    matrix = np.asarray(_matrix(value, name, 3))
    if not np.allclose(matrix, matrix.T) or np.linalg.eigvalsh(matrix).min() <= 0:
        raise ValueError(f"{name} must be positive definite")
    return tuple(tuple(float(item) for item in row) for row in matrix)


def _measured(raw: object, name: str) -> str:
    if not isinstance(raw, Mapping) or set(raw) != {
        "measurement_id",
        "measured",
        "artifact_sha256",
    }:
        raise ValueError(f"{name} must name a measured artifact")
    if raw["measured"] is not True:
        raise ValueError(f"{name} must be measured")
    _sha256(raw["artifact_sha256"], f"{name} artifact_sha256")
    return _text(raw["measurement_id"], f"{name} measurement_id")


@dataclass(frozen=True, slots=True)
class _Timing:
    measurement_id: str
    boot_id: str
    receipt_to_capture_s: float
    max_error_s: float

    @classmethod
    def from_mapping(cls, raw: object, name: str) -> _Timing:
        expected = {
            "measurement",
            "capture_clock_id",
            "boot_id",
            "receipt_to_capture_s",
            "max_error_s",
        }
        if not isinstance(raw, Mapping) or set(raw) != expected:
            raise ValueError(f"{name} timing fields do not match the contract")
        if raw["capture_clock_id"] != _ANDROID_CLOCK:
            raise ValueError(f"{name} timing clock is unsupported")
        return cls(
            _measured(raw["measurement"], f"{name} timing"),
            _text(raw["boot_id"], f"{name} timing boot_id"),
            _number(raw["receipt_to_capture_s"], f"{name} receipt_to_capture_s"),
            _number(raw["max_error_s"], f"{name} max_error_s", positive=True),
        )

    def capture_time(self, receipt_ms: object) -> float:
        return _integer(receipt_ms, "Android receipt time") / 1000 - self.receipt_to_capture_s


@dataclass(frozen=True, slots=True)
class _Attitude:
    timestamp: float
    rotation: tuple[tuple[float, ...], ...]


class VerifiedLocalizationIngestion:
    """Fail-closed raw-input adapter for a single pinned Android recording configuration."""

    def __init__(self, raw: object) -> None:
        if not isinstance(raw, Mapping) or set(raw) != {
            "publisher",
            "sensor",
            "localizer",
            "identity",
            "timing",
            "camera",
        }:
            raise ValueError("verified ingestion configuration fields do not match the contract")
        self.publisher_config = ControlPublisherConfig.from_mapping(raw["publisher"])
        self.sensor = SensorRecordAdapter(raw["sensor"])
        if self.sensor.publisher != self.publisher_config:
            raise ValueError("sensor and verified publisher configurations differ")
        if self.publisher_config.mode not in {"live", "replay"}:
            raise ValueError("publisher mode is unsupported")
        if not isinstance(raw["identity"], Mapping) or set(raw["identity"]) != set(
            _RAW_IDENTITY_FIELDS
        ) | {"pipeline_sha256", "camera_configuration_id"}:
            raise ValueError("run identity fields do not match the contract")
        self.identity = dict(raw["identity"])
        for name in _RAW_IDENTITY_FIELDS:
            if name in {"product_id", "drone_id", "connection_generation", "connection_epoch"}:
                self.identity[name] = _integer(
                    self.identity[name], name, positive=name != "product_id"
                )
            elif name == "recorder_config_sha256":
                self.identity[name] = _sha256(self.identity[name], name)
            else:
                self.identity[name] = _text(self.identity[name], name)
        self.pipeline_sha256 = _sha256(self.identity["pipeline_sha256"], "pipeline_sha256")
        self.camera_configuration_id = _text(
            self.identity["camera_configuration_id"], "camera_configuration_id"
        )
        if self.identity["drone_id"] != self.sensor.phone_drone_id:
            raise ValueError("run identity drone is not configured")
        fuser = self.publisher_config.drones[self.sensor.phone_drone_id].fuser
        if (
            self.publisher_config.mode == "replay"
            and self.identity["connection_epoch"] != fuser.connection_epoch
        ):
            raise ValueError("run identity epoch is not configured")
        if self.publisher_config.mode == "live" and fuser.connection_epoch != 0:
            raise ValueError("live fuser must begin without a bound epoch")
        if not fuser.production_evidence_verified:
            raise ValueError("verified ingestion requires a production-evidence fuser")
        if not isinstance(raw["timing"], Mapping) or set(raw["timing"]) != {
            "frame",
            "attitude",
            "telemetry",
        }:
            raise ValueError("timing fields do not match the contract")
        self.frame_timing = _Timing.from_mapping(raw["timing"]["frame"], "frame")
        self.attitude_timing = _Timing.from_mapping(raw["timing"]["attitude"], "attitude")
        self.telemetry_timing = _Timing.from_mapping(raw["timing"]["telemetry"], "telemetry")
        if (
            len(
                {
                    self.frame_timing.boot_id,
                    self.attitude_timing.boot_id,
                    self.telemetry_timing.boot_id,
                }
            )
            != 1
        ):
            raise ValueError("timing artifacts belong to different Android boots")
        if not isinstance(raw["camera"], Mapping) or set(raw["camera"]) != {
            "source_id",
            "camera_serial",
            "camera_calibration_id",
            "calibration_sha256",
            "pipeline_sha256",
            "body_extrinsics_id",
            "body_gimbal_mount",
            "gimbal_camera",
            "map_ned_rotation",
            "max_attitude_age_s",
            "max_attitude_skew_s",
            "max_body_orientation_error_deg",
            "position_covariance_map_enu_m2",
        }:
            raise ValueError("camera evidence fields do not match the contract")
        camera = raw["camera"]
        self.tag_source_id = _text(camera["source_id"], "camera source_id")
        self.camera_serial = _text(camera["camera_serial"], "camera_serial")
        self.calibration_id = _text(camera["camera_calibration_id"], "camera_calibration_id")
        self.body_extrinsics_id = _text(camera["body_extrinsics_id"], "body_extrinsics_id")
        if (
            self.tag_source_id != fuser.tag_source_id
            or self.calibration_id != fuser.camera_calibration_id
            or self.body_extrinsics_id != fuser.body_extrinsics_id
            or self.camera_serial != raw["localizer"].get("camera_serial")
            or _sha256(camera["calibration_sha256"], "camera calibration_sha256")
            != raw["localizer"].get("calibration_sha256")
            or _sha256(camera["pipeline_sha256"], "camera pipeline_sha256") != self.pipeline_sha256
        ):
            raise ValueError("camera evidence is not pinned to the configured localizer")
        pipeline_digest = hashlib.sha256(
            json.dumps(
                raw["localizer"].get("pipeline"), sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        if pipeline_digest != self.pipeline_sha256:
            raise ValueError("pipeline evidence does not match the configured localizer")
        self.localizer_config = dict(raw["localizer"])
        self.localizer = TagLocalizer(**self.localizer_config)
        if self.localizer.evidence_kind != "recorded_live":
            raise ValueError("verified ingestion rejects synthetic camera calibration evidence")
        self.body_gimbal_mount = np.asarray(
            self._measured_transform(camera["body_gimbal_mount"], "body_gimbal_mount")
        )
        self.gimbal_camera = np.asarray(
            self._measured_transform(camera["gimbal_camera"], "gimbal_camera")
        )
        self.map_ned_rotation = np.asarray(
            self._measured_rotation(camera["map_ned_rotation"], "map_ned_rotation")
        )
        self.max_attitude_age_s = _number(
            camera["max_attitude_age_s"], "max_attitude_age_s", positive=True
        )
        self.max_attitude_skew_s = _number(
            camera["max_attitude_skew_s"], "max_attitude_skew_s", positive=True
        )
        orientation_error = _number(
            camera["max_body_orientation_error_deg"],
            "max_body_orientation_error_deg",
            positive=True,
        )
        if orientation_error > 180:
            raise ValueError("max_body_orientation_error_deg is outside its bounded range")
        self.max_body_orientation_error_deg = orientation_error
        covariance = camera["position_covariance_map_enu_m2"]
        if not isinstance(covariance, Mapping) or set(covariance) != {"measurement", "matrix"}:
            raise ValueError("position covariance fields do not match the contract")
        _measured(covariance["measurement"], "position covariance")
        self.position_covariance = _covariance(covariance["matrix"], "position covariance")
        self._body: deque[_Attitude] = deque(maxlen=128)
        self._gimbal: deque[_Attitude] = deque(maxlen=128)
        self._live_epoch: int | None = None

    @staticmethod
    def _measured_transform(raw: object, name: str) -> tuple[tuple[float, ...], ...]:
        if not isinstance(raw, Mapping) or set(raw) != {"measurement", "matrix"}:
            raise ValueError(f"{name} fields do not match the contract")
        _measured(raw["measurement"], name)
        return _rigid(raw["matrix"], name)

    @staticmethod
    def _measured_rotation(raw: object, name: str) -> tuple[tuple[float, ...], ...]:
        if not isinstance(raw, Mapping) or set(raw) != {"measurement", "matrix"}:
            raise ValueError(f"{name} fields do not match the contract")
        _measured(raw["measurement"], name)
        return _rotation(raw["matrix"], name)

    def bind_live_epoch(self, epoch: object) -> None:
        if self.publisher_config.mode != "live":
            raise ValueError("replay ingestion has no live epoch")
        bound = _integer(epoch, "live connection epoch", positive=True)
        if bound != self.identity["connection_epoch"]:
            raise ValueError("authenticated live epoch does not match raw evidence")
        self._live_epoch = bound

    def records(self, raw: object) -> list[dict[str, object]]:
        if not isinstance(raw, Mapping):
            raise ValueError("raw input must be an object")
        self._identity(raw)
        if self.publisher_config.mode == "live" and self._live_epoch is None:
            raise ValueError("live ingestion has no authenticated epoch")
        kind = raw.get("kind")
        if kind in {"phone_velocity_raw", "phone_height_raw"}:
            record = self.sensor.record_if_selected(raw)
            if record is None:
                return []
            return [
                record
                | {
                    "capture_time": self.telemetry_timing.capture_time(
                        raw["received_at_android_elapsed_realtime_ms"]
                    ),
                    "source_verified": True,
                    "timing_verified": True,
                }
            ]
        if kind == "phone_attitude_raw":
            self.sensor.record_if_selected(raw)
            self._store_attitude(raw)
            return []
        if kind == "decoded_frame":
            return self._frame(raw)
        raise ValueError("raw input kind is unsupported")

    def _identity(self, raw: Mapping[str, object]) -> None:
        for name in _RAW_IDENTITY_FIELDS:
            if raw.get(name) != self.identity[name]:
                raise ValueError(f"raw input {name} is not pinned")

    def _store_attitude(self, raw: Mapping[str, object]) -> None:
        timestamp = self.attitude_timing.capture_time(
            raw["received_at_android_elapsed_realtime_ms"]
        )
        rotation = _rpy(raw["yaw_deg"], raw["pitch_deg"], raw["roll_deg"])
        sample = _Attitude(
            timestamp, tuple(tuple(float(value) for value in row) for row in rotation)
        )
        target = self._body if raw["sdk_key"] == "KeyAircraftAttitude" else self._gimbal
        target.append(sample)

    def _frame(self, raw: Mapping[str, object]) -> list[dict[str, object]]:
        expected = set(_RAW_IDENTITY_FIELDS) | {
            "kind",
            "event_id",
            "received_at_android_elapsed_realtime_ms",
            "decoded_at_android_elapsed_realtime_ms",
            "frame_path",
            "frame_sha256",
            "camera_serial",
            "camera_configuration_id",
            "pipeline_sha256",
        }
        if set(raw) != expected:
            raise ValueError("decoded frame fields do not match the contract")
        if (
            raw["camera_serial"] != self.camera_serial
            or raw["camera_configuration_id"] != self.camera_configuration_id
        ):
            raise ValueError("decoded frame camera configuration is not pinned")
        if _sha256(raw["pipeline_sha256"], "frame pipeline_sha256") != self.pipeline_sha256:
            raise ValueError("decoded frame pipeline is not pinned")
        received = _integer(raw["received_at_android_elapsed_realtime_ms"], "frame receipt time")
        decoded = _integer(raw["decoded_at_android_elapsed_realtime_ms"], "frame decode time")
        if decoded < received:
            raise ValueError("decoded frame precedes receipt")
        capture = self.frame_timing.capture_time(received)
        body = self._nearest(self._body, capture, "aircraft attitude")
        gimbal = self._nearest(self._gimbal, capture, "gimbal attitude")
        if (
            abs(body.timestamp - gimbal.timestamp) + 2 * self.attitude_timing.max_error_s
            > self.max_attitude_skew_s
        ):
            raise ValueError("body and gimbal transforms are asynchronous")
        dynamic = self.body_gimbal_mount @ _homogeneous(gimbal.rotation) @ self.gimbal_camera
        path = Path(_text(raw["frame_path"], "frame_path"))
        payload = path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != _sha256(raw["frame_sha256"], "frame_sha256"):
            raise ValueError("decoded frame hash does not match")
        image = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
        if image is None:
            raise ValueError("decoded frame cannot be read")
        decode_time = decoded / 1000
        result = self.localizer.estimate_with_body_camera(
            image, capture, decode_time, decode_time, dynamic
        )
        if not result["accepted"]:
            return []
        observed = np.asarray(result["T_map_body"])[:3, :3]
        expected_rotation = self.map_ned_rotation @ np.asarray(body.rotation)
        if _angle_deg(observed, expected_rotation) > self.max_body_orientation_error_deg:
            raise ValueError("tag body orientation contradicts measured aircraft attitude")
        extrinsics = BodyExtrinsics(
            self.body_extrinsics_id,
            self.tag_source_id,
            tuple(tuple(float(item) for item in row) for row in dynamic),
            capture,
            capture,
            capture,
            True,
        )
        return [
            {
                "kind": "tag",
                "event_id": _text(raw["event_id"], "frame event_id"),
                "drone_id": self.identity["drone_id"],
                "connection_epoch": self.identity["connection_epoch"],
                "map_id": self.publisher_config.drones[self.identity["drone_id"]].fuser.map_id,
                "geometry_id": self.publisher_config.drones[
                    self.identity["drone_id"]
                ].fuser.geometry_id,
                "clock_id": _ANDROID_CLOCK,
                "capture_time": capture,
                "position_map_enu_m": [
                    float(item) for item in np.asarray(result["T_map_body"])[:3, 3]
                ],
                "covariance_map_enu_m2": [list(row) for row in self.position_covariance],
                "source_id": self.tag_source_id,
                "camera_calibration_id": self.calibration_id,
                "source_verified": True,
                "timing_verified": True,
                "extrinsics": {
                    "extrinsics_id": extrinsics.extrinsics_id,
                    "source_id": extrinsics.source_id,
                    "matrix": [list(row) for row in extrinsics.matrix],
                    "capture_time": capture,
                    "gimbal_time": capture,
                    "attitude_time": capture,
                    "measured": True,
                },
            }
        ]

    def _nearest(self, samples: deque[_Attitude], capture: float, name: str) -> _Attitude:
        if not samples:
            raise ValueError(f"{name} is missing")
        sample = min(samples, key=lambda item: abs(item.timestamp - capture))
        if (
            abs(sample.timestamp - capture)
            + self.frame_timing.max_error_s
            + self.attitude_timing.max_error_s
            > self.max_attitude_age_s
        ):
            raise ValueError(f"{name} is stale at frame capture")
        return sample


def _rpy(yaw: object, pitch: object, roll: object) -> np.ndarray:
    values = [float(value) for value in (yaw, pitch, roll)]
    if not np.isfinite(values).all():
        raise ValueError("attitude angles must be finite")
    yaw_r, pitch_r, roll_r = np.deg2rad(values)
    cz, sz, cy, sy, cx, sx = (
        math.cos(yaw_r),
        math.sin(yaw_r),
        math.cos(pitch_r),
        math.sin(pitch_r),
        math.cos(roll_r),
        math.sin(roll_r),
    )
    return np.array(
        [
            [cz * cy, cz * sy * sx - sz * cx, cz * sy * cx + sz * sx],
            [sz * cy, sz * sy * sx + cz * cx, sz * sy * cx - cz * sx],
            [-sy, cy * sx, cy * cx],
        ]
    )


def _homogeneous(rotation: tuple[tuple[float, ...], ...]) -> np.ndarray:
    result = np.eye(4)
    result[:3, :3] = np.asarray(rotation)
    return result


def _angle_deg(left: np.ndarray, right: np.ndarray) -> float:
    relative = left @ right.T
    return math.degrees(math.acos(float(np.clip((np.trace(relative) - 1) / 2, -1, 1))))


def run_replay(
    ingestion: VerifiedLocalizationIngestion, lines: Iterable[str], output: TextIO
) -> None:
    publisher = ControlPublisher(ingestion.publisher_config)
    try:
        publisher.bind_credentials()
        for line in lines:
            envelope = json.loads(line)
            if not isinstance(envelope, Mapping) or set(envelope) != {"now_s", "raw"}:
                raise ValueError("replay input requires exactly now_s and raw")
            now_s = _number(envelope["now_s"], "now_s")
            for record in ingestion.records(envelope["raw"]):
                publisher.enqueue(record)
                frame = publisher.publish(record["drone_id"], now_s)
                output.write(json.dumps(frame, sort_keys=True, separators=(",", ":")) + "\n")
    finally:
        publisher.close()


def run_live(ingestion: VerifiedLocalizationIngestion, lines: Iterable[str]) -> None:
    if ingestion.publisher_config.mode != "live":
        raise ValueError("live input requires a live publisher")
    transport = WebSocketPublisherTransport(ingestion.publisher_config.websocket_url)  # type: ignore[arg-type]
    publisher = ControlPublisher(ingestion.publisher_config, transport)
    try:
        publisher.bind_credentials()
        ingestion.bind_live_epoch(publisher.bound_epoch(ingestion.identity["drone_id"]))
        for line in lines:
            raw = json.loads(line)
            for record in ingestion.records(raw):
                publisher.enqueue(record)
                publisher.publish_live(record["drone_id"], time.monotonic())
    finally:
        publisher.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--replay-output", type=Path)
    args = parser.parse_args()
    ingestion = VerifiedLocalizationIngestion(json.loads(args.config.read_text(encoding="utf-8")))
    if ingestion.publisher_config.mode == "replay" and args.replay_output is None:
        raise SystemExit("replay publisher requires --replay-output")
    if ingestion.publisher_config.mode == "live" and args.replay_output is not None:
        raise SystemExit("live publisher cannot write replay output")
    source = sys.stdin if args.input is None else args.input.open(encoding="utf-8")
    try:
        if ingestion.publisher_config.mode == "replay":
            assert args.replay_output is not None
            with args.replay_output.open("x", encoding="utf-8") as output:
                run_replay(ingestion, source, output)
        else:
            run_live(ingestion, source)
    finally:
        if source is not sys.stdin:
            source.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
