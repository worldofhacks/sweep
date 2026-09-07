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


class RunnerSimulation:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, fault: str | None = None) -> None:
        self.clock = Clock()
        self.fault = fault
        self.units = (0, 0)
        self.started_moving: float | None = None
        self.last_tick = self.clock()
        self.last_scan = self.clock()
        self.left = self.right = 1000.0
        self.poll = 0
        self.sequence = 1
        simulation = self

        class SimulatedShell(Shell):
            def command(self, command: str, **kwargs: object) -> str:
                super().command(command, **kwargs)
                if command.startswith("manual_move "):
                    self_units = tuple(map(int, command.split()[1:]))
                    simulation.units = self_units
                    if any(self_units) and simulation.started_moving is None:
                        simulation.started_moving = simulation.clock()
                if command == "sleep":
                    simulation.units = (0, 0)
                return ""

        monkeypatch.setattr("adapters.ohmni.device.time.monotonic", self.clock)
        self.shell = SimulatedShell()
        self.device = OhmniDevice(
            Config(spotter_present=True, wheel_diameter_mm=152.4),
            shell_factory=lambda _path: self.shell,
            lidar_discover=lambda: None,
            autostart=False,
        )
        self.device.lidar = RawLidar(self.clock)  # type: ignore[assignment]
        self.device.lidar.raw_revolution = lambda now: RawRevolution(  # type: ignore[method-assign]
            tuple(Measurement(index == 0, 15, float(index * 3), 1000.0) for index in range(100)),
            self.last_scan,
        )
        self.lease = _lease(self.clock)
        self._sample()

    def _sample(self) -> None:
        self.poll += 1
        self.device.odometry._update_paired_sample(
            EncoderPair(
                self.poll,
                round(self.left) % 16384,
                round(self.right) % 16384,
                round(self.clock() * 1e9),
                round(self.clock() * 1e9),
            )
        )

    def sleep(self, delay: float) -> None:
        self.clock.value += delay
        if self.clock() - self.last_tick < 0.1 - 1e-8:
            return
        elapsed = self.clock() - self.last_tick
        self.last_tick = self.clock()
        moving = self.started_moving is not None
        gain = 0.0 if self.fault == "no_motion" else 1.0
        self.left += self.units[0] * 0.18 / 250 * 1000 * TICKS_PER_MM * elapsed * gain
        self.right += self.units[1] * 0.18 / 250 * 1000 * TICKS_PER_MM * elapsed * gain
        if not (moving and self.fault == "encoder"):
            self._sample()
        if not (moving and self.fault == "lidar"):
            self.last_scan = self.clock()
        if not (moving and self.fault == "lease"):
            self.sequence += 1
            assert self.lease.renew(self.sequence, b"c" * 32)
        self.device.step(self.clock())


def test_runner_completes_three_stages_using_command_driven_wheels_and_real_odometry(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    import json

    from .calibration import CalibrationRunner

    simulation = RunnerSimulation(monkeypatch)
    output = tmp_path / "capture.json"
    CalibrationRunner(
        simulation.device,
        simulation.lease,
        output,
        monotonic=simulation.clock,
        sleep=simulation.sleep,
    ).run()
    capture = json.loads(output.read_text())
    stages = capture["stages"]
    assert set(stages) == {"baseline", "after_forward", "after_yaw"}
    assert all(len(stage["revolutions"]) == 10 for stage in stages.values())
    assert 0.075 <= stages["after_forward"]["pose"]["x_m"] <= 0.105
    assert 9.5 <= stages["after_yaw"]["pose"]["yaw_deg"] <= 14.0
    assert simulation.device.motion is None
    assert not simulation.device.enabled
    assert simulation.shell.commands[-2:] == ["manual_move 0 0", "sleep"]


@pytest.mark.parametrize("fault", ["lease", "encoder", "lidar", "no_motion"])
def test_runner_stops_and_removes_incomplete_capture_after_live_fault(
    monkeypatch: pytest.MonkeyPatch, tmp_path, fault: str
) -> None:
    from .calibration import CalibrationError, CalibrationRunner

    simulation = RunnerSimulation(monkeypatch, fault=fault)
    output = tmp_path / "capture.json"
    with pytest.raises((CalibrationError, RuntimeError)):
        CalibrationRunner(
            simulation.device,
            simulation.lease,
            output,
            monotonic=simulation.clock,
            sleep=simulation.sleep,
        ).run()
    assert simulation.started_moving is not None
    assert not output.exists()
    assert simulation.device.motion is None
    assert not simulation.device.enabled
    assert simulation.shell.commands[-2:] == ["manual_move 0 0", "sleep"]


def test_runner_refuses_an_existing_output_before_enabling(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from .calibration import CalibrationRunner

    clock = Clock()
    device = _device(monkeypatch, clock)
    device.disable()
    output = tmp_path / "existing.json"
    output.write_text("reserved")
    with pytest.raises(FileExistsError):
        CalibrationRunner(device, _lease(clock), output, monotonic=clock).run()
    assert not device.enabled
    assert output.read_text() == "reserved"
