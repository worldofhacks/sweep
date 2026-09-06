"""Immutable evidence that authorizes a control-localization pose."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from math import isfinite


@dataclass(frozen=True, slots=True)
class ControlProvenance:
    map_id: str
    geometry_id: str
    camera_calibration_id: str
    body_extrinsics_id: str
    capture_clock_id: str
    relay_clock_id: str
    source_ids: tuple[str, ...]
    capture_time_s: float | None
    conversion_error_ms: int
    reason: str
    evaluated_at_relay_ms: int | None = None
    position_uncertainty_m: float | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.source_ids, tuple)
            or not self.source_ids
            or not all(
                isinstance(value, str) and value
                for value in (
                    self.map_id,
                    self.geometry_id,
                    self.camera_calibration_id,
                    self.body_extrinsics_id,
                    self.capture_clock_id,
                    self.relay_clock_id,
                    self.reason,
                    *self.source_ids,
                )
            )
            or isinstance(self.conversion_error_ms, bool)
            or not isinstance(self.conversion_error_ms, int)
            or self.conversion_error_ms < 0
            or self.capture_time_s is not None
            and (
                isinstance(self.capture_time_s, bool)
                or not isinstance(self.capture_time_s, int | float)
                or not isfinite(self.capture_time_s)
            )
            or self.evaluated_at_relay_ms is not None
            and (
                isinstance(self.evaluated_at_relay_ms, bool)
                or not isinstance(self.evaluated_at_relay_ms, int)
                or self.evaluated_at_relay_ms < 0
            )
            or self.position_uncertainty_m is not None
            and (
                isinstance(self.position_uncertainty_m, bool)
                or not isinstance(self.position_uncertainty_m, int | float)
                or not isfinite(self.position_uncertainty_m)
                or self.position_uncertainty_m < 0
            )
        ):
            raise ValueError("control provenance is invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "map_id": self.map_id,
            "geometry_id": self.geometry_id,
            "camera_calibration_id": self.camera_calibration_id,
            "body_extrinsics_id": self.body_extrinsics_id,
            "capture_clock_id": self.capture_clock_id,
            "relay_clock_id": self.relay_clock_id,
            "source_ids": list(self.source_ids),
            "capture_time_s": self.capture_time_s,
            "conversion_error_ms": self.conversion_error_ms,
            "reason": self.reason,
            "evaluated_at_relay_ms": self.evaluated_at_relay_ms,
            "position_uncertainty_m": self.position_uncertainty_m,
        }

    @classmethod
    def from_mapping(cls, raw: object) -> ControlProvenance:
        if not isinstance(raw, Mapping):
            raise ValueError("control_provenance must be an object")
        expected = {
            "map_id",
            "geometry_id",
            "camera_calibration_id",
            "body_extrinsics_id",
            "capture_clock_id",
            "relay_clock_id",
            "source_ids",
            "capture_time_s",
            "conversion_error_ms",
            "reason",
            "evaluated_at_relay_ms",
            "position_uncertainty_m",
        }
        if set(raw) != expected:
            raise ValueError("control_provenance fields are invalid")
        source_ids = raw["source_ids"]
        if not isinstance(source_ids, list | tuple):
            raise ValueError("control_provenance source_ids must be an array")
        return cls(
            map_id=_text(raw["map_id"], "map_id"),
            geometry_id=_text(raw["geometry_id"], "geometry_id"),
            camera_calibration_id=_text(raw["camera_calibration_id"], "camera_calibration_id"),
            body_extrinsics_id=_text(raw["body_extrinsics_id"], "body_extrinsics_id"),
            capture_clock_id=_text(raw["capture_clock_id"], "capture_clock_id"),
            relay_clock_id=_text(raw["relay_clock_id"], "relay_clock_id"),
            source_ids=tuple(_text(value, "source_ids") for value in source_ids),
            capture_time_s=_optional_number(raw["capture_time_s"], "capture_time_s"),
            conversion_error_ms=_nonnegative_int(raw["conversion_error_ms"], "conversion_error_ms"),
            reason=_text(raw["reason"], "reason"),
            evaluated_at_relay_ms=_optional_nonnegative_int(
                raw["evaluated_at_relay_ms"], "evaluated_at_relay_ms"
            ),
            position_uncertainty_m=_optional_nonnegative_number(
                raw["position_uncertainty_m"], "position_uncertainty_m"
            ),
        )


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be nonempty text")
    return value


def _nonnegative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _optional_nonnegative_int(value: object, name: str) -> int | None:
    return None if value is None else _nonnegative_int(value, name)


def _optional_number(value: object, name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float) or not isfinite(value):
        raise ValueError(f"{name} must be finite")
    return float(value)


def _optional_nonnegative_number(value: object, name: str) -> float | None:
    result = _optional_number(value, name)
    if result is not None and result < 0:
        raise ValueError(f"{name} must be non-negative")
    return result
