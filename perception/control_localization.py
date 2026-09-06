"""Fail-closed, bounded map-body localization for control consumers."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Literal

import numpy as np


def _finite(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not isfinite(value):
        raise ValueError(f"{name} must be finite")
    return float(value)


def _vector(value: object, name: str, size: int) -> tuple[float, ...]:
    array = np.asarray(value, dtype=float)
    if array.shape != (size,) or not np.isfinite(array).all():
        raise ValueError(f"{name} must contain {size} finite values")
    return tuple(float(item) for item in array)


def _identity(value: object, name: str, *, positive: bool = False) -> int:
    if type(value) is not int or (positive and value <= 0) or (not positive and value < 0):
        raise ValueError(f"{name} must be a valid integer identity")
    return value


def _verified(value: object, name: str) -> bool:
    if value is not True:
        raise ValueError(f"{name} must be true")
    return True


def _covariance(value: object) -> tuple[tuple[float, ...], ...]:
    array = np.asarray(value, dtype=float)
    if (
        array.shape != (3, 3)
        or not np.isfinite(array).all()
        or not np.allclose(array, array.T)
        or np.linalg.eigvalsh(array).min() <= 0
    ):
        raise ValueError("covariance must be positive definite 3x3 square meters")
    return tuple(tuple(float(item) for item in row) for row in array)


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
        matrix = np.asarray(self.matrix, dtype=float)
        if (
            not self.extrinsics_id
            or not self.source_id
            or matrix.shape != (4, 4)
            or not np.isfinite(matrix).all()
            or not np.allclose(matrix[3], [0, 0, 0, 1])
            or not np.allclose(matrix[:3, :3].T @ matrix[:3, :3], np.eye(3), atol=1e-6)
            or np.linalg.det(matrix[:3, :3]) <= 0
            or self.measured is not True
        ):
            raise ValueError("body extrinsics must be a measured rigid transform")
        capture = _finite(self.capture_time, "capture_time")
        gimbal = _finite(self.gimbal_time, "gimbal_time")
        attitude = _finite(self.attitude_time, "attitude_time")
        if gimbal != capture or attitude != capture:
            raise ValueError("gimbal and attitude transforms must be sampled at capture time")
        normalized_matrix = tuple(tuple(float(item) for item in row) for row in matrix)
        object.__setattr__(self, "matrix", normalized_matrix)
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
    production_evidence_verified: bool = False
    horizon_s: float = 2.0
    max_events: int = 256
    max_fix_age_s: float = 0.5
    max_velocity_age_s: float = 0.2
    max_height_age_s: float = 0.2
    land_after_fix_age_s: float = 2.0
    max_tag_variance_m2: float = 0.0625
    max_position_variance_m2: float = 0.0625

    def __post_init__(self) -> None:
        if (
            isinstance(self.drone_id, bool)
            or not isinstance(self.drone_id, int)
            or self.drone_id <= 0
            or isinstance(self.connection_epoch, bool)
            or not isinstance(self.connection_epoch, int)
            or self.connection_epoch < 0
            or not all(
                isinstance(value, str) and value
                for value in (
                    self.map_id,
                    self.geometry_id,
                    self.clock_id,
                    self.tag_source_id,
                    self.velocity_source_id,
                    self.height_source_id,
                    self.camera_calibration_id,
                    self.body_extrinsics_id,
                )
            )
            or isinstance(self.max_events, bool)
            or not isinstance(self.max_events, int)
            or self.max_events < 1
            or not isinstance(self.production_evidence_verified, bool)
        ):
            raise ValueError("control localization identity is invalid")
        limits = (
            self.horizon_s,
            self.max_fix_age_s,
            self.max_velocity_age_s,
            self.max_height_age_s,
            self.land_after_fix_age_s,
            self.max_tag_variance_m2,
            self.max_position_variance_m2,
        )
        if (
            not all(isfinite(value) and value > 0 for value in limits)
            or self.land_after_fix_age_s < self.max_fix_age_s
        ):
            raise ValueError("control localization timing limits are invalid")


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
        if (
            not self.event_id
            or not self.source_id
            or not self.camera_calibration_id
            or not self.source_verified
            or not self.timing_verified
        ):
            raise ValueError("tag fix requires verified source and timing evidence")
        _identity(self.drone_id, "drone_id", positive=True)
        _identity(self.connection_epoch, "connection_epoch")
        _finite(self.capture_time, "capture_time")
        position = _vector(self.position_map_enu_m, "position_map_enu_m", 3)
        covariance = _covariance(self.covariance_map_enu_m2)
        _verified(self.source_verified, "source_verified")
        _verified(self.timing_verified, "timing_verified")
        if not isinstance(self.extrinsics, BodyExtrinsics):
            raise ValueError("tag fix requires validated body extrinsics")
        object.__setattr__(self, "capture_time", float(self.capture_time))
        object.__setattr__(self, "position_map_enu_m", position)
        object.__setattr__(self, "covariance_map_enu_m2", covariance)


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
        if (
            not self.event_id
            or not self.source_id
            or not self.source_verified
            or not self.timing_verified
        ):
            raise ValueError("velocity requires verified source and timing evidence")
        _identity(self.drone_id, "drone_id", positive=True)
        _identity(self.connection_epoch, "connection_epoch")
        _finite(self.capture_time, "capture_time")
        velocity = _vector(self.velocity_map_enu_mps, "velocity_map_enu_mps", 3)
        covariance = _covariance(self.covariance_m2ps2)
        _verified(self.source_verified, "source_verified")
        _verified(self.timing_verified, "timing_verified")
        object.__setattr__(self, "capture_time", float(self.capture_time))
        object.__setattr__(self, "velocity_map_enu_mps", velocity)
        object.__setattr__(self, "covariance_m2ps2", covariance)


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
        if (
            not self.event_id
            or not self.source_id
            or not self.source_verified
            or not self.timing_verified
        ):
            raise ValueError("height requires verified source and timing evidence")
        _identity(self.drone_id, "drone_id", positive=True)
        _identity(self.connection_epoch, "connection_epoch")
        _finite(self.capture_time, "capture_time")
        height = _finite(self.height_map_enu_m, "height_map_enu_m")
        variance = _finite(self.variance_m2, "variance_m2")
        if variance <= 0:
            raise ValueError("height variance must be positive")
        _verified(self.source_verified, "source_verified")
        _verified(self.timing_verified, "timing_verified")
        object.__setattr__(self, "capture_time", float(self.capture_time))
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
    last_fix_capture_time_s: float | None
    fix_age_s: float | None
    velocity_age_s: float | None
    height_age_s: float | None
    status: Literal["ready", "hold", "land"]
    control_eligible: bool
    reason: str
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
            "last_fix_capture_time_s": self.last_fix_capture_time_s,
            "fix_age_s": self.fix_age_s,
            "velocity_age_s": self.velocity_age_s,
            "height_age_s": self.height_age_s,
            "localization_status": self.status,
            "control_eligible": self.control_eligible,
            "localization_reason": self.reason,
            "source_ids": self.source_ids,
            "camera_calibration_id": self.camera_calibration_id,
            "body_extrinsics_id": self.body_extrinsics_id,
        }


@dataclass(frozen=True, slots=True)
class _Event:
    timestamp: float
    kind: Literal["tag", "velocity", "height"]
    value: np.ndarray
    covariance: np.ndarray


class ControlLocalization:
    """A per-drone filter. It accepts only map-frame, source-provenanced measurements."""

    def __init__(self, config: ControlLocalizationConfig) -> None:
        self.config = config
        self._events: dict[str, _Event] = {}
        self._checkpoint_time = 0.0
        self._checkpoint_vector: np.ndarray | None = None
        self._checkpoint_covariance: np.ndarray | None = None
        self._checkpoint_last: dict[str, float | None] = {
            "tag": None,
            "velocity": None,
            "height": None,
        }
        self._closed_through: float | None = None
        self._now = 0.0
        self._started_at: float | None = None
        self._last_rejection: str | None = None

    def ingest_tag_fix(self, fix: TagFix, now: float) -> ControlLocalizationSnapshot:
        reason = self._tag_reason(fix)
        if reason is not None:
            return self._reject(reason, now)
        return self._ingest(
            fix.event_id,
            _Event(
                fix.capture_time,
                "tag",
                np.array(fix.position_map_enu_m, dtype=float, copy=True),
                np.array(fix.covariance_map_enu_m2, dtype=float, copy=True),
            ),
            now,
        )

    def ingest_velocity(
        self, observation: VelocityObservation, now: float
    ) -> ControlLocalizationSnapshot:
        reason = self._observation_reason(observation, self.config.velocity_source_id)
        if reason is not None:
            return self._reject(reason, now)
        return self._ingest(
            observation.event_id,
            _Event(
                observation.capture_time,
                "velocity",
                np.array(observation.velocity_map_enu_mps, dtype=float, copy=True),
                np.array(observation.covariance_m2ps2, dtype=float, copy=True),
            ),
            now,
        )

    def ingest_height(
        self, observation: HeightObservation, now: float
    ) -> ControlLocalizationSnapshot:
        reason = self._observation_reason(observation, self.config.height_source_id)
        if reason is not None:
            return self._reject(reason, now)
        return self._ingest(
            observation.event_id,
            _Event(
                observation.capture_time,
                "height",
                np.array([observation.height_map_enu_m]),
                np.array([[observation.variance_m2]]),
            ),
            now,
        )

    def snapshot(self, now: float) -> ControlLocalizationSnapshot:
        self._check_now(now)
        vector, covariance, last, _ = self._replay(now, prune=True)
        return self._snapshot(now, vector, covariance, last)

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
        if self._largest_variance(fix.covariance_map_enu_m2) > self.config.max_tag_variance_m2:
            return "tag_uncertainty_excessive"
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

    def _reject(self, reason: str, now: float) -> ControlLocalizationSnapshot:
        self._check_now(now)
        self._last_rejection = reason
        return self.snapshot(now)

    def _ingest(self, event_id: str, event: _Event, now: float) -> ControlLocalizationSnapshot:
        self._check_now(now)
        if event_id in self._events:
            return self._reject("duplicate_event", now)
        if any(self._same_measurement(event, existing) for existing in self._events.values()):
            return self._reject("duplicate_measurement", now)
        if event.timestamp > now or event.timestamp < 0:
            return self._reject("capture_time_invalid", now)
        if event.timestamp < now - self.config.horizon_s or (
            self._closed_through is not None and event.timestamp <= self._closed_through
        ):
            return self._reject("capture_time_too_old", now)
        self._events[event_id] = event
        self._last_rejection = None
        vector, covariance, last, decisions = self._replay(now, prune=True)
        if decisions.get(event_id) == "rejected":
            self._last_rejection = "innovation_rejected"
        return self._snapshot(now, vector, covariance, last)

    def _check_now(self, now: float) -> None:
        _finite(now, "evaluation time")
        if now < self._now:
            raise ValueError("evaluation time must be monotonic")
        self._now = now
        if self._started_at is None:
            self._started_at = now

    @staticmethod
    def _same_measurement(left: _Event, right: _Event) -> bool:
        return (
            left.kind == right.kind
            and left.timestamp == right.timestamp
            and np.array_equal(left.value, right.value)
            and np.array_equal(left.covariance, right.covariance)
        )

    @staticmethod
    def _largest_variance(covariance: object) -> float:
        return float(np.linalg.eigvalsh(np.asarray(covariance, dtype=float)).max())

    @staticmethod
    def _event_sort_key(item: tuple[str, _Event]) -> tuple[object, ...]:
        _, event = item
        kind_order = {"tag": 0, "velocity": 1, "height": 2}
        return (
            event.timestamp,
            kind_order[event.kind],
            tuple(event.value.flat),
            tuple(event.covariance.flat),
        )

    def _predict(
        self, vector: np.ndarray | None, covariance: np.ndarray | None, start: float, until: float
    ):
        if vector is None:
            return None, None
        dt = until - start
        transition = np.eye(6)
        transition[:3, 3:] = np.eye(3) * dt
        # A small acceleration model prevents stale velocity from becoming false certainty.
        process = 0.1 * np.kron(np.array([[dt**3 / 3, dt**2 / 2], [dt**2 / 2, dt]]), np.eye(3))
        return transition @ vector, transition @ covariance @ transition.T + process

    @staticmethod
    def _update(vector: np.ndarray | None, covariance: np.ndarray | None, event: _Event):
        if event.kind == "tag" and vector is None:
            return (
                np.r_[event.value, np.zeros(3)],
                np.block([[event.covariance, np.zeros((3, 3))], [np.zeros((3, 3)), np.eye(3)]]),
                True,
            )
        if vector is None:
            return vector, covariance, None
        if event.kind == "tag":
            selector = np.c_[np.eye(3), np.zeros((3, 3))]
        elif event.kind == "velocity":
            selector = np.c_[np.zeros((3, 3)), np.eye(3)]
        else:
            selector = np.array([[0, 0, 1, 0, 0, 0]], dtype=float)
        innovation = event.value - selector @ vector
        innovation_covariance = selector @ covariance @ selector.T + event.covariance
        if innovation @ np.linalg.solve(innovation_covariance, innovation) > 16.27:
            return vector, covariance, False
        gain = covariance @ selector.T @ np.linalg.inv(innovation_covariance)
        residual = np.eye(6) - gain @ selector
        updated = vector + gain @ innovation
        updated_covariance = residual @ covariance @ residual.T + gain @ event.covariance @ gain.T
        return updated, (updated_covariance + updated_covariance.T) / 2, True

    def _replay(self, now: float, *, prune: bool):
        ordered = sorted(self._events.items(), key=self._event_sort_key)
        vector, covariance = self._checkpoint_vector, self._checkpoint_covariance
        previous = self._checkpoint_time
        last = dict(self._checkpoint_last)
        states = []
        decisions: dict[str, str] = {}
        for event_id, event in ordered:
            vector, covariance = self._predict(vector, covariance, previous, event.timestamp)
            vector, covariance, accepted = self._update(vector, covariance, event)
            if accepted:
                last[event.kind] = event.timestamp
            decisions[event_id] = (
                "accepted" if accepted else "pending" if accepted is None else "rejected"
            )
            previous = event.timestamp
            states.append((event.timestamp, vector, covariance, dict(last)))
        vector, covariance = self._predict(vector, covariance, previous, now)
        if prune:
            cutoff = max(0.0, now - self.config.horizon_s)
            remove = max(0, len(ordered) - self.config.max_events)
            while remove < len(ordered) and ordered[remove][1].timestamp < cutoff:
                remove += 1
            if remove:
                boundary = states[remove - 1][0]
                while remove < len(ordered) and states[remove][0] == boundary:
                    remove += 1
                _, checkpoint_vector, checkpoint_covariance, checkpoint_last = states[remove - 1]
                self._checkpoint_time = boundary
                self._checkpoint_vector = checkpoint_vector
                self._checkpoint_covariance = checkpoint_covariance
                self._checkpoint_last = checkpoint_last
                self._closed_through = boundary
                for event_id, _ in ordered[:remove]:
                    del self._events[event_id]
            if cutoff > self._checkpoint_time:
                self._checkpoint_vector, self._checkpoint_covariance = self._predict(
                    self._checkpoint_vector,
                    self._checkpoint_covariance,
                    self._checkpoint_time,
                    cutoff,
                )
                self._checkpoint_time = cutoff
                self._closed_through = cutoff
        return vector, covariance, last, decisions

    def _snapshot(
        self,
        now: float,
        vector: np.ndarray | None,
        covariance: np.ndarray | None,
        last: dict[str, float | None],
    ) -> ControlLocalizationSnapshot:
        ages = {kind: None if stamp is None else now - stamp for kind, stamp in last.items()}
        status, reason = "hold", self._last_rejection or "tag_fix_missing"
        if not self.config.production_evidence_verified:
            reason = "production_evidence_unverified"
        elif ages["tag"] is not None and ages["tag"] >= self.config.land_after_fix_age_s:
            status, reason = "land", "tag_fix_lost"
        elif ages["tag"] is None:
            missing_for = now - self._started_at if self._started_at is not None else 0.0
            status, reason = (
                ("land", "tag_fix_missing")
                if missing_for >= self.config.land_after_fix_age_s
                else ("hold", reason)
            )
        elif self._last_rejection is not None:
            reason = self._last_rejection
        elif ages["tag"] > self.config.max_fix_age_s:
            reason = "tag_fix_stale"
        elif ages["velocity"] is None or ages["velocity"] > self.config.max_velocity_age_s:
            reason = "velocity_stale"
        elif ages["height"] is None or ages["height"] > self.config.max_height_age_s:
            reason = "height_stale"
        elif (
            covariance is None
            or self._largest_variance(covariance[:3, :3]) > self.config.max_position_variance_m2
        ):
            reason = "position_uncertainty_excessive"
        elif self._last_rejection is None:
            status, reason = "ready", "fresh_verified_measurements"
        return ControlLocalizationSnapshot(
            self.config.drone_id,
            self.config.connection_epoch,
            self.config.map_id,
            self.config.geometry_id,
            self.config.clock_id,
            now,
            None if vector is None else tuple(float(value) for value in vector[:3]),
            None if vector is None else tuple(float(value) for value in vector[3:]),
            None
            if covariance is None
            else tuple(tuple(float(value) for value in row) for row in covariance[:3, :3]),
            last["tag"],
            ages["tag"],
            ages["velocity"],
            ages["height"],
            status,
            status == "ready",
            reason,
            (
                self.config.tag_source_id,
                self.config.velocity_source_id,
                self.config.height_source_id,
            ),
            self.config.camera_calibration_id,
            self.config.body_extrinsics_id,
            len(self._events),
        )
