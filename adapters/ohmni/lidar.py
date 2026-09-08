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


@dataclass(frozen=True, slots=True)
class SelfReturnBinding:
    device_id: int
    source_boot_id: str
    offset_deg: float
    angle_sign: int
    mount_x_m: float
    mount_y_m: float
    mount_z_m: float

    def __post_init__(self) -> None:
        if type(self.device_id) is not int or self.device_id <= 0:
            raise ValueError("self-return device ID must be positive")
        if (
            not isinstance(self.source_boot_id, str)
            or not self.source_boot_id
            or self.source_boot_id != self.source_boot_id.strip()
            or not self.source_boot_id.isprintable()
        ):
            raise ValueError("self-return source boot ID must be bounded non-empty text")
        if (
            not math.isfinite(self.offset_deg)
            or type(self.angle_sign) is not int
            or self.angle_sign not in (-1, 1)
        ):
            raise ValueError("self-return calibration needs a finite offset and angle sign")
        mount = (self.mount_x_m, self.mount_y_m, self.mount_z_m)
        if not all(math.isfinite(value) for value in mount):
            raise ValueError("self-return calibration needs a finite mount")


@dataclass(frozen=True, slots=True)
class SelfReturnBand:
    raw_angle_min_deg: float
    raw_angle_max_deg: float
    range_min_mm: float
    range_max_mm: float

    def __post_init__(self) -> None:
        if not (
            0 <= self.raw_angle_min_deg <= self.raw_angle_max_deg < 360
            and 0 < self.range_min_mm <= self.range_max_mm
        ):
            raise ValueError("self-return band bounds are invalid")

    def matches(self, point: Measurement) -> bool:
        return (
            self.raw_angle_min_deg <= point.angle_deg <= self.raw_angle_max_deg
            and self.range_min_mm <= point.distance_mm <= self.range_max_mm
        )


@dataclass(frozen=True, slots=True)
class SelfReturnProfile:
    evidence_id: str
    bands: tuple[SelfReturnBand, ...]
    binding: SelfReturnBinding
    enabled: bool = False
    physical_qualification_id: str | None = None
    qualification_valid_until_s: float | None = None

    def __post_init__(self) -> None:
        if not self.evidence_id or not self.bands:
            raise ValueError("self-return profile needs evidence and at least one band")
        if self.physical_qualification_id is not None and not self.physical_qualification_id:
            raise ValueError("self-return qualification ID must be non-empty")
        if self.qualification_valid_until_s is not None and not math.isfinite(
            self.qualification_valid_until_s
        ):
            raise ValueError("self-return qualification expiry must be finite")

    def matches(
        self, point: Measurement, binding: SelfReturnBinding | None, now: float | None
    ) -> bool:
        return (
            self.enabled
            and self.physical_qualification_id is not None
            and self.qualification_valid_until_s is not None
            and now is not None
            and now <= self.qualification_valid_until_s
            and binding == self.binding
            and any(band.matches(point) for band in self.bands)
        )


UNIT12_SELF_RETURN_CANDIDATE = SelfReturnProfile(
    evidence_id="unit12-baseline-4-5-6-rear-180mm",
    bands=(SelfReturnBand(174.0, 184.0, 170.0, 195.0),),
    binding=SelfReturnBinding(
        device_id=12,
        source_boot_id="c6a5f679-6ddd-4c2e-b447-835d44cab45a",
        offset_deg=131.269876,
        angle_sign=-1,
        mount_x_m=-0.218548,
        mount_y_m=0.155,
        mount_z_m=0.5334,
    ),
)


def robot_bins(
    points: Iterable[Measurement],
    offset_deg: float,
    angle_sign: int,
    *,
    self_return_profile: SelfReturnProfile | None = None,
    self_return_binding: SelfReturnBinding | None = None,
    now: float | None = None,
) -> list[int]:
    """robot_angle = offset + sign * raw_angle, counter-clockwise from robot forward.

    Calibration describes the mounting AND angle handedness. It is never guessed from
    model documentation. Multiple points in a bin retain the closest valid return.
    """
    if (
        not math.isfinite(offset_deg)
        or type(angle_sign) is not int
        or angle_sign not in (-1, 1)
    ):
        raise ValueError("lidar needs a finite mounting offset and angle sign -1 or 1")
    bins = [0] * 360
    masked_bins: set[int] = set()
    for point in points:
        if not 0 <= point.angle_deg < 360 or point.quality == 0:
            continue
        bucket = int(round(offset_deg + angle_sign * point.angle_deg)) % 360
        if self_return_profile is not None and self_return_profile.matches(
            point, self_return_binding, now
        ):
            masked_bins.add(bucket)
            continue
        if not 150 <= point.distance_mm <= 12000:
            continue
        cm = int(round(point.distance_mm / 10))
        bins[bucket] = min(bins[bucket], cm) if bins[bucket] else cm
    for bucket in masked_bins:
        bins[bucket] = 0
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
        self_return_profile: SelfReturnProfile | None = None,
        self_return_binding: SelfReturnBinding | None = None,
    ) -> None:
        self.shell, self.port, self.pose = shell, port, pose
        self.offset_deg, self.angle_sign = offset_deg, angle_sign
        self.self_return_profile = self_return_profile
        self.self_return_binding = self_return_binding
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
            and type(self.angle_sign) is int
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
        bins = robot_bins(
            revolution,
            self.offset_deg,
            self.angle_sign,  # type: ignore[arg-type]
            self_return_profile=self.self_return_profile,
            self_return_binding=self.self_return_binding,
            now=now,
        )
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
