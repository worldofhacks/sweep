"""Required robust RPLIDAR owner, raw safety ranges and calibrated robot-frame scans."""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path

from nodekit.device import Scan

from .botshell import BotShell
from .odometry import Pose
from .rplidar import Lidar as RawLidar
from .spike.rplidar_protocol import Measurement


def discover(sysfs: Path = Path("/sys/bus/usb-serial/devices")) -> str | None:
    """Never assume ttyUSB0: without the kit that name belongs to the wheel bus."""
    if not sysfs.exists():
        return None
    for entry in sorted(sysfs.iterdir()):
        if not entry.name.startswith("ttyUSB"):
            continue
        for parent in entry.resolve().parents:
            try:
                vendor = (parent / "idVendor").read_text().strip().lower()
                product = (parent / "idProduct").read_text().strip().lower()
            except OSError:
                continue
            if (vendor, product) == ("10c4", "ea60"):
                return f"/dev/{entry.name}"
            break
    return None


def robot_bins(points: Iterable[Measurement], offset_deg: float, angle_sign: int) -> list[int]:
    """robot_angle = offset + sign * raw_angle, counter-clockwise from robot forward.

    Calibration describes the mounting AND angle handedness. It is never guessed from
    model documentation. Multiple points in a bin retain the closest valid return.
    """
    if not math.isfinite(offset_deg) or angle_sign not in (-1, 1):
        raise ValueError("lidar needs a finite mounting offset and angle sign -1 or 1")
    bins = [0] * 360
    for point in points:
        if not 0 <= point.angle_deg < 360 or point.quality == 0:
            continue
        if not 150 <= point.distance_mm <= 12000:
            continue
        bucket = int(round(offset_deg + angle_sign * point.angle_deg)) % 360
        cm = int(round(point.distance_mm / 10))
        bins[bucket] = min(bins[bucket], cm) if bins[bucket] else cm
    return bins


class _ShellAdapter:
    """The tested serial owner uses wait; packaged bot-shell uses timeout."""

    def __init__(self, shell: BotShell, prepare: Callable[[], None]) -> None:
        self.shell, self.prepare = shell, prepare

    def command(self, text: str, *, wait: float = 0.2) -> str:
        # RawLidar has already claimed its cross-process owner lock, but has not
        # opened the serial reader. Vendor init can only occur before that reader.
        self.prepare()
        return self.shell.command(text, timeout=wait)


class Lidar:
    """The robust serial owner plus optional calibrated world-map projection.

    Safety uses raw full-circle ranges, which need no mounting assumption. Only
    calibrated scans with fresh wheel odometry may enter the robot/world map.
    """

    def __init__(
        self,
        shell: BotShell,
        port: str | None,
        pose: Callable[[], Pose],
        *,
        offset_deg: float | None,
        angle_sign: int | None,
        prepare: Callable[[], None] = lambda: None,
        reader_factory=RawLidar,
    ) -> None:
        self.shell, self.pose = shell, pose
        self.offset_deg, self.angle_sign = offset_deg, angle_sign
        self.updated = 0.0
        self.scan: Scan | None = None
        self.error: str | None = "lidar_starting"
        self._ranges = (0,) * 360
        self._lock = threading.RLock()
        self._reader = reader_factory(_ShellAdapter(shell, prepare), port)
        self._running = False

    @property
    def port(self) -> str | None:
        return self._reader.port

    @property
    def calibrated(self) -> bool:
        return (
            self.offset_deg is not None
            and math.isfinite(self.offset_deg)
            and self.angle_sign in (-1, 1)
        )

    def publish(self, points: list[Measurement], now: float) -> None:
        """Publish a measured revolution; also used by calibration fixture tests."""
        bins = [0] * 360
        for point in points:
            if not 0 <= point.angle_deg < 360 or point.quality == 0:
                continue
            if not math.isfinite(point.distance_mm) or point.distance_mm <= 0:
                continue
            index = int(point.angle_deg)
            cm = max(1, int(point.distance_mm / 10))
            bins[index] = min(bins[index], cm) if bins[index] else cm
        with self._lock:
            self._publish_bins(tuple(bins), now)
            # Preserve precise angles for mounting calibration fixtures.
            if self.scan is not None:
                self.scan.ranges_cm = robot_bins(points, self.offset_deg, self.angle_sign)

    def _publish_bins(self, bins: tuple[int, ...], updated: float) -> None:
        self._ranges, self.updated = bins, updated
        self.scan = None
        if not updated:
            self.error = "lidar_stale"
            return
        if not self.calibrated:
            self.error = "lidar_calibration_required"
            return
        pose = self.pose()
        if not pose.quality:
            self.error = "odometry_unavailable"
            return
        ranges = [0] * 360
        for raw_angle, distance in enumerate(bins):
            if not 15 <= distance <= 1200:
                continue
            index = int(round(self.offset_deg + self.angle_sign * raw_angle)) % 360
            ranges[index] = min(ranges[index], distance) if ranges[index] else distance
        self.scan = Scan(
            int(updated * 1000), (pose.x, pose.y, pose.yaw_deg), 0.0, 1.0, 0.15, 12.0, ranges
        )
        self.error = None

    def refresh(self) -> None:
        with self._lock:
            if not self._running:
                return
            bins, updated = self._reader.snapshot()
            if updated != self.updated or not updated:
                self._publish_bins(bins, updated)

    def raw_snapshot(self) -> tuple[tuple[int, ...], float]:
        with self._lock:
            self.refresh()
            return self._ranges, self.updated

    def diagnostics(self, now: float | None = None) -> dict[str, object]:
        now = time.monotonic() if now is None else now
        bins, updated = self.raw_snapshot()
        age = max(0, int((now - updated) * 1000)) if updated else None
        valid = [value for value in bins if value > 0]
        info = self._reader.info
        return {
            "present": self.port is not None,
            "port": self.port,
            "model": info.get("model"),
            "firmware": info.get("firmware"),
            "health": info.get("health") if updated else None,
            "baudrate": info.get("baudrate"),
            "motor_pwm_requested": self._reader.motor_pwm,
            "status": "scanning" if updated and age <= 500 else "unavailable",
            "fault": (self._reader.fault_reason or "")[:512] or None if self._running else None,
            "calibrated": self.calibrated,
            "frame": "lidar_raw",
            "scan_age_ms": age,
            "valid_bins": len(valid),
            "sectors": len({i // 30 for i, value in enumerate(bins) if value > 0}),
            "nearest_m": min(valid) / 100 if valid else None,
            "ranges_cm": list(bins),
            "map_available": self.scan is not None and age is not None and age <= 500,
            "map_status": self.error or "available",
        }

    def start(self) -> None:
        self._running = True
        self._reader.start()

    def close(self) -> None:
        try:
            self._reader.stop()
        finally:
            with self._lock:
                self._running = False
                self._ranges, self.updated, self.scan = (0,) * 360, 0.0, None
            self.shell.close()
