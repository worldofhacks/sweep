"""Fail-closed RPLIDAR A2M8 reader using Android/Linux's serial APIs.

Protocol references: Slamtec rplidar_sdk/sdk/include/sl_lidar_cmd.h and
Roboticia/RPLidar's A2 motor control. No wheel-bus serial port is probed.
"""

from __future__ import annotations

import fcntl
import logging
import os
import re
import select
import stat
import struct
import tempfile
import termios
import threading
import time
from pathlib import Path

LOG = logging.getLogger("ohmni-node.lidar")
EMPTY_SCAN = (0,) * 360


class LidarError(RuntimeError):
    """A scan is unsafe to use, or the scanner cannot be controlled."""


class _Stopped(Exception):
    pass


class _Revolution:
    """Consume standard five-byte measurements after a validated descriptor."""

    def __init__(self) -> None:
        self.bins = [0] * 360
        self.started: float | None = None
        self.previous_angle = 0.0
        self.sweep = 0.0

    def feed(self, packet: bytes, now: float) -> tuple[tuple[int, ...], float] | None:
        quality_sync, angle_low, angle_high, distance_low, distance_high = packet
        start = bool(quality_sync & 1)
        if start == bool(quality_sync & 2) or not angle_low & 1:
            raise LidarError("invalid scan measurement framing")
        angle = ((angle_low | angle_high << 8) >> 1) / 64.0
        if not 0 <= angle < 360:
            raise LidarError("invalid scan angle")
        complete = None
        if self.started is not None:
            # A2M8 reports corrected angles that can move backwards by several
            # degrees between adjacent returns. Count signed net rotation:
            # modulo-only accumulation turns each correction into a false lap.
            self.sweep += (angle - self.previous_angle + 180) % 360 - 180
            if self.sweep > 420:
                raise LidarError(
                    "scan lost revolution synchronization: "
                    f"previous={self.previous_angle:.3f} angle={angle:.3f} "
                    f"start={start} sweep={self.sweep:.3f}"
                )
        if start:
            if self.started is not None:
                count = sum(value > 0 for value in self.bins)
                quadrants = {i // 90 for i, value in enumerate(self.bins) if value > 0}
                if self.sweep < 300 or count < Lidar.MIN_VALID_BINS or len(quadrants) < 3:
                    raise LidarError(
                        f"insufficient scan coverage: {count} bins, {len(quadrants)} quadrants"
                    )
                if not 0 <= now - self.started <= Lidar.MAX_SCAN_AGE_S:
                    raise LidarError("scan revolution is stale")
                # Use the beginning of acquisition, so old points cannot gain a
                # fresh timestamp merely because the final byte just arrived.
                complete = (tuple(self.bins), self.started)
            self.bins = [0] * 360
            self.started = now
            self.sweep = 0.0
        self.previous_angle = angle
        if self.started is not None and quality_sync >> 2:
            distance_q2 = distance_low | distance_high << 8
            if distance_q2:
                # Floor to centimetres, preserving very close hits as 1 cm.
                distance_cm = max(1, distance_q2 // 40)
                bucket = int(angle)
                previous = self.bins[bucket]
                self.bins[bucket] = min(previous, distance_cm) if previous else distance_cm
        return complete


class Lidar:
    """Own one CP210x A2M8 and expose only fresh, complete scan snapshots."""

    SYSFS_DIRECTORY = "/sys/bus/usb-serial/devices"
    DEVICE_DIRECTORY = "/dev"
    MAX_SCAN_AGE_S = 0.75
    MIN_VALID_BINS = 30
    RESPONSE_TIMEOUT_S = 1.0
    FIRST_SCAN_TIMEOUT_S = 2.0
    RETRY_S = 2.0
    MOTOR_SPINUP_S = 1.5
    MAX_QUEUED_BYTES = 4096
    OWNER_LOCK_PATH = os.path.join(
        "/data/local/tmp" if os.path.isdir("/data/local/tmp") else tempfile.gettempdir(),
        "sweep-rplidar-owner.lock",
    )

    def __init__(self, shell, port: str | None = None) -> None:
        self.shell = shell
        self._configured_port = port
        self._port: str | None = None
        self._lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._bins = EMPTY_SCAN
        self._updated = 0.0
        self._fault: str | None = "LiDAR has not started"
        self.info: dict[str, object] = {}
        self.motor_pwm = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @classmethod
    def _ports(cls) -> list[str]:
        try:
            names = sorted(os.listdir(cls.SYSFS_DIRECTORY))
        except OSError:
            return []
        matches = []
        for name in names:
            if not re.fullmatch(r"ttyUSB\d+", name):
                continue
            device = Path(os.path.realpath(os.path.join(cls.SYSFS_DIRECTORY, name)))
            for ancestor in (device, *device.parents):
                try:
                    vendor = (ancestor / "idVendor").read_text().strip().lower()
                    product = (ancestor / "idProduct").read_text().strip().lower()
                except OSError:
                    continue
                if (vendor, product) == ("10c4", "ea60"):
                    port = os.path.join(cls.DEVICE_DIRECTORY, name)
                    try:
                        if stat.S_ISCHR(os.stat(port).st_mode):
                            matches.append(port)
                    except OSError:
                        pass
                break
        return matches

    @classmethod
    def discover(cls) -> str | None:
        ports = cls._ports()
        return ports[0] if len(ports) == 1 else None

    @classmethod
    def _verified_port(cls, requested: str) -> str:
        resolved = os.path.realpath(requested)
        try:
            requested_stat = os.stat(requested)
        except OSError:
            requested_stat = None
        for port in cls._ports():
            if os.path.realpath(port) == resolved:
                return port
            # Android may expose another character-device node rather than a
            # symlink under /dev/usb. The device number proves that alias maps
            # to the same sysfs-confirmed CP210x interface.
            if requested_stat is not None and stat.S_ISCHR(requested_stat.st_mode):
                try:
                    if os.stat(port).st_rdev == requested_stat.st_rdev:
                        return port
                except OSError:
                    continue
        raise LidarError(
            f"configured LiDAR port is not a present CP210x 10c4:ea60 device: {requested}"
        )

    @property
    def port(self) -> str | None:
        with self._lock:
            return self._port

    def _expire_locked(self) -> None:
        if self._updated and not 0 <= time.monotonic() - self._updated <= self.MAX_SCAN_AGE_S:
            self._bins, self._updated, self._fault = EMPTY_SCAN, 0.0, "LiDAR scan is stale"

    def snapshot(self) -> tuple[tuple[int, ...], float]:
        with self._lock:
            self._expire_locked()
            return self._bins, self._updated

    @property
    def healthy(self) -> bool:
        return self.snapshot()[1] > 0

    @property
    def fault_reason(self) -> str | None:
        with self._lock:
            self._expire_locked()
            return self._fault

    def _invalidate(self, reason: str) -> None:
        with self._lock:
            self._bins, self._updated, self._fault = EMPTY_SCAN, 0.0, reason

    def _publish(self, bins: tuple[int, ...], updated: float) -> None:
        with self._lock:
            if not self._stop.is_set():
                self._bins, self._updated, self._fault = bins, updated, None
                self._expire_locked()

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._invalidate("LiDAR is starting")
            self._thread = threading.Thread(target=self._run, name="rplidar-a2m8", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        with self._lifecycle_lock:
            self._stop.set()
            self._invalidate("LiDAR stopped")
            if self._thread is not None and self._thread is not threading.current_thread():
                # Serial waits are interruptible. Vendor socket acquisition has
                # a bounded connect timeout and may take longer than serial I/O.
                self._thread.join(timeout=20.0)
                if self._thread.is_alive():
                    raise LidarError("LiDAR worker did not stop within 20 seconds")

    def _pause(self, seconds: float) -> None:
        if self._stop.wait(seconds):
            raise _Stopped()

    def _claim(self) -> int:
        """Protect vendor handoff even when the vendor owns an exclusive tty."""
        fd = os.open(
            self.OWNER_LOCK_PATH,
            os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise LidarError("LiDAR ownership lock is not a regular file")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _release_vendor(self) -> None:
        for command in ("lidar_stop", "lidar_release"):
            if self._stop.is_set():
                raise _Stopped()
            self.shell.command(command, wait=0.2)

    def _open(self, port: str) -> int:
        verified = self._verified_port(port)
        fd = os.open(verified, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        exclusive = False
        try:
            # flock also protects against duplicate root processes, which can
            # bypass TIOCEXCL. Claim ownership before vendor release commands.
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.ioctl(fd, getattr(termios, "TIOCEXCL", 0x540C))
            exclusive = True
            if os.fstat(fd).st_rdev != os.stat(self._verified_port(verified)).st_rdev:
                raise LidarError("LiDAR USB device changed during open")
            attr = termios.tcgetattr(fd)
            attr[0] = attr[1] = attr[3] = 0
            attr[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
            attr[4] = attr[5] = termios.B115200
            attr[6][termios.VMIN] = attr[6][termios.VTIME] = 0
            termios.tcsetattr(fd, termios.TCSANOW, attr)
            termios.tcflush(fd, termios.TCIOFLUSH)
            fcntl.ioctl(
                fd, getattr(termios, "TIOCMBIC", 0x5417), struct.pack("I", termios.TIOCM_DTR)
            )
            return fd
        except BaseException:
            if exclusive:
                try:
                    fcntl.ioctl(fd, getattr(termios, "TIOCNXCL", 0x540D))
                except OSError:
                    pass
            os.close(fd)
            raise

    def _write(self, fd: int, data: bytes, *, cleanup: bool = False) -> None:
        deadline = time.monotonic() + (0.2 if cleanup else self.RESPONSE_TIMEOUT_S)
        while data:
            if self._stop.is_set() and not cleanup:
                raise _Stopped()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LidarError("serial write timed out")
            if not select.select([], [fd], [], min(remaining, 0.05))[1]:
                continue
            try:
                count = os.write(fd, data)
            except BlockingIOError:
                continue
            if count <= 0:
                raise LidarError("LiDAR serial write disconnected")
            data = data[count:]

    def _motor(self, fd: int, pwm: int, *, cleanup: bool = False) -> None:
        frame = b"\xa5\xf0\x02" + struct.pack("<H", pwm)
        checksum = 0
        for value in frame:
            checksum ^= value
        self._write(fd, frame + bytes([checksum]), cleanup=cleanup)

    def _read(self, fd: int, size: int, deadline: float) -> bytes:
        while True:
            if self._stop.is_set():
                raise _Stopped()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LidarError("LiDAR serial read timed out")
            if not select.select([fd], [], [], min(remaining, 0.05))[0]:
                continue
            try:
                chunk = os.read(fd, size)
            except BlockingIOError:
                continue
            if not chunk:
                raise LidarError("LiDAR serial disconnected")
            return chunk

    def _read_exact(self, fd: int, size: int, deadline: float) -> bytes:
        data = bytearray()
        while len(data) < size:
            data.extend(self._read(fd, size - len(data), deadline))
        return bytes(data)

    def _descriptor(self, fd: int, size: int, mode: int, kind: int, deadline: float) -> None:
        raw = self._read_exact(fd, 7, deadline)
        size_mode = int.from_bytes(raw[2:6], "little")
        if raw[:2] != b"\xa5\x5a" or (size_mode & 0x3FFFFFFF, size_mode >> 30, raw[6]) != (
            size,
            mode,
            kind,
        ):
            raise LidarError(f"unexpected LiDAR response descriptor: {raw.hex()}")

    def _query(self, fd: int, command: int, size: int, kind: int) -> bytes:
        self._write(fd, bytes([0xA5, command]))
        deadline = time.monotonic() + self.RESPONSE_TIMEOUT_S
        self._descriptor(fd, size, 0, kind, deadline)
        return self._read_exact(fd, size, deadline)

    def _startup(self, fd: int) -> None:
        self._write(fd, b"\xa5\x25")
        self._pause(0.1)
        termios.tcflush(fd, termios.TCIFLUSH)
        info = self._query(fd, 0x50, 20, 0x04)
        if info[0] != 0x28:
            raise LidarError(f"expected A2M8 (0x28), found model 0x{info[0]:02x}")
        health = self._query(fd, 0x52, 3, 0x06)
        error_code = int.from_bytes(health[1:], "little")
        if health[0] != 0 or error_code != 0:
            raise LidarError(f"LiDAR health status={health[0]} error={error_code}")
        with self._lock:
            self.info = {"model": "RPLIDAR A2M8", "firmware": f"{info[2]}.{info[1]}",
                         "health": "good", "baudrate": 115200}
        LOG.info(
            "LiDAR A2M8 verified on %s firmware=%d.%d health=good; starting motor PWM=660",
            self.port,
            info[2],
            info[1],
        )
        fcntl.ioctl(fd, getattr(termios, "TIOCMBIC", 0x5417), struct.pack("I", termios.TIOCM_DTR))
        self._motor(fd, 660)
        self.motor_pwm = 660
        self._pause(self.MOTOR_SPINUP_S)
        self._write(fd, b"\xa5\x20")
        self._descriptor(fd, 5, 1, 0x81, time.monotonic() + self.RESPONSE_TIMEOUT_S)

    def _scan(self, fd: int) -> None:
        accumulator = _Revolution()
        buf = bytearray()
        deadline = time.monotonic() + self.FIRST_SCAN_TIMEOUT_S
        logged = 0.0
        scans = 0
        while not self._stop.is_set():
            # At 115200 baud, >4096 queued bytes already represents >350 ms
            # latency. Restart instead of relabelling buffered scans as fresh.
            queued = struct.unpack(
                "I", fcntl.ioctl(fd, getattr(termios, "FIONREAD", 0x541B), struct.pack("I", 0))
            )[0]
            if queued > self.MAX_QUEUED_BYTES:
                raise LidarError(f"LiDAR serial backlog: {queued} bytes")
            buf.extend(self._read(fd, 4096, deadline))
            consumed = 0
            while consumed + 5 <= len(buf):
                if self._stop.is_set():
                    raise _Stopped()
                now = time.monotonic()
                if now > deadline:
                    raise LidarError("LiDAR complete scan timed out")
                result = accumulator.feed(bytes(buf[consumed : consumed + 5]), now)
                consumed += 5
                if result is not None:
                    bins, updated = result
                    self._publish(bins, updated)
                    deadline = updated + self.MAX_SCAN_AGE_S
                    scans += 1
                    if now - logged >= 10.0:
                        valid = [value for value in bins if value]
                        sectors = len({i // 30 for i, value in enumerate(bins) if value})
                        LOG.info(
                            "LiDAR healthy scans=%d valid_bins=%d sectors=%d/12 nearest_cm=%d",
                            scans,
                            len(valid),
                            sectors,
                            min(valid),
                        )
                        logged = now
            del buf[:consumed]

    def _close(self, fd: int) -> None:
        self.motor_pwm = 0
        # Each operation is independent: a failed stop must never leak the fd
        # or prevent the second motor-stop mechanism from being attempted.
        for action in (
            lambda: self._write(fd, b"\xa5\x25", cleanup=True),
            lambda: self._motor(fd, 0, cleanup=True),
            lambda: fcntl.ioctl(
                fd, getattr(termios, "TIOCMBIS", 0x5416), struct.pack("I", termios.TIOCM_DTR)
            ),
            lambda: fcntl.ioctl(fd, getattr(termios, "TIOCNXCL", 0x540D)),
        ):
            try:
                action()
            except (OSError, LidarError):
                pass
        os.close(fd)

    def _run(self) -> None:
        while not self._stop.is_set():
            fd = None
            owner_fd = None
            try:
                port = (
                    self._verified_port(self._configured_port)
                    if self._configured_port
                    else self.discover()
                )
                with self._lock:
                    self._port = port
                if port is None:
                    raise LidarError(
                        "no unique CP210x 10c4:ea60 LiDAR device found; check powered USB hub/cable"
                    )
                self._invalidate("LiDAR is connecting")
                owner_fd = self._claim()
                self._release_vendor()
                fd = self._open(port)
                self._startup(fd)
                self._scan(fd)
            except _Stopped:
                break
            except Exception as error:
                self._invalidate(str(error))
                LOG.warning("LiDAR unavailable: %s; retrying in %.1fs", error, self.RETRY_S)
            finally:
                if fd is not None:
                    self._invalidate(
                        "LiDAR stopped"
                        if self._stop.is_set()
                        else self.fault_reason or "LiDAR disconnected"
                    )
                    try:
                        self._close(fd)
                    except OSError:
                        LOG.exception("LiDAR descriptor close failed")
                if owner_fd is not None:
                    os.close(owner_fd)
            if self._stop.wait(self.RETRY_S):
                break
