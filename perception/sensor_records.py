"""Convert Android raw-phone JSONL into deliberately unverified publisher records."""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

import numpy as np

from perception.control_publisher import ControlPublisherConfig

ANDROID_ELAPSED_REALTIME_CLOCK = "android_elapsed_realtime"
RAW_SCHEMA_VERSION = 3
_RAW_TIME_BASIS = "android_callback_receipt_elapsed_realtime_ms"
_RAW_TIMESTAMP_STATUS = "not_provided_by_msdk_key_listener"
_VELOCITY_KEY = "KeyAircraftVelocity"
_HEIGHT_KEYS = frozenset({"KeyAltitude", "KeyUltrasonicHeight"})


def _finite(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    return float(value)


def _nonnegative_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")
    return value


def _positive_integer(value: object, name: str) -> int:
    value = _nonnegative_integer(value, name)
    if value == 0:
        raise ValueError(f"{name} must be positive")
    return value


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{name} must be nonempty text")
    return value


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


def _sha256(value: object, name: str) -> str:
    text = _text(value, name)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise ValueError(f"{name} must be lowercase SHA-256 hex")
    return text


def _measured_id(value: object, name: str) -> str:
    if not isinstance(value, Mapping) or set(value) != {"measurement_id", "measured"}:
        raise ValueError(f"{name} must identify a measured value")
    if value["measured"] is not True:
        raise ValueError(f"{name} must be measured")
    return _text(value["measurement_id"], f"{name} measurement_id")


@dataclass(frozen=True, slots=True)
class VelocitySource:
    source_id: str
    sdk_key: str
    rotation: tuple[tuple[float, ...], ...]
    covariance: tuple[tuple[float, ...], ...]

    @classmethod
    def from_mapping(cls, raw: object) -> VelocitySource:
        if not isinstance(raw, Mapping):
            raise ValueError("velocity source configuration must be an object")
        expected = {"source_id", "sdk_key", "map_rotation", "covariance_map_enu_m2ps2"}
        if set(raw) != expected:
            raise ValueError("velocity source configuration has unsupported fields")
        rotation = raw["map_rotation"]
        covariance = raw["covariance_map_enu_m2ps2"]
        if not isinstance(rotation, Mapping) or set(rotation) != {"measurement", "matrix"}:
            raise ValueError("velocity map rotation has unsupported fields")
        if not isinstance(covariance, Mapping) or set(covariance) != {"measurement", "matrix"}:
            raise ValueError("velocity covariance has unsupported fields")
        _measured_id(rotation["measurement"], "velocity map rotation")
        _measured_id(covariance["measurement"], "velocity covariance")
        sdk_key = _text(raw["sdk_key"], "velocity sdk_key")
        if sdk_key != _VELOCITY_KEY:
            raise ValueError("velocity sdk_key must be KeyAircraftVelocity")
        return cls(
            _text(raw["source_id"], "velocity source_id"),
            sdk_key,
            _rotation(rotation["matrix"], "velocity map rotation"),
            _covariance(covariance["matrix"], "velocity covariance"),
        )


@dataclass(frozen=True, slots=True)
class HeightSource:
    source_id: str
    sdk_key: str
    datum_offset_m: float
    variance_m2: float

    @classmethod
    def from_mapping(cls, raw: object) -> HeightSource:
        if not isinstance(raw, Mapping):
            raise ValueError("height source configuration must be an object")
        expected = {"source_id", "sdk_key", "map_datum", "variance_m2"}
        if set(raw) != expected:
            raise ValueError("height source configuration has unsupported fields")
        datum = raw["map_datum"]
        variance = raw["variance_m2"]
        if not isinstance(datum, Mapping) or set(datum) != {"measurement", "offset_m"}:
            raise ValueError("height map datum has unsupported fields")
        if not isinstance(variance, Mapping) or set(variance) != {"measurement", "value"}:
            raise ValueError("height variance has unsupported fields")
        _measured_id(datum["measurement"], "height map datum")
        _measured_id(variance["measurement"], "height variance")
        sdk_key = _text(raw["sdk_key"], "height sdk_key")
        if sdk_key not in _HEIGHT_KEYS:
            raise ValueError("height sdk_key is unsupported")
        variance_m2 = _finite(variance["value"], "height variance")
        if variance_m2 <= 0:
            raise ValueError("height variance must be positive")
        return cls(
            _text(raw["source_id"], "height source_id"),
            sdk_key,
            _finite(datum["offset_m"], "height map datum offset_m"),
            variance_m2,
        )


class SensorRecordAdapter:
    """Converts pinned Android raw telemetry without asserting source or timing verification."""

    def __init__(self, raw: object) -> None:
        if not isinstance(raw, Mapping) or set(raw) != {"publisher", "phone"}:
            raise ValueError("sensor recording configuration must contain publisher and phone")
        if not isinstance(raw["publisher"], Mapping) or not isinstance(raw["phone"], Mapping):
            raise ValueError("sensor recording configuration has invalid sections")
        phone = raw["phone"]
        if set(phone) != {"drone_id", "recorder_config_sha256", "velocity", "height"}:
            raise ValueError("phone configuration has unsupported fields")
        self.publisher = ControlPublisherConfig.from_mapping(raw["publisher"])
        self.phone_drone_id = _nonnegative_integer(phone["drone_id"], "phone drone_id")
        self.recorder_config_sha256 = _sha256(
            phone["recorder_config_sha256"], "phone recorder_config_sha256"
        )
        if self.phone_drone_id not in self.publisher.drones:
            raise ValueError("phone drone_id is not configured")
        self.velocity = VelocitySource.from_mapping(phone["velocity"])
        self.height = HeightSource.from_mapping(phone["height"])
        for drone in self.publisher.drones.values():
            fuser = drone.fuser
            if (
                fuser.velocity_source_id != self.velocity.source_id
                or fuser.height_source_id != self.height.source_id
                or fuser.clock_id != ANDROID_ELAPSED_REALTIME_CLOCK
            ):
                raise ValueError("recording source identities do not match publisher configuration")

    def record(self, raw: object) -> dict[str, object]:
        if not isinstance(raw, Mapping):
            raise ValueError("raw sensor sample must be an object")
        kind = raw.get("kind")
        if kind == "phone_velocity_raw":
            return self._phone_record(raw, self.velocity)
        if kind == "phone_height_raw":
            return self._phone_record(raw, self.height)
        if kind == "phone_attitude_raw":
            self._attitude(raw)
            raise ValueError(
                "attitude records are retained as raw evidence and are not publisher input"
            )
        raise ValueError("raw sensor sample kind is unsupported")

    def record_if_selected(self, raw: object) -> dict[str, object] | None:
        if not isinstance(raw, Mapping):
            raise ValueError("raw sensor sample must be an object")
        if raw.get("kind") == "phone_attitude_raw":
            self._attitude(raw)
            return None
        if raw.get("kind") == "phone_height_raw" and raw.get("sdk_key") in _HEIGHT_KEYS:
            if raw["sdk_key"] != self.height.sdk_key:
                self._unselected_height(raw)
                return None
            return self._phone_record(raw, self.height)
        return self.record(raw)

    def _common(self, raw: Mapping[str, object], sample_fields: set[str]) -> int:
        expected = {
            "record_schema_version",
            "kind",
            "event_id",
            "recording_run_id",
            "run_sequence",
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
            "time_basis",
            "source_timestamp_status",
            "received_at_android_elapsed_realtime_ms",
            "written_at_android_elapsed_realtime_ms",
        } | sample_fields
        if set(raw) != expected:
            raise ValueError("raw sensor sample fields do not match Android schema v3")
        if raw["record_schema_version"] != RAW_SCHEMA_VERSION:
            raise ValueError("raw sensor sample schema version is unsupported")
        if raw["time_basis"] != _RAW_TIME_BASIS:
            raise ValueError("raw sensor sample time basis is unsupported")
        if raw["source_timestamp_status"] != _RAW_TIMESTAMP_STATUS:
            raise ValueError("raw sensor sample source timestamp status is unsupported")
        for name in (
            "event_id",
            "recording_run_id",
            "session",
            "product_type",
            "aircraft_firmware",
            "rc_firmware",
            "sdk_version",
            "recorder_config_sha256",
        ):
            _text(raw[name], name)
        _sha256(raw["recorder_config_sha256"], "recorder_config_sha256")
        _nonnegative_integer(raw["product_id"], "product_id")
        for name in ("drone_id", "connection_generation", "connection_epoch", "run_sequence"):
            _positive_integer(raw[name], name)
        received_ms = _nonnegative_integer(
            raw["received_at_android_elapsed_realtime_ms"], "received timestamp"
        )
        written_ms = _nonnegative_integer(
            raw["written_at_android_elapsed_realtime_ms"], "written timestamp"
        )
        if written_ms < received_ms:
            raise ValueError("raw sensor sample was written before it was received")
        if raw["session"] != self.publisher.session:
            raise ValueError("raw sensor sample session is not configured")
        if raw["recorder_config_sha256"] != self.recorder_config_sha256:
            raise ValueError("raw sensor sample recorder configuration is not configured")
        if raw["drone_id"] != self.phone_drone_id:
            raise ValueError("raw sensor sample drone is not configured")
        if (
            raw["connection_epoch"]
            != self.publisher.drones[self.phone_drone_id].fuser.connection_epoch
        ):
            raise ValueError("raw sensor sample connection epoch is not configured")
        return received_ms

    def _unselected_height(self, raw: Mapping[str, object]) -> None:
        self._common(raw, {"sdk_key", "height_value", "height_unit"})
        key = _text(raw["sdk_key"], "sdk_key")
        if raw["height_unit"] != ("m" if key == "KeyAltitude" else "dm"):
            raise ValueError("height unit does not match its SDK key")
        _finite(raw["height_value"], "height_value")

    def _phone(self, raw: Mapping[str, object], source: VelocitySource | HeightSource) -> int:
        sample_fields = (
            {"sdk_key", "coordinate_frame", "north_mps", "east_mps", "down_mps"}
            if isinstance(source, VelocitySource)
            else {"sdk_key", "height_value", "height_unit"}
        )
        received_ms = self._common(raw, sample_fields)
        if _text(raw["sdk_key"], "sdk_key") != source.sdk_key:
            raise ValueError("phone sample SDK key does not match configured source")
        if isinstance(source, VelocitySource):
            if raw["coordinate_frame"] != "ned":
                raise ValueError("velocity coordinate frame must be ned")
            for name in ("north_mps", "east_mps", "down_mps"):
                _finite(raw[name], name)
        else:
            expected_unit = "m" if source.sdk_key == "KeyAltitude" else "dm"
            if raw["height_unit"] != expected_unit:
                raise ValueError("height unit does not match its SDK key")
            _finite(raw["height_value"], "height_value")
        return received_ms

    def _phone_record(
        self, raw: Mapping[str, object], source: VelocitySource | HeightSource
    ) -> dict[str, object]:
        received_ms = self._phone(raw, source)
        config = self.publisher.drones[self.phone_drone_id]
        common: dict[str, object] = {
            "kind": "velocity" if isinstance(source, VelocitySource) else "height",
            "event_id": raw["event_id"],
            "drone_id": self.phone_drone_id,
            "connection_epoch": config.fuser.connection_epoch,
            "map_id": config.fuser.map_id,
            "geometry_id": config.fuser.geometry_id,
            "clock_id": ANDROID_ELAPSED_REALTIME_CLOCK,
            "capture_time": received_ms / 1000,
            "source_id": source.source_id,
            "source_verified": False,
            "timing_verified": False,
        }
        if isinstance(source, VelocitySource):
            vector = np.asarray([raw["north_mps"], raw["east_mps"], raw["down_mps"]], dtype=float)
            return common | {
                "velocity_map_enu_mps": [
                    float(item) for item in np.asarray(source.rotation) @ vector
                ],
                "covariance_m2ps2": [list(row) for row in source.covariance],
            }
        scale = 1 if raw["height_unit"] == "m" else 0.1
        return common | {
            "height_map_enu_m": _finite(raw["height_value"], "height_value") * scale
            + source.datum_offset_m,
            "variance_m2": source.variance_m2,
        }

    def _attitude(self, raw: Mapping[str, object]) -> None:
        expected = {"sdk_key", "attitude_frame", "yaw_deg", "pitch_deg", "roll_deg"}
        self._common(raw, expected)
        key = _text(raw["sdk_key"], "sdk_key")
        frame = raw["attitude_frame"]
        if (key, frame) not in {
            ("KeyAircraftAttitude", "aircraft_body_to_ned"),
            ("KeyGimbalAttitude", "raw_sdk_axes"),
        }:
            raise ValueError("attitude key and frame do not match Android schema")
        for name in ("yaw_deg", "pitch_deg", "roll_deg"):
            _finite(raw[name], name)


def _write(records: Iterable[dict[str, object]], output: TextIO) -> None:
    for record in records:
        output.write(json.dumps(record, allow_nan=False) + "\n")
        output.flush()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    adapter = SensorRecordAdapter(json.loads(args.config.read_text(encoding="utf-8")))
    output: TextIO
    if args.output is None:
        output = sys.stdout
        close_output = False
    else:
        output = args.output.open("x", encoding="utf-8")
        close_output = True
    try:
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
