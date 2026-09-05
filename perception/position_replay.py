"""Offline position Kalman replay using explicit map-frame velocity controls."""

import numpy as np


class PositionReplay:
    def __init__(self, start_time, position, variance=1.0, process_variance_per_s=0.01):
        values = np.array([start_time, variance, process_variance_per_s], dtype=float)
        self.initial = np.asarray(position, dtype=float)
        if (
            not np.isfinite(values).all()
            or start_time < 0
            or variance <= 0
            or process_variance_per_s <= 0
            or self.initial.shape != (3,)
            or not np.isfinite(self.initial).all()
        ):
            raise ValueError("invalid initial state")
        self.start = start_time
        self.variance = variance
        self.process = process_variance_per_s
        self.events = {}

    def add(self, event_id, kind, timestamp, value, variance=None):
        """Velocity is map-frame m/s; fixes are map-body meters at capture time."""
        vector = np.asarray(value, dtype=float)
        if (
            not isinstance(event_id, str)
            or not event_id
            or event_id in self.events
            or kind not in ("velocity", "fix")
            or not np.isfinite(timestamp)
            or timestamp < self.start
            or vector.shape != (3,)
            or not np.isfinite(vector).all()
        ):
            raise ValueError("invalid or duplicate event")
        if kind == "fix" and (variance is None or not np.isfinite(variance) or variance <= 0):
            raise ValueError("fix requires positive variance in square meters")
        if any(event[0] == timestamp and event[1] == kind for event in self.events.values()):
            raise ValueError("duplicate event time and kind")
        self.events[event_id] = (timestamp, kind, vector, variance)

    def at(self, now, max_fix_age=0.5, max_velocity_age=0.2):
        if (
            not np.isfinite([now, max_fix_age, max_velocity_age]).all()
            or now < self.start
            or min(max_fix_age, max_velocity_age) <= 0
        ):
            raise ValueError("invalid prediction time")
        position, covariance = self.initial.copy(), np.eye(3) * self.variance
        velocity, previous = np.zeros(3), self.start
        last_fix = last_velocity = None
        rejected_fixes = []

        def predict(until):
            nonlocal position, covariance, previous
            dt = until - previous
            control_dt = (
                0
                if last_velocity is None
                else max(0, min(until, last_velocity + max_velocity_age) - previous)
            )
            position += velocity * control_dt
            covariance += np.eye(3) * self.process * dt
            previous = until

        for timestamp, kind, value, variance in sorted(
            self.events.values(), key=lambda e: (e[0], e[1])
        ):
            if timestamp > now:
                continue
            predict(timestamp)
            if kind == "velocity":
                velocity, last_velocity = value, timestamp
            else:
                innovation = value - position
                inverse = np.linalg.inv(covariance + np.eye(3) * variance)
                if innovation @ inverse @ innovation > 16.27:
                    rejected_fixes.append(timestamp)
                    continue
                gain = covariance @ inverse
                position += gain @ (value - position)
                identity = np.eye(3) - gain
                covariance = (
                    identity @ covariance @ identity.T + gain @ (np.eye(3) * variance) @ gain.T
                )
                last_fix = timestamp
            previous = timestamp
        predict(now)
        fix_age = None if last_fix is None else now - last_fix
        velocity_age = None if last_velocity is None else now - last_velocity
        valid = (
            fix_age is not None
            and fix_age <= max_fix_age
            and velocity_age is not None
            and velocity_age <= max_velocity_age
        )
        return dict(
            accepted=valid,
            flight_approved=False,
            position_map_m=position.tolist(),
            covariance_m2=covariance.tolist(),
            fix_age_s=fix_age,
            confidence=(
                "red" if fix_age is None or fix_age >= 2 else "amber" if fix_age >= 0.5 else "green"
            ),
            rejected_fix_times=rejected_fixes,
            velocity_age_s=velocity_age,
            timestamp=now,
        )
