"""Fail-closed, bounded map-body localization for control consumers."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from numbers import Real
from typing import Literal

import numpy as np

from perception._kalman_replay import (
    _ConstantVelocityReplay,
    _ReplayMeasurement,
    _ReplayResult,
)

_MAX_IDENTIFIER_CHARS = 128
_GREEN_FIX_AGE_S = 0.5
_RED_FIX_AGE_S = 2.0
_LAND_AFTER_LOSS_S = 3.0
_STATE_CONTRADICTIONS = {
    "map_id_mismatch",
    "geometry_id_mismatch",
    "clock_id_mismatch",
    "extrinsics_mismatch",
    "camera_calibration_mismatch",
    "extrinsics_source_mismatch",
    "extrinsics_capture_time_mismatch",
}


def _identifier(value: object, name: str) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or not value.isprintable()
        or len(value) > _MAX_IDENTIFIER_CHARS
    ):
        raise ValueError(f"{name} must be canonical text of at most 128 characters")
    return value


def _positive_int(value: object, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _nonnegative_int(value: object, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")
    return value


def _verified(value: object, name: str) -> bool:
    if value is not True:
        raise ValueError(f"{name} must be verified")
    return True


def _canonical_observation(value: object, *extra_ids: str) -> None:
    """Copy the provenance fields shared by every admitted measurement."""
    object.__setattr__(value, "event_id", _identifier(value.event_id, "event_id"))
    object.__setattr__(value, "drone_id", _positive_int(value.drone_id, "drone_id"))
    object.__setattr__(
        value,
        "connection_epoch",
        _nonnegative_int(value.connection_epoch, "connection_epoch"),
    )
    for name in ("map_id", "geometry_id", "clock_id", "source_id", *extra_ids):
        object.__setattr__(value, name, _identifier(getattr(value, name), name))
    capture = _finite(value.capture_time, "capture_time")
    if capture < 0:
        raise ValueError("capture_time must be nonnegative")
    object.__setattr__(value, "capture_time", capture)
    object.__setattr__(value, "source_verified", _verified(value.source_verified, "source"))
    object.__setattr__(value, "timing_verified", _verified(value.timing_verified, "timing"))


def _finite(value: object, name: str) -> float:
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, Real) or not isfinite(value):
        raise ValueError(f"{name} must be finite")
    return float(value)


def _vector(value: object, name: str, size: int) -> tuple[float, ...]:
    if isinstance(value, (str, bytes)):
        raise ValueError(f"{name} must contain {size} finite values")
    try:
        items = tuple(value)  # type: ignore[arg-type]
    except TypeError as error:
        raise ValueError(f"{name} must contain {size} finite values") from error
    if len(items) != size:
        raise ValueError(f"{name} must contain {size} finite values")
    return tuple(_finite(item, name) for item in items)


def _matrix(value: object, name: str, rows: int, columns: int) -> tuple[tuple[float, ...], ...]:
    if isinstance(value, (str, bytes)):
        raise ValueError(f"{name} must be a {rows}x{columns} finite matrix")
    try:
        items = tuple(value)  # type: ignore[arg-type]
    except TypeError as error:
        raise ValueError(f"{name} must be a {rows}x{columns} finite matrix") from error
    if len(items) != rows:
        raise ValueError(f"{name} must be a {rows}x{columns} finite matrix")
    try:
        return tuple(_vector(row, name, columns) for row in items)
    except ValueError as error:
        raise ValueError(f"{name} must be a {rows}x{columns} finite matrix") from error


def _covariance(value: object) -> tuple[tuple[float, ...], ...]:
    result = _matrix(value, "covariance", 3, 3)
    array = np.asarray(result)
    try:
        eigenvalues = np.linalg.eigvalsh(array)
    except np.linalg.LinAlgError as error:
        raise ValueError("covariance must be positive definite 3x3 square meters") from error
    if not np.allclose(array, array.T) or eigenvalues.min() <= 0:
        raise ValueError("covariance must be positive definite 3x3 square meters")
    return result


def _bounds(value: object, name: str, size: int) -> tuple[tuple[float, float], ...]:
    result = _matrix(value, name, size, 2)
    if any(lower >= upper for lower, upper in result):
        raise ValueError(f"{name} lower limits must be below upper limits")
    return result  # type: ignore[return-value]


def _inside(value: tuple[float, ...], bounds: tuple[tuple[float, float], ...]) -> bool:
    return all(lower <= item <= upper for item, (lower, upper) in zip(value, bounds, strict=True))


@dataclass(frozen=True, slots=True)
class BodyExtrinsics:
    extrinsics_id: str
    source_id: str
    matrix: tuple[tuple[float, ...], ...]
    capture_time: float
    gimbal_time: float
    attitude_time: float
    measured: bool

    def __post_init__(self) -> None:
        extrinsics_id = _identifier(self.extrinsics_id, "extrinsics_id")
        source_id = _identifier(self.source_id, "source_id")
        rigid = _matrix(self.matrix, "body extrinsics", 4, 4)
        matrix = np.asarray(rigid)
        capture = _finite(self.capture_time, "capture_time")
        gimbal = _finite(self.gimbal_time, "gimbal_time")
        attitude = _finite(self.attitude_time, "attitude_time")
        if (
            capture < 0
            or not np.allclose(matrix[3], [0, 0, 0, 1])
            or not np.allclose(matrix[:3, :3].T @ matrix[:3, :3], np.eye(3), atol=1e-6)
            or not np.isclose(np.linalg.det(matrix[:3, :3]), 1.0, atol=1e-6)
        ):
            raise ValueError("body extrinsics must be a measured rigid transform")
        if self.measured is not True:
            raise ValueError("body extrinsics must be measured")
        if gimbal != capture or attitude != capture:
            raise ValueError("gimbal and attitude transforms must be sampled at capture time")
        object.__setattr__(self, "extrinsics_id", extrinsics_id)
        object.__setattr__(self, "source_id", source_id)
        object.__setattr__(self, "matrix", rigid)
        object.__setattr__(self, "capture_time", capture)
        object.__setattr__(self, "gimbal_time", gimbal)
        object.__setattr__(self, "attitude_time", attitude)


@dataclass(frozen=True, slots=True)
class ControlLocalizationConfig:
    drone_id: int
    connection_epoch: int
    map_id: str
    geometry_id: str
    clock_id: str
    tag_source_id: str
    velocity_source_id: str
    height_source_id: str
    camera_calibration_id: str
    body_extrinsics_id: str
    position_bounds_map_enu_m: tuple[tuple[float, float], ...]
    height_bounds_map_enu_m: tuple[float, float]
    max_speed_mps: float
    position_variance_bounds_m2: tuple[float, float]
    velocity_variance_bounds_m2ps2: tuple[float, float]
    height_variance_bounds_m2: tuple[float, float]
    production_evidence_verified: bool = False
    horizon_s: float = 2.0
    max_events: int = 256
    max_velocity_age_s: float = 0.2
    max_height_age_s: float = 0.2
    acceleration_variance_m2ps3: float = 0.1
    initial_velocity_variance_m2ps2: float = 1.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "drone_id", _positive_int(self.drone_id, "drone_id"))
        object.__setattr__(
            self,
            "connection_epoch",
            _nonnegative_int(self.connection_epoch, "connection_epoch"),
        )
        for name in (
            "map_id",
            "geometry_id",
            "clock_id",
            "tag_source_id",
            "velocity_source_id",
            "height_source_id",
            "camera_calibration_id",
            "body_extrinsics_id",
        ):
            object.__setattr__(self, name, _identifier(getattr(self, name), name))
        position_bounds = _bounds(self.position_bounds_map_enu_m, "position_bounds_map_enu_m", 3)
        height_bounds = _bounds((self.height_bounds_map_enu_m,), "height_bounds_map_enu_m", 1)[0]
        if height_bounds[0] < position_bounds[2][0] or height_bounds[1] > position_bounds[2][1]:
            raise ValueError("height bounds must lie inside the position z bounds")
        object.__setattr__(self, "position_bounds_map_enu_m", position_bounds)
        object.__setattr__(self, "height_bounds_map_enu_m", height_bounds)
        variance_bounds = {
            name: _bounds((getattr(self, name),), name, 1)[0]
            for name in (
                "position_variance_bounds_m2",
                "velocity_variance_bounds_m2ps2",
                "height_variance_bounds_m2",
            )
        }
        for name, value in variance_bounds.items():
            if value[0] <= 0:
                raise ValueError("measurement variance lower bounds must be positive")
            object.__setattr__(self, name, value)
        if (
            type(self.max_events) is not int
            or not 1 <= self.max_events <= 10_000
            or not isinstance(self.production_evidence_verified, bool)
        ):
            raise ValueError("control localization limits are invalid")
        numeric_names = (
            "max_speed_mps",
            "horizon_s",
            "max_velocity_age_s",
            "max_height_age_s",
            "acceleration_variance_m2ps3",
            "initial_velocity_variance_m2ps2",
        )
        for name in numeric_names:
            value = _finite(getattr(self, name), name)
            if value <= 0:
                raise ValueError(
                    "control localization timing and uncertainty limits must be positive"
                )
            object.__setattr__(self, name, value)
        velocity_variance = self.velocity_variance_bounds_m2ps2
        if not velocity_variance[0] <= self.initial_velocity_variance_m2ps2 <= velocity_variance[1]:
            raise ValueError("initial velocity variance must lie inside configured bounds")


@dataclass(frozen=True, slots=True)
class TagFix:
    event_id: str
    drone_id: int
    connection_epoch: int
    map_id: str
    geometry_id: str
    clock_id: str
    capture_time: float
    position_map_enu_m: tuple[float, float, float]
    covariance_map_enu_m2: tuple[tuple[float, ...], ...]
    source_id: str
    camera_calibration_id: str
    source_verified: bool
    timing_verified: bool
    extrinsics: BodyExtrinsics

    def __post_init__(self) -> None:
        _canonical_observation(self, "camera_calibration_id")
        if not isinstance(self.extrinsics, BodyExtrinsics):
            raise ValueError("tag fix requires measured body extrinsics")
        object.__setattr__(
            self,
            "position_map_enu_m",
            _vector(self.position_map_enu_m, "position_map_enu_m", 3),
        )
        object.__setattr__(self, "covariance_map_enu_m2", _covariance(self.covariance_map_enu_m2))


@dataclass(frozen=True, slots=True)
class VelocityObservation:
    event_id: str
    drone_id: int
    connection_epoch: int
    map_id: str
    geometry_id: str
    clock_id: str
    capture_time: float
    velocity_map_enu_mps: tuple[float, float, float]
    covariance_m2ps2: tuple[tuple[float, ...], ...]
    source_id: str
    source_verified: bool
    timing_verified: bool

    def __post_init__(self) -> None:
        _canonical_observation(self)
        object.__setattr__(
            self,
            "velocity_map_enu_mps",
            _vector(self.velocity_map_enu_mps, "velocity_map_enu_mps", 3),
        )
        object.__setattr__(self, "covariance_m2ps2", _covariance(self.covariance_m2ps2))


@dataclass(frozen=True, slots=True)
class HeightObservation:
    event_id: str
    drone_id: int
    connection_epoch: int
    map_id: str
    geometry_id: str
    clock_id: str
    capture_time: float
    height_map_enu_m: float
    variance_m2: float
    source_id: str
    source_verified: bool
    timing_verified: bool

    def __post_init__(self) -> None:
        _canonical_observation(self)
        height = _finite(self.height_map_enu_m, "height_map_enu_m")
        variance = _finite(self.variance_m2, "variance_m2")
        if variance <= 0:
            raise ValueError("height variance must be positive")
        object.__setattr__(self, "height_map_enu_m", height)
        object.__setattr__(self, "variance_m2", variance)


@dataclass(frozen=True, slots=True)
class ControlLocalizationSnapshot:
    drone_id: int
    connection_epoch: int
    map_id: str
    geometry_id: str
    capture_clock_id: str
    evaluated_at_s: float
    position_map_enu_m: tuple[float, float, float] | None
    velocity_map_enu_mps: tuple[float, float, float] | None
    covariance_map_enu_m2: tuple[tuple[float, ...], ...] | None
    fix_age_s: float | None
    velocity_age_s: float | None
    height_age_s: float | None
    confidence: Literal["green", "amber", "red"]
    loss_age_s: float | None
    status: Literal["ready", "hold", "land"]
    control_eligible: bool
    reason: str
    last_rejection: str | None
    active_contradictions: tuple[str, ...]
    source_ids: tuple[str, ...]
    camera_calibration_id: str
    body_extrinsics_id: str
    retained_event_count: int

    def to_relay_state(self) -> dict[str, object]:
        return {
            "drone_id": self.drone_id,
            "connection_epoch": self.connection_epoch,
            "map_id": self.map_id,
            "geometry_id": self.geometry_id,
            "capture_clock_id": self.capture_clock_id,
            "evaluated_at_s": self.evaluated_at_s,
            "position_map_enu_m": self.position_map_enu_m,
            "velocity_map_enu_mps": self.velocity_map_enu_mps,
            "covariance_map_enu_m2": self.covariance_map_enu_m2,
            "fix_age_s": self.fix_age_s,
            "velocity_age_s": self.velocity_age_s,
            "height_age_s": self.height_age_s,
            "localization_confidence": self.confidence,
            "localization_loss_age_s": self.loss_age_s,
            "localization_status": self.status,
            "control_eligible": self.control_eligible,
            "flight_approved": False,
            "localization_reason": self.reason,
            "last_localization_rejection": self.last_rejection,
            "active_localization_contradictions": self.active_contradictions,
            "source_ids": self.source_ids,
            "camera_calibration_id": self.camera_calibration_id,
            "body_extrinsics_id": self.body_extrinsics_id,
        }


class ControlLocalization:
    """A per-drone filter. It accepts only map-frame, source-provenanced measurements."""

    def __init__(self, config: ControlLocalizationConfig) -> None:
        self.config = config
        self._replay = _ConstantVelocityReplay(
            horizon_s=config.horizon_s,
            max_events=config.max_events,
            acceleration_variance_m2ps3=config.acceleration_variance_m2ps3,
            initial_velocity_variance_m2ps2=config.initial_velocity_variance_m2ps2,
        )
        self._last_rejection: str | None = None
        self._contradictions: dict[str, tuple[float, str]] = {}
        self._loss_started_at: float | None = None

    def ingest_tag_fix(self, fix: TagFix, now: float) -> ControlLocalizationSnapshot:
        admission = self._replay.preflight(fix.event_id, fix.capture_time, "tag", now=now)
        if admission is not None:
            return self._reject(admission, now, kind="tag")
        reason = self._tag_reason(fix)
        if reason is not None:
            return self._reject(
                reason,
                now,
                kind="tag",
                contradiction_time=fix.capture_time if reason in _STATE_CONTRADICTIONS else None,
            )
        reason = self._tag_measurement_reason(fix)
        if reason is not None:
            return self._reject(reason, now, kind="tag", contradiction_time=fix.capture_time)
        return self._ingest(
            _ReplayMeasurement(
                fix.event_id,
                fix.capture_time,
                "tag",
                np.asarray(fix.position_map_enu_m),
                np.asarray(fix.covariance_map_enu_m2),
            ),
            now,
        )

    def ingest_velocity(
        self, observation: VelocityObservation, now: float
    ) -> ControlLocalizationSnapshot:
        admission = self._replay.preflight(
            observation.event_id, observation.capture_time, "velocity", now=now
        )
        if admission is not None:
            return self._reject(admission, now, kind="velocity")
        reason = self._observation_reason(observation, self.config.velocity_source_id)
        if reason is not None:
            return self._reject(
                reason,
                now,
                kind="velocity",
                contradiction_time=(
                    observation.capture_time if reason in _STATE_CONTRADICTIONS else None
                ),
            )
        reason = self._velocity_measurement_reason(observation)
        if reason is not None:
            return self._reject(
                reason, now, kind="velocity", contradiction_time=observation.capture_time
            )
        return self._ingest(
            _ReplayMeasurement(
                observation.event_id,
                observation.capture_time,
                "velocity",
                np.asarray(observation.velocity_map_enu_mps),
                np.asarray(observation.covariance_m2ps2),
            ),
            now,
        )

    def ingest_height(
        self, observation: HeightObservation, now: float
    ) -> ControlLocalizationSnapshot:
        admission = self._replay.preflight(
            observation.event_id, observation.capture_time, "height", now=now
        )
        if admission is not None:
            return self._reject(admission, now, kind="height")
        reason = self._observation_reason(observation, self.config.height_source_id)
        if reason is not None:
            return self._reject(
                reason,
                now,
                kind="height",
                contradiction_time=(
                    observation.capture_time if reason in _STATE_CONTRADICTIONS else None
                ),
            )
        reason = self._height_measurement_reason(observation)
        if reason is not None:
            return self._reject(
                reason, now, kind="height", contradiction_time=observation.capture_time
            )
        return self._ingest(
            _ReplayMeasurement(
                observation.event_id,
                observation.capture_time,
                "height",
                np.array([observation.height_map_enu_m]),
                np.array([[observation.variance_m2]]),
            ),
            now,
        )

    def snapshot(self, now: float) -> ControlLocalizationSnapshot:
        return self._snapshot(now, self._replay.at(now))

    def _tag_reason(self, fix: TagFix) -> str | None:
        reason = self._observation_reason(fix, self.config.tag_source_id)
        if reason is not None:
            return reason
        if fix.extrinsics.extrinsics_id != self.config.body_extrinsics_id:
            return "extrinsics_mismatch"
        if fix.camera_calibration_id != self.config.camera_calibration_id:
            return "camera_calibration_mismatch"
        if fix.extrinsics.source_id != fix.source_id:
            return "extrinsics_source_mismatch"
        if fix.extrinsics.capture_time != fix.capture_time:
            return "extrinsics_capture_time_mismatch"
        return None

    def _tag_measurement_reason(self, fix: TagFix) -> str | None:
        if not _inside(fix.position_map_enu_m, self.config.position_bounds_map_enu_m):
            return "position_out_of_bounds"
        lower, upper = self.config.position_variance_bounds_m2
        eigenvalues = np.linalg.eigvalsh(np.asarray(fix.covariance_map_enu_m2))
        if eigenvalues.min() < lower or eigenvalues.max() > upper:
            return "position_uncertainty_out_of_bounds"
        return None

    def _velocity_measurement_reason(self, observation: VelocityObservation) -> str | None:
        if np.linalg.norm(observation.velocity_map_enu_mps) > self.config.max_speed_mps:
            return "velocity_out_of_bounds"
        lower, upper = self.config.velocity_variance_bounds_m2ps2
        eigenvalues = np.linalg.eigvalsh(np.asarray(observation.covariance_m2ps2))
        if eigenvalues.min() < lower or eigenvalues.max() > upper:
            return "velocity_uncertainty_out_of_bounds"
        return None

    def _height_measurement_reason(self, observation: HeightObservation) -> str | None:
        if not _inside((observation.height_map_enu_m,), (self.config.height_bounds_map_enu_m,)):
            return "height_out_of_bounds"
        lower, upper = self.config.height_variance_bounds_m2
        if not lower <= observation.variance_m2 <= upper:
            return "height_uncertainty_out_of_bounds"
        return None

    def _observation_reason(self, observation: object, source_id: str) -> str | None:
        for name, expected in (
            ("drone_id", self.config.drone_id),
            ("connection_epoch", self.config.connection_epoch),
            ("map_id", self.config.map_id),
            ("geometry_id", self.config.geometry_id),
            ("clock_id", self.config.clock_id),
            ("source_id", source_id),
        ):
            if getattr(observation, name) != expected:
                return f"{name}_mismatch"
        return None

    def _reject(
        self,
        reason: str,
        now: float,
        *,
        kind: Literal["tag", "velocity", "height"],
        contradiction_time: float | None = None,
    ) -> ControlLocalizationSnapshot:
        replay = self._replay.at(now)
        self._last_rejection = reason
        current = self._contradictions.get(kind)
        if contradiction_time is not None and (current is None or contradiction_time >= current[0]):
            self._contradictions[kind] = (contradiction_time, reason)
        return self._snapshot(now, replay)

    def _ingest(self, event: _ReplayMeasurement, now: float) -> ControlLocalizationSnapshot:
        result = self._replay.add(event, now=now)
        if result.admission in {
            "duplicate_event",
            "duplicate_observation",
            "capture_time_invalid",
            "capture_time_too_old",
        }:
            self._last_rejection = result.admission
        elif result.admission == "rejected":
            self._last_rejection = "innovation_rejected"
            current = self._contradictions.get(event.kind)
            if current is None or event.timestamp >= current[0]:
                self._contradictions[event.kind] = (event.timestamp, "innovation_rejected")
        elif result.admission == "accepted":
            self._last_rejection = None
            current = self._contradictions.get(event.kind)
            if current is not None and event.timestamp >= current[0]:
                self._contradictions.pop(event.kind)
        return self._snapshot(now, result)

    def _reconcile_innovation_rejections(self, replay: _ReplayResult) -> None:
        for kind, latest in replay.latest_decisions.items():
            if latest is None:
                continue
            timestamp, decision = latest
            current = self._contradictions.get(kind)
            if decision == "rejected":
                if current is None or timestamp >= current[0]:
                    self._contradictions[kind] = (timestamp, "innovation_rejected")
            elif decision == "accepted" and current is not None and timestamp >= current[0]:
                self._contradictions.pop(kind)

    def _snapshot(
        self,
        now: float,
        replay: _ReplayResult,
    ) -> ControlLocalizationSnapshot:
        self._reconcile_innovation_rejections(replay)
        vector, covariance, last = replay.vector, replay.covariance, replay.last_accepted
        ages = {kind: None if stamp is None else now - stamp for kind, stamp in last.items()}
        fix_age = ages["tag"]
        confidence: Literal["green", "amber", "red"]
        if fix_age is None or fix_age >= _RED_FIX_AGE_S:
            confidence = "red"
        elif fix_age >= _GREEN_FIX_AGE_S:
            confidence = "amber"
        else:
            confidence = "green"
        if confidence != "green":
            inferred_start = now if last["tag"] is None else last["tag"] + _GREEN_FIX_AGE_S
            if self._loss_started_at is None:
                self._loss_started_at = inferred_start
            loss_age = max(0.0, now - self._loss_started_at)
        else:
            self._loss_started_at = None
            loss_age = None

        state_reason = self._state_reason(vector, covariance)
        status, reason = "hold", "tag_fix_missing"
        if loss_age is not None and loss_age >= _LAND_AFTER_LOSS_S:
            status, reason = "land", "tag_fix_lost"
        elif not self.config.production_evidence_verified:
            reason = "production_evidence_unverified"
        elif self._contradictions:
            reason = max(
                (timestamp, kind, reason)
                for kind, (timestamp, reason) in self._contradictions.items()
            )[2]
        elif fix_age is None:
            reason = "tag_fix_missing"
        elif confidence != "green":
            reason = "tag_fix_stale"
        elif ages["velocity"] is None or ages["velocity"] > self.config.max_velocity_age_s:
            reason = "velocity_stale"
        elif ages["height"] is None or ages["height"] > self.config.max_height_age_s:
            reason = "height_stale"
        elif state_reason is not None:
            reason = state_reason
        else:
            status, reason = "ready", "fresh_verified_measurements"

        state_is_finite = (
            vector is not None
            and covariance is not None
            and np.isfinite(vector).all()
            and np.isfinite(covariance).all()
        )
        source_ids = tuple(
            source_id
            for kind, source_id in (
                ("tag", self.config.tag_source_id),
                ("velocity", self.config.velocity_source_id),
                ("height", self.config.height_source_id),
            )
            if last[kind] is not None
        )
        return ControlLocalizationSnapshot(
            self.config.drone_id,
            self.config.connection_epoch,
            self.config.map_id,
            self.config.geometry_id,
            self.config.clock_id,
            now,
            None if not state_is_finite else tuple(float(value) for value in vector[:3]),
            None if not state_is_finite else tuple(float(value) for value in vector[3:]),
            None
            if not state_is_finite
            else tuple(tuple(float(value) for value in row) for row in covariance[:3, :3]),
            fix_age,
            ages["velocity"],
            ages["height"],
            confidence,
            loss_age,
            status,
            status == "ready",
            reason,
            self._last_rejection,
            tuple(f"{kind}:{reason}" for kind, (_, reason) in sorted(self._contradictions.items())),
            source_ids,
            self.config.camera_calibration_id,
            self.config.body_extrinsics_id,
            len(replay.retained_event_ids),
        )

    def _state_reason(self, vector: np.ndarray | None, covariance: np.ndarray | None) -> str | None:
        if vector is None or covariance is None:
            return "state_uninitialized"
        if (
            vector.shape != (6,)
            or covariance.shape != (6, 6)
            or not np.isfinite(vector).all()
            or not np.isfinite(covariance).all()
        ):
            return "state_nonfinite"
        position = tuple(float(value) for value in vector[:3])
        if not _inside(position, self.config.position_bounds_map_enu_m):
            return "state_position_out_of_bounds"
        if not _inside((position[2],), (self.config.height_bounds_map_enu_m,)):
            return "state_height_out_of_bounds"
        if np.linalg.norm(vector[3:]) > self.config.max_speed_mps:
            return "state_velocity_out_of_bounds"
        position_covariance = covariance[:3, :3]
        try:
            eigenvalues = np.linalg.eigvalsh(position_covariance)
        except np.linalg.LinAlgError:
            return "state_position_uncertain"
        if not np.allclose(position_covariance, position_covariance.T) or (
            eigenvalues.min() < -1e-12
            or eigenvalues.max() > self.config.position_variance_bounds_m2[1]
        ):
            return "state_position_uncertain"
        return None
