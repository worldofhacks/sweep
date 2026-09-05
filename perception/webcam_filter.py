"""Constant-velocity Kalman filter, a linear EKF specialization, for webcam PnP fixes."""

from dataclasses import dataclass

import numpy as np


@dataclass
class _State:
    time: float
    vector: np.ndarray | None = None
    covariance: np.ndarray | None = None
    last_fix: float | None = None


class WebcamFilter:
    def __init__(self, horizon_s=2, max_events=256, fix_variance_m2=0.01, acceleration_variance=1):
        if (
            not np.isfinite([horizon_s, fix_variance_m2, acceleration_variance]).all()
            or min(horizon_s, fix_variance_m2, acceleration_variance) <= 0
            or isinstance(max_events, bool)
            or not isinstance(max_events, int)
            or max_events < 1
        ):
            raise ValueError("filter limits and variances must be positive")
        self.horizon_s = horizon_s
        self.max_events = max_events
        self.fix_variance_m2 = fix_variance_m2
        self.acceleration_variance = acceleration_variance
        self._events = {}
        self._checkpoint = _State(0.0)
        self._closed_through = None
        self._now = 0.0

    def observe(self, event_id, capture_time, position, now):
        """Add a map-frame position in meters; times are nonnegative seconds on one clock.

        IDs are unique within retained history; captures at/before a pruned checkpoint are too old.
        """
        vector = np.asarray(position, dtype=float)
        if (
            not isinstance(event_id, str)
            or not event_id
            or not np.isfinite(capture_time)
            or capture_time < 0
            or capture_time > now
            or vector.shape != (3,)
            or not np.isfinite(vector).all()
        ):
            raise ValueError("invalid captured position")
        result = self.at(now)
        if event_id in self._events:
            result["observation_status"] = "duplicate"
            return result
        if capture_time < now - self.horizon_s or (
            self._closed_through is not None and capture_time <= self._closed_through
        ):
            result["observation_status"] = "too_old"
            return result
        self._events[event_id] = (capture_time, vector.copy())
        result, decisions = self._replay(now)
        result["observation_status"] = decisions[event_id]
        return result

    def at(self, now):
        """Predict at monotonic arrival time; stale estimates never authorize flight."""
        if not np.isfinite(now) or now < self._now:
            raise ValueError("prediction time must be finite and monotonic")
        self._now = now
        result, _ = self._replay(now)
        return result

    def _predict(self, state, until):
        if state.vector is None:
            return _State(until, last_fix=state.last_fix)
        dt = until - state.time
        transition = np.eye(6)
        transition[:3, 3:] = np.eye(3) * dt
        # Continuous white acceleration makes prediction independent of replay subdivisions.
        process = self.acceleration_variance * np.kron(
            np.array([[dt**3 / 3, dt**2 / 2], [dt**2 / 2, dt]]), np.eye(3)
        )
        return _State(
            until,
            transition @ state.vector,
            transition @ state.covariance @ transition.T + process,
            state.last_fix,
        )

    def _update(self, state, position):
        noise = np.eye(3) * self.fix_variance_m2
        if state.vector is None:
            covariance = np.diag([self.fix_variance_m2] * 3 + [1.0] * 3)
            return _State(state.time, np.r_[position, np.zeros(3)], covariance, state.time), True
        innovation = position - state.vector[:3]
        innovation_covariance = state.covariance[:3, :3] + noise
        if innovation @ np.linalg.solve(innovation_covariance, innovation) > 16.27:
            return state, False
        gain = np.linalg.solve(innovation_covariance, state.covariance[:3, :]).T
        residual = np.eye(6)
        residual[:, :3] -= gain
        covariance = residual @ state.covariance @ residual.T + gain @ noise @ gain.T
        return _State(
            state.time,
            state.vector + gain @ innovation,
            (covariance + covariance.T) / 2,
            state.time,
        ), True

    def _replay(self, now):
        ordered = sorted(self._events, key=lambda key: (self._events[key][0], key))
        state = self._checkpoint
        states = []
        decisions = {}
        for event_id in ordered:
            timestamp, position = self._events[event_id]
            state, accepted = self._update(self._predict(state, timestamp), position)
            states.append(state)
            decisions[event_id] = "accepted" if accepted else "rejected"

        cutoff = max(0.0, now - self.horizon_s)
        remove_count = max(0, len(ordered) - self.max_events)
        while remove_count < len(ordered) and self._events[ordered[remove_count]][0] < cutoff:
            remove_count += 1
        if remove_count:
            boundary = states[remove_count - 1].time
            while remove_count < len(ordered) and states[remove_count].time == boundary:
                remove_count += 1
            self._checkpoint = states[remove_count - 1]
            self._closed_through = self._checkpoint.time
            for event_id in ordered[:remove_count]:
                del self._events[event_id]
        if cutoff > self._checkpoint.time:
            self._checkpoint = self._predict(self._checkpoint, cutoff)
            self._closed_through = cutoff

        predicted = self._predict(state, now)
        age = None if predicted.last_fix is None else now - predicted.last_fix
        confidence = "red" if age is None or age >= 2 else "amber" if age >= 0.5 else "green"
        return {
            "timestamp": now,
            "position_map_m": None if predicted.vector is None else predicted.vector[:3].tolist(),
            "velocity_map_mps": None if predicted.vector is None else predicted.vector[3:].tolist(),
            "covariance_m2": None
            if predicted.covariance is None
            else predicted.covariance[:3, :3].tolist(),
            "state_covariance": None
            if predicted.covariance is None
            else predicted.covariance.tolist(),
            "fix_age_s": age,
            "last_fix_capture_time": predicted.last_fix,
            "confidence": confidence,
            "accepted": confidence == "green",
            "flight_approved": False,
            "retained_event_count": len(self._events),
            "fix_decisions": {key: decisions[key] for key in self._events},
        }, decisions
