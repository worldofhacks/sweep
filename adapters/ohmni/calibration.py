"""Fixed, lease-bound Ohmni LiDAR self-calibration capture."""

from __future__ import annotations

import argparse
import hmac
import json
import math
import os
import signal
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from .device import Config, OhmniDevice
from .odometry import BASE_MM, Pose, encoder_delta
from .paired_encoder import EncoderPair

WHEEL_DIAMETER_MM = 152.4
TICKS_PER_MM = 16384 * (30 / 11) / (math.pi * WHEEL_DIAMETER_MM)
MAX_RUNTIME_S = 60.0
LEASE_MAX_AGE_S = 0.35
LEASE_TICK_S = 0.1
PULSE_DURATION_S = 0.5
FORWARD_SPEED_M_S = 0.04
FORWARD_DISTANCE_M = 0.08
YAW_RATE_DEG_S = 10.0
YAW_DEGREES = 10.0
MAX_WHEEL_TRAVEL_M = 0.18
MAX_YAW_DEGREES = 15.0
REVOLUTIONS_PER_STAGE = 10
STAGE_TIMEOUT_S = 8.0
SETTLE_TIMEOUT_S = 2.0
MAX_CAPTURE_DRIFT_M = 0.01
MAX_CAPTURE_DRIFT_DEG = 2.0
MAX_OUTPUT_BYTES = 2 * 1024 * 1024
MOUNT_X_M = -0.3951312693270427
MOUNT_Y_M = 0.3951312693270427
MOUNT_Z_M = 0.6096


class CalibrationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CalibrationConfig:
    wheel_diameter_mm: float = WHEEL_DIAMETER_MM
    forward_speed_m_s: float = FORWARD_SPEED_M_S
    forward_distance_m: float = FORWARD_DISTANCE_M
    yaw_rate_deg_s: float = YAW_RATE_DEG_S
    yaw_degrees: float = YAW_DEGREES
    pulse_duration_s: float = PULSE_DURATION_S
    max_wheel_travel_m: float = MAX_WHEEL_TRAVEL_M
    max_yaw_degrees: float = MAX_YAW_DEGREES
    max_runtime_s: float = MAX_RUNTIME_S

    def __post_init__(self) -> None:
        if (
            self.wheel_diameter_mm,
            self.forward_speed_m_s,
            self.forward_distance_m,
            self.yaw_rate_deg_s,
            self.yaw_degrees,
            self.pulse_duration_s,
            self.max_wheel_travel_m,
            self.max_yaw_degrees,
            self.max_runtime_s,
        ) != (
            WHEEL_DIAMETER_MM,
            FORWARD_SPEED_M_S,
            FORWARD_DISTANCE_M,
            YAW_RATE_DEG_S,
            YAW_DEGREES,
            PULSE_DURATION_S,
            MAX_WHEEL_TRAVEL_M,
            MAX_YAW_DEGREES,
            MAX_RUNTIME_S,
        ):
            raise ValueError("calibration limits are fixed by this tool")


class HostLease:
    def __init__(self, token: bytes, *, monotonic: Callable[[], float] = time.monotonic) -> None:
        if len(token) != 32:
            raise ValueError("calibration lease token must be 256 bits")
        self._token = token
        self._monotonic = monotonic
        self._last_seq = 0
        self._renewed_at: float | None = None
        self._closed = False
        self._lock = threading.Lock()

    def renew(self, sequence: int, token: bytes) -> bool:
        now = self._monotonic()
        with self._lock:
            if (
                self._closed
                or self._renewed_at is not None
                and now - self._renewed_at >= LEASE_MAX_AGE_S
                or not hmac.compare_digest(token, self._token)
                or type(sequence) is not int
                or sequence <= self._last_seq
            ):
                self._closed = True
                return False
            self._last_seq = sequence
            self._renewed_at = now
            return True

    def close(self) -> None:
        with self._lock:
            self._closed = True

    def ready(self) -> bool:
        with self._lock:
            return not self._closed and self._renewed_at is not None

    def reason(self, now: float) -> str | None:
        with self._lock:
            if (
                self._closed
                or self._renewed_at is None
                or now - self._renewed_at >= LEASE_MAX_AGE_S
            ):
                self._closed = True
                return "calibration_host_lease_expired"
            return None


class LeaseSocketPump:
    def __init__(
        self,
        host: str,
        port: int,
        token: bytes,
        lease: HostLease,
        on_lost: Callable[[], None],
    ) -> None:
        self.host, self.port = host, port
        self.token, self.lease, self.on_lost = token, lease, on_lost
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="ohmni-calibration-lease", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=LEASE_MAX_AGE_S)
        self.lease.close()

    def _run(self) -> None:
        try:
            with socket.create_connection(
                (self.host, self.port), timeout=LEASE_MAX_AGE_S
            ) as connection:
                connection.sendall(self.token.hex().encode() + b"\n")
                stream = connection.makefile("rb")
                while not self._stop.is_set():
                    line = stream.readline(80)
                    if not line:
                        return
                    try:
                        sequence_text, token_text = line.decode("ascii").strip().split(" ")
                        token = bytes.fromhex(token_text)
                    except (UnicodeDecodeError, ValueError):
                        return
                    if not self.lease.renew(int(sequence_text), token):
                        return
        except OSError:
            pass
        finally:
            self.lease.close()
            self.on_lost()


def _yaw_delta(current: float, previous: float) -> float:
    return (current - previous + 180.0) % 360.0 - 180.0


@dataclass(slots=True)
class _Progress:
    previous: EncoderPair | None = None
    wheel_travel_m: float = 0.0
    yaw_degrees: float = 0.0

    def add(self, pair: EncoderPair) -> None:
        if self.previous is not None:
            left = encoder_delta(self.previous.left, pair.left) / TICKS_PER_MM / 1000
            right = -encoder_delta(self.previous.right, pair.right) / TICKS_PER_MM / 1000
            self.wheel_travel_m += (abs(left) + abs(right)) / 2
            self.yaw_degrees += math.degrees(abs(right - left) / (BASE_MM / 1000))
        self.previous = pair


class CalibrationRunner:
    def __init__(
        self,
        device: OhmniDevice,
        lease: HostLease,
        output: Path,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        config: CalibrationConfig | None = None,
    ) -> None:
        self.device, self.lease, self.output = device, lease, output
        self.monotonic, self.sleep = monotonic, sleep
        self.config = CalibrationConfig() if config is None else config
        self._started = 0.0
        self._progress = _Progress()

    def run(self) -> Path:
        self.output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(self.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        self._started = self.monotonic()
        stages: dict[str, object] = {}
        try:
            self._require_lease()
            if not self.device.enable():
                raise CalibrationError("calibration device enable refused")
            stages["baseline"] = self._capture_stage()
            self._forward()
            self._settle()
            stages["after_forward"] = self._capture_stage()
            self._yaw()
            self._settle()
            stages["after_yaw"] = self._capture_stage()
            return self._write(stages)
        except BaseException:
            self.output.unlink(missing_ok=True)
            raise
        finally:
            self.device.disable()

    def _require_lease(self) -> None:
        if self.monotonic() - self._started >= self.config.max_runtime_s:
            raise CalibrationError("calibration_runtime_expired")
        reason = self.lease.reason(self.monotonic())
        if reason:
            self.device.stop()
            raise CalibrationError(reason)

    def _snapshot(self, now: float | None = None) -> tuple[Pose, EncoderPair]:
        pose, pair = self.device.odometry.snapshot_with_sample(
            self.monotonic() if now is None else now
        )
        if not pose.quality or pair is None:
            self.device.stop()
            raise CalibrationError("wheel_odometry_unavailable")
        self._progress.add(pair)
        if self._progress.wheel_travel_m > self.config.max_wheel_travel_m:
            self.device.stop()
            raise CalibrationError("calibration_wheel_travel_limit")
        if self._progress.yaw_degrees > self.config.max_yaw_degrees:
            self.device.stop()
            raise CalibrationError("calibration_yaw_limit")
        return pose, pair

    def _device_guard(self, now: float) -> str | None:
        reason = self.lease.reason(now)
        if reason is not None:
            return reason
        if now - self._started >= self.config.max_runtime_s:
            return "calibration_runtime_expired"
        try:
            self._snapshot(now)
        except CalibrationError as error:
            return str(error)
        wheel_reserve_m = FORWARD_SPEED_M_S * 0.1
        yaw_reserve_deg = YAW_RATE_DEG_S * 0.1
        if self._progress.wheel_travel_m + wheel_reserve_m > self.config.max_wheel_travel_m:
            return "calibration_wheel_travel_limit"
        if self._progress.yaw_degrees + yaw_reserve_deg > self.config.max_yaw_degrees:
            return "calibration_yaw_limit"
        return None

    def _capture_stage(self) -> dict[str, object]:
        stage_started = self.monotonic()
        deadline = stage_started + STAGE_TIMEOUT_S
        pose, pair = self._snapshot(stage_started)
        revolutions: list[dict[str, object]] = []
        timestamps: set[float] = set()
        while len(revolutions) < REVOLUTIONS_PER_STAGE:
            self._require_lease()
            current, current_pair = self._snapshot()
            if (
                math.hypot(current.x - pose.x, current.y - pose.y) > MAX_CAPTURE_DRIFT_M
                or abs(_yaw_delta(current.yaw_deg, pose.yaw_deg)) > MAX_CAPTURE_DRIFT_DEG
            ):
                self.device.stop()
                raise CalibrationError("calibration_capture_drift")
            revolution = (
                self.device.lidar.raw_revolution(self.monotonic()) if self.device.lidar else None
            )
            if (
                revolution is not None
                and revolution.monotonic_s not in timestamps
                and revolution.points
            ):
                timestamps.add(revolution.monotonic_s)
                revolutions.append(
                    {
                        "monotonic_s": revolution.monotonic_s,
                        "points": [
                            {
                                "angle_deg": point.angle_deg,
                                "distance_mm": point.distance_mm,
                                "quality": point.quality,
                            }
                            for point in revolution.points
                        ],
                    }
                )
                _ = current_pair
            if len(revolutions) == REVOLUTIONS_PER_STAGE:
                break
            if self.monotonic() >= deadline:
                self.device.stop()
                raise CalibrationError("raw_lidar_revolution_stale")
            self.sleep(0.01)
        return {
            "pose": {
                "x_m": pose.x,
                "y_m": pose.y,
                "yaw_deg": pose.yaw_deg,
                "quality": pose.quality,
            },
            "encoder": asdict(pair),
            "revolutions": revolutions,
        }

    def _settle(self) -> None:
        deadline = self.monotonic() + SETTLE_TIMEOUT_S
        first: Pose | None = None
        receipts: set[int] = set()
        while True:
            self._require_lease()
            pose, pair = self._snapshot()
            if first is None:
                first = pose
            receipts.add(pair.right_receipt_ns)
            stable = (
                math.hypot(pose.vx, pose.vy) <= 0.01
                and math.hypot(pose.x - first.x, pose.y - first.y) <= 0.0001
                and abs(_yaw_delta(pose.yaw_deg, first.yaw_deg)) <= 0.1
                and len(receipts) >= 3
                and self.device.motion is None
            )
            if stable:
                return
            if self.monotonic() >= deadline:
                self.device.stop()
                raise CalibrationError("calibration_settle_timeout")
            self.sleep(0.01)

    def _pulse_until(self, velocity_m_s: float, yaw_rate_deg_s: float, target: float) -> None:
        started_travel, started_yaw = self._progress.wheel_travel_m, self._progress.yaw_degrees
        while True:
            self._require_lease()
            pose_before, _ = self._snapshot()
            current = (
                self._progress.wheel_travel_m - started_travel
                if velocity_m_s
                else self._progress.yaw_degrees - started_yaw
            )
            if current >= target:
                return
            motion_id = self.device.calibration_drive_velocity(
                velocity_m_s,
                yaw_rate_deg_s,
                self.config.pulse_duration_s,
                host_lease=self._device_guard,
            )
            deadline = self.monotonic() + self.config.pulse_duration_s + LEASE_MAX_AGE_S
            while self.device.motion_done(motion_id) is False:
                self._require_lease()
                self._snapshot()
                if self.monotonic() >= deadline:
                    self.device.stop()
                    raise CalibrationError("calibration_pulse_timeout")
                self.sleep(0.01)
            if self.device.motion_done(motion_id) is not True:
                self.device.stop()
                raise CalibrationError("calibration_pulse_failed")
            pose_after, _ = self._snapshot()
            if (
                velocity_m_s
                and math.hypot(pose_after.x - pose_before.x, pose_after.y - pose_before.y) > target
            ):
                self.device.stop()
                raise CalibrationError("calibration_forward_limit")

    def _forward(self) -> None:
        self._pulse_until(self.config.forward_speed_m_s, 0.0, self.config.forward_distance_m)

    def _yaw(self) -> None:
        self._pulse_until(0.0, self.config.yaw_rate_deg_s, self.config.yaw_degrees)

    def _write(self, stages: dict[str, object]) -> Path:
        body = {
            "schema_version": 1,
            "kind": "ohmni_supervised_lidar_calibration_capture",
            "device_id": 11,
            "mount": {"x_m": MOUNT_X_M, "y_m": MOUNT_Y_M, "z_m": MOUNT_Z_M},
            "wheel_diameter_mm": self.config.wheel_diameter_mm,
            "limits": asdict(self.config),
            "stages": stages,
        }
        encoded = (json.dumps(body, separators=(",", ":"), allow_nan=False) + "\n").encode()
        if len(encoded) > MAX_OUTPUT_BYTES:
            raise CalibrationError("calibration_capture_exceeds_byte_limit")
        descriptor = os.open(self.output, os.O_WRONLY | os.O_TRUNC)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
        return self.output


def _token(path: Path) -> bytes:
    encoded = path.read_bytes().strip()
    if len(encoded) != 64:
        raise ValueError("calibration token file must contain 64 hexadecimal characters")
    return bytes.fromhex(encoded.decode("ascii"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lease-port", required=True, type=int)
    parser.add_argument("--lease-token-file", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    token = _token(args.lease_token_file)
    lease = HostLease(token)
    device = OhmniDevice(
        Config(
            spotter_present=True,
            lidar_offset_deg=None,
            lidar_angle_sign=None,
            wheel_diameter_mm=WHEEL_DIAMETER_MM,
        )
    )
    pump = LeaseSocketPump("127.0.0.1", args.lease_port, token, lease, device.disable)
    previous = signal.getsignal(signal.SIGTERM)

    def stop(_signal: int, _frame: object) -> None:
        pump.close()
        device.disable()

    signal.signal(signal.SIGTERM, stop)
    pump.start()
    deadline = time.monotonic() + 2.0
    while not lease.ready() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not lease.ready():
        pump.close()
        device.disable()
        raise RuntimeError("calibration host lease was not received")
    try:
        CalibrationRunner(device, lease, args.output).run()
    finally:
        pump.close()
        device.close()
        signal.signal(signal.SIGTERM, previous)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
