"""Observation-only webcam PnP filter built on the shared bounded replay primitive."""

from __future__ import annotations

import math

import numpy as np

from perception._kalman_replay import (
    _ConstantVelocityReplay,
    _ReplayMeasurement,
    _ReplayResult,
)


class WebcamFilter:
    """Fuse estimated-capture-time tag positions without granting control authority."""

    def __init__(
        self,
        horizon_s=2,
        max_events=256,
        fix_variance_m2=0.01,
        acceleration_variance=1,
    ):
        if (
            any(
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not math.isfinite(value)
                or value <= 0
                for value in (horizon_s, fix_variance_m2, acceleration_variance)
            )
            or isinstance(max_events, bool)
            or not isinstance(max_events, int)
            or not 1 <= max_events <= 10_000
        ):
            raise ValueError("filter limits and variances must be positive and bounded")
        self.horizon_s = float(horizon_s)
        self.max_events = max_events
        self.fix_variance_m2 = float(fix_variance_m2)
        self.acceleration_variance = float(acceleration_variance)
        self._replay = _ConstantVelocityReplay(
            horizon_s=self.horizon_s,
            max_events=self.max_events,
            acceleration_variance_m2ps3=self.acceleration_variance,
            initial_velocity_variance_m2ps2=1.0,
        )

    def observe(self, event_id, capture_time, position, now):
        """Add a map-frame position; retained IDs are unique and old fixes fail closed."""
        if not isinstance(event_id, str) or not event_id:
            raise ValueError("invalid captured position")
        try:
            vector = np.array(position, dtype=float, copy=True)
        except (TypeError, ValueError) as error:
            raise ValueError("invalid captured position") from error
        if (
            isinstance(capture_time, bool)
            or not isinstance(capture_time, int | float)
            or not math.isfinite(capture_time)
            or capture_time < 0
            or isinstance(now, bool)
            or not isinstance(now, int | float)
            or not math.isfinite(now)
            or capture_time > now
            or vector.shape != (3,)
            or not np.isfinite(vector).all()
        ):
            raise ValueError("invalid captured position")
        replay = self._replay.add(
            _ReplayMeasurement(
                event_id,
                capture_time,
                "tag",
                vector,
                np.eye(3) * self.fix_variance_m2,
            ),
            now=now,
        )
        result = self._result(replay, now)
        result["observation_status"] = {
            "duplicate_event": "duplicate",
            "duplicate_observation": "duplicate",
            "capture_time_too_old": "too_old",
        }.get(replay.admission, replay.admission)
        return result

    def at(self, now):
        """Predict at monotonic arrival time; estimates never authorize flight."""
        return self._result(self._replay.at(now), now)

    @staticmethod
    def _result(replay: _ReplayResult, now: float) -> dict[str, object]:
        age = (
            None
            if replay.last_accepted["tag"] is None
            else float(now) - replay.last_accepted["tag"]
        )
        confidence = "red" if age is None or age >= 2 else "amber" if age >= 0.5 else "green"
        vector, covariance = replay.vector, replay.covariance
        finite = bool(
            vector is not None
            and covariance is not None
            and np.isfinite(vector).all()
            and np.isfinite(covariance).all()
        )
        return {
            "timestamp": float(now),
            "position_map_m": None if not finite else vector[:3].tolist(),
            "velocity_map_mps": None if not finite else vector[3:].tolist(),
            "covariance_m2": None if not finite else covariance[:3, :3].tolist(),
            "state_covariance": None if not finite else covariance.tolist(),
            "last_fix_capture_time": replay.last_accepted["tag"],
            "fix_age_s": age,
            "confidence": confidence,
            "accepted": finite and confidence == "green",
            "flight_approved": False,
            "retained_event_count": len(replay.retained_event_ids),
            "fix_decisions": {
                event_id: replay.decisions[event_id] for event_id in replay.retained_event_ids
            },
        }
