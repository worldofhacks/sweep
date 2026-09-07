from __future__ import annotations

import math
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from .models import GroundStatus, RangeScan


@dataclass
class FakeGroundDevice:
    monotonic: Callable[[], float] = time.monotonic
    max_speed_m_s: float = 0.18
    lidar_available: bool = True
    camera_available: bool = True
    spotter_present: bool = True
    x: float = 0.0
    y: float = 0.0
    yaw_deg: float = 0.0
    enabled: bool = False
    stopped: bool = True
    _motion: tuple[str, float, float, float, float] | None = None

    @property
    def capabilities(self) -> tuple[str, ...]:
        values = ["ground_drive"]
        if self.lidar_available:
            values.append("lidar")
        if self.camera_available:
            values.append("camera")
        return tuple(values)

    def enable(self) -> bool:
        self.enabled = self.spotter_present and self.lidar_available
        self.stopped = not self.enabled
        return self.enabled

    def stop(self) -> None:
        self._advance()
        self._motion = None
        self.stopped = True

    def disable(self) -> None:
        self.stop()
        self.enabled = False

    def drive_velocity(self, velocity_m_s: float, yaw_rate_deg_s: float, duration_s: float) -> str:
        if not self.enabled or not self.lidar_available or velocity_m_s < 0:
            raise RuntimeError("ground_guard_not_ready")
        if velocity_m_s and yaw_rate_deg_s:
            raise ValueError("ground pulses cannot combine forward and yaw motion")
        if (
            velocity_m_s > self.max_speed_m_s
            or abs(yaw_rate_deg_s) > 45
            or not 0 < duration_s <= 0.5
        ):
            raise ValueError("ground pulse exceeds the local safety bounds")
        self._advance()
        identity = str(uuid.uuid4())
        now = self.monotonic()
        self._motion = (identity, velocity_m_s, yaw_rate_deg_s, now, now + duration_s)
        self.stopped = False
        return identity

    def motion_done(self, identity: str) -> bool | None:
        self._advance()
        if self._motion is not None and self._motion[0] == identity:
            return False
        return True

    def status(self) -> GroundStatus:
        self._advance()
        return GroundStatus(
            self.x,
            self.y,
            self.yaw_deg,
            0.0,
            0.0,
            0.8,
            0.9,
            0.9 if self.lidar_available else 0.0,
            "moving" if self._motion is not None else "idle" if self.enabled else "stopped",
            self.enabled,
            {
                "lidar_present": self.lidar_available,
                "camera_present": self.camera_available,
                "spotter_present": self.spotter_present,
            },
        )

    def latest_scan(self) -> RangeScan | None:
        if not self.lidar_available:
            return None
        return RangeScan(
            int(self.monotonic() * 1_000),
            (self.x, self.y, self.yaw_deg),
            0.0,
            1.0,
            0.15,
            12.0,
            [400] * 360,
        )

    def video_publish_state(self) -> str:
        return "stopped"

    def _advance(self) -> None:
        motion = self._motion
        if motion is None:
            return
        identity, velocity, yaw_rate, started, ends_at = motion
        now = min(self.monotonic(), ends_at)
        elapsed = now - started
        if velocity:
            heading = math.radians(self.yaw_deg)
            self.x += velocity * elapsed * math.cos(heading)
            self.y += velocity * elapsed * math.sin(heading)
        else:
            self.yaw_deg = (self.yaw_deg + yaw_rate * elapsed) % 360
        if self.monotonic() >= ends_at:
            self._motion = None
            self.stopped = True
        else:
            self._motion = (identity, velocity, yaw_rate, now, ends_at)
