"""Deployment pins for applying authenticated localization to autonomy snapshots."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from math import isfinite

from relay.control_localization import (
    ClockMapping,
    ControlLocalizationPins,
    ControlLocalizationStore,
)


@dataclass(frozen=True, slots=True)
class ControlRuntimeConfig:
    pins: Mapping[int, ControlLocalizationPins]
    max_clock_error_ms: int
    max_fix_age_ms: int
    max_position_uncertainty_m: float
    land_after_fix_age_ms: int

    def __post_init__(self) -> None:
        if (
            not self.pins
            or self.max_clock_error_ms < 0
            or self.max_fix_age_ms <= 0
            or self.land_after_fix_age_ms < self.max_fix_age_ms
            or self.max_position_uncertainty_m <= 0
        ):
            raise ValueError("control runtime configuration is incomplete")

    def create_store(self) -> ControlLocalizationStore:
        return ControlLocalizationStore(
            self.pins,
            max_clock_error_ms=self.max_clock_error_ms,
            max_fix_age_ms=self.max_fix_age_ms,
            max_position_uncertainty_m=self.max_position_uncertainty_m,
        )

    @classmethod
    def from_mapping(cls, raw: object) -> ControlRuntimeConfig:
        if not isinstance(raw, Mapping) or set(raw) != {"limits", "drones"}:
            raise ValueError("control runtime configuration must be an object")
        limits = raw.get("limits")
        drones = raw.get("drones")
        if (
            not isinstance(limits, Mapping)
            or set(limits)
            != {
                "max_clock_error_ms",
                "max_fix_age_ms",
                "max_position_uncertainty_m",
                "land_after_fix_age_ms",
            }
            or not isinstance(drones, list)
            or not drones
        ):
            raise ValueError("control runtime requires limits and drones")
        pins: dict[int, ControlLocalizationPins] = {}
        for item in drones:
            if not isinstance(item, Mapping) or set(item) != {
                "drone_id",
                "connection_epoch",
                "map_id",
                "geometry_id",
                "camera_calibration_id",
                "body_extrinsics_id",
                "capture_clock_id",
                "relay_clock_id",
                "source_ids",
                "clock_mapping",
            }:
                raise ValueError("control runtime drone entries must be objects")
            drone_id = _integer(item.get("drone_id"), "drone_id", positive=True)
            pin = ControlLocalizationPins(
                drone_id,
                _integer(item.get("connection_epoch"), "connection_epoch"),
                _text(item.get("map_id"), "map_id"),
                _text(item.get("geometry_id"), "geometry_id"),
                _text(item.get("camera_calibration_id"), "camera_calibration_id"),
                _text(item.get("body_extrinsics_id"), "body_extrinsics_id"),
                _text(item.get("capture_clock_id"), "capture_clock_id"),
                _text(item.get("relay_clock_id"), "relay_clock_id"),
                tuple(_text(value, "source_ids") for value in item.get("source_ids", ())),
                ClockMapping.from_mapping(item.get("clock_mapping")),
            )
            if drone_id in pins:
                raise ValueError("control runtime drone ids must be unique")
            pins[drone_id] = pin
        return cls(
            pins,
            _integer(limits.get("max_clock_error_ms"), "max_clock_error_ms"),
            _integer(limits.get("max_fix_age_ms"), "max_fix_age_ms"),
            _number(limits.get("max_position_uncertainty_m"), "max_position_uncertainty_m"),
            _integer(limits.get("land_after_fix_age_ms"), "land_after_fix_age_ms"),
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
                self.max_position_uncertainty_m,
                self.land_after_fix_age_ms,
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
    if isinstance(value, bool) or not isinstance(value, int | float) or not isfinite(value):
        raise ValueError(f"{name} must be finite")
    return float(value)
