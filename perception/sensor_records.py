"""Turn pinned phone and AprilTag samples into ControlPublisher JSONL records."""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

import cv2
import numpy as np

from perception.control_publisher import ControlPublisherConfig
from perception.tag_localization import TagLocalizer
from perception.webcam_stream import WebcamStream


def _finite(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    return float(value)


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be nonempty text")
    return value


def _identity_matrix(value: object, name: str) -> tuple[tuple[float, ...], ...]:
    matrix = np.asarray(value, dtype=float)
    if (
        matrix.shape != (4, 4)
        or not np.isfinite(matrix).all()
        or not np.allclose(matrix[3], [0, 0, 0, 1])
        or not np.allclose(matrix[:3, :3].T @ matrix[:3, :3], np.eye(3), atol=1e-6)
        or np.linalg.det(matrix[:3, :3]) <= 0
    ):
        raise ValueError(f"{name} must be a rigid transform")
    return tuple(tuple(float(item) for item in row) for row in matrix)


def _rotation(value: object, name: str) -> tuple[tuple[float, ...], ...]:
    matrix = np.asarray(value, dtype=float)
    if (
        matrix.shape != (3, 3)
        or not np.isfinite(matrix).all()
        or not np.allclose(matrix.T @ matrix, np.eye(3), atol=1e-6)
        or np.linalg.det(matrix) <= 0
    ):
        raise ValueError(f"{name} must be a proper 3x3 rotation")
    return tuple(tuple(float(item) for item in row) for row in matrix)


def _covariance(value: object, name: str) -> tuple[tuple[float, ...], ...]:
    matrix = np.asarray(value, dtype=float)
    if (
        matrix.shape != (3, 3)
        or not np.isfinite(matrix).all()
        or not np.allclose(matrix, matrix.T)
        or np.linalg.eigvalsh(matrix).min() <= 0
    ):
        raise ValueError(f"{name} must be a positive definite 3x3 covariance")
    return tuple(tuple(float(item) for item in row) for row in matrix)


@dataclass(frozen=True, slots=True)
class PhoneSource:
    source_id: str
    sdk_key: str
    source_verified: bool
    max_sample_age_s: float
    covariance: tuple[tuple[float, ...], ...] | None = None
    variance_m2: float | None = None

    @classmethod
    def from_mapping(cls, raw: object, kind: str) -> PhoneSource:
        if not isinstance(raw, Mapping):
            raise ValueError(f"{kind} source configuration must be an object")
        common = {"source_id", "sdk_key", "source_verified", "max_sample_age_s"}
        expected = common | ({"covariance_m2ps2"} if kind == "velocity" else {"variance_m2"})
        if set(raw) != expected:
            raise ValueError(f"{kind} source configuration has unsupported fields")
        source_verified = raw["source_verified"]
        if not isinstance(source_verified, bool):
            raise ValueError(f"{kind} source_verified must be a boolean")
        age = _finite(raw["max_sample_age_s"], "max_sample_age_s")
        if age <= 0:
            raise ValueError("max_sample_age_s must be positive")
        if kind == "velocity":
            return cls(
                _text(raw["source_id"], "source_id"),
                _text(raw["sdk_key"], "sdk_key"),
                source_verified,
                age,
                _covariance(raw["covariance_m2ps2"], "covariance_m2ps2"),
            )
        variance = _finite(raw["variance_m2"], "variance_m2")
        if variance <= 0:
            raise ValueError("variance_m2 must be positive")
        return cls(
            _text(raw["source_id"], "source_id"),
            _text(raw["sdk_key"], "sdk_key"),
            source_verified,
            age,
            variance_m2=variance,
        )


@dataclass(frozen=True, slots=True)
class TagSource:
    source_id: str
    source_verified: bool
    timing_evidence_verified: bool
    max_frame_age_s: float
    covariance_map_enu_m2: tuple[tuple[float, ...], ...]
    extrinsics_id: str
    require_measured_extrinsics: bool
    localizer: TagLocalizer

    @classmethod
    def from_mapping(cls, raw: object) -> TagSource:
        if not isinstance(raw, Mapping):
            raise ValueError("tag source configuration must be an object")
        expected = {
            "source_id",
            "source_verified",
            "timing_evidence_verified",
            "max_frame_age_s",
            "covariance_map_enu_m2",
            "body_extrinsics",
            "localizer",
        }
        if set(raw) != expected or not isinstance(raw["body_extrinsics"], Mapping):
            raise ValueError("tag source configuration has unsupported fields")
        body = raw["body_extrinsics"]
        if set(body) != {"extrinsics_id", "require_measured"}:
            raise ValueError("tag body extrinsics have unsupported fields")
        if body["require_measured"] is not True:
            raise ValueError("tag body extrinsics must require measured samples")
        source_verified = raw["source_verified"]
        timing_verified = raw["timing_evidence_verified"]
        if not isinstance(source_verified, bool) or not isinstance(timing_verified, bool):
            raise ValueError("tag verification fields must be booleans")
        max_age = _finite(raw["max_frame_age_s"], "max_frame_age_s")
        if max_age <= 0:
            raise ValueError("max_frame_age_s must be positive")
        if not isinstance(raw["localizer"], Mapping):
            raise ValueError("tag localizer configuration must be an object")
        localizer = TagLocalizer(**dict(raw["localizer"]))
        return cls(
            _text(raw["source_id"], "source_id"),
            source_verified,
            timing_verified,
            max_age,
            _covariance(raw["covariance_map_enu_m2"], "covariance_map_enu_m2"),
            _text(body["extrinsics_id"], "extrinsics_id"),
            body["require_measured"],
            localizer,
        )


class SensorRecordAdapter:
    """Creates only records whose identity and metadata agree with the publisher configuration."""

    def __init__(self, raw: object) -> None:
        if not isinstance(raw, Mapping) or set(raw) != {"publisher", "phone", "tag"}:
            raise ValueError(
                "sensor recording configuration must contain publisher, phone, and tag"
            )
        if not isinstance(raw["publisher"], Mapping) or not isinstance(raw["phone"], Mapping):
            raise ValueError("sensor recording configuration has invalid sections")
        phone = raw["phone"]
        if set(phone) != {
            "drone_id",
            "velocity",
            "height",
            "velocity_ned_to_map_rotation",
            "height_datum_m",
        }:
            raise ValueError("phone configuration has unsupported fields")
        self.publisher = ControlPublisherConfig.from_mapping(raw["publisher"])
        self.phone_drone_id = phone["drone_id"]
        if type(self.phone_drone_id) is not int or self.phone_drone_id not in self.publisher.drones:
            raise ValueError("phone drone_id is not configured")
        self.velocity_rotation = _rotation(
            phone["velocity_ned_to_map_rotation"], "velocity rotation"
        )
        self.height_datum_m = _finite(phone["height_datum_m"], "height_datum_m")
        self.velocity = PhoneSource.from_mapping(phone["velocity"], "velocity")
        self.height = PhoneSource.from_mapping(phone["height"], "height")
        self.tag = TagSource.from_mapping(raw["tag"])
        for drone in self.publisher.drones.values():
            fuser = drone.fuser
            if (
                fuser.tag_source_id != self.tag.source_id
                or fuser.velocity_source_id != self.velocity.source_id
                or fuser.height_source_id != self.height.source_id
                or fuser.camera_calibration_id != self.tag.localizer.calibration_sha256
                or fuser.body_extrinsics_id != self.tag.extrinsics_id
                or fuser.map_id != self.tag.localizer.manifest["content_sha256"]
            ):
                raise ValueError("recording source identities do not match publisher configuration")

    def record(self, raw: object) -> dict[str, object]:
        if not isinstance(raw, Mapping):
            raise ValueError("raw sensor sample must be an object")
        kind = raw.get("kind")
        if kind == "phone_velocity_raw":
            return self._phone_record(raw, "velocity")
        if kind == "phone_height_raw":
            return self._phone_record(raw, "height")
        if kind == "tag_frame":
            return self._tag_record(raw)
        raise ValueError("raw sensor sample kind is unsupported")

    def record_if_selected(self, raw: object) -> dict[str, object] | None:
        if self._known_unselected_phone_sample(raw):
            return None
        return self.record(raw)

    def _known_unselected_phone_sample(self, raw: object) -> bool:
        if not isinstance(raw, Mapping):
            return False
        kind = raw.get("kind")
        key = raw.get("sdk_key")
        expected = {
            "phone_velocity_raw": ("KeyAircraftVelocity",),
            "phone_height_raw": ("KeyAltitude", "KeyUltrasonicHeight"),
        }.get(kind)
        configured = self.velocity.sdk_key if kind == "phone_velocity_raw" else self.height.sdk_key
        return expected is not None and key in expected and key != configured

    def _drone(self, raw: Mapping[str, object]) -> tuple[int, object]:
        drone_id = raw.get("drone_id")
        if isinstance(drone_id, bool) or not isinstance(drone_id, int):
            raise ValueError("drone_id must be an integer")
        config = self.publisher.drones.get(drone_id)
        if config is None:
            raise ValueError("raw sensor sample drone is not configured")
        return drone_id, config

    def _tag_times(self, raw: Mapping[str, object]) -> tuple[float, float, bool, str]:
        received = _finite(raw.get("received_at_s"), "received_at_s")
        capture_raw = raw.get("sdk_capture_time_s")
        if capture_raw is None:
            capture = received
            verified, provenance = False, "receipt_timestamp"
        else:
            capture = _finite(capture_raw, "sdk_capture_time_s")
            verified = self.tag.timing_evidence_verified
            provenance = "sdk_capture_timestamp"
        max_age = self.tag.max_frame_age_s
        if capture > received or received - capture > max_age:
            raise ValueError("sensor sample is stale or from the future")
        return capture, received, verified, provenance

    def _phone_record(self, raw: Mapping[str, object], kind: str) -> dict[str, object]:
        value_key = "velocity_ned_mps" if kind == "velocity" else "height_m"
        expected = {"kind", "event_id", "received_at_monotonic_ms", "sdk_key", value_key}
        if set(raw) != expected:
            raise ValueError("phone sample has unsupported fields")
        drone_id = self.phone_drone_id
        config = self.publisher.drones[drone_id]
        source = self.velocity if kind == "velocity" else self.height
        if _text(raw["sdk_key"], "sdk_key") != source.sdk_key:
            raise ValueError("phone sample SDK key does not match configured source")
        received_ms = _finite(raw["received_at_monotonic_ms"], "received_at_monotonic_ms")
        capture = received_ms / 1000
        now = capture
        timing_verified = False
        provenance = "android_callback_receipt"
        common: dict[str, object] = {
            "kind": kind,
            "event_id": _text(raw.get("event_id"), "event_id"),
            "drone_id": drone_id,
            "connection_epoch": config.fuser.connection_epoch,
            "map_id": config.fuser.map_id,
            "geometry_id": config.fuser.geometry_id,
            "clock_id": "android_elapsed_realtime",
            "capture_time": capture,
            "source_id": source.source_id,
            "source_verified": source.source_verified,
            "timing_verified": timing_verified,
            "timing_provenance": provenance,
            "now_s": now,
        }
        if kind == "velocity":
            vector = np.asarray(raw[value_key], dtype=float)
            if vector.shape != (3,) or not np.isfinite(vector).all():
                raise ValueError("velocity_ned_mps must contain three finite values")
            assert source.covariance is not None
            return common | {
                "velocity_map_enu_mps": [
                    float(item) for item in np.asarray(self.velocity_rotation) @ vector
                ],
                "covariance_m2ps2": [list(row) for row in source.covariance],
            }
        assert source.variance_m2 is not None
        return common | {
            "height_map_enu_m": _finite(raw[value_key], value_key) + self.height_datum_m,
            "variance_m2": source.variance_m2,
        }

    def _tag_record(self, raw: Mapping[str, object]) -> dict[str, object]:
        expected = {"kind", "event_id", "drone_id", "image_path", "received_at_s"}
        optional = {"sdk_capture_time_s", "decode_time_s", "body_extrinsics"}
        if not set(raw).issubset(expected | optional) or not expected.issubset(raw):
            raise ValueError("tag frame has unsupported fields")
        drone_id, config = self._drone(raw)
        capture, now, timing_verified, provenance = self._tag_times(raw)
        decode = _finite(raw.get("decode_time_s", now), "decode_time_s")
        if not capture <= decode <= now:
            raise ValueError("tag frame timing is invalid")
        image_path = Path(_text(raw.get("image_path"), "image_path"))
        image = cv2.imread(str(image_path), cv2.IMREAD_UNCHANGED)
        if image is None:
            raise ValueError("tag frame image cannot be decoded")
        result = self.tag.localizer.estimate(image, capture, decode, now, self.tag.max_frame_age_s)
        if result.get("accepted") is not True:
            raise ValueError(f"tag frame was rejected: {result.get('reason', 'invalid_pose')}")
        return self._tag_output(
            drone_id,
            _text(raw.get("event_id"), "event_id"),
            result,
            capture,
            now,
            timing_verified,
            provenance,
            self._frame_extrinsics(raw.get("body_extrinsics"), capture),
        )

    def _frame_extrinsics(
        self, raw: object, capture: float
    ) -> tuple[dict[str, object] | None, bool]:
        if raw is None:
            return None, False
        basic = {"extrinsics_id", "source_id", "matrix", "measured"}
        timing = {"gimbal_time_s", "attitude_time_s"}
        if (
            not isinstance(raw, Mapping)
            or not basic.issubset(raw)
            or not set(raw).issubset(basic | timing)
        ):
            raise ValueError("frame body extrinsics have unsupported fields")
        if (
            raw["extrinsics_id"] != self.tag.extrinsics_id
            or raw["source_id"] != self.tag.source_id
            or not isinstance(raw["measured"], bool)
        ):
            raise ValueError("frame body extrinsics do not match configured identity")
        if not timing.issubset(raw):
            return None, False
        gimbal = _finite(raw["gimbal_time_s"], "gimbal_time_s")
        attitude = _finite(raw["attitude_time_s"], "attitude_time_s")
        measured = raw["measured"] is True
        return (
            {
                "extrinsics_id": self.tag.extrinsics_id,
                "source_id": self.tag.source_id,
                "matrix": [list(row) for row in _identity_matrix(raw["matrix"], "body extrinsics")],
                "capture_time": capture,
                "gimbal_time": gimbal,
                "attitude_time": attitude,
                "measured": measured,
            },
            measured and gimbal == capture and attitude == capture,
        )

    def _tag_output(
        self,
        drone_id: int,
        event_id: str,
        result: Mapping[str, object],
        capture: float,
        now: float,
        capture_timing_verified: bool,
        timing_provenance: str,
        frame_extrinsics: tuple[dict[str, object] | None, bool],
    ) -> dict[str, object]:
        body = np.asarray(result.get("T_map_body"), dtype=float)
        if body.shape != (4, 4) or not np.isfinite(body).all():
            raise ValueError("tag localizer returned an invalid body pose")
        config = self.publisher.drones[drone_id]
        extrinsics, synchronized = frame_extrinsics
        recorded_live = self.tag.localizer.evidence_kind == "recorded_live"
        return {
            "kind": "tag",
            "event_id": event_id,
            "drone_id": drone_id,
            "connection_epoch": config.fuser.connection_epoch,
            "map_id": config.fuser.map_id,
            "geometry_id": config.fuser.geometry_id,
            "clock_id": config.fuser.clock_id,
            "capture_time": capture,
            "position_map_enu_m": [float(item) for item in body[:3, 3]],
            "covariance_map_enu_m2": [list(row) for row in self.tag.covariance_map_enu_m2],
            "source_id": self.tag.source_id,
            "camera_calibration_id": self.tag.localizer.calibration_sha256,
            "source_verified": self.tag.source_verified and recorded_live,
            "timing_verified": capture_timing_verified and synchronized,
            "timing_provenance": timing_provenance,
            "tag_ids": result["tag_ids"],
            "calibration_evidence_kind": self.tag.localizer.evidence_kind,
            "extrinsics": extrinsics,
            "now_s": now,
        }

    def record_rtsp(
        self,
        url: str,
        *,
        frames: int,
        timeout_s: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> Iterable[dict[str, object]]:
        if frames < 1:
            raise ValueError("frames must be positive")
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("RTSP timeout must be positive")
        drone_ids = tuple(self.publisher.drones)
        if len(drone_ids) != 1:
            raise ValueError("RTSP recording requires exactly one configured drone")
        with WebcamStream(url) as stream:
            emitted = 0
            deadline = _finite(clock(), "monotonic_s") + timeout_s
            while emitted < frames:
                frame = stream.read_timed(0.1)
                if frame is None:
                    if _finite(clock(), "monotonic_s") >= deadline:
                        raise RuntimeError("RTSP source timed out before a usable frame arrived")
                    continue
                now = _finite(clock(), "monotonic_s")
                capture = (
                    frame.captured_at_s if frame.capture_time_verified else frame.received_at_s
                )
                result = self._tag_from_image(
                    drone_ids[0],
                    f"rtsp-tag-{emitted + 1}",
                    frame.image,
                    capture,
                    frame.received_at_s,
                    now,
                )
                yield result
                emitted += 1

    def _tag_from_image(
        self,
        drone_id: int,
        event_id: str,
        image: np.ndarray,
        capture: float,
        received: float,
        now: float,
    ) -> dict[str, object]:
        if capture > received or now < received or now - capture > self.tag.max_frame_age_s:
            raise ValueError("RTSP frame is stale or from the future")
        result = self.tag.localizer.estimate(
            image, capture, received, now, self.tag.max_frame_age_s
        )
        if result.get("accepted") is not True:
            raise ValueError(f"tag frame was rejected: {result.get('reason', 'invalid_pose')}")
        return self._tag_output(
            drone_id,
            event_id,
            result,
            capture,
            now,
            False,
            "receipt_timestamp",
            (None, False),
        )


def _write(records: Iterable[dict[str, object]], output: TextIO) -> None:
    for record in records:
        output.write(json.dumps(record, allow_nan=False) + "\n")
        output.flush()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--rtsp-url")
    parser.add_argument("--frames", type=int, default=1)
    parser.add_argument("--timeout-s", type=float, default=5.0)
    args = parser.parse_args()
    if args.input is not None and args.rtsp_url is not None:
        raise SystemExit("choose --input or --rtsp-url")
    adapter = SensorRecordAdapter(json.loads(args.config.read_text(encoding="utf-8")))
    output: TextIO
    if args.output is None:
        output = sys.stdout
        close_output = False
    else:
        output = args.output.open("x", encoding="utf-8")
        close_output = True
    try:
        if args.rtsp_url is not None:
            _write(
                adapter.record_rtsp(args.rtsp_url, frames=args.frames, timeout_s=args.timeout_s),
                output,
            )
        else:
            source = sys.stdin if args.input is None else args.input.open(encoding="utf-8")
            try:
                _write(
                    (
                        record
                        for line in source
                        if line.strip()
                        for record in [adapter.record_if_selected(json.loads(line))]
                        if record is not None
                    ),
                    output,
                )
            finally:
                if source is not sys.stdin:
                    source.close()
    finally:
        if close_output:
            output.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
