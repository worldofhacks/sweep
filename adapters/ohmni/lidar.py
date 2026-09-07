"""Optional CP2102 RPLIDAR, with explicit mounting calibration and robot-frame scans."""

from __future__ import annotations

import fcntl
import math
import os
import select
import struct
import termios
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from .botshell import BotShell
from .models import RangeScan
from .odometry import Pose
from .spike.rplidar_protocol import (
    Measurement,
    RevolutionCollector,
    ScanParser,
    scan_request,
    set_motor_pwm_request,
    stop_request,
)


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


@dataclass(frozen=True, slots=True)
class RawRevolution:
    points: tuple[Measurement, ...]
    monotonic_s: float


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


class Lidar:
    def __init__(
        self,
        shell: BotShell,
        port: str,
        pose: Callable[[], Pose],
        *,
        offset_deg: float | None,
        angle_sign: int | None,
    ) -> None:
        self.shell, self.port, self.pose = shell, port, pose
        self.offset_deg, self.angle_sign = offset_deg, angle_sign
        self.updated = 0.0
        self.scan: RangeScan | None = None
        self._raw_revolution: RawRevolution | None = None
        self._lock = threading.RLock()
        self.error: str | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="ohmni-lidar", daemon=True)

    @property
    def calibrated(self) -> bool:
        return (
            self.offset_deg is not None
            and math.isfinite(self.offset_deg)
            and self.angle_sign in (-1, 1)
        )

    def publish(self, points: list[Measurement], now: float) -> None:
        revolution = tuple(points)
        with self._lock:
            self._raw_revolution = RawRevolution(revolution, now)
        if not self.calibrated:
            self.error = "lidar_calibration_required"
            return
        pose = self.pose()
        if pose.quality == 0:
            self.error = "odometry_unavailable"
            return
        bins = robot_bins(revolution, self.offset_deg, self.angle_sign)  # type: ignore[arg-type]
        self.scan = RangeScan(
            int(now * 1000), (pose.x, pose.y, pose.yaw_deg), 0.0, 1.0, 0.15, 12.0, bins
        )
        self.updated, self.error = now, None

    def raw_revolution(self, now: float | None = None) -> RawRevolution | None:
        now = time.monotonic() if now is None else now
        with self._lock:
            revolution = self._raw_revolution
            if revolution is None or now - revolution.monotonic_s > 0.5:
                return None
            return revolution

    def start(self) -> None:
        self._thread.start()

    def _open(self) -> int:
        fd = os.open(self.port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            attr = termios.tcgetattr(fd)
            attr[:6] = [
                0,
                0,
                termios.CS8 | termios.CREAD | termios.CLOCAL,
                0,
                termios.B115200,
                termios.B115200,
            ]
            attr[6][termios.VMIN], attr[6][termios.VTIME] = 0, 0
            termios.tcsetattr(fd, termios.TCSANOW, attr)
            termios.tcflush(fd, termios.TCIOFLUSH)
            # Linux/Android ioctl constants; DTR MUST be cleared for this motor to spin.
            fcntl.ioctl(fd, 0x5417, struct.pack("I", 0x002))
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _run(self) -> None:
        while not self._stop.is_set():
            fd = None
            try:
                self.shell.command("lidar_stop")
                self.shell.command("lidar_release")
                fd = self._open()
                os.write(fd, stop_request())
                self._stop.wait(0.1)
                termios.tcflush(fd, termios.TCIFLUSH)
                os.write(fd, set_motor_pwm_request(660))
                if self._stop.wait(1.5):
                    break
                os.write(fd, scan_request())
                parser, collector = ScanParser(), RevolutionCollector()
                # Consume the 7-byte scan descriptor before packet parsing.
                descriptor = bytearray()
                while not self._stop.is_set():
                    if not select.select([fd], [], [], 0.1)[0]:
                        continue
                    chunk = os.read(fd, 4096)
                    if not chunk:
                        raise OSError("lidar stream ended")
                    if descriptor is not None:
                        descriptor.extend(chunk)
                        if len(descriptor) < 7:
                            continue
                        if descriptor[:2] != b"\xa5\x5a" or descriptor[6] != 0x81:
                            raise OSError("unexpected lidar scan descriptor")
                        chunk = bytes(descriptor[7:])
                        descriptor = None
                    for points in collector.feed(parser.feed(chunk)):
                        self.publish(points, time.monotonic())
            except (OSError, ValueError):
                self.error = "lidar_disconnected"
                self.scan = None
            finally:
                if fd is not None:
                    try:
                        os.write(fd, stop_request())
                        os.write(fd, set_motor_pwm_request(0))
                        fcntl.ioctl(fd, 0x5416, struct.pack("I", 0x002))
                    except OSError:
                        pass
                    os.close(fd)
            self._stop.wait(1.0)

    def close(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self.shell.close()
