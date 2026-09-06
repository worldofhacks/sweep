"""Shared bounded replay for constant-velocity position measurement filters.

This module owns numerical prediction, measurement updates, capture-time ordering,
and bounded checkpoint pruning. It deliberately owns no source trust, flight policy,
or control-eligibility decision; callers must enforce those at their boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Literal

import numpy as np

_MeasurementKind = Literal["tag", "velocity", "height"]
_Decision = Literal["accepted", "rejected", "pending"]
_Admission = Literal[
    "duplicate_event",
    "duplicate_observation",
    "capture_time_invalid",
    "capture_time_too_old",
]
_KIND_ORDER: dict[_MeasurementKind, int] = {"tag": 0, "velocity": 1, "height": 2}
_INNOVATION_LIMIT = {"tag": 16.27, "velocity": 16.27, "height": 10.83}


def _immutable_array(value: object, shape: tuple[int, ...], name: str) -> np.ndarray:
    try:
        array = np.array(value, dtype=float, copy=True)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be a finite {shape} array") from error
    if array.shape != shape or not np.isfinite(array).all():
        raise ValueError(f"{name} must be a finite {shape} array")
    array.flags.writeable = False
    return array


@dataclass(frozen=True, slots=True)
class _ReplayMeasurement:
    event_id: str
    timestamp: float
    kind: _MeasurementKind
    value: np.ndarray
    covariance: np.ndarray

    def __post_init__(self) -> None:
        if type(self.event_id) is not str or not self.event_id:
            raise ValueError("replay event_id must be nonempty text")
        if (
            type(self.timestamp) not in (int, float)
            or not isfinite(self.timestamp)
            or self.timestamp < 0
        ):
            raise ValueError("replay timestamp must be finite and nonnegative")
        if self.kind not in _KIND_ORDER:
            raise ValueError("unsupported replay measurement kind")
        size = 1 if self.kind == "height" else 3
        value = _immutable_array(self.value, (size,), "measurement value")
        covariance = _immutable_array(self.covariance, (size, size), "measurement covariance")
        if not np.allclose(covariance, covariance.T) or np.linalg.eigvalsh(covariance).min() <= 0:
            raise ValueError("measurement covariance must be positive definite")
        object.__setattr__(self, "timestamp", float(self.timestamp))
        object.__setattr__(self, "value", value)
        object.__setattr__(self, "covariance", covariance)


@dataclass(frozen=True, slots=True)
class _ReplayResult:
    vector: np.ndarray | None
    covariance: np.ndarray | None
    last_accepted: dict[_MeasurementKind, float | None]
    decisions: dict[str, _Decision]
    latest_decisions: dict[_MeasurementKind, tuple[float, _Decision] | None]
    retained_event_ids: tuple[str, ...]
    admission: _Admission | _Decision | None = None


class _ConstantVelocityReplay:
    """Bounded six-state Kalman replay with deterministic tag-first initialization."""

    def __init__(
        self,
        *,
        horizon_s: float,
        max_events: int,
        acceleration_variance_m2ps3: float,
        initial_velocity_variance_m2ps2: float,
    ) -> None:
        numbers = (
            horizon_s,
            acceleration_variance_m2ps3,
            initial_velocity_variance_m2ps2,
        )
        if (
            any(type(value) not in (int, float) for value in numbers)
            or not all(isfinite(value) and value > 0 for value in numbers)
            or type(max_events) is not int
            or not 1 <= max_events <= 10_000
        ):
            raise ValueError("replay limits and variances must be positive and bounded")
        self.horizon_s = float(horizon_s)
        self.max_events = max_events
        self.acceleration_variance_m2ps3 = float(acceleration_variance_m2ps3)
        self.initial_velocity_variance_m2ps2 = float(initial_velocity_variance_m2ps2)
        self._events: dict[str, _ReplayMeasurement] = {}
        self._checkpoint_time = 0.0
        self._checkpoint_vector: np.ndarray | None = None
        self._checkpoint_covariance: np.ndarray | None = None
        self._checkpoint_last: dict[_MeasurementKind, float | None] = {
            "tag": None,
            "velocity": None,
            "height": None,
        }
        self._closed_through: float | None = None
        self._now = 0.0

    def add(self, measurement: _ReplayMeasurement, *, now: float) -> _ReplayResult:
        now = self._check_now(now)
        admission = self._admission(
            measurement.event_id, measurement.timestamp, measurement.kind, now
        )
        if admission is not None:
            return self._result(now, admission=admission)
        self._events[measurement.event_id] = measurement
        result = self._result(now)
        return _ReplayResult(
            result.vector,
            result.covariance,
            result.last_accepted,
            result.decisions,
            result.latest_decisions,
            result.retained_event_ids,
            result.decisions[measurement.event_id],
        )

    def at(self, now: float) -> _ReplayResult:
        return self._result(self._check_now(now))

    def preflight(
        self,
        event_id: str,
        timestamp: float,
        kind: _MeasurementKind,
        *,
        now: float,
    ) -> _Admission | None:
        """Check replay-window admission without retaining a measurement."""
        return self._admission(event_id, timestamp, kind, self._check_now(now))

    def _admission(
        self, event_id: str, timestamp: float, kind: _MeasurementKind, now: float
    ) -> _Admission | None:
        if event_id in self._events:
            return "duplicate_event"
        if timestamp > now:
            return "capture_time_invalid"
        if timestamp < now - self.horizon_s or (
            self._closed_through is not None and timestamp <= self._closed_through
        ):
            return "capture_time_too_old"
        if any(
            retained.timestamp == timestamp and retained.kind == kind
            for retained in self._events.values()
        ):
            return "duplicate_observation"
        return None

    def _check_now(self, now: object) -> float:
        if type(now) not in (int, float) or not isfinite(now) or now < self._now:
            raise ValueError("replay time must be finite, nonnegative, and monotonic")
        self._now = float(now)
        return self._now

    def _result(self, now: float, admission: _Admission | None = None) -> _ReplayResult:
        vector, covariance, last, decisions, latest = self._replay(now)
        return _ReplayResult(
            vector,
            covariance,
            last,
            decisions,
            latest,
            tuple(self._events),
            admission,
        )

    def _predict(
        self,
        vector: np.ndarray | None,
        covariance: np.ndarray | None,
        start: float,
        until: float,
    ) -> tuple[np.ndarray | None, np.ndarray | None]:
        if vector is None:
            return None, None
        dt = until - start
        if dt < 0:
            raise RuntimeError("replay order moved backwards")
        transition = np.eye(6)
        transition[:3, 3:] = np.eye(3) * dt
        process = self.acceleration_variance_m2ps3 * np.kron(
            np.array([[dt**3 / 3, dt**2 / 2], [dt**2 / 2, dt]]), np.eye(3)
        )
        return transition @ vector, transition @ covariance @ transition.T + process

    def _update(
        self,
        vector: np.ndarray | None,
        covariance: np.ndarray | None,
        event: _ReplayMeasurement,
    ) -> tuple[np.ndarray | None, np.ndarray | None, bool | None]:
        if event.kind == "tag" and vector is None:
            return (
                np.r_[event.value, np.zeros(3)],
                np.block(
                    [
                        [event.covariance, np.zeros((3, 3))],
                        [
                            np.zeros((3, 3)),
                            np.eye(3) * self.initial_velocity_variance_m2ps2,
                        ],
                    ]
                ),
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
        try:
            distance = innovation @ np.linalg.solve(innovation_covariance, innovation)
            gain = np.linalg.solve(innovation_covariance, selector @ covariance).T
        except np.linalg.LinAlgError:
            return vector, covariance, False
        if not isfinite(float(distance)) or distance > _INNOVATION_LIMIT[event.kind]:
            return vector, covariance, False
        residual = np.eye(6) - gain @ selector
        updated = vector + gain @ innovation
        updated_covariance = residual @ covariance @ residual.T + gain @ event.covariance @ gain.T
        return updated, (updated_covariance + updated_covariance.T) / 2, True

    def _replay(
        self, now: float
    ) -> tuple[
        np.ndarray | None,
        np.ndarray | None,
        dict[_MeasurementKind, float | None],
        dict[str, _Decision],
        dict[_MeasurementKind, tuple[float, _Decision] | None],
    ]:
        ordered = sorted(
            self._events.items(),
            key=lambda item: (item[1].timestamp, _KIND_ORDER[item[1].kind], item[0]),
        )
        vector, covariance = self._checkpoint_vector, self._checkpoint_covariance
        previous = self._checkpoint_time
        last = dict(self._checkpoint_last)
        states: list[
            tuple[
                float,
                np.ndarray | None,
                np.ndarray | None,
                dict[_MeasurementKind, float | None],
            ]
        ] = []
        decisions: dict[str, _Decision] = {}
        latest: dict[_MeasurementKind, tuple[float, _Decision] | None] = dict.fromkeys(_KIND_ORDER)
        for event_id, event in ordered:
            vector, covariance = self._predict(vector, covariance, previous, event.timestamp)
            vector, covariance, accepted = self._update(vector, covariance, event)
            if accepted:
                last[event.kind] = event.timestamp
            decisions[event_id] = (
                "accepted" if accepted else "pending" if accepted is None else "rejected"
            )
            latest[event.kind] = (event.timestamp, decisions[event_id])
            previous = event.timestamp
            states.append((event.timestamp, vector, covariance, dict(last)))
        vector, covariance = self._predict(vector, covariance, previous, now)
        self._prune(now, ordered, states)
        return vector, covariance, last, decisions, latest

    def _prune(
        self,
        now: float,
        ordered: list[tuple[str, _ReplayMeasurement]],
        states: list[
            tuple[
                float,
                np.ndarray | None,
                np.ndarray | None,
                dict[_MeasurementKind, float | None],
            ]
        ],
    ) -> None:
        cutoff = max(0.0, now - self.horizon_s)
        remove = max(0, len(ordered) - self.max_events)
        while remove < len(ordered) and ordered[remove][1].timestamp < cutoff:
            remove += 1
        if remove:
            boundary = states[remove - 1][0]
            while remove < len(ordered) and states[remove][0] == boundary:
                remove += 1
            _, vector, covariance, last = states[remove - 1]
            self._checkpoint_time = boundary
            self._checkpoint_vector = vector
            self._checkpoint_covariance = covariance
            self._checkpoint_last = last
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
