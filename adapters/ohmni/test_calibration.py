from __future__ import annotations

import json
import socket
import threading

import pytest

from .calibration import TICKS_PER_MM, CalibrationConfig, HostLease, LeaseSocketPump, _Progress
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

    def close(self) -> None:
        pass

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


def test_supervised_calibration_pulse_uses_raw_lidar_while_normal_drive_requires_calibration(
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


def test_lease_diagnostics_retains_the_late_renewal_gap() -> None:
    clock = Clock()
    token = b"d" * 32
    lease = HostLease(token, monotonic=clock)
    assert lease.renew(1, token)
    clock.value += 0.36
    assert not lease.renew(2, token)

    assert lease.diagnostics(clock()) == {
        "termination_reason": "lease_renewal_rejected",
        "max_received_gap_s": pytest.approx(0.36),
        "last_renewal_age_s": pytest.approx(0.36),
    }


def test_lease_diagnostics_separates_last_renewal_age_from_received_gaps() -> None:
    clock = Clock()
    token = b"d" * 32
    lease = HostLease(token, monotonic=clock)
    assert lease.renew(1, token)
    clock.value += 0.2

    assert lease.diagnostics(clock()) == {
        "termination_reason": None,
        "max_received_gap_s": 0.0,
        "last_renewal_age_s": pytest.approx(0.2),
    }


def test_lease_pump_records_a_socket_timeout() -> None:
    token = b"d" * 32
    lease = HostLease(token)
    lost = threading.Event()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)

        def server() -> None:
            with listener.accept()[0] as connection:
                with connection.makefile("rb") as stream:
                    assert stream.readline(66) == token.hex().encode() + b"\n"
                connection.sendall(b"1 " + token.hex().encode() + b"\n")
                assert lost.wait(1.0)

        thread = threading.Thread(target=server)
        thread.start()
        pump = LeaseSocketPump("127.0.0.1", listener.getsockname()[1], token, lease, lost.set)
        pump.start()
        assert lost.wait(1.0)
        pump.close()
        thread.join(1.0)

    assert not thread.is_alive()
    assert lease.diagnostics()["termination_reason"] == "lease_socket_timeout"


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
        if self.fault == "wheel_limit":
            gain = 10.0
        if self.fault == "yaw_limit" and self.units[0] == self.units[1]:
            gain = 4.0
        self.left -= self.units[0] * 0.18 / 250 * 1000 * TICKS_PER_MM * elapsed * gain
        self.right -= self.units[1] * 0.18 / 250 * 1000 * TICKS_PER_MM * elapsed * gain
        if not (moving and self.fault == "encoder"):
            self._sample()
        if not (moving and self.fault == "lidar"):
            self.last_scan = self.clock()
        if not (moving and self.fault == "lease"):
            self.sequence += 1
            assert self.lease.renew(self.sequence, b"c" * 32)
        self.device.step(self.clock())


def test_longer_calibration_profile_rejects_mixed_limits() -> None:
    assert CalibrationConfig.longer().forward_distance_m == 0.4
    assert CalibrationConfig.longer().yaw_degrees == 30.0
    with pytest.raises(ValueError, match="immutable capture profile"):
        CalibrationConfig(forward_distance_m=0.4)


def test_longer_runner_reaches_the_extended_motion_targets_and_stops(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    import json

    from .calibration import CalibrationRunner

    simulation = RunnerSimulation(monkeypatch)
    output = tmp_path / "longer-capture.json"
    CalibrationRunner(
        simulation.device,
        simulation.lease,
        output,
        monotonic=simulation.clock,
        sleep=simulation.sleep,
        config=CalibrationConfig.longer(),
    ).run()
    capture = json.loads(output.read_text())
    assert capture["limits"] == {
        "wheel_diameter_mm": 152.4,
        "forward_speed_m_s": 0.04,
        "forward_distance_m": 0.4,
        "yaw_rate_deg_s": 10.0,
        "yaw_degrees": 30.0,
        "pulse_duration_s": 0.5,
        "max_wheel_travel_m": 0.6,
        "max_yaw_degrees": 40.0,
        "max_runtime_s": 60.0,
    }
    stages = capture["stages"]
    assert 0.4 <= stages["after_forward"]["pose"]["x_m"] < 0.43
    assert 30.0 <= stages["after_yaw"]["pose"]["yaw_deg"] < 40.0
    assert simulation.device.motion is None
    assert not simulation.device.enabled
    assert simulation.shell.commands[-2:] == ["manual_move 0 0", "sleep"]


@pytest.mark.parametrize("fault", ["lease", "wheel_limit"])
def test_longer_runner_fails_closed_on_lease_or_extended_motion_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path, fault: str
) -> None:
    from .calibration import CalibrationError, CalibrationRunner

    simulation = RunnerSimulation(monkeypatch, fault=fault)
    output = tmp_path / "longer-capture.json"
    with pytest.raises(CalibrationError):
        CalibrationRunner(
            simulation.device,
            simulation.lease,
            output,
            monotonic=simulation.clock,
            sleep=simulation.sleep,
            config=CalibrationConfig.longer(),
        ).run()
    assert not output.exists()
    assert simulation.device.motion is None
    assert not simulation.device.enabled
    assert simulation.shell.commands[-2:] == ["manual_move 0 0", "sleep"]


def test_longer_runner_stops_an_active_turn_that_exceeds_its_yaw_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from .calibration import CalibrationError, CalibrationRunner

    simulation = RunnerSimulation(monkeypatch)
    output = tmp_path / "longer-capture.json"
    original_sleep = simulation.sleep
    injected_turn_samples = 0

    def overshoot_turn_encoder(delay: float) -> None:
        nonlocal injected_turn_samples
        if (
            simulation.units[0] == simulation.units[1]
            and simulation.units != (0, 0)
            and injected_turn_samples < 2
            and simulation.clock() + delay - simulation.last_tick >= 0.1 - 1e-8
        ):
            simulation.left -= 8_000
            simulation.right -= 8_000
            injected_turn_samples += 1
        original_sleep(delay)

    with pytest.raises(CalibrationError):
        CalibrationRunner(
            simulation.device,
            simulation.lease,
            output,
            monotonic=simulation.clock,
            sleep=overshoot_turn_encoder,
            config=CalibrationConfig.longer(),
        ).run()
    assert simulation.started_moving is not None
    assert simulation.device.last_refusal == "calibration_yaw_limit"
    assert not output.exists()
    assert simulation.device.motion is None
    assert not simulation.device.enabled
    assert simulation.shell.commands[-2:] == ["manual_move 0 0", "sleep"]


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
    with pytest.raises((CalibrationError, RuntimeError)) as error:
        CalibrationRunner(
            simulation.device,
            simulation.lease,
            output,
            monotonic=simulation.clock,
            sleep=simulation.sleep,
        ).run()
    assert simulation.started_moving is not None
    if fault == "no_motion":
        assert str(error.value) == "calibration_no_motion"
        assert simulation.clock() - simulation.started_moving < 1.0
    assert not output.exists()
    assert simulation.device.motion is None
    assert not simulation.device.enabled
    assert simulation.shell.commands[-2:] == ["manual_move 0 0", "sleep"]


def test_runner_preserves_lease_expiry_when_pump_teardown_clears_active_motion(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from .calibration import CalibrationError, CalibrationRunner

    simulation = RunnerSimulation(monkeypatch)
    output = tmp_path / "capture.json"
    original_sleep = simulation.sleep
    lease_lost = False

    def pump_teardown(delay: float) -> None:
        nonlocal lease_lost
        if simulation.units != (0, 0) and not lease_lost:
            lease_lost = True
            simulation.lease.close("lease_socket_eof")
            simulation.device.disable()
        original_sleep(delay)

    with pytest.raises(CalibrationError, match="calibration_host_lease_expired"):
        CalibrationRunner(
            simulation.device,
            simulation.lease,
            output,
            monotonic=simulation.clock,
            sleep=pump_teardown,
        ).run()

    failure = json.loads((tmp_path / "capture.json.failed.json").read_text())
    assert lease_lost
    assert failure["failure"] == "calibration_host_lease_expired"
    assert failure["device_refusal"] is None
    assert failure["lease_diagnostics"]["termination_reason"] == "lease_socket_eof"
    assert failure["lease_diagnostics"]["max_received_gap_s"] == pytest.approx(0.1)
    assert failure["lease_diagnostics"]["last_renewal_age_s"] is not None
    assert not output.exists()


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


def _cli_arguments(tmp_path) -> list[str]:
    from pathlib import Path

    from .calibration import calibration_source_sha256

    token = tmp_path / "token"
    token.write_text((b"c" * 32).hex())
    return [
        "--lease-port",
        "18912",
        "--lease-token-file",
        str(token),
        "--output",
        str(tmp_path / "capture.json"),
        "--expected-boot-id",
        Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        "--expected-source-sha256",
        calibration_source_sha256(),
        "--supervised-clear-space",
    ]


@pytest.mark.parametrize("invalid", ["boot", "source", "clear-space"])
def test_cli_rejects_missing_supervision_or_mismatched_provenance_before_opening_device(
    monkeypatch: pytest.MonkeyPatch, tmp_path, invalid: str
) -> None:
    from . import calibration

    arguments = _cli_arguments(tmp_path)
    if invalid == "clear-space":
        arguments.remove("--supervised-clear-space")
    else:
        flag = "--expected-boot-id" if invalid == "boot" else "--expected-source-sha256"
        arguments[arguments.index(flag) + 1] = "invalid"
    monkeypatch.setattr(calibration, "OhmniDevice", lambda config: pytest.fail("device opened"))
    with pytest.raises((calibration.CalibrationError, SystemExit)):
        calibration.main(arguments)


@pytest.mark.parametrize("device_id", [11, 12])
def test_cli_writes_actual_boot_and_source_pin_through_real_runner(
    monkeypatch: pytest.MonkeyPatch, tmp_path, device_id: int
) -> None:
    import json

    from . import calibration

    arguments = [*_cli_arguments(tmp_path), "--device-id", str(device_id)]
    simulation = RunnerSimulation(monkeypatch)
    runner = calibration.CalibrationRunner

    class Pump:
        def __init__(self, *args):
            pass

        def start(self):
            pass

        def close(self):
            simulation.lease.close()

    monkeypatch.setattr(calibration, "OhmniDevice", lambda config: simulation.device)
    monkeypatch.setattr(calibration, "HostLease", lambda token: simulation.lease)
    monkeypatch.setattr(calibration, "LeaseSocketPump", Pump)
    monkeypatch.setattr(
        calibration,
        "CalibrationRunner",
        lambda *args, **kwargs: runner(
            *args, **kwargs, monotonic=simulation.clock, sleep=simulation.sleep
        ),
    )
    assert calibration.main(arguments) == 0
    capture = json.loads((tmp_path / "capture.json").read_text())
    assert capture["device_id"] == device_id
    if device_id == 12:
        assert capture["mount_source"] == "unqualified_legacy_seed"
    else:
        assert "mount_source" not in capture
    assert capture["boot_id"] == arguments[arguments.index("--expected-boot-id") + 1]
    assert capture["executed_bundle_source_sha256"] == calibration.calibration_source_sha256()
    for stage in capture["stages"].values():
        expected = max(
            abs(scan["monotonic_s"] - stage["encoder"]["right_receipt_ns"] / 1e9)
            for scan in stage["revolutions"]
        )
        assert stage["max_encoder_revolution_delta_s"] == pytest.approx(expected)


def test_cli_closes_device_when_initial_host_lease_never_arrives(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from . import calibration

    arguments = _cli_arguments(tmp_path)
    clock = Clock()
    closed = []

    class Device:
        def disable(self):
            pass

        def close(self):
            closed.append("device")

    class Pump:
        def __init__(self, *args):
            pass

        def start(self):
            pass

        def close(self):
            closed.append("pump")

    monkeypatch.setattr(calibration, "OhmniDevice", lambda config: Device())
    monkeypatch.setattr(calibration, "LeaseSocketPump", Pump)
    monkeypatch.setattr(calibration.time, "monotonic", clock)
    monkeypatch.setattr(
        calibration.time, "sleep", lambda delay: setattr(clock, "value", clock() + delay)
    )
    with pytest.raises(RuntimeError, match="host lease was not received"):
        calibration.main(arguments)
    assert closed == ["pump", "device"]


def test_failed_final_disable_discards_capture(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    from .calibration import CalibrationRunner

    simulation = RunnerSimulation(monkeypatch)
    real_disable = simulation.device.disable
    calls = 0

    def disable():
        nonlocal calls
        calls += 1
        real_disable()
        if calls == 2:
            raise RuntimeError("stop acknowledgement missing")

    monkeypatch.setattr(simulation.device, "disable", disable)
    output = tmp_path / "capture.json"
    with pytest.raises(RuntimeError, match="stop acknowledgement missing"):
        CalibrationRunner(
            simulation.device,
            simulation.lease,
            output,
            monotonic=simulation.clock,
            sleep=simulation.sleep,
        ).run()
    assert not output.exists()


@pytest.mark.parametrize("fault", ["wheel_limit", "yaw_limit"])
def test_runner_stops_when_actual_encoder_travel_exceeds_session_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path, fault: str
) -> None:
    from .calibration import CalibrationError, CalibrationRunner

    simulation = RunnerSimulation(monkeypatch, fault=fault)
    output = tmp_path / "capture.json"
    with pytest.raises(CalibrationError):
        CalibrationRunner(
            simulation.device,
            simulation.lease,
            output,
            monotonic=simulation.clock,
            sleep=simulation.sleep,
        ).run()
    expected = (
        "calibration_wheel_travel_limit" if fault == "wheel_limit" else "calibration_yaw_limit"
    )
    assert simulation.device.last_refusal == expected
    assert simulation.device.motion is None
    assert simulation.units == (0, 0)
    assert not output.exists()


def test_cleanup_preserves_a_replacement_at_the_capture_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from .calibration import CalibrationError, CalibrationRunner

    simulation = RunnerSimulation(monkeypatch)
    output = tmp_path / "capture.json"
    runner = CalibrationRunner(
        simulation.device,
        simulation.lease,
        output,
        monotonic=simulation.clock,
        sleep=simulation.sleep,
    )
    original_write = runner._write

    def replace_then_write(stages, descriptor):
        output.rename(tmp_path / "original.json")
        output.write_text("belongs to another writer")
        original_write(stages, descriptor)

    monkeypatch.setattr(runner, "_write", replace_then_write)
    with pytest.raises(CalibrationError, match="calibration_output_replaced"):
        runner.run()
    assert output.read_text() == "belongs to another writer"
    assert simulation.units == (0, 0)


@pytest.mark.parametrize("drift", ["translation", "yaw"])
def test_capture_stops_when_encoder_drift_breaks_the_settled_stage(
    monkeypatch: pytest.MonkeyPatch, tmp_path, drift: str
) -> None:
    from .calibration import CalibrationError, CalibrationRunner

    simulation = RunnerSimulation(monkeypatch)
    original_sleep = simulation.sleep

    def drifting_sleep(delay):
        original_sleep(delay)
        if drift == "translation":
            simulation.left += 120
            simulation.right -= 120
        else:
            simulation.left -= 40
            simulation.right -= 40
        simulation._sample()

    output = tmp_path / "capture.json"
    with pytest.raises(CalibrationError, match="calibration_capture_drift"):
        CalibrationRunner(
            simulation.device,
            simulation.lease,
            output,
            monotonic=simulation.clock,
            sleep=drifting_sleep,
        ).run()
    assert not output.exists()
    assert simulation.started_moving is None
    assert simulation.units == (0, 0)


def test_multistage_runner_reaches_two_independent_translations_within_fixed_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    import json

    from .calibration import CalibrationRunner

    simulation = RunnerSimulation(monkeypatch)
    output = tmp_path / "multistage-capture.json"
    CalibrationRunner(
        simulation.device,
        simulation.lease,
        output,
        monotonic=simulation.clock,
        sleep=simulation.sleep,
        config=CalibrationConfig.multistage(),
    ).run()
    capture = json.loads(output.read_text())

    assert set(capture["stages"]) == {
        "baseline",
        "after_forward",
        "after_yaw",
        "after_cross_forward",
    }
    assert capture["limits"]["max_wheel_travel_m"] == 1.05
    assert capture["limits"]["max_yaw_degrees"] == 85.0
    assert capture["limits"]["max_runtime_s"] == 90.0
    cross = capture["stages"]["after_cross_forward"]["pose"]
    turned = capture["stages"]["after_yaw"]["pose"]
    assert cross["x_m"] - turned["x_m"] > 0.15
    assert cross["y_m"] - turned["y_m"] > 0.25
    assert simulation.device.motion is None
    assert not simulation.device.enabled
    assert simulation.shell.commands[-2:] == ["manual_move 0 0", "sleep"]


def test_interrupted_second_leg_preserves_three_raw_stages_and_exact_stop_reason(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    import json

    from .calibration import CalibrationError, CalibrationRunner

    simulation = RunnerSimulation(monkeypatch)
    output = tmp_path / "capture.json"
    def interrupt_second_leg(delay: float) -> None:
        pose = simulation.device.odometry.snapshot(simulation.clock())
        if 55 < pose.yaw_deg < 70 and simulation.units[0] != simulation.units[1]:
            simulation.device.stop()
            simulation.device.last_refusal = "raw_lidar_revolution_stale"
        simulation.sleep(delay)

    with pytest.raises(CalibrationError, match="raw_lidar_revolution_stale"):
        CalibrationRunner(
            simulation.device, simulation.lease, output,
            monotonic=simulation.clock, sleep=interrupt_second_leg,
            config=CalibrationConfig.multistage(), device_id=12,
            boot_id="unit12-test-boot", executed_bundle_source_sha256="a" * 64,
        ).run()
    assert not output.exists()
    failure = json.loads((tmp_path / "capture.json.failed.json").read_text())
    assert failure["kind"] == "ohmni_lidar_calibration_failed_attempt"
    assert failure["device_id"] == 12
    assert failure["boot_id"] == "unit12-test-boot"
    assert failure["device_refusal"] == "raw_lidar_revolution_stale"
    assert set(failure["completed_stages"]) == {"baseline", "after_forward", "after_yaw"}
    assert all(len(stage["revolutions"]) == 10 for stage in failure["completed_stages"].values())
    assert not simulation.device.enabled
    assert simulation.device.motion is None
    assert simulation.units == (0, 0)


def test_multistage_budget_keeps_motion_targets_fixed_and_rejects_mixed_limits() -> None:
    profile = CalibrationConfig.multistage()
    assert profile.forward_distance_m == .4
    assert profile.yaw_degrees == 60
    assert profile.forward_speed_m_s == .04
    assert profile.yaw_rate_deg_s == 10
    with pytest.raises(ValueError, match="immutable"):
        CalibrationConfig(max_runtime_s=90)
