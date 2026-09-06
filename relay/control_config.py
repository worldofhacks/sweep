"""Host-owned configuration for diagnostic localization projection."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256

from relay.control_localization import (
    ClockMapping,
    ControlLocalizationPins,
    ControlLocalizationProjector,
)


@dataclass(frozen=True, slots=True)
class ControlRuntimeConfig:
    pins: Mapping[int, ControlLocalizationPins]
    max_clock_error_ms: int
    max_fix_age_ms: int
    max_velocity_age_ms: int
    max_height_age_ms: int
    max_position_uncertainty_p95_m: float

    def __post_init__(self) -> None:
        self.create_projector()

    def create_projector(self) -> ControlLocalizationProjector:
        relay_clock_ids = {pin.clock_mapping.relay_clock_id for pin in self.pins.values()}
        if len(relay_clock_ids) != 1:
            raise ValueError("control localization pins must share one relay clock")
        return ControlLocalizationProjector(
            self.pins,
            relay_clock_id=relay_clock_ids.pop(),
            max_clock_error_ms=self.max_clock_error_ms,
            max_fix_age_ms=self.max_fix_age_ms,
            max_velocity_age_ms=self.max_velocity_age_ms,
            max_height_age_ms=self.max_height_age_ms,
            max_position_uncertainty_p95_m=self.max_position_uncertainty_p95_m,
        )

    @classmethod
    def from_mapping(cls, raw: object) -> ControlRuntimeConfig:
        if not isinstance(raw, Mapping) or set(raw) != {"limits", "drones"}:
            raise ValueError("control localization configuration must be an object")
        limits = raw["limits"]
        drones = raw["drones"]
        if (
            not isinstance(limits, Mapping)
            or set(limits)
            != {
                "max_clock_error_ms",
                "max_fix_age_ms",
                "max_velocity_age_ms",
                "max_height_age_ms",
                "max_position_uncertainty_p95_m",
            }
            or not isinstance(drones, list)
            or not drones
        ):
            raise ValueError("control localization requires limits and drones")
        pins: dict[int, ControlLocalizationPins] = {}
        for item in drones:
            if not isinstance(item, Mapping) or set(item) != {
                "drone_id",
                "map_id",
                "geometry_id",
                "camera_calibration_id",
                "body_extrinsics_id",
                "source_ids",
                "clock_mapping",
            }:
                raise ValueError("control localization drone entries must be objects")
            drone_id = _integer(item["drone_id"], "drone_id", positive=True)
            if drone_id in pins:
                raise ValueError("control localization drone ids must be unique")
            pins[drone_id] = ControlLocalizationPins(
                drone_id=drone_id,
                map_id=_text(item["map_id"], "map_id"),
                geometry_id=_text(item["geometry_id"], "geometry_id"),
                camera_calibration_id=_text(
                    item["camera_calibration_id"], "camera_calibration_id"
                ),
                body_extrinsics_id=_text(item["body_extrinsics_id"], "body_extrinsics_id"),
                source_ids=tuple(_text(value, "source_ids") for value in item["source_ids"]),
                clock_mapping=ClockMapping.from_mapping(item["clock_mapping"]),
            )
        return cls(
            pins=pins,
            max_clock_error_ms=_integer(limits["max_clock_error_ms"], "max_clock_error_ms"),
            max_fix_age_ms=_integer(limits["max_fix_age_ms"], "max_fix_age_ms", positive=True),
            max_velocity_age_ms=_integer(
                limits["max_velocity_age_ms"], "max_velocity_age_ms", positive=True
            ),
            max_height_age_ms=_integer(
                limits["max_height_age_ms"], "max_height_age_ms", positive=True
            ),
            max_position_uncertainty_p95_m=_number(
                limits["max_position_uncertainty_p95_m"], "max_position_uncertainty_p95_m"
            ),
        )

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> ControlRuntimeConfig:
        source = os.environ if environ is None else environ
        path = source.get("SWEEP_CONTROL_LOCALIZATION_CONFIG")
        if not path:
            raise ValueError("SWEEP_CONTROL_LOCALIZATION_CONFIG is required")
        with open(path, encoding="utf-8") as handle:
            return cls.from_mapping(json.load(handle))

    @property
    def identity(self) -> str:
        payload = repr(
            (
                tuple(sorted(self.pins.items())),
                self.max_clock_error_ms,
                self.max_fix_age_ms,
                self.max_velocity_age_ms,
                self.max_height_age_ms,
                self.max_position_uncertainty_p95_m,
            )
        ).encode()
        return sha256(payload).hexdigest()


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be nonempty text")
    return value


def _integer(value: object, name: str, *, positive: bool = False) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or positive
        and value == 0
    ):
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        raise ValueError(f"{name} must be a positive number")
    return float(value)
