"""Ohmni's measured Android hardware with local safety controls.

All movement uses manual_move, independently polled encoders, and a local drive loop.
Neither pre_drive/pre_rot nor vendor Docker/ROS paths work on the measured robots.
"""

from __future__ import annotations

import math
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass

from .botshell import DEFAULT_PATH, BotShell
from .camera import Camera
from .camera import from_environment as camera_from_environment
from .lidar import Lidar, discover
from .models import GroundStatus, RangeScan
from .odometry import BASE_MM, Odometry
from .paired_encoder import PairedEncoderStream, default_socket_path


@dataclass(frozen=True)
class Config:
    socket_path: str = DEFAULT_PATH
    paired_encoder_socket: str = default_socket_path()
    launch: tuple[float, float, float] = (0.0, 0.0, 0.0)
    lidar_offset_deg: float | None = None
    lidar_angle_sign: int | None = None
    # Retained only to reject old configurations that requested a bypass.
    allow_spotted_without_lidar: bool = False
    footprint_radius_m: float | None = None
    stopping_distance_m: float | None = None
    clearance_margin_m: float | None = None
    lidar_mount_x_m: float | None = None
    lidar_mount_y_m: float | None = None
    lidar_mount_z_m: float | None = None
    spotter_present: bool = False
    max_speed_m_s: float = 0.18
    max_goto_m: float = 2.0
    scan_max_age_s: float = 0.5
    owner_timeout_s: float = 0.35
    motion_timeout_s: float = 25.0
    wheel_diameter_mm: float = 150.5

    def __post_init__(self) -> None:
        if self.allow_spotted_without_lidar is not False:
            raise ValueError("LiDAR avoidance is mandatory; a no-LiDAR bypass is not supported")
        for name in (
            "footprint_radius_m",
            "stopping_distance_m",
            "clearance_margin_m",
            "lidar_mount_x_m",
            "lidar_mount_y_m",
            "lidar_mount_z_m",
        ):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not math.isfinite(value)
            ):
                raise ValueError(f"{name} requires an explicit finite measurement")
        for name in ("footprint_radius_m", "stopping_distance_m"):
            value = getattr(self, name)
            if value is not None and value <= 0:
                raise ValueError(f"{name} must be positive")
        if self.clearance_margin_m is not None and self.clearance_margin_m < 0:
            raise ValueError("clearance_margin_m cannot be negative")
        if len(self.launch) != 3 or not all(
            type(v) in (int, float) and math.isfinite(v) for v in self.launch
        ):
            raise ValueError("launch pose must be finite")
        for name in (
            "max_speed_m_s",
            "max_goto_m",
            "scan_max_age_s",
            "owner_timeout_s",
            "motion_timeout_s",
            "wheel_diameter_mm",
        ):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ValueError(f"{name} must be finite")
        if self.wheel_diameter_mm <= 0:
            raise ValueError("wheel diameter must be positive")
        if not 0 < self.max_speed_m_s <= 0.18:
            raise ValueError("drive speed exceeds the measured 0.18 m/s envelope")
        if not 0 < self.max_goto_m <= 2.0 or not 0 < self.scan_max_age_s <= 0.5:
            raise ValueError("invalid motion or scan-age cap")
        if not 0 < self.owner_timeout_s <= 0.35 or not 0 < self.motion_timeout_s <= 25:
            raise ValueError("invalid owner or motion timeout cap")
        if self.lidar_offset_deg is not None and not math.isfinite(self.lidar_offset_deg):
            raise ValueError("lidar offset must be finite")
        if isinstance(self.lidar_angle_sign, bool) or self.lidar_angle_sign not in (None, -1, 1):
            raise ValueError("lidar angle sign must be -1 or 1")


@dataclass
class Motion:
    identity: str
    target: tuple[float, float] | None
    heading: float | None
    speed: float
    started: float
    velocity_m_s: float | None = None
    yaw_rate_deg_s: float | None = None
    ends_at: float | None = None
    phase: str = "turn"


class OhmniDevice:
    device_class = "ground_vehicle"

    def __init__(
        self,
        config: Config,
        *,
        camera: Camera | None = None,
        shell_factory=BotShell,
        lidar_discover=discover,
        autostart: bool = True,
    ) -> None:
        self.config = config
        # Replies interleave on a shared socket. Encoders, drive, battery and lidar setup
        # each own a socket; no battery wait can delay wheel STOP or encoder sampling.
        self.drive_shell = shell_factory(config.socket_path)
        self.battery_shell = shell_factory(config.socket_path)
        self.odometry = Odometry(
            PairedEncoderStream(config.paired_encoder_socket),
            config.launch,
            wheel_diameter_mm=config.wheel_diameter_mm,
        )
        self.camera = camera
        port = lidar_discover()
        self.lidar = (
            Lidar(
                shell_factory(config.socket_path),
                port,
                self.odometry.snapshot,
                offset_deg=config.lidar_offset_deg,
                angle_sign=config.lidar_angle_sign,
            )
            if port
            else None
        )
        self.capabilities = ["ground_drive", "screen"]
        if self.lidar:
            self.capabilities.append("lidar")
        if self.camera:
            self.capabilities.append("camera")
        self.enabled = False
        self._pending_stop = True
        self._pending_sleep = True
        self.spotter_present = config.spotter_present
        self.battery = 0.0  # unknown does not claim a safe charge
        self.battery_voltage: float | None = None
        self.battery_updated = 0.0
        self.docked = False
        self.last_refusal: str | None = None
        self.motion: Motion | None = None
        self._results: dict[str, bool | None] = {}
        self._last_owner_tick = 0.0
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._drive_thread = threading.Thread(
            target=self._drive_loop, daemon=True, name="ohmni-drive-deadman"
        )
        self._battery_thread = threading.Thread(
            target=self._battery_loop, daemon=True, name="ohmni-battery"
        )
        if autostart:
            self.odometry.start()
            self._drive_thread.start()
            self._battery_thread.start()
            if self.lidar:
                self.lidar.start()
            if self.camera:
                self.camera.start()

    def status(self) -> GroundStatus:
        pose = self.odometry.snapshot()
        state = (
            "docked"
            if self.docked
            else "fault"
            if self.odometry.lost
            else "moving"
            if self.motion
            else "idle"
            if self.enabled
            else "stopped"
        )
        battery = self.battery if time.monotonic() - self.battery_updated < 30 else 0.0
        return GroundStatus(
            x=pose.x,
            y=pose.y,
            yaw_deg=pose.yaw_deg,
            vx=pose.vx,
            vy=pose.vy,
            battery=battery,
            link=1.0,
            pos_quality=pose.quality,
            state=state,
            drive_authority=self.enabled and pose.quality > 0 and self.guard_reason() is None,
            t_ms=int(time.monotonic() * 1_000),
            extras={
                "battery_voltage": self.battery_voltage,
                "obstacle_guard": self.guard_reason() or "available",
                "lidar_present": self.lidar is not None,
                "lidar_calibrated": bool(self.lidar and self.lidar.calibrated),
                "spotter_present": self.spotter_present,
                "last_refusal": self.last_refusal,
            },
        )

    def enable(self) -> bool:
        with self._lock:
            reason = self.pre_enable_refusal()
            if reason:
                self.enabled = False
                self.last_refusal = reason
                return False
            try:
                self._flush_stop()
                self.drive_shell.command("init")
                if self.lidar:
                    # The vendor detector cannot share the serial port with our reader.
                    self.drive_shell.command("stop_collision_detection")
                else:
                    self.drive_shell.command("start_collision_detection")
                self.drive_shell.command("manual_move 0 0")
            except OSError:
                self.last_refusal = "bot_shell_unavailable"
                self.enabled = False
                self._pending_stop = self._pending_sleep = True
                return False
            self.enabled = True
            return True

    def _finish(self, result: bool | None, reason: str | None = None) -> None:
        motion = self.motion
        self.motion = None
        if motion:
            # A failed STOP must never leave a successful terminal result behind.
            self._results[motion.identity] = None
        if reason is not None:
            self.last_refusal = reason
        self._pending_stop = True
        self._flush_stop()
        if motion:
            self._results[motion.identity] = result

    def _flush_stop(self) -> None:
        """Persist failed safety writes across ticks, including when motion is None."""
        error = None
        if self._pending_stop:
            try:
                self.drive_shell.command("manual_move 0 0")
                self._pending_stop = False
            except OSError as failure:
                error = failure
        if self._pending_sleep:
            try:
                self.drive_shell.command("sleep")
                self._pending_sleep = False
            except OSError as failure:
                error = failure
        if error is not None:
            self.last_refusal = "hardware_stop_unconfirmed"
            raise error

    def stop(self) -> None:
        with self._lock:
            self._finish(None)

    def disable(self) -> None:
        with self._lock:
            self.enabled = False
            self._pending_sleep = True
            self._finish(None)

    def set_spotter_present(self, present: bool) -> None:
        self.spotter_present = bool(present)
        if not present:
            self.disable()

    def guard_reason(self, *, forward: bool = False, now: float | None = None) -> str | None:
        now = time.monotonic() if now is None else now
        if not self.spotter_present:
            return "spotter_missing"
        if self.docked:
            return "robot_docked"
        measured = (
            self.config.footprint_radius_m,
            self.config.stopping_distance_m,
            self.config.clearance_margin_m,
            self.config.lidar_mount_x_m,
            self.config.lidar_mount_y_m,
            self.config.lidar_mount_z_m,
        )
        if any(value is None for value in measured):
            return "ground_clearance_unconfigured"
        if self.lidar is None:
            return "lidar_missing"
        if not self.lidar.calibrated:
            return "lidar_calibration_required"
        scan = self.lidar.scan
        if scan is None or not 0 <= now - self.lidar.updated <= self.config.scan_max_age_s:
            return "lidar_stale"
        values = scan.ranges_cm
        if (
            scan.angle_min_deg != 0.0
            or scan.angle_increment_deg != 1.0
            or len(values) != 360
            or any(type(value) is not int or value <= 0 for value in values)
        ):
            return "lidar_full_circle_coverage_missing"
        # Ranges originate at the sensor. Enclose the body about that origin,
        # including measured braking clearance and bounded travel during scan
        # age, owner timeout and one hardware-loop tick. Unknown bins stay blocked.
        radius, stopping, margin, mount_x, mount_y, _ = measured
        clearance = (
            radius
            + stopping
            + margin
            + math.hypot(mount_x, mount_y)
            + self.config.max_speed_m_s
            * (self.config.scan_max_age_s + self.config.owner_timeout_s + 0.1)
        )
        if any(value <= math.ceil(clearance * 100) for value in values):
            return "obstacle_within_clearance"
        return None

    def pre_enable_refusal(self) -> str | None:
        reason = self.guard_reason()
        if reason:
            return reason
        if not self.odometry.snapshot().quality:
            return "wheel_odometry_unavailable"
        return None

    def stop_confirmed(self) -> bool:
        """Local STOP writes completed; physical braking is separately qualified."""
        return self.motion is None and not self._pending_stop and not self._pending_sleep

    def _admit_motion(self) -> None:
        reason = self.guard_reason()
        if not self.enabled:
            reason = "control_authority_missing"
        elif self._pending_stop or self._pending_sleep:
            reason = "hardware_stop_unconfirmed"
        elif not self.odometry.snapshot().quality:
            reason = "wheel_odometry_unavailable"
        if reason:
            self.last_refusal = reason
            raise RuntimeError(reason)

    def _start(self, target, heading, speed) -> str:
        with self._lock:
            self._admit_motion()
            self._finish(None)
            now = time.monotonic()
            identity = str(uuid.uuid4())
            self.motion = Motion(identity, target, heading, speed, now)
            self._last_owner_tick = now
            # Bound terminal-result storage independently of relay lifetime.
            while len(self._results) > 64:
                del self._results[next(iter(self._results))]
            return identity

    def move_to(self, x_m: float, y_m: float, z_m: float, speed_m_s: float) -> str:
        if not all(math.isfinite(v) for v in (x_m, y_m, z_m, speed_m_s)):
            raise ValueError("motion values must be finite")
        pose = self.odometry.snapshot()
        if z_m != 0 or not 0 < speed_m_s <= 0.5:
            raise ValueError("ground goto needs z=0 and positive speed within the ground cap")
        if math.hypot(x_m - pose.x, y_m - pose.y) > self.config.max_goto_m:
            raise ValueError("target exceeds node motion distance cap")
        return self._start((x_m, y_m), None, min(speed_m_s, self.config.max_speed_m_s))

    def rotate_to(self, yaw_deg: float, speed_deg_s: float) -> str:
        if not math.isfinite(yaw_deg) or not math.isfinite(speed_deg_s) or speed_deg_s <= 0:
            raise ValueError("rotation needs finite heading and positive speed")
        return self._start(None, yaw_deg % 360, min(speed_deg_s, 45.0))

    def drive_velocity(self, velocity_m_s: float, yaw_rate_deg_s: float, duration_s: float) -> str:
        if (
            not all(math.isfinite(value) for value in (velocity_m_s, yaw_rate_deg_s, duration_s))
            or velocity_m_s < 0
            or velocity_m_s > self.config.max_speed_m_s
            or abs(yaw_rate_deg_s) > 45
            or not 0 < duration_s <= 0.5
            or (velocity_m_s and yaw_rate_deg_s)
        ):
            raise ValueError("ground velocity must be a bounded forward or yaw-only pulse")
        with self._lock:
            self._admit_motion()
            self._finish(None)
            now = time.monotonic()
            identity = str(uuid.uuid4())
            self.motion = Motion(
                identity,
                None,
                None,
                max(velocity_m_s, abs(yaw_rate_deg_s)),
                now,
                velocity_m_s,
                yaw_rate_deg_s,
                now + duration_s,
            )
            self._last_owner_tick = now
            while len(self._results) > 64:
                del self._results[next(iter(self._results))]
            return identity

    def motion_done(self, motion_id: str) -> bool | None:
        with self._lock:
            if self.motion and self.motion.identity == motion_id:
                # The hardware thread stops if the owner loop hangs, even when the
                # asyncio watchdog cannot get CPU time to execute its own stop.
                self._last_owner_tick = time.monotonic()
                return False
            return self._results.get(motion_id)

    def step(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        with self._lock:
            self._flush_stop()
            motion = self.motion
            if motion is None:
                return
            pose = self.odometry.snapshot(now)
            reason = self.guard_reason(now=now)
            if not self.enabled:
                reason = "control_authority_missing"
            elif now - self._last_owner_tick > self.config.owner_timeout_s:
                reason = "owner_loop_stalled"
            elif now - motion.started > self.config.motion_timeout_s:
                reason = "motion_timeout"
            elif not pose.quality:
                reason = "wheel_odometry_unavailable"
            if reason:
                self._finish(None, reason)
                return
            if motion.ends_at is not None:
                if now >= motion.ends_at:
                    self._finish(True)
                    return
                if motion.velocity_m_s:
                    reason = self.guard_reason(forward=True, now=now)
                    if reason:
                        self._finish(None, reason)
                        return
                    units = max(1, int(250 * motion.velocity_m_s / 0.18))
                    self.drive_shell.command(f"manual_move {units} {-units}")
                elif motion.yaw_rate_deg_s:
                    units = max(
                        1,
                        int(250 * math.radians(abs(motion.yaw_rate_deg_s)) * BASE_MM / 2000 / 0.18),
                    )
                    signed = -units if motion.yaw_rate_deg_s > 0 else units
                    self.drive_shell.command(f"manual_move {signed} {signed}")
                else:
                    self._finish(True)
                return
            if motion.target:
                dx, dy = motion.target[0] - pose.x, motion.target[1] - pose.y
                remaining = math.hypot(dx, dy)
                if remaining <= 0.08:
                    self._finish(True)
                    return
                heading = math.degrees(math.atan2(dy, dx)) % 360
            else:
                heading, remaining = motion.heading, 0.0
            error = (heading - pose.yaw_deg + 180) % 360 - 180
            if abs(error) > 6:
                # Both positive turns CLOCKWISE; positive yaw is counter-clockwise.
                speed = min(45.0, motion.speed if motion.target is None else 45.0)
                wheel_speed = math.radians(speed) * BASE_MM / 2000
                units = max(1, min(250, int(250 * wheel_speed / 0.18)))
                if abs(error) < 25:
                    units = max(1, int(units * 0.6))
                signed = -units if error > 0 else units
                self.drive_shell.command(f"manual_move {signed} {signed}")
            elif motion.target is None:
                self._finish(True)
            else:
                reason = self.guard_reason(forward=True, now=now)
                if reason:
                    self._finish(None, reason)
                    return
                units = max(1, min(250, int(250 * motion.speed / 0.18)))
                if remaining < 0.2:
                    units = max(1, int(units * 0.6))
                self.drive_shell.command(f"manual_move {units} {-units}")

    def _drive_loop(self) -> None:
        while not self._stop.wait(0.1):
            try:
                self.step()
            except OSError:
                self.enabled = False
                self.motion = None
                self.last_refusal = "bot_shell_unavailable"
                self._pending_stop = self._pending_sleep = True
                # The next hardware tick retries both safety writes on a fresh socket,
                # even though the motion was cleared. Never retry the motion itself.

    def _battery_loop(self) -> None:
        while not self._stop.is_set():
            try:
                text = self.battery_shell.command(
                    "battery", expected=r"Last docked:.*\n", timeout=0.5
                )
                match = re.search(r"Last battery:\s*\[([^]]+)\]", text)
                if match:
                    cells = [int(v.strip()) for v in match[1].split(",")]
                    if len(cells) == 5 and all(2000 <= v <= 4300 for v in cells):
                        self.battery_voltage = sum(cells) / 1000
                        self.battery = max(0.0, min(1.0, (self.battery_voltage - 13.0) / 3.8))
                        self.battery_updated = time.monotonic()
                docked = re.search(r"Last docked:\s*([01])", text)
                if docked:
                    self.docked = docked[1] == "1"
            except (OSError, ValueError):
                pass
            self._stop.wait(10)

    def latest_scan(self) -> RangeScan | None:
        if self.lidar and 0 <= time.monotonic() - self.lidar.updated <= self.config.scan_max_age_s:
            return self.lidar.scan
        return None

    def hardware_profile(self) -> dict[str, object]:
        return {
            "aircraft_model": "Ohmni UP-CHT01",
            "aircraft_firmware": "telebot 4.1.4.4",
            "phone_model": "Intel Atom x5-Z8350",
            "android_version": "7.1.2",
            "sdk_version": "bot_shell",
            "measured_hfov_deg": None,
        }

    def video_publish_state(self) -> str:
        return self.camera.state if self.camera else "stopped"

    def close(self) -> None:
        try:
            deadline = time.monotonic() + 2.0
            while True:
                try:
                    self.disable()
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise OSError(
                            "hardware stop remains unconfirmed after shutdown retries"
                        ) from None
                    time.sleep(0.1)
        finally:
            self._stop.set()
            for thread in (self._drive_thread, self._battery_thread):
                if thread.is_alive():
                    thread.join(timeout=1)
            self.odometry.close()
            if self.lidar:
                self.lidar.close()
            if self.camera:
                self.camera.close()
            self.drive_shell.close()
            self.battery_shell.close()


def from_environment(*, key: str = "") -> OhmniDevice:
    offset = os.environ.get("SWEEP_LIDAR_OFFSET_DEG")
    sign = os.environ.get("SWEEP_LIDAR_ANGLE_SIGN")
    wheel_diameter = os.environ.get("SWEEP_WHEEL_DIAMETER_MM")
    if os.environ.get("SWEEP_ALLOW_NO_LIDAR", "0") not in ("", "0"):
        raise ValueError("LiDAR avoidance is mandatory; SWEEP_ALLOW_NO_LIDAR is not supported")

    def measurement(name: str) -> float | None:
        raw = os.environ.get(name)
        return None if raw is None or not raw.strip() else float(raw)

    config = Config(
        socket_path=os.environ.get("SWEEP_BOTSHELL", DEFAULT_PATH),
        paired_encoder_socket=os.environ.get("SWEEP_PAIRED_ENCODER_SOCKET", default_socket_path()),
        launch=tuple(
            float(os.environ.get(f"SWEEP_HOME_{axis}", "0")) for axis in ("X", "Y", "YAW_DEG")
        ),
        lidar_offset_deg=float(offset) if offset else None,
        lidar_angle_sign=int(sign) if sign else None,
        footprint_radius_m=measurement("SWEEP_GROUND_FOOTPRINT_RADIUS_M"),
        stopping_distance_m=measurement("SWEEP_GROUND_STOPPING_DISTANCE_M"),
        clearance_margin_m=measurement("SWEEP_GROUND_CLEARANCE_MARGIN_M"),
        lidar_mount_x_m=measurement("SWEEP_LIDAR_MOUNT_X_M"),
        lidar_mount_y_m=measurement("SWEEP_LIDAR_MOUNT_Y_M"),
        lidar_mount_z_m=measurement("SWEEP_LIDAR_MOUNT_Z_M"),
        spotter_present=os.environ.get("SWEEP_SPOTTER") == "1",
        wheel_diameter_mm=float(wheel_diameter) if wheel_diameter else Config.wheel_diameter_mm,
    )
    media_host = os.environ.get("SWEEP_MEDIA_HOST")
    camera = camera_from_environment(media_host, key) if media_host else None
    return OhmniDevice(config, camera=camera)


def build() -> OhmniDevice:
    return from_environment(key=os.environ.get("SWEEP_NODE_KEY", ""))
