"""In-memory devices for tests and for wiring checks before hardware is on the bench.

``FakeAircraft`` is the kinematic fixture the DJI fake node has always used: it teleports,
it does not model flight, and every hardware-profile string says so. ``FakeGroundVehicle``
adds the ground-vehicle kinematics: a 2D pose driven at the commanded speed, the
``docked``/``idle``/``moving``/``stopped`` drive vocabulary, and a synthetic room-shaped
scan around its own pose when it claims ``lidar``.

These are fixtures, not stand-ins for hardware: they belong in automated tests and in the
reference node, never in a demo that claims a robot.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any

from nodekit.device import DeviceStatus, Scan

_EPSILON = 1e-9


def epoch_ms() -> int:
    return time.time_ns() // 1_000_000


@dataclass
class _Motion:
    motion_id: str
    kind: str  # "drive" or "turn"
    started_at: float
    duration: float
    from_x: float
    from_y: float
    to_x: float
    to_y: float
    from_yaw: float
    delta_yaw: float


class FakeAircraft:
    """The kinematic aircraft fixture: poses move the instant a command arrives."""

    device_class = "aircraft"

    def __init__(
        self,
        *,
        home: tuple[float, float, float] = (0.0, 0.0, 0.0),
        capabilities: tuple[str, ...] = ("flight", "pano_360", "reconstruct_8"),
        profile: dict[str, Any] | None = None,
        battery: float = 0.8,
        link: float = 0.9,
        pos_quality: float = 0.95,
    ) -> None:
        self.home = home
        self.capabilities = list(capabilities)
        self.x, self.y, self.z = home
        self.yaw_deg = 0.0
        self.gimbal_pitch_deg = 0.0
        self.state = "landed"
        self.battery = battery
        self.link = link
        self.pos_quality = pos_quality
        self.control_authority = True
        self._profile = dict(profile or {})
        self._motions = 0
        self._done: set[str] = set()

    # ------------------------------------------------------------------ Device

    def status(self) -> DeviceStatus:
        return DeviceStatus(
            x=self.x,
            y=self.y,
            z=self.z,
            yaw_deg=self.yaw_deg,
            vx=0.0,
            vy=0.0,
            vz=0.0,
            battery=self.battery,
            link=self.link,
            pos_quality=self.pos_quality,
            state=self.state,
            control_authority=self.control_authority,
            extras={"gimbal_pitch_deg": self.gimbal_pitch_deg},
        )

    def stop(self) -> None:
        if self.state != "landed":
            self.state = "hovering"

    def disable(self) -> None:
        """Hold and drop control authority. The fixture has no motor cutoff: an airborne
        aircraft keeps hovering, which is what an indoor node can honestly claim."""
        self.stop()
        self.control_authority = False

    def enable(self) -> bool:
        self.control_authority = True
        return True

    def move_to(self, x_m: float, y_m: float, z_m: float, speed_m_s: float) -> str:
        self.x, self.y, self.z = x_m, y_m, z_m
        self.state = "hovering"
        return self._finished()

    def rotate_to(self, yaw_deg: float, speed_deg_s: float) -> str:
        self.yaw_deg = yaw_deg
        return self._finished()

    def motion_done(self, motion_id: str) -> bool | None:
        return True if motion_id in self._done else None

    def latest_scan(self) -> Scan | None:
        return None

    def hardware_profile(self) -> dict[str, Any]:
        return dict(self._profile)

    # ------------------------------------------------------------------ aircraft extras

    def takeoff(self, z_m: float) -> None:
        self.z = z_m
        self.state = "hovering"

    def land(self) -> None:
        self.z = self.home[2]
        self.state = "landed"

    def set_gimbal_pitch(self, pitch_deg: float) -> None:
        self.gimbal_pitch_deg = pitch_deg

    def _finished(self) -> str:
        self._motions += 1
        motion_id = f"motion-{self._motions}"
        self._done.add(motion_id)
        return motion_id


class FakeGroundVehicle:
    """A wheeled robot on a floor: 2D pose, the drive vocabulary, and a room-shaped scan.

    Motion is time-stepped at the commanded speed against ``monotonic``, so a test can
    drive it with its own clock and see ``moving`` before ``idle``. ``room`` is the
    rectangle the synthetic lidar sees, in the same frame as the pose.
    """

    device_class = "ground_vehicle"

    def __init__(
        self,
        *,
        start: tuple[float, float, float] = (0.0, 0.0, 0.0),
        capabilities: tuple[str, ...] = ("ground_drive", "lidar", "camera"),
        docked: bool = False,
        room: tuple[float, float, float, float] = (-4.0, -2.5, 4.0, 2.5),
        scan_hz: float = 5.0,
        range_min_m: float = 0.15,
        range_max_m: float = 12.0,
        battery: float = 0.86,
        link: float = 0.95,
        pos_quality: float = 0.6,
        monotonic: Any = time.monotonic,
        clock_ms: Any = epoch_ms,
    ) -> None:
        if scan_hz <= 0:
            raise ValueError("scan_hz must be positive")
        self.capabilities = list(capabilities)
        self.x, self.y, self.yaw_deg = start
        self.state = "docked" if docked else "idle"
        self.battery = battery
        self.link = link
        # Wheel odometry only: 0.6 is what the contract calls an uncorrected ground pose.
        self.pos_quality = pos_quality
        self.enabled = not docked
        self.control_authority = not docked
        self.room = room
        self.scan_hz = scan_hz
        self.range_min_m = range_min_m
        self.range_max_m = range_max_m
        self.video_state = "stopped"
        self._monotonic = monotonic
        self._clock_ms = clock_ms
        self._motion: _Motion | None = None
        self._motions = 0
        self._done: set[str] = set()
        self._failed: set[str] = set()
        self._scan: Scan | None = None
        self._scan_at: float | None = None

    # ------------------------------------------------------------------ Device

    def status(self) -> DeviceStatus:
        self._advance()
        vx, vy = self._velocity()
        return DeviceStatus(
            x=self.x,
            y=self.y,
            z=0.0,
            yaw_deg=self.yaw_deg % 360.0,
            vx=vx,
            vy=vy,
            vz=0.0,
            battery=self.battery,
            link=self.link,
            pos_quality=self.pos_quality if self.enabled or self.state == "docked" else 0.0,
            state=self.state,
            control_authority=self.control_authority,
            extras={"docked": self.state == "docked", "wheels_enabled": self.enabled},
        )

    def stop(self) -> None:
        """Halt and hold, wheels still enabled: the hold action and the hover command."""
        self._advance()
        self._abandon_motion()
        if self.state != "docked":
            self.state = "stopped"

    def disable(self) -> None:
        """Wheels off. Only ``enable`` brings them back, and the node only calls it on a
        join, so a failsafe survives until the robot rejoins."""
        self.stop()
        self.enabled = False
        self.control_authority = False

    def enable(self) -> bool:
        self._advance()
        self.enabled = True
        self.control_authority = True
        if self.state == "docked":
            self.state = "idle"
        return True

    def move_to(self, x_m: float, y_m: float, z_m: float, speed_m_s: float) -> str:
        """Drive to a point on the floor. ``z_m`` is ignored: the floor is the only plane."""
        self._start_guard()
        distance = math.hypot(x_m - self.x, y_m - self.y)
        speed = max(0.01, float(speed_m_s))
        motion = self._begin("drive", distance / speed)
        motion.to_x, motion.to_y = x_m, y_m
        return motion.motion_id

    def rotate_to(self, yaw_deg: float, speed_deg_s: float) -> str:
        self._start_guard()
        delta = ((float(yaw_deg) - self.yaw_deg) + 180.0) % 360.0 - 180.0
        speed = max(1.0, float(speed_deg_s))
        motion = self._begin("turn", abs(delta) / speed)
        motion.delta_yaw = delta
        return motion.motion_id

    def motion_done(self, motion_id: str) -> bool | None:
        self._advance()
        if motion_id in self._done:
            return True
        if self._motion is not None and self._motion.motion_id == motion_id:
            return False
        return None

    def latest_scan(self) -> Scan | None:
        if "lidar" not in self.capabilities:
            return None
        self._advance()
        now = self._monotonic()
        if self._scan is not None and self._scan_at is not None:
            if now - self._scan_at < 1.0 / self.scan_hz:
                return self._scan
        self._scan = self._build_scan()
        self._scan_at = now
        return self._scan

    def hardware_profile(self) -> dict[str, Any]:
        return {
            "native_panorama_modes": [],
            "photo_capture": False,
            # A fixed forward camera: the narrowest ordered pitch range the frame admits.
            "gimbal_pitch_min_deg": -1.0,
            "gimbal_pitch_max_deg": 1.0,
            "horizontal_fov_deg": 78.0,
            "storage_remaining_bytes": 0,
            "media_retrieval": False,
            "aircraft_model": "fake-ground-vehicle",
            "aircraft_firmware": "fake",
            "rc_firmware": "fake",
            "phone_model": "fake-node",
            "android_version": "fake",
            "sdk_version": "fake",
            "measured_hfov_deg": None,
        }

    def video_publish_state(self) -> str:
        return self.video_state

    # ------------------------------------------------------------------ kinematics

    def _start_guard(self) -> None:
        if not self.enabled:
            raise RuntimeError("the wheels are disabled; the robot must rejoin before it drives")

    def _begin(self, kind: str, duration: float) -> _Motion:
        self._advance()
        self._abandon_motion()
        self._motions += 1
        motion = _Motion(
            motion_id=f"motion-{self._motions}",
            kind=kind,
            started_at=self._monotonic(),
            duration=max(0.0, duration),
            from_x=self.x,
            from_y=self.y,
            to_x=self.x,
            to_y=self.y,
            from_yaw=self.yaw_deg,
            delta_yaw=0.0,
        )
        self._motion = motion
        self.state = "moving"
        return motion

    def _abandon_motion(self) -> None:
        if self._motion is not None:
            self._failed.add(self._motion.motion_id)
            self._motion = None

    def _advance(self) -> None:
        motion = self._motion
        if motion is None:
            return
        elapsed = self._monotonic() - motion.started_at
        fraction = 1.0 if motion.duration <= _EPSILON else min(1.0, elapsed / motion.duration)
        if motion.kind == "drive":
            self.x = motion.from_x + (motion.to_x - motion.from_x) * fraction
            self.y = motion.from_y + (motion.to_y - motion.from_y) * fraction
        else:
            self.yaw_deg = (motion.from_yaw + motion.delta_yaw * fraction) % 360.0
        if fraction >= 1.0:
            self._done.add(motion.motion_id)
            self._motion = None
            self.state = "idle"

    def _velocity(self) -> tuple[float, float]:
        motion = self._motion
        if motion is None or motion.kind != "drive" or motion.duration <= _EPSILON:
            return 0.0, 0.0
        return (
            (motion.to_x - motion.from_x) / motion.duration,
            (motion.to_y - motion.from_y) / motion.duration,
        )

    def _build_scan(self) -> Scan:
        increment = 1.0
        bins = int(round(360 / increment))
        heading = math.radians(self.yaw_deg)
        ranges = []
        for index in range(bins):
            # Angle 0 is the robot's forward axis; angles increase counter-clockwise.
            angle = heading + math.radians(index * increment)
            distance = self._wall_distance(math.cos(angle), math.sin(angle))
            if distance is None or distance < self.range_min_m or distance > self.range_max_m:
                ranges.append(0)
            elif index % 13 == 7:
                # Real indoor scans lose returns to dark and glancing surfaces; the fixture
                # drops one bin in thirteen so consumers exercise the no-return path.
                ranges.append(0)
            else:
                ranges.append(int(round(distance * 100)))
        return Scan(
            t_ms=self._clock_ms(),
            pose=(self.x, self.y, self.yaw_deg % 360.0),
            angle_min_deg=0.0,
            angle_increment_deg=increment,
            range_min_m=self.range_min_m,
            range_max_m=self.range_max_m,
            ranges_cm=ranges,
        )

    def _wall_distance(self, dx: float, dy: float) -> float | None:
        """Distance from the pose to the room rectangle along (dx, dy), or None outside it."""
        min_x, min_y, max_x, max_y = self.room
        if not (min_x < self.x < max_x and min_y < self.y < max_y):
            return None
        best = None
        for delta, low, high, position in (
            (dx, min_x, max_x, self.x),
            (dy, min_y, max_y, self.y),
        ):
            if delta > _EPSILON:
                candidate = (high - position) / delta
            elif delta < -_EPSILON:
                candidate = (low - position) / delta
            else:
                continue
            if best is None or candidate < best:
                best = candidate
        return best
