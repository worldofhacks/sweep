"""Project retained localization frames into signed phone control-pose packets."""

from __future__ import annotations

import json
import os
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from math import isfinite

from planner.models import FleetSnapshot
from relay.auth import sign_event
from relay.control_frames import ControlLocalizationFrame
from relay.control_localization import (
    ClockMapping,
    ControlLocalizationPins,
    ControlLocalizationStore,
    IngestResult,
)


@dataclass(frozen=True, slots=True)
class ControlRuntimeConfig:
    pins: Mapping[int, ControlLocalizationPins]
    max_clock_error_ms: int
    max_fix_age_ms: int
    max_position_uncertainty_m: float
    land_after_fix_age_ms: int
    node_keys: Mapping[int, bytes]

    def __post_init__(self) -> None:
        if (
            not self.pins
            or self.max_clock_error_ms < 0
            or self.max_fix_age_ms <= 0
            or self.land_after_fix_age_ms < self.max_fix_age_ms
            or self.max_position_uncertainty_m <= 0
            or any(drone_id not in self.node_keys for drone_id in self.pins)
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
    def from_mapping(cls, raw: object, *, node_keys: Mapping[int, bytes]) -> ControlRuntimeConfig:
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
            dict(node_keys),
        )

    @classmethod
    def from_env(
        cls, environ: Mapping[str, str] | None = None, *, node_keys: Mapping[int, bytes]
    ) -> ControlRuntimeConfig:
        source = os.environ if environ is None else environ
        path = source.get("SWEEP_CONTROL_LOCALIZATION_CONFIG")
        if not path:
            raise ValueError("SWEEP_CONTROL_LOCALIZATION_CONFIG is required")
        with open(path, encoding="utf-8") as handle:
            return cls.from_mapping(json.load(handle), node_keys=node_keys)

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


class ControlRuntime:
    def __init__(self, config: ControlRuntimeConfig) -> None:
        self.config = config
        self.store = config.create_store()
        self._seen: dict[int, deque[str]] = {
            drone_id: deque(maxlen=256) for drone_id in config.pins
        }
        self._sequence = 0
        self._last_fix: dict[int, int] = {}
        self._loss_started: dict[int, int] = {}
        self._last_pose_time: dict[int, int] = {}

    def ingest(
        self,
        frame: ControlLocalizationFrame,
        authenticated_drone_id: int,
        authenticated_connection_epoch: int,
        now_ms: int,
    ) -> IngestResult:
        seen = self._seen.get(authenticated_drone_id)
        if seen is None or frame.event_id in seen:
            return IngestResult(False, "duplicate_event")
        result = self.store.ingest(
            frame.to_event(), authenticated_drone_id, authenticated_connection_epoch, now_ms
        )
        if result.accepted:
            seen.append(frame.event_id)
        return result

    def apply(self, snapshot: FleetSnapshot) -> FleetSnapshot:
        return self.store.apply(snapshot)

    def control_pose(
        self, drone_id: int, snapshot: FleetSnapshot, session: str, now_ms: int
    ) -> dict[str, object] | None:
        pin = self.config.pins[drone_id]
        aircraft = snapshot.aircraft.get(drone_id)
        if aircraft is None or aircraft.connection_epoch != pin.connection_epoch:
            return self._packet(drone_id, pin, session, now_ms, "hold", 0, 0, 0, 0, 0, now_ms)
        provenance = aircraft.control_provenance
        valid = provenance is not None and self._matches_pins(provenance, pin)
        fix_time = aircraft.position_last_seen_ms if valid else self._last_fix.get(drone_id, 0)
        pose_time = (
            provenance.evaluated_at_relay_ms
            if valid and provenance.evaluated_at_relay_ms is not None
            else fix_time
        )
        age = now_ms - fix_time
        if (
            not valid
            or aircraft.position_quality <= 0
            or age > self.config.max_fix_age_ms
            or pose_time < fix_time
        ):
            started = self._loss_started.setdefault(drone_id, now_ms)
            status = "land" if now_ms - started >= self.config.land_after_fix_age_ms else "hold"
            return self._packet(
                drone_id, pin, session, now_ms, status, 0, 0, 0, 0, pose_time, fix_time
            )
        uncertainty = provenance.position_uncertainty_m
        if uncertainty is None or uncertainty > self.config.max_position_uncertainty_m:
            self._loss_started.setdefault(drone_id, now_ms)
            return self._packet(
                drone_id, pin, session, now_ms, "hold", 0, 0, 0, 0, pose_time, fix_time
            )
        if pose_time <= self._last_pose_time.get(drone_id, -1):
            return None
        self._last_fix[drone_id] = fix_time
        self._loss_started.pop(drone_id, None)
        return self._packet(
            drone_id,
            pin,
            session,
            now_ms,
            "ready",
            round(aircraft.pose.x * 1000),
            round(aircraft.pose.y * 1000),
            round(aircraft.pose.z * 1000),
            round(uncertainty * 1000),
            pose_time,
            fix_time,
        )

    def _packet(
        self,
        drone_id: int,
        pin: ControlLocalizationPins,
        session: str,
        now_ms: int,
        status: str,
        x_mm: int,
        y_mm: int,
        z_mm: int,
        uncertainty_mm: int,
        pose_time_ms: int,
        fix_time_ms: int,
    ) -> dict[str, object]:
        self._sequence += 1
        unsigned = {
            "v": 1,
            "type": "control_pose",
            "t": now_ms,
            "event_id": f"control-pose-{drone_id}-{self._sequence}",
            "session": session,
            "drone_id": drone_id,
            "connection_epoch": pin.connection_epoch,
            "map_id": pin.map_id,
            "geometry_id": pin.geometry_id,
            "camera_calibration_id": pin.camera_calibration_id,
            "body_extrinsics_id": pin.body_extrinsics_id,
            "pose_time_ms": pose_time_ms
            if status == "ready"
            else max(now_ms, fix_time_ms, self._last_pose_time.get(drone_id, -1) + 1),
            "fix_time_ms": fix_time_ms,
            "x_mm": x_mm,
            "y_mm": y_mm,
            "z_mm": z_mm,
            "position_uncertainty_mm": max(0, uncertainty_mm),
            "status": status,
        }
        self._last_pose_time[drone_id] = unsigned["pose_time_ms"]
        return {**unsigned, "signature": sign_event(unsigned, self.config.node_keys[drone_id])}

    @staticmethod
    def _matches_pins(provenance: object, pin: ControlLocalizationPins) -> bool:
        return all(
            getattr(provenance, field) == getattr(pin, field)
            for field in (
                "map_id",
                "geometry_id",
                "camera_calibration_id",
                "body_extrinsics_id",
                "capture_clock_id",
                "relay_clock_id",
                "source_ids",
            )
        )


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
