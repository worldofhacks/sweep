"""Ohmni's measured Android hardware behind the nodekit Device interface.

All movement uses manual_move, independently polled encoders, and a local drive loop.
Neither pre_drive/pre_rot nor vendor Docker/ROS paths work on the measured robots.
"""

from __future__ import annotations

import math
import os
import re
import subprocess
import threading
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass

from nodekit.device import DeviceStatus, Scan

from .botshell import DEFAULT_PATH, BotShell
from .camera import Camera
from .lidar import Lidar, discover
from .odometry import BASE_MM, Odometry
from .peripherals import RobotPeripherals


def battery_charge_band(cells_mv: list[int]) -> int:
    """The installed telebot_node.js battery_new minimum-cell display bands.

    This is the vendor's coarse estimate, not a lithium-ion voltage curve. Ohmni's
    five-cell LiFePO4 pack uses different chemistry from a four-cell 4.2 V pack.
    """
    if len(cells_mv) != 5 or not all(2000 <= value <= 4300 for value in cells_mv):
        raise ValueError("expected five plausible Ohmni battery cell readings")
    minimum = min(cells_mv)
    return 20 if minimum < 3150 else 50 if minimum < 3250 else 80 if minimum < 3380 else 100


@dataclass(frozen=True)
class Config:
    socket_path: str = DEFAULT_PATH
    launch: tuple[float, float, float] = (0.0, 0.0, 0.0)
    lidar_offset_deg: float | None = None
    lidar_angle_sign: int | None = None
    # Legacy setting retained only so unsafe configurations fail explicitly.
    allow_spotted_without_lidar: bool = False
    spotter_present: bool = False
    max_speed_m_s: float = 0.18
    max_goto_m: float = 2.0
    obstacle_margin_m: float = 0.45
    scan_max_age_s: float = 0.5
    owner_timeout_s: float = 0.35
    motion_timeout_s: float = 25.0

    def __post_init__(self) -> None:
        if self.allow_spotted_without_lidar:
            raise ValueError("LiDAR is mandatory; allow_spotted_without_lidar is not supported")
        if not all(math.isfinite(v) for v in self.launch):
            raise ValueError("launch pose must be finite")
        if not 0 < self.max_speed_m_s <= 0.18:
            raise ValueError("drive speed exceeds the measured 0.18 m/s envelope")
        if not 0 < self.max_goto_m <= 2.0 or not 0 < self.scan_max_age_s <= 0.5:
            raise ValueError("invalid motion or scan-age cap")
        if self.lidar_offset_deg is not None and not math.isfinite(self.lidar_offset_deg):
            raise ValueError("lidar offset must be finite")
        if self.lidar_angle_sign not in (None, -1, 1):
            raise ValueError("lidar angle sign must be -1 or 1")
        if not math.isfinite(self.obstacle_margin_m) or self.obstacle_margin_m < 0.45:
            raise ValueError("obstacle clearance cannot be below 0.45 m")


@dataclass
class Motion:
    identity: str
    target: tuple[float, float] | None
    heading: float | None
    speed: float
    started: float
    phase: str = "turn"


class OhmniDevice:
    device_class = "ground_vehicle"
    # The legacy camera-capabilities DTO requires numeric aircraft gimbal/FOV
    # values that this hardware has not supplied. Real camera diagnostics travel
    # in custom telemetry without inventing calibration or capture support.
    publish_camera_capabilities = False

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
        self.peripherals = RobotPeripherals(shell_factory(config.socket_path))
        self.odometry = Odometry(shell_factory(config.socket_path), config.launch)
        self.camera = camera
        self._initial_lidar_port = lidar_discover()
        self._vendor_initialized = False
        self._init_lock = threading.Lock()
        # Always create the owner, even when USB is absent at boot. The robust reader
        # rediscovers a unique CP210x device on every retry; never pin a tty number.
        self.lidar = Lidar(
            shell_factory(config.socket_path),
            None,
            self.odometry.snapshot,
            offset_deg=config.lidar_offset_deg,
            angle_sign=config.lidar_angle_sign,
            prepare=self._initialize_vendor,
        )
        self.capabilities = ["ground_drive", "screen", "lidar"]
        self.capabilities += ["robot_peripheral_v1"] + sorted(self.peripherals.kinds - {"screen"})
        if self.camera:
            self.capabilities.append("camera")
        self.enabled = False
        self._pending_stop = True
        self._pending_sleep = True
        self.spotter_present = config.spotter_present
        self.battery = 0.0  # unknown does not claim a safe charge
        self.battery_voltage: float | None = None
        self.battery_cells_mv: list[int] | None = None
        self.battery_updated = 0.0
        self.docked = False
        self.docked_updated = 0.0
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
        self._hardware = (
            self._read_hardware_profile() if autostart else self._unknown_hardware_profile()
        )
        if autostart:
            self.odometry.start()
            self._drive_thread.start()
            self._battery_thread.start()
            self.lidar.start()
            if self.camera:
                self.camera.start()

    def status(self) -> DeviceStatus:
        now = time.monotonic()
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
        battery_fresh = self.battery_updated > 0 and 0 <= now - self.battery_updated < 30
        battery = self.battery if battery_fresh else 0.0
        guard = self.guard_reason(now=now)
        reason = guard
        if not reason and (self._pending_stop or self._pending_sleep):
            reason = "hardware_stop_unconfirmed"
        if not reason and not pose.quality:
            reason = "wheel_odometry_unavailable"
        if not reason and not self.enabled:
            reason = "control_authority_missing"
        lidar = self.lidar.diagnostics(now)
        return DeviceStatus(
            pose.x,
            pose.y,
            0.0,
            pose.yaw_deg,
            pose.vx,
            pose.vy,
            0.0,
            battery,
            1.0,
            pose.quality,
            state,
            self.enabled and reason is None,
            {
                "adapter": "ohmni",
                "source": "bot_shell",
                "identity": self.hardware_profile(),
                "position": {
                    "x_m": pose.x,
                    "y_m": pose.y,
                    "yaw_deg": pose.yaw_deg,
                    "frame": "launch_wheel_odometry",
                },
                "velocity": {"x_m_s": pose.vx, "y_m_s": pose.vy},
                "link": {
                    "legacy_value_semantics": "authenticated_transport_liveness",
                    "radio_quality": None,
                    "radio_quality_source": "unreported",
                },
                "battery": {
                    "voltage": self.battery_voltage,
                    "cells_mv": self.battery_cells_mv,
                    "charge_percent": int(self.battery * 100) if battery_fresh else None,
                    "charge_source": "vendor_min_cell_band" if self.battery_cells_mv else None,
                    "charge_estimated": True if self.battery_cells_mv else None,
                    "age_ms": int((now - self.battery_updated) * 1000)
                    if self.battery_updated
                    else None,
                    "fresh": battery_fresh,
                    "docked": self.docked
                    if self.docked_updated and now - self.docked_updated < 30
                    else None,
                },
                "odometry": {
                    "quality": pose.quality,
                    "lost": self.odometry.lost,
                    "age_ms": int((now - self.odometry.updated) * 1000)
                    if self.odometry.updated
                    else None,
                    "encoders": list(self.odometry._previous) if self.odometry._previous else None,
                },
                "lidar": lidar,
                "safety": {
                    "blocked": reason is not None,
                    "reasons": [reason] if reason else [],
                    "obstacle_avoidance_required": True,
                    "spotter_present": self.spotter_present,
                    "motion_enabled": self.enabled,
                    "hardware_stop_confirmed": not self._pending_stop and not self._pending_sleep,
                    "stop_confirmation_source": "bot_shell_write",
                    "obstacle_margin_m": self.config.obstacle_margin_m,
                    "scan_max_age_ms": int(self.config.scan_max_age_s * 1000),
                    "last_refusal": self.last_refusal,
                },
                "controls": {
                    "supported_operations": [
                        "goto",
                        "rotate_to",
                        "hover",
                        "estop",
                        "robot_peripheral",
                    ],
                    "supported_peripherals": sorted(self.peripherals.kinds),
                    "unsupported_operations": ["takeoff", "land", "dock"],
                },
                "peripherals": self.peripherals.telemetry(),
                "cameras": self.camera.telemetry() if self.camera else [],
                "authority_change_reason": reason,
                "battery_voltage": self.battery_voltage,
                "obstacle_guard": guard or "available",
                "lidar_present": lidar["present"],
                "lidar_calibrated": self.lidar.calibrated,
                "last_refusal": self.last_refusal,
            },
        )

    def enable(self) -> bool:
        with self._lock:
            reason = self.guard_reason()
            if not reason and not self.odometry.snapshot().quality:
                reason = "wheel_odometry_unavailable"
            if reason:
                self.last_refusal = reason
                self.enabled = False
                return False
            try:
                self._flush_stop()
                # Successful startup ran this before opening the raw reader. Never
                # reinitialize the vendor serial stack during a relay re-enable.
                self._initialize_vendor()
                self.drive_shell.command("manual_move 0 0")
            except OSError:
                self.last_refusal = "bot_shell_unavailable"
                self.enabled = False
                self._pending_stop = self._pending_sleep = True
                return False
            self.enabled = True
            return True

    def _initialize_vendor(self) -> None:
        with self._lock, self._init_lock:
            if self._vendor_initialized:
                return
            self._flush_stop()
            self.drive_shell.command("init")
            self.drive_shell.command("stop_collision_detection")
            self.drive_shell.command("manual_move 0 0")
            if not self.enabled:
                # Vendor initialization must not leave a diagnostics-only startup
                # awake. Retain retryable disable evidence after reinitialization.
                self._pending_sleep = True
                self._flush_stop()
            self._vendor_initialized = True

    def _finish(self, result: bool | None, reason: str | None = None) -> None:
        motion = self.motion
        self.motion = None
        if motion:
            self._results[motion.identity] = result
        if reason is not None:
            self.last_refusal = reason
        self._pending_stop = True
        self._flush_stop()

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
        bins, updated = self.lidar.raw_snapshot()
        if not updated and self.lidar.port is None:
            return "lidar_missing"
        if not updated or not 0 <= now - updated <= self.config.scan_max_age_s:
            return "lidar_stale"
        if any(0 < value <= self.config.obstacle_margin_m * 100 for value in bins):
            return "obstacle_nearby"
        if (
            len(bins) != 360
            or sum(value > 0 for value in bins) < 30
            or any(not any(bins[start : start + 30]) for start in range(0, 360, 30))
        ):
            return "lidar_coverage_missing"
        return None

    def _admit_motion(self) -> None:
        reason = self.guard_reason()
        if not self.enabled:
            reason = reason or "control_authority_missing"
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
                self.update_battery(text, time.monotonic())
            except (OSError, ValueError):
                pass
            self._stop.wait(10)

    def update_battery(self, text: str, now: float) -> None:
        match = re.search(r"Last battery:\s*\[([^]]+)\]", text)
        if match:
            try:
                cells = [int(value.strip()) for value in match[1].split(",")]
                charge = battery_charge_band(cells)
            except ValueError:
                pass
            else:
                self.battery_cells_mv = cells
                self.battery_voltage = sum(cells) / 1000
                self.battery = charge / 100
                self.battery_updated = now
        docked = re.search(r"Last docked:\s*([01])", text)
        if docked:
            self.docked = docked[1] == "1"
            self.docked_updated = now

    def latest_scan(self) -> Scan | None:
        self.lidar.refresh()
        if (
            self.odometry.snapshot().quality
            and self.lidar.updated
            and (0 <= time.monotonic() - self.lidar.updated <= self.config.scan_max_age_s)
        ):
            return self.lidar.scan
        return None

    def hardware_profile(self) -> dict[str, object]:
        return dict(self._hardware)

    @staticmethod
    def _unknown_hardware_profile() -> dict[str, object]:
        return {
            "aircraft_model": "unreported",
            "aircraft_firmware": "unreported",
            "phone_model": "unreported",
            "android_version": "unreported",
            "sdk_version": "bot_shell",
            "measured_hfov_deg": None,
        }

    @classmethod
    def _read_hardware_profile(cls) -> dict[str, object]:
        """Read platform identity once; never borrow another robot's measured version."""
        result = cls._unknown_hardware_profile()
        for field, key in (
            ("aircraft_model", "ro.product.model"),
            ("android_version", "ro.build.version.release"),
        ):
            try:
                value = subprocess.run(
                    ["/system/bin/getprop", key],
                    capture_output=True,
                    text=True,
                    timeout=1,
                    check=True,
                ).stdout.strip()
            except (OSError, subprocess.SubprocessError):
                continue
            if value and len(value) <= 128 and not any(ord(char) < 32 for char in value):
                result[field] = value
        try:
            output = subprocess.run(
                ["/system/bin/dumpsys", "package", "com.ohmnilabs.telebot_rtc"],
                capture_output=True,
                text=True,
                timeout=2,
                check=True,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            pass
        else:
            version = re.search(
                r"^\s*versionName=([A-Za-z0-9._+-]{1,100})\s*$", output, re.MULTILINE
            )
            if version:
                result["aircraft_firmware"] = "telebot " + version[1]
        return result

    def run_peripheral(self, args: Mapping[str, object]) -> str:
        return self.peripherals.run(
            args, neck_allowed=self.enabled and self.spotter_present and not self.docked
        )

    def supported_peripherals(self) -> frozenset[str]:
        return self.peripherals.kinds

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
            failures = []
            # An unresponsive sensor worker must not skip camera/peripheral/socket
            # cleanup, and a camera error must not skip the independent motor stop.
            for resource in (
                self.odometry,
                self.lidar,
                self.camera,
                self.peripherals,
                self.drive_shell,
                self.battery_shell,
            ):
                if resource is None:
                    continue
                try:
                    resource.close()
                except Exception as error:
                    failures.append(error)
            if failures:
                raise OSError(f"{len(failures)} hardware resources failed to close") from failures[
                    0
                ]


def from_environment(*, key: str = "") -> OhmniDevice:
    if os.environ.get("SWEEP_ALLOW_NO_LIDAR") not in (None, "", "0"):
        raise ValueError("LiDAR is mandatory; SWEEP_ALLOW_NO_LIDAR is not supported")
    offset = os.environ.get("SWEEP_LIDAR_OFFSET_DEG")
    sign = os.environ.get("SWEEP_LIDAR_ANGLE_SIGN")
    if os.environ.get("SWEEP_HOME_CONFIRMED") == "1" and any(
        not os.environ.get(f"SWEEP_HOME_{axis}", "").strip() for axis in ("X", "Y", "YAW_DEG")
    ):
        raise ValueError("confirmed home requires explicit measured X, Y and yaw")
    config = Config(
        socket_path=os.environ.get("SWEEP_BOTSHELL", DEFAULT_PATH),
        launch=tuple(
            float(os.environ.get(f"SWEEP_HOME_{axis}", "0")) for axis in ("X", "Y", "YAW_DEG")
        ),
        lidar_offset_deg=float(offset) if offset else None,
        lidar_angle_sign=int(sign) if sign else None,
        allow_spotted_without_lidar=os.environ.get("SWEEP_ALLOW_NO_LIDAR") == "1",
        spotter_present=os.environ.get("SWEEP_SPOTTER") == "1",
    )
    camera = Camera.from_environment(os.environ, key=key)
    return OhmniDevice(config, camera=camera)


def build() -> OhmniDevice:
    return from_environment(key=os.environ.get("SWEEP_NODE_KEY", ""))
