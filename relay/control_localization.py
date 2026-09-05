"""Authenticated control-localization evidence projected into planner snapshots."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from math import isfinite
from types import MappingProxyType

import numpy as np

from perception.control_localization import ControlLocalizationSnapshot
from planner.models import FleetSnapshot, Position


def _finite(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not isfinite(value):
        raise ValueError(f"{name} must be finite")
    return float(value)


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be nonempty text")
    return value


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _vector(value: object, name: str) -> tuple[float, float, float] | None:
    if value is None:
        return None
    if not isinstance(value, list | tuple) or len(value) != 3:
        raise ValueError(f"{name} must be a three-axis vector")
    return tuple(_finite(item, name) for item in value)  # type: ignore[return-value]


def _covariance(value: object) -> tuple[tuple[float, ...], ...] | None:
    if value is None:
        return None
    if (
        not isinstance(value, list | tuple)
        or len(value) != 3
        or any(not isinstance(row, list | tuple) or len(row) != 3 for row in value)
    ):
        raise ValueError("covariance_map_enu_m2 must be 3x3")
    rows = tuple(tuple(_finite(item, "covariance_map_enu_m2") for item in row) for row in value)
    matrix = np.asarray(rows)
    if not np.allclose(matrix, matrix.T) or np.linalg.eigvalsh(matrix).min() <= 0:
        raise ValueError("covariance_map_enu_m2 must be symmetric positive definite")
    return rows


@dataclass(frozen=True, slots=True)
class ClockMapping:
    capture_clock_id: str
    relay_clock_id: str
    capture_reference_s: float
    relay_reference_ms: int
    milliseconds_per_capture_second: float
    max_error_ms: int
    measured: bool

    def __post_init__(self) -> None:
        if (
            not isinstance(self.capture_clock_id, str)
            or not self.capture_clock_id
            or not isinstance(self.relay_clock_id, str)
            or not self.relay_clock_id
            or self.measured is not True
            or isinstance(self.relay_reference_ms, bool)
            or not isinstance(self.relay_reference_ms, int)
            or self.relay_reference_ms < 0
            or isinstance(self.max_error_ms, bool)
            or not isinstance(self.max_error_ms, int)
            or self.max_error_ms < 0
            or self.milliseconds_per_capture_second <= 0
        ):
            raise ValueError("clock mapping requires measured bounded evidence")
        _finite(self.capture_reference_s, "capture_reference_s")
        _finite(self.milliseconds_per_capture_second, "milliseconds_per_capture_second")

    def to_relay_ms(self, capture_time_s: float) -> int:
        return round(
            self.relay_reference_ms
            + (capture_time_s - self.capture_reference_s) * self.milliseconds_per_capture_second
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "capture_clock_id": self.capture_clock_id,
            "relay_clock_id": self.relay_clock_id,
            "capture_reference_s": self.capture_reference_s,
            "relay_reference_ms": self.relay_reference_ms,
            "milliseconds_per_capture_second": self.milliseconds_per_capture_second,
            "max_error_ms": self.max_error_ms,
            "measured": self.measured,
        }

    @classmethod
    def from_mapping(cls, raw: object) -> ClockMapping:
        if not isinstance(raw, Mapping):
            raise ValueError("clock_mapping must be an object")
        return cls(
            capture_clock_id=_text(raw.get("capture_clock_id"), "capture_clock_id"),
            relay_clock_id=_text(raw.get("relay_clock_id"), "relay_clock_id"),
            capture_reference_s=_finite(raw.get("capture_reference_s"), "capture_reference_s"),
            relay_reference_ms=_integer(raw.get("relay_reference_ms"), "relay_reference_ms"),
            milliseconds_per_capture_second=_finite(
                raw.get("milliseconds_per_capture_second"), "milliseconds_per_capture_second"
            ),
            max_error_ms=_integer(raw.get("max_error_ms"), "max_error_ms"),
            measured=raw.get("measured") is True,
        )


@dataclass(frozen=True, slots=True)
class ControlLocalizationPins:
    drone_id: int
    connection_epoch: int
    map_id: str
    geometry_id: str
    camera_calibration_id: str
    body_extrinsics_id: str
    capture_clock_id: str
    relay_clock_id: str
    source_ids: tuple[str, ...]
    clock_mapping: ClockMapping

    def __post_init__(self) -> None:
        if (
            isinstance(self.drone_id, bool)
            or not isinstance(self.drone_id, int)
            or self.drone_id <= 0
            or isinstance(self.connection_epoch, bool)
            or not isinstance(self.connection_epoch, int)
            or self.connection_epoch < 0
            or not isinstance(self.source_ids, tuple)
            or not self.source_ids
            or not isinstance(self.clock_mapping, ClockMapping)
            or not all(
                isinstance(value, str) and value
                for value in (
                    self.map_id,
                    self.geometry_id,
                    self.camera_calibration_id,
                    self.body_extrinsics_id,
                    self.capture_clock_id,
                    self.relay_clock_id,
                    *self.source_ids,
                )
            )
        ):
            raise ValueError("control localization pins are invalid")
        if (
            self.clock_mapping.capture_clock_id != self.capture_clock_id
            or self.clock_mapping.relay_clock_id != self.relay_clock_id
        ):
            raise ValueError("control localization pins must bind the measured clock mapping")


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

    def to_dict(self) -> dict[str, object]:
        return {
            "map_id": self.map_id,
            "geometry_id": self.geometry_id,
            "camera_calibration_id": self.camera_calibration_id,
            "body_extrinsics_id": self.body_extrinsics_id,
            "capture_clock_id": self.capture_clock_id,
            "relay_clock_id": self.relay_clock_id,
            "source_ids": self.source_ids,
            "capture_time_s": self.capture_time_s,
            "conversion_error_ms": self.conversion_error_ms,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class ControlLocalizationWire:
    event_id: str
    drone_id: int
    connection_epoch: int
    map_id: str
    geometry_id: str
    camera_calibration_id: str
    body_extrinsics_id: str
    capture_clock_id: str
    evaluated_at_s: float
    position_map_enu_m: tuple[float, float, float] | None
    velocity_map_enu_mps: tuple[float, float, float] | None
    covariance_map_enu_m2: tuple[tuple[float, ...], ...] | None
    last_fix_capture_time_s: float | None
    fix_age_s: float | None
    status: str
    control_eligible: bool
    reason: str
    source_ids: tuple[str, ...]
    clock_mapping: ClockMapping
    signature: str

    def __post_init__(self) -> None:
        if (
            not self.event_id
            or self.status not in {"ready", "hold", "land"}
            or not isinstance(self.control_eligible, bool)
            or not self.signature
        ):
            raise ValueError("localization status is invalid")
        _finite(self.evaluated_at_s, "evaluated_at_s")
        if self.last_fix_capture_time_s is not None:
            capture = _finite(self.last_fix_capture_time_s, "last_fix_capture_time_s")
            if capture > self.evaluated_at_s:
                raise ValueError("fix capture time cannot follow evaluation")
            if (
                self.fix_age_s is None
                or abs(self.fix_age_s - (self.evaluated_at_s - capture)) > 1e-6
            ):
                raise ValueError("fix age does not match capture and evaluation times")
        elif self.fix_age_s is not None:
            raise ValueError("fix age requires a capture time")
        if self.clock_mapping.capture_clock_id != self.capture_clock_id:
            raise ValueError("clock mapping does not identify the capture clock")

    def to_mapping(self) -> dict[str, object]:
        return {
            "v": 1,
            "type": "control_localization",
            "event_id": self.event_id,
            "drone_id": self.drone_id,
            "connection_epoch": self.connection_epoch,
            "map_id": self.map_id,
            "geometry_id": self.geometry_id,
            "camera_calibration_id": self.camera_calibration_id,
            "body_extrinsics_id": self.body_extrinsics_id,
            "capture_clock_id": self.capture_clock_id,
            "evaluated_at_s": self.evaluated_at_s,
            "position_map_enu_m": self.position_map_enu_m,
            "velocity_map_enu_mps": self.velocity_map_enu_mps,
            "covariance_map_enu_m2": self.covariance_map_enu_m2,
            "last_fix_capture_time_s": self.last_fix_capture_time_s,
            "fix_age_s": self.fix_age_s,
            "localization_status": self.status,
            "control_eligible": self.control_eligible,
            "localization_reason": self.reason,
            "source_ids": self.source_ids,
            "clock_mapping": self.clock_mapping.to_mapping(),
            "signature": self.signature,
        }

    @classmethod
    def from_mapping(cls, raw: object) -> ControlLocalizationWire:
        if (
            not isinstance(raw, Mapping)
            or raw.get("v") != 1
            or raw.get("type") != "control_localization"
        ):
            raise ValueError("invalid control localization payload")
        sources = raw.get("source_ids")
        if not isinstance(sources, list | tuple):
            raise ValueError("source_ids must be an array")
        control_eligible = raw.get("control_eligible")
        if not isinstance(control_eligible, bool):
            raise ValueError("control_eligible must be a boolean")
        return cls(
            event_id=_text(raw.get("event_id"), "event_id"),
            drone_id=_integer(raw.get("drone_id"), "drone_id"),
            connection_epoch=_integer(raw.get("connection_epoch"), "connection_epoch"),
            map_id=_text(raw.get("map_id"), "map_id"),
            geometry_id=_text(raw.get("geometry_id"), "geometry_id"),
            camera_calibration_id=_text(raw.get("camera_calibration_id"), "camera_calibration_id"),
            body_extrinsics_id=_text(raw.get("body_extrinsics_id"), "body_extrinsics_id"),
            capture_clock_id=_text(raw.get("capture_clock_id"), "capture_clock_id"),
            evaluated_at_s=_finite(raw.get("evaluated_at_s"), "evaluated_at_s"),
            position_map_enu_m=_vector(raw.get("position_map_enu_m"), "position_map_enu_m"),
            velocity_map_enu_mps=_vector(raw.get("velocity_map_enu_mps"), "velocity_map_enu_mps"),
            covariance_map_enu_m2=_covariance(raw.get("covariance_map_enu_m2")),
            last_fix_capture_time_s=(
                None
                if raw.get("last_fix_capture_time_s") is None
                else _finite(raw.get("last_fix_capture_time_s"), "last_fix_capture_time_s")
            ),
            fix_age_s=(
                None if raw.get("fix_age_s") is None else _finite(raw.get("fix_age_s"), "fix_age_s")
            ),
            status=_text(raw.get("localization_status"), "localization_status"),
            control_eligible=control_eligible,
            reason=_text(raw.get("localization_reason"), "localization_reason"),
            source_ids=tuple(_text(value, "source_ids") for value in sources),
            clock_mapping=ClockMapping.from_mapping(raw.get("clock_mapping")),
            signature=_text(raw.get("signature"), "signature"),
        )


def to_wire_payload(
    snapshot: ControlLocalizationSnapshot,
    clock_mapping: ClockMapping,
    signature: str,
    event_id: str,
) -> dict[str, object]:
    if clock_mapping.capture_clock_id != snapshot.capture_clock_id:
        raise ValueError("clock mapping must match the fuser capture clock")
    return ControlLocalizationWire(
        event_id=_text(event_id, "event_id"),
        drone_id=snapshot.drone_id,
        connection_epoch=snapshot.connection_epoch,
        map_id=snapshot.map_id,
        geometry_id=snapshot.geometry_id,
        camera_calibration_id=snapshot.camera_calibration_id,
        body_extrinsics_id=snapshot.body_extrinsics_id,
        capture_clock_id=snapshot.capture_clock_id,
        evaluated_at_s=snapshot.evaluated_at_s,
        position_map_enu_m=snapshot.position_map_enu_m,
        velocity_map_enu_mps=snapshot.velocity_map_enu_mps,
        covariance_map_enu_m2=snapshot.covariance_map_enu_m2,
        last_fix_capture_time_s=snapshot.last_fix_capture_time_s,
        fix_age_s=snapshot.fix_age_s,
        status=snapshot.status,
        control_eligible=snapshot.control_eligible,
        reason=snapshot.reason,
        source_ids=snapshot.source_ids,
        clock_mapping=clock_mapping,
        signature=_text(signature, "signature"),
    ).to_mapping()


@dataclass(frozen=True, slots=True)
class _Patch:
    pose: Position | None
    quality: float
    last_seen_ms: int
    provenance: ControlProvenance


@dataclass(frozen=True, slots=True)
class IngestResult:
    accepted: bool
    reason: str


class ControlLocalizationStore:
    def __init__(
        self,
        pins: Mapping[int, ControlLocalizationPins],
        *,
        max_clock_error_ms: int,
        max_fix_age_ms: int,
    ) -> None:
        if (
            isinstance(max_clock_error_ms, bool)
            or not isinstance(max_clock_error_ms, int)
            or max_clock_error_ms < 0
            or isinstance(max_fix_age_ms, bool)
            or not isinstance(max_fix_age_ms, int)
            or max_fix_age_ms < 0
        ):
            raise ValueError("control localization age limits must be non-negative")
        self._pins = MappingProxyType(dict(pins))
        if any(drone_id != pin.drone_id for drone_id, pin in self._pins.items()):
            raise ValueError("control localization pin keys must match drone ids")
        self._max_clock_error_ms = max_clock_error_ms
        self._max_fix_age_ms = max_fix_age_ms
        self._patches: dict[int, _Patch] = {}
        self._last_capture_ms: dict[int, int] = {}
        self._last_evaluated_s: dict[int, float] = {}
        self._last_event_id: dict[int, str] = {}

    def ingest(
        self,
        raw: object,
        authenticated_drone_id: int,
        authenticated_connection_epoch: int,
        now_ms: int,
    ) -> IngestResult:
        try:
            wire = ControlLocalizationWire.from_mapping(raw)
            reason = self._validate(
                wire, authenticated_drone_id, authenticated_connection_epoch, now_ms
            )
        except ValueError:
            self._record_loss(authenticated_drone_id, "invalid_payload", now_ms)
            return IngestResult(False, "invalid_payload")
        if reason is not None:
            self._record_loss(authenticated_drone_id, reason, now_ms, wire)
            return IngestResult(False, reason)
        capture_ms = self._conservative_capture_ms(wire)
        self._last_capture_ms[wire.drone_id] = capture_ms
        self._last_evaluated_s[wire.drone_id] = wire.evaluated_at_s
        self._last_event_id[wire.drone_id] = wire.event_id
        self._patches[wire.drone_id] = _Patch(
            pose=Position(*wire.position_map_enu_m),
            quality=1.0,
            last_seen_ms=capture_ms,
            provenance=self._provenance(wire),
        )
        return IngestResult(True, "accepted")

    def apply(self, snapshot: FleetSnapshot) -> FleetSnapshot:
        aircraft = dict(snapshot.aircraft)
        for drone_id, pins in self._pins.items():
            current = aircraft.get(drone_id)
            if current is None:
                continue
            patch = self._patches.get(drone_id)
            if current.connection_epoch != pins.connection_epoch:
                self._record_loss(drone_id, "connection_epoch_mismatch", snapshot.now_ms)
                patch = self._patches[drone_id]
            elif patch is None or not self._fresh(patch, snapshot.now_ms):
                self._record_loss(drone_id, "localization_missing", snapshot.now_ms)
                patch = self._patches[drone_id]
            changes: dict[str, object] = {
                "pose": current.pose if patch.pose is None else patch.pose,
                "position_quality": patch.quality,
                "position_last_seen_ms": patch.last_seen_ms,
            }
            if "control_provenance" in current.__dataclass_fields__:
                changes["control_provenance"] = patch.provenance
            aircraft[drone_id] = replace(current, **changes)
        return replace(snapshot, aircraft=aircraft)

    def _validate(
        self,
        wire: ControlLocalizationWire,
        authenticated_drone_id: int,
        authenticated_epoch: int,
        now_ms: int,
    ) -> str | None:
        pin = self._pins.get(authenticated_drone_id)
        if (
            pin is None
            or wire.drone_id != authenticated_drone_id
            or wire.connection_epoch != authenticated_epoch
        ):
            return "authentication_mismatch"
        if wire.connection_epoch != pin.connection_epoch:
            return "connection_epoch_mismatch"
        if (
            wire.map_id != pin.map_id
            or wire.geometry_id != pin.geometry_id
            or wire.camera_calibration_id != pin.camera_calibration_id
            or wire.body_extrinsics_id != pin.body_extrinsics_id
            or wire.capture_clock_id != pin.capture_clock_id
            or wire.clock_mapping.relay_clock_id != pin.relay_clock_id
            or wire.source_ids != pin.source_ids
            or wire.clock_mapping != pin.clock_mapping
        ):
            return "provenance_mismatch"
        if wire.clock_mapping.max_error_ms > self._max_clock_error_ms:
            return "clock_uncertainty_exceeded"
        if (
            wire.status != "ready"
            or not wire.control_eligible
            or wire.position_map_enu_m is None
            or wire.last_fix_capture_time_s is None
        ):
            return wire.reason
        capture_ms = self._conservative_capture_ms(wire)
        if capture_ms > now_ms + wire.clock_mapping.max_error_ms:
            return "capture_time_in_future"
        if now_ms - capture_ms > self._max_fix_age_ms:
            return "capture_time_expired"
        evaluated_ms = wire.clock_mapping.to_relay_ms(wire.evaluated_at_s)
        if evaluated_ms > now_ms + wire.clock_mapping.max_error_ms:
            return "evaluation_time_in_future"
        previous = self._last_capture_ms.get(wire.drone_id)
        if previous is not None and capture_ms < previous:
            return "capture_time_regressed"
        previous_evaluation = self._last_evaluated_s.get(wire.drone_id)
        if previous_evaluation is not None and wire.evaluated_at_s < previous_evaluation:
            return "evaluation_time_regressed"
        if self._last_event_id.get(wire.drone_id) == wire.event_id:
            return "duplicate_event"
        return None

    def _fresh(self, patch: _Patch, now_ms: int) -> bool:
        return (
            patch.quality > 0
            and patch.last_seen_ms <= now_ms + self._max_clock_error_ms
            and now_ms - patch.last_seen_ms <= self._max_fix_age_ms
        )

    @staticmethod
    def _conservative_capture_ms(wire: ControlLocalizationWire) -> int:
        return max(
            0,
            wire.clock_mapping.to_relay_ms(wire.last_fix_capture_time_s)
            - wire.clock_mapping.max_error_ms,
        )

    def _provenance(self, wire: ControlLocalizationWire) -> ControlProvenance:
        return ControlProvenance(
            wire.map_id,
            wire.geometry_id,
            wire.camera_calibration_id,
            wire.body_extrinsics_id,
            wire.capture_clock_id,
            wire.clock_mapping.relay_clock_id,
            wire.source_ids,
            wire.last_fix_capture_time_s,
            wire.clock_mapping.max_error_ms,
            wire.reason,
        )

    def _record_loss(
        self, drone_id: int, reason: str, now_ms: int, wire: ControlLocalizationWire | None = None
    ) -> None:
        pin = self._pins.get(drone_id)
        if pin is None:
            return
        last_seen = self._last_capture_ms.get(drone_id, 0)
        provenance = (
            self._provenance(wire)
            if wire is not None
            else ControlProvenance(
                pin.map_id,
                pin.geometry_id,
                pin.camera_calibration_id,
                pin.body_extrinsics_id,
                pin.capture_clock_id,
                pin.relay_clock_id,
                pin.source_ids,
                None,
                self._max_clock_error_ms,
                reason,
            )
        )
        self._patches[drone_id] = _Patch(None, 0.0, last_seen, provenance)
