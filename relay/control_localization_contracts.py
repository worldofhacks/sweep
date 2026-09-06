"""Exact, bounded contracts for localization evidence and diagnostic control poses."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from math import isfinite
from typing import Protocol

import numpy as np

MAX_IDENTIFIER_CHARS = 128
MAX_SOURCE_IDS = 16
MAX_INT32 = 2**31 - 1
MAX_INT64 = 2**63 - 1
MAX_COORDINATE_MM = 1_000_000
MAX_POSITION_UNCERTAINTY_MM = 1_000_000
POSITION_P95_SIGMA_MULTIPLIER = 2.7954834829151074
MAX_COVARIANCE_M2 = 1_000_000.0
MAX_CLOCK_ERROR_MS = 10_000
MAX_CLOCK_RATE_MS_PER_S = 1_000_000.0

LOCALIZATION_BODY_FIELDS = frozenset(
    {
        "drone_id",
        "connection_epoch",
        "map_id",
        "geometry_id",
        "camera_calibration_id",
        "body_extrinsics_id",
        "capture_clock_id",
        "evaluated_at_s",
        "position_map_enu_m",
        "covariance_map_enu_m2",
        "fix_age_s",
        "velocity_age_s",
        "height_age_s",
        "localization_confidence",
        "localization_loss_age_s",
        "localization_status",
        "control_eligible",
        "flight_approved",
        "localization_reason",
        "source_ids",
        "clock_mapping",
    }
)


class LocalizationSnapshot(Protocol):
    """Producer-owned snapshot shape consumed by the relay-neutral encoder."""

    drone_id: int
    connection_epoch: int
    map_id: str
    geometry_id: str
    capture_clock_id: str
    evaluated_at_s: float
    position_map_enu_m: tuple[float, float, float] | None
    covariance_map_enu_m2: tuple[tuple[float, ...], ...] | None
    fix_age_s: float | None
    velocity_age_s: float | None
    height_age_s: float | None
    confidence: str
    loss_age_s: float | None
    status: str
    control_eligible: bool
    reason: str
    source_ids: tuple[str, ...]
    camera_calibration_id: str
    body_extrinsics_id: str


def identifier(value: object, name: str) -> str:
    return _bounded_text(value, name, MAX_IDENTIFIER_CHARS)


def session_identifier(value: object) -> str:
    return _bounded_text(value, "session", 512)


def _bounded_text(value: object, name: str, maximum: int) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or not value.isprintable()
        or len(value) > maximum
    ):
        raise ValueError(f"{name} must be canonical printable text of at most {maximum} characters")
    return value


def positive_int32(value: object, name: str) -> int:
    if type(value) is not int or not 1 <= value <= MAX_INT32:
        raise ValueError(f"{name} must be a positive signed 32-bit integer")
    return value


def nonnegative_int64(value: object, name: str) -> int:
    if type(value) is not int or not 0 <= value <= MAX_INT64:
        raise ValueError(f"{name} must be a non-negative signed 64-bit integer")
    return value


def bounded_nonnegative(value: object, name: str, maximum: int) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError(f"{name} must be an integer from 0 through {maximum}")
    return value


def finite(value: object, name: str) -> float:
    if type(value) not in {int, float} or not isfinite(value):
        raise ValueError(f"{name} must be finite")
    return float(value)


def optional_age(value: object, name: str, evaluated_at_s: float) -> float | None:
    if value is None:
        return None
    age = finite(value, name)
    if age < 0 or age > evaluated_at_s:
        raise ValueError(f"{name} must identify a non-negative capture time")
    return age


def source_ids(value: object, *, allow_empty: bool) -> tuple[str, ...]:
    if not isinstance(value, list | tuple):
        raise ValueError("source_ids must be an array")
    result = tuple(identifier(item, "source_ids") for item in value)
    minimum = 0 if allow_empty else 1
    if not minimum <= len(result) <= MAX_SOURCE_IDS or len(set(result)) != len(result):
        raise ValueError("source_ids must be a bounded array of unique identifiers")
    return result


def position(value: object) -> tuple[float, float, float] | None:
    if value is None:
        return None
    if not isinstance(value, list | tuple) or len(value) != 3:
        raise ValueError("position_map_enu_m must contain three finite coordinates")
    result = tuple(finite(item, "position_map_enu_m") for item in value)
    if any(abs(item * 1_000.0) > MAX_COORDINATE_MM for item in result):
        raise ValueError("position_map_enu_m exceeds the bounded control-pose envelope")
    return result  # type: ignore[return-value]


def covariance(value: object) -> tuple[tuple[float, ...], ...] | None:
    if value is None:
        return None
    if (
        not isinstance(value, list | tuple)
        or len(value) != 3
        or any(not isinstance(row, list | tuple) or len(row) != 3 for row in value)
    ):
        raise ValueError("covariance_map_enu_m2 must be a 3x3 matrix")
    rows = tuple(tuple(finite(item, "covariance_map_enu_m2") for item in row) for row in value)
    if any(abs(item) > MAX_COVARIANCE_M2 for row in rows for item in row):
        raise ValueError("covariance_map_enu_m2 exceeds the physical envelope")
    matrix = np.asarray(rows, dtype=float)
    try:
        eigenvalues = np.linalg.eigvalsh(matrix)
    except np.linalg.LinAlgError as error:
        raise ValueError("covariance_map_enu_m2 must be positive definite") from error
    if not np.allclose(matrix, matrix.T, rtol=0.0, atol=1e-12) or eigenvalues.min() <= 0:
        raise ValueError("covariance_map_enu_m2 must be symmetric positive definite")
    return rows


def position_uncertainty_p95_m(value: tuple[tuple[float, ...], ...]) -> float:
    try:
        maximum = float(np.linalg.eigvalsh(np.asarray(value, dtype=float)).max())
    except np.linalg.LinAlgError as error:
        raise ValueError("covariance cannot be decomposed") from error
    uncertainty = POSITION_P95_SIGMA_MULTIPLIER * float(np.sqrt(maximum))
    if not isfinite(uncertainty):
        raise ValueError("position uncertainty must be finite")
    return uncertainty


@dataclass(frozen=True, slots=True)
class ClockMapping:
    """One measured affine mapping from a capture clock to relay Unix epoch ms."""

    capture_clock_id: str
    relay_clock_id: str
    capture_reference_s: float
    relay_reference_ms: int
    milliseconds_per_capture_second: float
    max_error_ms: int
    measured: bool

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "capture_clock_id", identifier(self.capture_clock_id, "capture_clock_id")
        )
        object.__setattr__(
            self, "relay_clock_id", identifier(self.relay_clock_id, "relay_clock_id")
        )
        capture_reference = finite(self.capture_reference_s, "capture_reference_s")
        if capture_reference < 0:
            raise ValueError("capture_reference_s must be non-negative")
        rate = finite(
            self.milliseconds_per_capture_second,
            "milliseconds_per_capture_second",
        )
        if not 0 < rate <= MAX_CLOCK_RATE_MS_PER_S:
            raise ValueError("milliseconds_per_capture_second is outside its bounded range")
        if self.measured is not True:
            raise ValueError("clock mapping must be measured")
        object.__setattr__(self, "capture_reference_s", capture_reference)
        object.__setattr__(
            self,
            "relay_reference_ms",
            nonnegative_int64(self.relay_reference_ms, "relay_reference_ms"),
        )
        object.__setattr__(
            self,
            "max_error_ms",
            bounded_nonnegative(self.max_error_ms, "max_error_ms", MAX_CLOCK_ERROR_MS),
        )
        object.__setattr__(self, "milliseconds_per_capture_second", rate)

    def to_relay_ms(self, capture_time_s: object) -> int:
        capture = finite(capture_time_s, "capture_time_s")
        if capture < 0:
            raise ValueError("capture_time_s must be non-negative")
        mapped = (
            self.relay_reference_ms
            + (capture - self.capture_reference_s) * self.milliseconds_per_capture_second
        )
        if not isfinite(mapped) or not 0 <= mapped <= MAX_INT64:
            raise ValueError("mapped relay timestamp is outside signed 64-bit range")
        return round(mapped)

    def age_to_relay_ms(self, age_s: float) -> int:
        mapped = age_s * self.milliseconds_per_capture_second
        if not isfinite(mapped) or not 0 <= mapped <= MAX_INT64:
            raise ValueError("mapped relay age is outside signed 64-bit range")
        return int(np.ceil(mapped))

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
        expected = {
            "capture_clock_id",
            "relay_clock_id",
            "capture_reference_s",
            "relay_reference_ms",
            "milliseconds_per_capture_second",
            "max_error_ms",
            "measured",
        }
        if not isinstance(raw, Mapping) or set(raw) != expected:
            raise ValueError("clock_mapping fields do not match the contract")
        return cls(
            capture_clock_id=raw["capture_clock_id"],  # type: ignore[arg-type]
            relay_clock_id=raw["relay_clock_id"],  # type: ignore[arg-type]
            capture_reference_s=raw["capture_reference_s"],  # type: ignore[arg-type]
            relay_reference_ms=raw["relay_reference_ms"],  # type: ignore[arg-type]
            milliseconds_per_capture_second=raw["milliseconds_per_capture_second"],  # type: ignore[arg-type]
            max_error_ms=raw["max_error_ms"],  # type: ignore[arg-type]
            measured=raw["measured"],  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class ControlLocalizationPins:
    """Static deployment identities; connection epochs remain registry-owned."""

    drone_id: int
    map_id: str
    geometry_id: str
    camera_calibration_id: str
    body_extrinsics_id: str
    source_ids: tuple[str, ...]
    clock_mapping: ClockMapping

    def __post_init__(self) -> None:
        object.__setattr__(self, "drone_id", positive_int32(self.drone_id, "drone_id"))
        for name in (
            "map_id",
            "geometry_id",
            "camera_calibration_id",
            "body_extrinsics_id",
        ):
            object.__setattr__(self, name, identifier(getattr(self, name), name))
        object.__setattr__(self, "source_ids", source_ids(self.source_ids, allow_empty=False))
        if not isinstance(self.clock_mapping, ClockMapping):
            raise ValueError("clock_mapping must be a measured ClockMapping")


@dataclass(frozen=True, slots=True)
class ControlLocalizationWire:
    """Exact producer body authenticated by ``ControlLocalizationFrame``."""

    drone_id: int
    connection_epoch: int
    map_id: str
    geometry_id: str
    camera_calibration_id: str
    body_extrinsics_id: str
    capture_clock_id: str
    evaluated_at_s: float
    position_map_enu_m: tuple[float, float, float] | None
    covariance_map_enu_m2: tuple[tuple[float, ...], ...] | None
    fix_age_s: float | None
    velocity_age_s: float | None
    height_age_s: float | None
    confidence: str
    loss_age_s: float | None
    status: str
    control_eligible: bool
    flight_approved: bool
    reason: str
    source_ids: tuple[str, ...]
    clock_mapping: ClockMapping

    def __post_init__(self) -> None:
        object.__setattr__(self, "drone_id", positive_int32(self.drone_id, "drone_id"))
        object.__setattr__(
            self,
            "connection_epoch",
            positive_int32(self.connection_epoch, "connection_epoch"),
        )
        for name in (
            "map_id",
            "geometry_id",
            "camera_calibration_id",
            "body_extrinsics_id",
            "capture_clock_id",
        ):
            object.__setattr__(self, name, identifier(getattr(self, name), name))
        evaluated = finite(self.evaluated_at_s, "evaluated_at_s")
        if evaluated < 0:
            raise ValueError("evaluated_at_s must be non-negative")
        fix_age = optional_age(self.fix_age_s, "fix_age_s", evaluated)
        velocity_age = optional_age(self.velocity_age_s, "velocity_age_s", evaluated)
        height_age = optional_age(self.height_age_s, "height_age_s", evaluated)
        loss_age = optional_age(self.loss_age_s, "localization_loss_age_s", evaluated)
        projected_position = position(self.position_map_enu_m)
        projected_covariance = covariance(self.covariance_map_enu_m2)
        if (projected_position is None) != (projected_covariance is None):
            raise ValueError("position and covariance must be present together")
        if self.confidence not in {"green", "amber", "red"}:
            raise ValueError("localization_confidence is invalid")
        if self.status not in {"ready", "hold", "land"}:
            raise ValueError("localization_status is invalid")
        if type(self.control_eligible) is not bool:
            raise ValueError("control_eligible must be a boolean")
        if self.control_eligible != (self.status == "ready"):
            raise ValueError("control_eligible must describe fuser health consistently")
        if self.flight_approved is not False:
            raise ValueError("control localization is not flight-approved")
        if self.status == "ready" and (
            self.confidence != "green"
            or projected_position is None
            or projected_covariance is None
            or fix_age is None
            or velocity_age is None
            or height_age is None
            or loss_age is not None
            or not self.source_ids
        ):
            raise ValueError("ready localization requires fresh, green fuser evidence")
        if not isinstance(self.clock_mapping, ClockMapping):
            raise ValueError("clock_mapping must be a measured ClockMapping")
        if self.clock_mapping.capture_clock_id != self.capture_clock_id:
            raise ValueError("clock mapping does not identify the capture clock")
        object.__setattr__(self, "evaluated_at_s", evaluated)
        object.__setattr__(self, "position_map_enu_m", projected_position)
        object.__setattr__(self, "covariance_map_enu_m2", projected_covariance)
        object.__setattr__(self, "fix_age_s", fix_age)
        object.__setattr__(self, "velocity_age_s", velocity_age)
        object.__setattr__(self, "height_age_s", height_age)
        object.__setattr__(self, "loss_age_s", loss_age)
        object.__setattr__(self, "reason", identifier(self.reason, "localization_reason"))
        object.__setattr__(self, "source_ids", source_ids(self.source_ids, allow_empty=True))

    @property
    def fix_capture_time_s(self) -> float | None:
        if self.fix_age_s is None:
            return None
        return self.evaluated_at_s - self.fix_age_s

    def to_mapping(self) -> dict[str, object]:
        return {
            "drone_id": self.drone_id,
            "connection_epoch": self.connection_epoch,
            "map_id": self.map_id,
            "geometry_id": self.geometry_id,
            "camera_calibration_id": self.camera_calibration_id,
            "body_extrinsics_id": self.body_extrinsics_id,
            "capture_clock_id": self.capture_clock_id,
            "evaluated_at_s": self.evaluated_at_s,
            "position_map_enu_m": self.position_map_enu_m,
            "covariance_map_enu_m2": self.covariance_map_enu_m2,
            "fix_age_s": self.fix_age_s,
            "velocity_age_s": self.velocity_age_s,
            "height_age_s": self.height_age_s,
            "localization_confidence": self.confidence,
            "localization_loss_age_s": self.loss_age_s,
            "localization_status": self.status,
            "control_eligible": self.control_eligible,
            "flight_approved": self.flight_approved,
            "localization_reason": self.reason,
            "source_ids": self.source_ids,
            "clock_mapping": self.clock_mapping.to_mapping(),
        }

    @classmethod
    def from_mapping(cls, raw: object) -> ControlLocalizationWire:
        if not isinstance(raw, Mapping) or set(raw) != LOCALIZATION_BODY_FIELDS:
            raise ValueError("control localization fields do not match the contract")
        return cls(
            drone_id=raw["drone_id"],  # type: ignore[arg-type]
            connection_epoch=raw["connection_epoch"],  # type: ignore[arg-type]
            map_id=raw["map_id"],  # type: ignore[arg-type]
            geometry_id=raw["geometry_id"],  # type: ignore[arg-type]
            camera_calibration_id=raw["camera_calibration_id"],  # type: ignore[arg-type]
            body_extrinsics_id=raw["body_extrinsics_id"],  # type: ignore[arg-type]
            capture_clock_id=raw["capture_clock_id"],  # type: ignore[arg-type]
            evaluated_at_s=raw["evaluated_at_s"],  # type: ignore[arg-type]
            position_map_enu_m=raw["position_map_enu_m"],  # type: ignore[arg-type]
            covariance_map_enu_m2=raw["covariance_map_enu_m2"],  # type: ignore[arg-type]
            fix_age_s=raw["fix_age_s"],  # type: ignore[arg-type]
            velocity_age_s=raw["velocity_age_s"],  # type: ignore[arg-type]
            height_age_s=raw["height_age_s"],  # type: ignore[arg-type]
            confidence=raw["localization_confidence"],  # type: ignore[arg-type]
            loss_age_s=raw["localization_loss_age_s"],  # type: ignore[arg-type]
            status=raw["localization_status"],  # type: ignore[arg-type]
            control_eligible=raw["control_eligible"],  # type: ignore[arg-type]
            flight_approved=raw["flight_approved"],  # type: ignore[arg-type]
            reason=raw["localization_reason"],  # type: ignore[arg-type]
            source_ids=raw["source_ids"],  # type: ignore[arg-type]
            clock_mapping=ClockMapping.from_mapping(raw["clock_mapping"]),
        )


def to_wire_payload(
    snapshot: LocalizationSnapshot,
    clock_mapping: ClockMapping,
) -> dict[str, object]:
    """Encode a fuser snapshot while preserving its explicit non-approval."""
    return ControlLocalizationWire(
        drone_id=snapshot.drone_id,
        connection_epoch=snapshot.connection_epoch,
        map_id=snapshot.map_id,
        geometry_id=snapshot.geometry_id,
        camera_calibration_id=snapshot.camera_calibration_id,
        body_extrinsics_id=snapshot.body_extrinsics_id,
        capture_clock_id=snapshot.capture_clock_id,
        evaluated_at_s=snapshot.evaluated_at_s,
        position_map_enu_m=snapshot.position_map_enu_m,
        covariance_map_enu_m2=snapshot.covariance_map_enu_m2,
        fix_age_s=snapshot.fix_age_s,
        velocity_age_s=snapshot.velocity_age_s,
        height_age_s=snapshot.height_age_s,
        confidence=snapshot.confidence,
        loss_age_s=snapshot.loss_age_s,
        status=snapshot.status,
        control_eligible=snapshot.control_eligible,
        flight_approved=False,
        reason=snapshot.reason,
        source_ids=snapshot.source_ids,
        clock_mapping=clock_mapping,
    ).to_mapping()


@dataclass(frozen=True, slots=True)
class ControlPose:
    """Relay-authored integer payload; this v1 contract is diagnostic only."""

    t: int
    event_id: str
    session: str
    drone_id: int
    connection_epoch: int
    map_id: str
    geometry_id: str
    camera_calibration_id: str
    body_extrinsics_id: str
    pose_time_ms: int
    fix_time_ms: int
    x_mm: int
    y_mm: int
    z_mm: int
    position_frame: str
    position_uncertainty_mm: int
    status: str
    flight_approved: bool = False

    def __post_init__(self) -> None:
        nonnegative_int64(self.t, "t")
        positive_int32(self.drone_id, "drone_id")
        positive_int32(self.connection_epoch, "connection_epoch")
        for name in (
            "event_id",
            "map_id",
            "geometry_id",
            "camera_calibration_id",
            "body_extrinsics_id",
        ):
            identifier(getattr(self, name), name)
        session_identifier(self.session)
        if self.position_frame != "map_enu":
            raise ValueError("control pose position_frame must be map_enu")
        pose_time = nonnegative_int64(self.pose_time_ms, "pose_time_ms")
        fix_time = nonnegative_int64(self.fix_time_ms, "fix_time_ms")
        if not self.t >= pose_time >= fix_time:
            raise ValueError("control-pose times must satisfy t >= pose_time_ms >= fix_time_ms")
        for name in ("x_mm", "y_mm", "z_mm"):
            value = getattr(self, name)
            if type(value) is not int or not -MAX_COORDINATE_MM <= value <= MAX_COORDINATE_MM:
                raise ValueError(f"{name} exceeds the bounded control-pose envelope")
        bounded_nonnegative(
            self.position_uncertainty_mm,
            "position_uncertainty_mm",
            MAX_POSITION_UNCERTAINTY_MM,
        )
        if self.status not in {"ready", "hold", "land"}:
            raise ValueError("control-pose status is invalid")
        if self.flight_approved is not False:
            raise ValueError("control pose is diagnostic-only")

    def unsigned_event(self) -> dict[str, object]:
        return {
            "v": 1,
            "t": self.t,
            "type": "control_pose",
            "event_id": self.event_id,
            "session": self.session,
            "drone_id": self.drone_id,
            "connection_epoch": self.connection_epoch,
            "map_id": self.map_id,
            "geometry_id": self.geometry_id,
            "camera_calibration_id": self.camera_calibration_id,
            "body_extrinsics_id": self.body_extrinsics_id,
            "pose_time_ms": self.pose_time_ms,
            "fix_time_ms": self.fix_time_ms,
            "x_mm": self.x_mm,
            "y_mm": self.y_mm,
            "z_mm": self.z_mm,
            "position_frame": self.position_frame,
            "position_uncertainty_mm": self.position_uncertainty_mm,
            "status": self.status,
            "flight_approved": self.flight_approved,
        }
