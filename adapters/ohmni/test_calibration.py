from __future__ import annotations

import pytest

from .calibration import TICKS_PER_MM, HostLease, _Progress
from .device import Config, OhmniDevice
from .lidar import RawRevolution
from .odometry import Pose
from .paired_encoder import EncoderPair
from .spike.rplidar_protocol import Measurement


class Clock:
    def __init__(self) -> None:
        self.value = 10.0

    def __call__(self) -> float:
        return self.value


class Shell:
    def __init__(self) -> None:
        self.commands: list[str] = []

    def command(self, command: str, **_kwargs: object) -> str:
        self.commands.append(command)
        return ""

    def close(self) -> None:
        pass


class RawLidar:
    calibrated = False
    scan = None

    def __init__(self, clock: Clock, *, fresh: bool = True) -> None:
        self.clock, self.fresh = clock, fresh

    def raw_revolution(self, _now: float) -> RawRevolution | None:
        if not self.fresh:
            return None
        return RawRevolution(
            tuple(Measurement(True, 15, float(index), 1000.0) for index in range(20)), self.clock()
        )


def _device(
    monkeypatch: pytest.MonkeyPatch, clock: Clock, *, raw_fresh: bool = True
) -> OhmniDevice:
    monkeypatch.setattr("adapters.ohmni.device.time.monotonic", clock)
    shell = Shell()
    device = OhmniDevice(
        Config(spotter_present=True, owner_timeout_s=0.35),
        shell_factory=lambda _path: shell,
        lidar_discover=lambda: None,
        autostart=False,
    )
    device.lidar = RawLidar(clock, fresh=raw_fresh)  # type: ignore[assignment]
    sample = EncoderPair(1, 100, 100, int(clock() * 1e9), int(clock() * 1e9))
    device.odometry.snapshot = lambda now=None: Pose(0.0, 0.0, 0.0, quality=0.6)  # type: ignore[method-assign]
    device.odometry.snapshot_with_sample = lambda now=None: (  # type: ignore[method-assign]
        Pose(0.0, 0.0, 0.0, quality=0.6),
        sample,
    )
    assert device.enable()
    return device


def _lease(clock: Clock) -> HostLease:
    token = b"c" * 32
    lease = HostLease(token, monotonic=clock)
    assert lease.renew(1, token)
    return lease


def test_calibration_pulse_bypasses_only_the_uncalibrated_forward_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = Clock()
    device = _device(monkeypatch, clock)
    with pytest.raises(RuntimeError, match="lidar_calibration_required"):
        device.drive_velocity(0.04, 0.0, 0.5)

    motion = device.calibration_drive_velocity(0.04, 0.0, 0.5, host_lease=_lease(clock).reason)
    device.step(clock())

    assert device.motion_done(motion) is False
    assert "manual_move 55 -55" in device.drive_shell.commands


def test_calibration_lease_expiry_stops_the_actual_device_without_runner_polling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = Clock()
    device = _device(monkeypatch, clock)
    lease = _lease(clock)
    device.calibration_drive_velocity(0.04, 0.0, 0.5, host_lease=lease.reason)

    clock.value += 0.36
    device.step(clock())

    assert device.motion is None
    assert not device.enabled
    assert device.last_refusal == "calibration_host_lease_expired"
    assert device.drive_shell.commands[-2:] == ["manual_move 0 0", "sleep"]


@pytest.mark.parametrize(
    ("velocity", "yaw", "duration"),
    [(0.041, 0.0, 0.5), (0.0, 10.1, 0.5), (0.04, 10.0, 0.5), (0.04, 0.0, 0.51)],
)
def test_calibration_rejects_oversized_or_combined_pulses(
    monkeypatch: pytest.MonkeyPatch, velocity: float, yaw: float, duration: float
) -> None:
    clock = Clock()
    device = _device(monkeypatch, clock)
    with pytest.raises(ValueError, match="fixed safety bounds"):
        device.calibration_drive_velocity(velocity, yaw, duration, host_lease=_lease(clock).reason)
    assert device.motion is None


def test_calibration_rejects_missing_raw_revolution_and_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = Clock()
    device = _device(monkeypatch, clock, raw_fresh=False)
    with pytest.raises(RuntimeError, match="raw_lidar_revolution_stale"):
        device.calibration_drive_velocity(0.04, 0.0, 0.5, host_lease=_lease(clock).reason)
    assert device.motion is None


def test_owner_tick_stall_stops_calibration_before_the_pulse_can_continue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = Clock()
    device = _device(monkeypatch, clock)
    device.config = Config(spotter_present=True, owner_timeout_s=0.1)
    device.calibration_drive_velocity(0.04, 0.0, 0.5, host_lease=_lease(clock).reason)

    clock.value += 0.2
    device.step(clock())

    assert device.motion is None
    assert device.last_refusal == "owner_loop_stalled"
    assert device.drive_shell.commands[-1] == "manual_move 0 0"


def test_initial_or_bad_lease_never_authorizes_calibration_motion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = Clock()
    device = _device(monkeypatch, clock)
    lease = HostLease(b"d" * 32, monotonic=clock)
    assert not lease.renew(1, b"e" * 32)
    with pytest.raises(RuntimeError, match="calibration_host_lease_expired"):
        device.calibration_drive_velocity(0.04, 0.0, 0.5, host_lease=lease.reason)


def test_opposite_wheel_encoder_deltas_count_as_yaw_and_wheel_travel() -> None:
    progress = _Progress()
    progress.add(EncoderPair(1, 1_000, 1_000, 1, 1))
    progress.add(EncoderPair(2, 1_100, 1_100, 2, 2))

    expected_wheel_m = 100 / TICKS_PER_MM / 1000
    assert progress.wheel_travel_m == pytest.approx(expected_wheel_m)
    assert progress.yaw_degrees > 0
