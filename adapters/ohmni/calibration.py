"""Fixed, lease-bound Ohmni LiDAR self-calibration capture."""

from __future__ import annotations

import argparse
import hashlib
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
from dataclasses import field as dataclass_field
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
LONGER_FORWARD_DISTANCE_M = 0.4
LONGER_YAW_DEGREES = 30.0
LONGER_MAX_WHEEL_TRAVEL_M = 0.6
LONGER_MAX_YAW_DEGREES = 40.0
MULTISTAGE_FORWARD_DISTANCE_M = 0.4
MULTISTAGE_YAW_DEGREES = 60.0
MULTISTAGE_MAX_WHEEL_TRAVEL_M = 1.05
MULTISTAGE_MAX_YAW_DEGREES = 70.0
REVOLUTIONS_PER_STAGE = 10
STAGE_TIMEOUT_S = 8.0
SETTLE_TIMEOUT_S = 2.0
MAX_CAPTURE_DRIFT_M = 0.001
MAX_CAPTURE_DRIFT_DEG = 0.1
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
        ) not in (
            (
                WHEEL_DIAMETER_MM,
                FORWARD_SPEED_M_S,
                FORWARD_DISTANCE_M,
                YAW_RATE_DEG_S,
                YAW_DEGREES,
                PULSE_DURATION_S,
                MAX_WHEEL_TRAVEL_M,
                MAX_YAW_DEGREES,
                MAX_RUNTIME_S,
            ),
            (
                WHEEL_DIAMETER_MM,
                FORWARD_SPEED_M_S,
                LONGER_FORWARD_DISTANCE_M,
                YAW_RATE_DEG_S,
                LONGER_YAW_DEGREES,
                PULSE_DURATION_S,
                LONGER_MAX_WHEEL_TRAVEL_M,
                LONGER_MAX_YAW_DEGREES,
                MAX_RUNTIME_S,
            ),
            (
                WHEEL_DIAMETER_MM,
                FORWARD_SPEED_M_S,
                MULTISTAGE_FORWARD_DISTANCE_M,
                YAW_RATE_DEG_S,
                MULTISTAGE_YAW_DEGREES,
                PULSE_DURATION_S,
                MULTISTAGE_MAX_WHEEL_TRAVEL_M,
                MULTISTAGE_MAX_YAW_DEGREES,
                MAX_RUNTIME_S,
            ),
        ):
            raise ValueError("calibration limits must match an immutable capture profile")

    @classmethod
    def longer(cls) -> CalibrationConfig:
        return cls(
            forward_distance_m=LONGER_FORWARD_DISTANCE_M,
            yaw_degrees=LONGER_YAW_DEGREES,
            max_wheel_travel_m=LONGER_MAX_WHEEL_TRAVEL_M,
            max_yaw_degrees=LONGER_MAX_YAW_DEGREES,
        )

    @classmethod
    def multistage(cls) -> CalibrationConfig:
        return cls(
            forward_distance_m=MULTISTAGE_FORWARD_DISTANCE_M,
            yaw_degrees=MULTISTAGE_YAW_DEGREES,
            max_wheel_travel_m=MULTISTAGE_MAX_WHEEL_TRAVEL_M,
            max_yaw_degrees=MULTISTAGE_MAX_YAW_DEGREES,
        )

    @property
    def has_second_translation(self) -> bool:
        return self.yaw_degrees == MULTISTAGE_YAW_DEGREES


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
                        sequence = int(sequence_text)
                    except (UnicodeDecodeError, ValueError):
                        return
                    if not self.lease.renew(sequence, token):
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
    lock: threading.Lock = dataclass_field(default_factory=threading.Lock)

    def add(self, pair: EncoderPair) -> None:
        with self.lock:
            if (
                self.previous is not None
                and pair.right_receipt_ns <= self.previous.right_receipt_ns
            ):
                return
            if self.previous is not None:
                left = encoder_delta(self.previous.left, pair.left) / TICKS_PER_MM / 1000
                right = -encoder_delta(self.previous.right, pair.right) / TICKS_PER_MM / 1000
                self.wheel_travel_m += (abs(left) + abs(right)) / 2
                self.yaw_degrees += math.degrees(abs(right - left) / (BASE_MM / 1000))
            self.previous = pair

    def values(self) -> tuple[float, float]:
        with self.lock:
            return self.wheel_travel_m, self.yaw_degrees


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
        boot_id: str | None = None,
        executed_bundle_source_sha256: str | None = None,
    ) -> None:
        self.device, self.lease, self.output = device, lease, output
        self.monotonic, self.sleep = monotonic, sleep
        self.config = CalibrationConfig() if config is None else config
        self.boot_id = boot_id
        self.executed_bundle_source_sha256 = executed_bundle_source_sha256
        self._started = 0.0
        self._progress = _Progress()

    def run(self) -> Path:
        self.output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(self.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        self._started = self.monotonic()
        stages: dict[str, object] = {}
        try:
            self._require_lease()
            if not self.device.enable():
                raise CalibrationError("calibration device enable refused")
            self._initialize()
            stages["baseline"] = self._capture_stage()
            self._forward()
            self._settle()
            stages["after_forward"] = self._capture_stage()
            self._yaw()
            self._settle()
            stages["after_yaw"] = self._capture_stage()
            if self.config.has_second_translation:
                self._forward()
                self._settle()
                stages["after_cross_forward"] = self._capture_stage()
            self.device.disable()
            self._write(stages, descriptor)
        except BaseException:
            self._remove_owned_output(descriptor)
            raise
        finally:
            try:
                self.device.disable()
            except BaseException:
                self._remove_owned_output(descriptor)
                raise
            finally:
                os.close(descriptor)
        return self.output

    def _remove_owned_output(self, descriptor: int) -> None:
        try:
            current = self.output.lstat()
        except FileNotFoundError:
            return
        owned = os.fstat(descriptor)
        if (current.st_dev, current.st_ino) == (owned.st_dev, owned.st_ino):
            self.output.unlink()

    def _initialize(self) -> None:
        deadline = self.monotonic() + STAGE_TIMEOUT_S
        while self.monotonic() < deadline:
            self._require_lease()
            try:
                self._snapshot()
            except CalibrationError:
                self.sleep(0.01)
                continue
            revolution = (
                self.device.lidar.raw_revolution(self.monotonic()) if self.device.lidar else None
            )
            if revolution is not None and len(revolution.points) >= 20:
                return
            self.sleep(0.01)
        self.device.stop()
        raise CalibrationError("calibration_initialization_timeout")

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
        wheel_travel_m, yaw_degrees = self._progress.values()
        if wheel_travel_m > self.config.max_wheel_travel_m:
            self.device.stop()
            raise CalibrationError("calibration_wheel_travel_limit")
        if yaw_degrees > self.config.max_yaw_degrees:
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
        wheel_reserve_m = self.config.forward_speed_m_s * 0.1
        yaw_reserve_deg = self.config.yaw_rate_deg_s * 0.1
        wheel_travel_m, yaw_degrees = self._progress.values()
        if wheel_travel_m + wheel_reserve_m > self.config.max_wheel_travel_m:
            return "calibration_wheel_travel_limit"
        if yaw_degrees + yaw_reserve_deg > self.config.max_yaw_degrees:
            return "calibration_yaw_limit"
        return None

    def _capture_stage(self) -> dict[str, object]:
        stage_started = self.monotonic()
        deadline = stage_started + STAGE_TIMEOUT_S
        pose, pair = self._snapshot(stage_started)
        revolutions: list[dict[str, object]] = []
        timestamps: set[float] = set()
        max_translation_drift_m = 0.0
        max_yaw_drift_deg = 0.0
        max_encoder_revolution_delta_s = 0.0
        while len(revolutions) < REVOLUTIONS_PER_STAGE:
            self._require_lease()
            current, _ = self._snapshot()
            translation_drift_m = math.hypot(current.x - pose.x, current.y - pose.y)
            yaw_drift_deg = abs(_yaw_delta(current.yaw_deg, pose.yaw_deg))
            max_translation_drift_m = max(max_translation_drift_m, translation_drift_m)
            max_yaw_drift_deg = max(max_yaw_drift_deg, yaw_drift_deg)
            if translation_drift_m > MAX_CAPTURE_DRIFT_M or yaw_drift_deg > MAX_CAPTURE_DRIFT_DEG:
                self.device.stop()
                raise CalibrationError("calibration_capture_drift")
            revolution = (
                self.device.lidar.raw_revolution(self.monotonic()) if self.device.lidar else None
            )
            if (
                revolution is not None
                and revolution.monotonic_s >= stage_started
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
                max_encoder_revolution_delta_s = max(
                    max_encoder_revolution_delta_s,
                    abs(revolution.monotonic_s - pair.right_receipt_ns / 1_000_000_000),
                )
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
            "stage_started_monotonic_s": stage_started,
            "stage_completed_monotonic_s": self.monotonic(),
            "max_translation_drift_m": max_translation_drift_m,
            "max_yaw_drift_deg": max_yaw_drift_deg,
            "max_encoder_revolution_delta_s": max_encoder_revolution_delta_s,
            "monotonic_clock": "linux_monotonic",
        }

    def _settle(self) -> None:
        deadline = self.monotonic() + SETTLE_TIMEOUT_S
        samples: list[tuple[Pose, EncoderPair]] = []
        while True:
            self._require_lease()
            pose, pair = self._snapshot()
            if not samples or pair.right_receipt_ns > samples[-1][1].right_receipt_ns:
                samples.append((pose, pair))
                samples = samples[-3:]
            if len(samples) == 3:
                first, last = samples[0][0], samples[-1][0]
                stable = (
                    math.hypot(last.vx, last.vy) <= 0.01
                    and math.hypot(last.x - first.x, last.y - first.y) <= 0.0001
                    and abs(_yaw_delta(last.yaw_deg, first.yaw_deg)) <= 0.1
                    and self.device.motion is None
                )
                if stable:
                    return
            if self.monotonic() >= deadline:
                self.device.stop()
                raise CalibrationError("calibration_settle_timeout")
            self.sleep(0.01)

    def _pulse_until(self, velocity_m_s: float, yaw_rate_deg_s: float, target: float) -> None:
        started_travel, started_yaw = self._progress.values()
        while True:
            self._require_lease()
            pose_before, _ = self._snapshot()
            current = (
                self._progress.values()[0] - started_travel
                if velocity_m_s
                else self._progress.values()[1] - started_yaw
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
            pulse_progress = (
                math.hypot(pose_after.x - pose_before.x, pose_after.y - pose_before.y)
                if velocity_m_s
                else abs(_yaw_delta(pose_after.yaw_deg, pose_before.yaw_deg))
            )
            if pulse_progress < (0.001 if velocity_m_s else 0.25):
                self.device.stop()
                raise CalibrationError("calibration_no_motion")
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

    def _write(self, stages: dict[str, object], descriptor: int) -> None:
        body = {
            "schema_version": 1,
            "kind": "ohmni_supervised_lidar_calibration_capture",
            "device_id": 11,
            "mount": {"x_m": MOUNT_X_M, "y_m": MOUNT_Y_M, "z_m": MOUNT_Z_M},
            "wheel_diameter_mm": self.config.wheel_diameter_mm,
            "limits": asdict(self.config),
            "boot_id": self.boot_id,
            "executed_bundle_source_sha256": self.executed_bundle_source_sha256,
            "stages": stages,
        }
        encoded = (json.dumps(body, separators=(",", ":"), allow_nan=False) + "\n").encode()
        if len(encoded) > MAX_OUTPUT_BYTES:
            raise CalibrationError("calibration_capture_exceeds_byte_limit")
        with os.fdopen(os.dup(descriptor), "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        current, owned = self.output.lstat(), os.fstat(descriptor)
        if (current.st_dev, current.st_ino) != (owned.st_dev, owned.st_ino):
            raise CalibrationError("calibration_output_replaced")


def _token(path: Path) -> bytes:
    encoded = path.read_bytes().strip()
    if len(encoded) != 64:
        raise ValueError("calibration token file must contain 64 hexadecimal characters")
    return bytes.fromhex(encoded.decode("ascii"))


def calibration_source_sha256() -> str:
    root = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        digest.update(path.relative_to(root).as_posix().encode() + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lease-port", required=True, type=int)
    parser.add_argument("--lease-token-file", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--expected-boot-id", required=True)
    parser.add_argument("--expected-source-sha256", required=True)
    parser.add_argument("--supervised-clear-space", required=True, action="store_true")
    profile = parser.add_mutually_exclusive_group()
    profile.add_argument("--longer-calibration", action="store_true")
    profile.add_argument("--multistage-calibration", action="store_true")
    args = parser.parse_args(argv)
    boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    source_sha256 = calibration_source_sha256()
    if boot_id != args.expected_boot_id or source_sha256 != args.expected_source_sha256:
        raise CalibrationError("calibration_provenance_mismatch")
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

    try:
        signal.signal(signal.SIGTERM, stop)
        pump.start()
        deadline = time.monotonic() + 2.0
        while not lease.ready() and time.monotonic() < deadline:
            time.sleep(0.01)
        if not lease.ready():
            raise RuntimeError("calibration host lease was not received")
        CalibrationRunner(
            device,
            lease,
            args.output,
            config=(
                CalibrationConfig.multistage()
                if args.multistage_calibration
                else CalibrationConfig.longer()
                if args.longer_calibration
                else None
            ),
            boot_id=boot_id,
            executed_bundle_source_sha256=source_sha256,
        ).run()
    finally:
        try:
            pump.close()
        finally:
            try:
                device.close()
            finally:
                signal.signal(signal.SIGTERM, previous)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
