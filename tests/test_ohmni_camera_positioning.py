import hashlib
import importlib.util
import json
import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_ohmni_camera_inspection import _producer as _produce_inspection_capture

REPO = Path(__file__).parent.parent
PACKAGE = Path(__file__).parent
sys.path.insert(0, str(REPO))
calibration = importlib.import_module("adapters.ohmni.calibration")
TICKS_PER_MM = calibration.TICKS_PER_MM
CalibrationError = calibration.CalibrationError
RunnerSimulation = importlib.import_module("adapters.ohmni.test_calibration").RunnerSimulation
RangeScan = importlib.import_module("adapters.ohmni.models").RangeScan
spec = importlib.util.spec_from_file_location(
    "ohmni_camera_positioning", PACKAGE.parent / "tools" / "ohmni_camera_positioning.py"
)
assert spec and spec.loader
capture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(capture)


class Progress:
    def __init__(self) -> None:
        self.travel_m = 0.0

    def values(self) -> tuple[float, float]:
        return self.travel_m, 0.0


class Lease:
    def __init__(self, reason_after: int | None = None) -> None:
        self.calls = 0
        self.reason_after = reason_after

    def reason(self, _now: float) -> str | None:
        self.calls += 1
        if self.reason_after is not None and self.calls > self.reason_after:
            return "host_lease_expired"
        return None


class Device:
    def __init__(self, progress: Progress, step_m: float) -> None:
        self.progress = progress
        self.step_m = step_m
        self.stop_calls = 0
        self.disable_calls = 0
        self.enable_calls = 0
        self.last_refusal: str | None = None
        self.durations: list[float] = []

    def stop(self) -> None:
        self.stop_calls += 1

    def disable(self) -> None:
        self.disable_calls += 1

    def enable(self) -> bool:
        self.enable_calls += 1
        return True

    def guard_reason(self, *, forward=False, now=None):
        return None

    def calibration_drive_velocity(self, _speed, _yaw, duration, *, host_lease):
        self.durations.append(duration)
        reason = host_lease(0.0)
        if reason is not None:
            self.last_refusal = reason
            raise RuntimeError(reason)
        return "motion"

    def motion_done(self, _motion_id: str) -> bool:
        self.progress.travel_m += self.step_m
        return True


def runner(*, step_m: float, reason_after: int | None = None):
    instance = capture.CameraPoseCaptureRunner.__new__(capture.CameraPoseCaptureRunner)
    progress = Progress()
    instance.mode = "forward"
    instance.config = SimpleNamespace(
        pulse_duration_s=0.5,
        max_runtime_s=60.0,
        forward_speed_m_s=0.04,
        yaw_rate_deg_s=10.0,
        max_wheel_travel_m=0.6,
        max_yaw_degrees=40.0,
    )
    instance._progress = progress
    instance.lease = Lease(reason_after)
    instance.device = Device(progress, step_m)
    instance.monotonic = lambda: 0.0
    instance.sleep = lambda _seconds: None
    instance._started = 0.0
    instance.forward_pulses_completed = 0
    instance.forward_distance_m = 0.0
    instance._forward_origin = None
    instance._resume_decision = None
    instance.resume = None
    instance.device_id = capture.DEVICE_ID
    instance.neck_before = None
    instance.neck_after_motion = None
    instance.neck_after_cleanup = None

    def snapshot(_now=None):
        instance._check_private_limits()
        return capture.Pose(progress.travel_m, 0.0, 0.0, quality=1.0), None

    instance._snapshot = snapshot
    return instance


def test_closed_loop_forward_reaches_20_cm_within_the_pulse_cap() -> None:
    instance = runner(step_m=0.02)

    instance._forward_until_target()

    assert instance.forward_pulses_completed == 10
    assert instance.forward_pulses_completed <= capture.FORWARD_MAX_PULSES
    assert instance.forward_distance_m == pytest.approx(0.2)


def test_forward_clips_its_final_pulse_to_the_remaining_distance() -> None:
    instance = runner(step_m=0.005)
    instance._forward_origin = capture.Pose(0.0, 0.0, 0.0, quality=1.0)
    instance._progress.travel_m = 0.195

    instance._forward_until_target()

    assert instance.device.durations == [pytest.approx(0.125)]


def test_forward_finishes_inside_the_terminal_tolerance_without_another_motor_write() -> None:
    instance = runner(step_m=0.005)
    instance._forward_origin = capture.Pose(0.0, 0.0, 0.0, quality=1.0)
    instance._progress.travel_m = (
        capture.FORWARD_TARGET_M - capture.FORWARD_TERMINAL_TOLERANCE_M / 2
    )

    instance._forward_until_target()

    assert instance.device.durations == []


def test_forward_private_reserve_refuses_the_next_pulse_before_the_cap() -> None:
    instance = runner(step_m=0.02)
    instance._progress.travel_m = 0.25

    assert instance._device_guard(0.0) == "camera_pose_wheel_travel_limit"
    assert instance.device.stop_calls >= 1


@pytest.mark.parametrize(
    ("step_m", "reason_after"),
    [(0.27, None), (0.02, 1)],
)
def test_forward_failure_stops_and_disables(step_m, reason_after, tmp_path, monkeypatch) -> None:
    instance = runner(step_m=step_m, reason_after=reason_after)
    instance.output = tmp_path / "capture.json"
    instance.boot_id = "boot"
    instance.executed_bundle_source_sha256 = "source"
    instance._initialize = lambda: None
    instance._capture_stage = lambda: {"revolutions": []}
    instance._settle = lambda: None
    instance._write = lambda _stages, _descriptor: None
    instance._write_failure = lambda _stages, _error: None
    monkeypatch.setattr(
        capture,
        "_neck_status",
        lambda: {"position": 0, "target": 0, "flags": "NONE"},
    )

    with pytest.raises((capture.CalibrationError, RuntimeError)):
        instance.run()

    assert instance.device.stop_calls >= 1
    assert instance.device.disable_calls >= 1


def _half_response_sleep(simulation: RunnerSimulation):
    original_sleep = simulation.sleep

    def sleep(delay: float) -> None:
        units = simulation.units
        if not any(units):
            original_sleep(delay)
            return
        reduced = tuple(round(unit * 0.5) for unit in units)
        simulation.units = reduced
        original_sleep(delay)
        if simulation.units == reduced:
            simulation.units = units

    return sleep


def _overshoot_response_sleep(simulation: RunnerSimulation):
    original_sleep = simulation.sleep

    def sleep(delay: float) -> None:
        moving = simulation.units != (0, 0)
        original_sleep(delay)
        if moving:
            ticks = 0.23 * 1000 * TICKS_PER_MM
            simulation.left -= ticks
            simulation.right += ticks
            simulation._sample()
            simulation.device.step(simulation.clock())

    return sleep


def _reverse_response_sleep(simulation: RunnerSimulation):
    original_sleep = simulation.sleep

    def sleep(delay: float) -> None:
        units = simulation.units
        if not any(units):
            original_sleep(delay)
            return
        simulation.units = tuple(-unit for unit in units)
        original_sleep(delay)
        if simulation.units == tuple(-unit for unit in units):
            simulation.units = units

    return sleep


def test_forward_guard_refuses_an_uncalibrated_lidar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    simulation = RunnerSimulation(monkeypatch)
    _configure_unit12(simulation)
    simulation.device.lidar.calibrated = False
    runner = capture.CameraPoseCaptureRunner(
        simulation.device,
        simulation.lease,
        Path("capture.json"),
        mode="forward",
        boot_id="boot",
        executed_bundle_source_sha256="source",
        monotonic=simulation.clock,
        sleep=simulation.sleep,
    )
    runner._started = simulation.clock()

    assert runner._forward_device_guard(simulation.clock()) == "lidar_calibration_required"


@pytest.mark.parametrize(
    "reason", ["lidar_scan_invalid", "lidar_scan_geometry_invalid", "lidar_read_error"]
)
def test_invalid_lidar_reads_pause_the_forward_capture(reason: str) -> None:
    instance = runner(step_m=0.02)
    instance._forward_device_guard = lambda _now: reason

    with pytest.raises(capture.CameraPosePaused, match=reason):
        instance._forward_until_target()

    assert instance.device.durations == []


def test_positioning_requires_the_unit12_profile() -> None:
    profile = capture._profile(capture.DEVICE_ID)

    assert profile.footprint_radius_m == 0.3
    assert profile.stopping_distance_m == 0.1
    assert profile.clearance_margin_m == 0.1
    with pytest.raises(CalibrationError, match="camera_pose_device_profile_unavailable"):
        capture._profile(11)


def _set_forward_scan(simulation: RunnerSimulation) -> None:
    ranges = [100] * 360
    ranges[0] = getattr(simulation, "forward_scan_cm", 100)
    lidar = simulation.device.lidar
    lidar.calibrated = True
    lidar.scan = RangeScan(
        int(simulation.clock() * 1000),
        (0.0, 0.0, 0.0),
        0.0,
        1.0,
        0.15,
        12.0,
        ranges,
    )
    lidar.updated = simulation.clock()


def _configure_unit12(simulation: RunnerSimulation) -> None:
    profile = capture._profile(capture.DEVICE_ID)
    simulation.device.config = capture.Config(
        spotter_present=True,
        wheel_diameter_mm=profile.wheel_diameter_mm,
        lidar_offset_deg=profile.lidar_offset_deg,
        lidar_angle_sign=profile.lidar_angle_sign,
        footprint_radius_m=profile.footprint_radius_m,
        stopping_distance_m=profile.stopping_distance_m,
        clearance_margin_m=profile.clearance_margin_m,
        lidar_mount_x_m=profile.lidar_mount["x_m"],
        lidar_mount_y_m=profile.lidar_mount["y_m"],
        lidar_mount_z_m=profile.lidar_mount["z_m"],
    )
    simulation.device.lidar.calibrated = True
    simulation.device.lidar.scan = RangeScan(
        int(simulation.clock() * 1000),
        (0.0, 0.0, 0.0),
        0.0,
        1.0,
        0.15,
        12.0,
        [100] * 360,
    )
    simulation.device.lidar.updated = simulation.clock()


def _simulated_capture_runner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    fault: str | None = None,
    sleep_factory=None,
):
    simulation = RunnerSimulation(monkeypatch, fault=fault)
    _configure_unit12(simulation)
    simulation.forward_scan_cm = 100
    _set_forward_scan(simulation)
    raw_sleep = simulation.sleep if sleep_factory is None else sleep_factory(simulation)

    def sleep(delay: float) -> None:
        raw_sleep(delay)
        _set_forward_scan(simulation)

    runner = capture.CameraPoseCaptureRunner(
        simulation.device,
        simulation.lease,
        tmp_path / "capture.json",
        mode="forward",
        boot_id="boot",
        executed_bundle_source_sha256="source",
        monotonic=simulation.clock,
        sleep=sleep,
    )
    monkeypatch.setattr(
        capture,
        "_neck_status",
        lambda: {"position": -46844, "target": -47000, "flags": "NONE"},
    )
    return runner, simulation


def test_real_ohmni_runner_reaches_20_cm_within_the_pulse_cap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runner, simulation = _simulated_capture_runner(monkeypatch, tmp_path)
    runner._started = simulation.clock()

    assert simulation.device.enable()
    runner._initialize()
    runner._forward_until_target()

    assert 1 <= runner.forward_pulses_completed <= capture.FORWARD_MAX_PULSES
    assert (
        capture.FORWARD_TARGET_M - capture.FORWARD_TERMINAL_TOLERANCE_M
        <= runner.forward_distance_m
        <= capture.FORWARD_TARGET_M
    )
    assert simulation.device.motion is None


def test_real_ohmni_runner_reaches_20_cm_with_more_than_24_partial_pulses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runner, simulation = _simulated_capture_runner(
        monkeypatch, tmp_path, sleep_factory=_half_response_sleep
    )
    runner._started = simulation.clock()

    assert simulation.device.enable()
    runner._initialize()
    runner._forward_until_target()

    assert 1 < runner.forward_pulses_completed <= 28
    assert (
        capture.FORWARD_TARGET_M - capture.FORWARD_TERMINAL_TOLERANCE_M
        <= runner.forward_distance_m
        <= capture.FORWARD_TARGET_M
    )
    assert simulation.device.motion is None


def test_forward_capture_records_consistent_20_cm_diagnostics(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runner, simulation = _simulated_capture_runner(monkeypatch, tmp_path)

    runner.run()

    document = __import__("json").loads((tmp_path / "capture.json").read_text())
    motion = document["motion"]
    limits = document["limits"]["predeclared_modes"]["forward"]
    assert motion["target_distance_m"] == 0.2
    assert motion["terminal_tolerance_m"] == 0.004
    assert motion["velocity_m_s"] == 0.04
    assert motion["pulse_duration_s"] == 0.5
    assert motion["maximum_pulses"] == 28
    assert motion["pulses_completed"] == runner.forward_pulses_completed
    assert 0.196 <= motion["measured_distance_m"] <= 0.2
    assert limits["target_distance_m"] == motion["target_distance_m"]
    assert limits["max_wheel_travel_m"] == 0.26
    assert limits["max_yaw_degrees"] == 5.0
    assert simulation.device.motion is None
    assert not simulation.device.enabled


@pytest.mark.parametrize("fault", ["lease", "wheel_limit"])
def test_real_ohmni_runner_failure_stops_and_disables(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fault: str
) -> None:
    runner, simulation = _simulated_capture_runner(
        monkeypatch,
        tmp_path,
        fault=fault,
        sleep_factory=_overshoot_response_sleep if fault == "wheel_limit" else None,
    )
    runner._capture_stage = lambda: {"revolutions": []}
    runner._settle = lambda: None

    with pytest.raises((CalibrationError, RuntimeError)):
        runner.run()

    assert simulation.device.motion is None
    assert not simulation.device.enabled
    assert simulation.shell.commands[-2:] == ["manual_move 0 0", "sleep"]


def test_real_ohmni_runner_refuses_reverse_encoder_displacement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runner, simulation = _simulated_capture_runner(
        monkeypatch, tmp_path, sleep_factory=_reverse_response_sleep
    )
    runner._started = simulation.clock()

    assert simulation.device.enable()
    runner._initialize()
    with pytest.raises(CalibrationError, match="camera_pose_reverse_motion"):
        runner._forward_until_target()

    assert simulation.device.motion is None
    simulation.device.disable()
    assert simulation.shell.commands[-2:] == ["manual_move 0 0", "sleep"]


def _obstacle_after_motion_sleep(simulation: RunnerSimulation):
    original_sleep = simulation.sleep

    def sleep(delay: float) -> None:
        original_sleep(delay)
        if simulation.started_moving is not None:
            simulation.forward_scan_cm = 44

    return sleep


def test_forward_guard_rechecks_existing_obstacle_policy_during_motion(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runner, simulation = _simulated_capture_runner(
        monkeypatch, tmp_path, sleep_factory=_obstacle_after_motion_sleep
    )
    calls: list[tuple[bool, float | None]] = []
    original_guard = simulation.device.guard_reason

    def observed_guard(*, forward=False, now=None):
        calls.append((forward, now))
        return original_guard(forward=forward, now=now)

    simulation.device.guard_reason = observed_guard
    runner._started = simulation.clock()
    runner._deadline = simulation.clock() + runner.config.max_runtime_s
    assert simulation.device.enable()
    runner._initialize()
    with pytest.raises(capture.CameraPosePaused, match="obstacle_within_clearance"):
        runner._forward_until_target()
    simulation.device.disable()
    runner._pause_for_live_resume(
        {"before_motion": {"revolutions": []}}, "obstacle_within_clearance"
    )

    paused = __import__("json").loads((tmp_path / "capture.json.paused.json").read_text())
    assert not (tmp_path / "capture.json").exists()
    assert paused["pause_reason"] == "obstacle_within_clearance"
    assert paused["pause_guard_evidence"]["reason"] == paused["pause_reason"]
    assert paused["completed_stages"] == {"before_motion": {"revolutions": []}}
    assert 0 < paused["motion"]["measured_distance_m"] < capture.FORWARD_TARGET_M
    assert paused["motion"]["remaining_distance_m"] > 0
    assert len(calls) >= 2
    assert sum(forward for forward, _ in calls) >= 2
    assert simulation.device.motion is None
    assert not simulation.device.enabled
    assert simulation.shell.commands[-2:] == ["manual_move 0 0", "sleep"]
    assert runner._resume_gate is not None
    runner._resume_gate.close()


def _yaw_overshoot_sleep(simulation: RunnerSimulation):
    original_sleep = simulation.sleep

    def sleep(delay: float) -> None:
        moving = simulation.units != (0, 0)
        original_sleep(delay)
        if moving:
            ticks = 0.12 * 1000 * TICKS_PER_MM
            simulation.left += ticks
            simulation._sample()
            simulation.device.step(simulation.clock())

    return sleep


def test_real_ohmni_yaw_crosses_target_and_captures_after_motion(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    simulation = RunnerSimulation(monkeypatch)
    _configure_unit12(simulation)
    runner = capture.CameraPoseCaptureRunner(
        simulation.device,
        simulation.lease,
        tmp_path / "yaw.json",
        mode="yaw",
        boot_id="boot",
        executed_bundle_source_sha256="source",
        monotonic=simulation.clock,
        sleep=_half_response_sleep(simulation),
    )
    monkeypatch.setattr(
        capture,
        "_neck_status",
        lambda: {"position": -46844, "target": -47000, "flags": "NONE"},
    )

    runner.run()

    document = __import__("json").loads((tmp_path / "yaw.json").read_text())
    assert len(document["stages"]["after_yaw"]["revolutions"]) == 10
    assert 20.0 <= document["stages"]["after_yaw"]["pose"]["yaw_deg"] < 25.0
    assert simulation.device.motion is None
    assert not simulation.device.enabled
    assert simulation.shell.commands[-2:] == ["manual_move 0 0", "sleep"]


def test_real_ohmni_yaw_rejects_full_pulse_reserve_after_target_crossing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    simulation = RunnerSimulation(monkeypatch)
    _configure_unit12(simulation)
    runner = capture.CameraPoseCaptureRunner(
        simulation.device,
        simulation.lease,
        tmp_path / "yaw.json",
        mode="yaw",
        boot_id="boot",
        executed_bundle_source_sha256="source",
        monotonic=simulation.clock,
        sleep=_half_response_sleep(simulation),
    )
    monkeypatch.setattr(
        capture,
        "_neck_status",
        lambda: {"position": -46844, "target": -47000, "flags": "NONE"},
    )

    def old_guard(now: float) -> str | None:
        reason = super(capture.CameraPoseCaptureRunner, runner)._device_guard(now)
        if reason is not None:
            return reason
        try:
            runner._check_private_limits(reserve_duration_s=runner.config.pulse_duration_s)
        except CalibrationError as error:
            return str(error)
        return None

    runner._device_guard = old_guard
    with pytest.raises(CalibrationError, match="camera_pose_yaw_limit"):
        runner.run()

    assert simulation.device.motion is None
    assert not simulation.device.enabled
    assert simulation.shell.commands[-2:] == ["manual_move 0 0", "sleep"]


def test_real_ohmni_yaw_overshoot_stops_and_disables(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    simulation = RunnerSimulation(monkeypatch)
    _configure_unit12(simulation)
    runner = capture.CameraPoseCaptureRunner(
        simulation.device,
        simulation.lease,
        tmp_path / "yaw.json",
        mode="yaw",
        boot_id="boot",
        executed_bundle_source_sha256="source",
        monotonic=simulation.clock,
        sleep=_yaw_overshoot_sleep(simulation),
    )
    monkeypatch.setattr(
        capture,
        "_neck_status",
        lambda: {"position": -46844, "target": -47000, "flags": "NONE"},
    )

    with pytest.raises(CalibrationError, match="camera_pose_yaw_limit"):
        runner.run()

    assert simulation.device.motion is None
    assert not simulation.device.enabled
    assert simulation.shell.commands[-2:] == ["manual_move 0 0", "sleep"]


def test_live_owner_resumes_only_after_a_cli_request_without_constructing_a_second_device(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tripped = False

    def pause_once(simulation: RunnerSimulation):
        raw_sleep = simulation.sleep

        def sleep(delay: float) -> None:
            nonlocal tripped
            raw_sleep(delay)
            if simulation.started_moving is not None and not tripped:
                tripped = True
                simulation.forward_scan_cm = 44

        return sleep

    runner, simulation = _simulated_capture_runner(monkeypatch, tmp_path, sleep_factory=pause_once)
    boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    source_sha256 = capture.camera_positioning_source_sha256()
    runner.boot_id = boot_id
    runner.executed_bundle_source_sha256 = source_sha256
    runner._capture_stage = lambda: {"revolutions": []}
    runner._settle = lambda: None
    raw_sleep = runner.sleep

    def sleep(delay: float) -> None:
        raw_sleep(delay)
        _set_forward_scan(simulation)
        if runner._resume_gate is not None:
            time.sleep(0.001)

    runner.sleep = sleep
    outcome: list[BaseException] = []
    owner = threading.Thread(target=lambda: _run_owner(runner, outcome))
    owner.start()
    paused_path = tmp_path / "capture.json.paused.json"
    _wait_for(paused_path)
    simulation.forward_scan_cm = 100
    _set_forward_scan(simulation)
    monkeypatch.setattr(capture, "OhmniDevice", lambda *_args: pytest.fail("new device opened"))

    assert (
        capture.main(
            [
                "--lease-port",
                "1",
                "--lease-token-file",
                str(tmp_path / "unused-token"),
                "--output",
                str(tmp_path / "ignored.json"),
                "--mode",
                "forward",
                "--device-id",
                "12",
                "--resume-from",
                str(paused_path),
                "--expected-boot-id",
                boot_id,
                "--expected-source-sha256",
                source_sha256,
            ]
        )
        == 0
    )
    owner.join(2)

    assert not owner.is_alive()
    assert outcome == []
    document = json.loads((tmp_path / "capture.json").read_text())
    assert document["stages"]["before_motion"] == {"revolutions": []}
    assert (
        document["resume_decision"]["pause_evidence_sha256"]
        == hashlib.sha256(paused_path.read_bytes()).hexdigest()
    )
    assert simulation.device.motion is None
    assert not simulation.device.enabled


def _run_owner(runner, outcome) -> None:
    try:
        runner.run()
    except BaseException as error:
        outcome.append(error)


def _wait_for(path: Path) -> None:
    deadline = time.monotonic() + 2
    while not path.exists():
        if time.monotonic() >= deadline:
            pytest.fail("live pause artifact was not written")
        time.sleep(0.001)


def _live_resume_args(tmp_path: Path, paused: Path, boot_id: str, source_sha256: str) -> list[str]:
    return [
        "--lease-port",
        "1",
        "--lease-token-file",
        str(tmp_path / "unused-token"),
        "--output",
        str(tmp_path / "ignored.json"),
        "--mode",
        "forward",
        "--device-id",
        "12",
        "--resume-from",
        str(paused),
        "--expected-boot-id",
        boot_id,
        "--expected-source-sha256",
        source_sha256,
    ]


def test_two_live_pauses_keep_prior_evidence_and_the_original_deadline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pauses = 0
    allow_second_pause = False

    def two_obstacles(simulation: RunnerSimulation):
        raw_sleep = simulation.sleep

        def sleep(delay: float) -> None:
            nonlocal pauses
            raw_sleep(delay)
            if simulation.device.motion is not None and (
                pauses == 0 or (pauses == 1 and allow_second_pause)
            ):
                pauses += 1
                simulation.forward_scan_cm = 44

        return sleep

    runner, simulation = _simulated_capture_runner(
        monkeypatch, tmp_path, sleep_factory=two_obstacles
    )
    boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    source_sha256 = capture.camera_positioning_source_sha256()
    runner.boot_id = boot_id
    runner.executed_bundle_source_sha256 = source_sha256
    runner._capture_stage = lambda: {"revolutions": []}
    runner._settle = lambda: None
    raw_sleep = runner.sleep

    def sleep(delay: float) -> None:
        raw_sleep(delay)
        _set_forward_scan(simulation)
        if runner._resume_gate is not None:
            time.sleep(0.001)

    runner.sleep = sleep
    outcome: list[BaseException] = []
    owner = threading.Thread(target=lambda: _run_owner(runner, outcome))
    owner.start()
    first = tmp_path / "capture.json.paused.json"
    _wait_for(first)
    first_bytes = first.read_bytes()
    first_pause = json.loads(first_bytes)
    allow_second_pause = True
    simulation.forward_scan_cm = 100
    _set_forward_scan(simulation)
    assert capture.main(_live_resume_args(tmp_path, first, boot_id, source_sha256)) == 0
    second = tmp_path / "capture.json.paused-2.json"
    _wait_for(second)
    second_pause = json.loads(second.read_text())
    with pytest.raises(CalibrationError, match="camera_pose_resume_owner_unavailable"):
        capture.main(_live_resume_args(tmp_path, first, boot_id, source_sha256))
    simulation.forward_scan_cm = 100
    _set_forward_scan(simulation)
    assert capture.main(_live_resume_args(tmp_path, second, boot_id, source_sha256)) == 0
    owner.join(2)

    assert not owner.is_alive()
    assert outcome == []
    assert first.read_bytes() == first_bytes
    assert (
        first_pause["live_resume"]["expires_at_monotonic_s"]
        == second_pause["live_resume"]["expires_at_monotonic_s"]
    )
    assert runner._progress.values()[0] >= first_pause["motion"]["measured_distance_m"]
    assert (tmp_path / "capture.json").exists()


def test_live_resume_request_is_bound_to_one_exact_pause_artifact(tmp_path: Path) -> None:
    gate = capture._LiveResumeGate(
        boot_id="boot",
        device_id=12,
        source_sha256="source",
        deadline=time.monotonic() + 1,
    )
    artifact = {
        "live_resume": {
            "socket": str(gate.path),
            "pause_nonce": gate.nonce,
            "expires_at_monotonic_s": gate.deadline,
            "boot_id": "boot",
            "device_id": 12,
            "tool_bundle_sha256": "source",
        }
    }
    payload = (json.dumps(artifact, separators=(",", ":")) + "\n").encode()
    paused = tmp_path / "capture.paused.json"
    paused.write_bytes(payload)
    gate.publish(payload)
    record = capture._read_pause(paused)
    try:
        altered = dict(record)
        altered["pause_evidence_sha256"] = "wrong"
        with pytest.raises(CalibrationError, match="camera_pose_resume_request_invalid"):
            capture._submit_live_resume(
                altered, boot_id="boot", device_id=12, source_sha256="source"
            )
        with pytest.raises(CalibrationError, match="camera_pose_resume_record_invalid"):
            capture._submit_live_resume(record, boot_id="boot", device_id=12, source_sha256="other")

        capture._submit_live_resume(record, boot_id="boot", device_id=12, source_sha256="source")
        with pytest.raises(CalibrationError, match="camera_pose_resume_request_used"):
            capture._submit_live_resume(
                record, boot_id="boot", device_id=12, source_sha256="source"
            )
    finally:
        gate.close()


def test_live_resume_gate_rejects_requests_after_its_owner_deadline() -> None:
    gate = capture._LiveResumeGate(
        boot_id="boot",
        device_id=12,
        source_sha256="source",
        deadline=3.0,
        monotonic=lambda: 3.0,
    )

    assert gate._accept(b"{}") == "camera_pose_resume_request_expired"


def test_live_resume_request_rejects_a_fabricated_pause_artifact(tmp_path: Path) -> None:
    paused = tmp_path / "fabricated.json"
    paused.write_text("{}\n")

    with pytest.raises(CalibrationError, match="camera_pose_resume_record_invalid"):
        capture._submit_live_resume(
            capture._read_pause(paused), boot_id="boot", device_id=12, source_sha256="source"
        )


def _write_inspection_capture(
    challenge_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path]:
    challenge = capture.inspection.parse_challenge(json.loads(challenge_path.read_text()))
    output = _produce_inspection_capture(challenge_path.parent, monkeypatch, challenge)
    return output / "main" / "frame-000000.json", output / "manifest.json"


def test_inspected_forward_consumes_one_verified_review_for_one_pulse(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runner, simulation = _simulated_capture_runner(monkeypatch, tmp_path)
    runner.mode = "inspected-forward"
    runner.boot_id = "boot-1"
    runner.executed_bundle_source_sha256 = capture.camera_positioning_source_sha256()
    runner._capture_stage = lambda: {"revolutions": []}
    raw_sleep = runner.sleep
    submitted = False

    def sleep(delay: float) -> None:
        nonlocal submitted
        raw_sleep(delay)
        challenge_path = tmp_path / "capture.json.inspection-challenge.json"
        if challenge_path.exists() and not submitted:
            frame, manifest = _write_inspection_capture(challenge_path, monkeypatch)
            assert (
                capture.inspection.main(
                    [
                        "--challenge",
                        str(challenge_path),
                        "--frame-record",
                        str(frame),
                        "--manifest",
                        str(manifest),
                        "--operator-id",
                        "spotter",
                        "--review-notes",
                        "clear",
                        "--accept",
                    ]
                )
                == 0
            )
            submitted = True

    runner.sleep = sleep
    runner.run()

    document = json.loads((tmp_path / "capture.json").read_text())
    assert submitted
    assert document["motion"]["pulses_completed"] == 1
    assert document["motion"]["travel_tolerance_m"] == 0.001
    assert (
        document["limits"]["predeclared_modes"]["inspected-forward"]["travel_tolerance_m"] == 0.001
    )
    assert document["inspection_approval"]["consumed"] is True
    assert simulation.device.motion is None
    assert not simulation.device.enabled


def test_inspected_forward_without_an_approval_never_starts_the_motor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runner, simulation = _simulated_capture_runner(monkeypatch, tmp_path)
    runner.mode = "inspected-forward"
    runner.boot_id = "boot"
    runner.executed_bundle_source_sha256 = capture.camera_positioning_source_sha256()
    runner._started = simulation.clock()
    assert simulation.device.enable()
    runner._initialize()
    runner._deadline = simulation.clock() + 0.02

    with pytest.raises(CalibrationError, match="camera_inspection_challenge_expired"):
        runner._run_inspected_forward({})

    assert simulation.started_moving is None
    assert simulation.device.motion is None


def test_inspection_request_reader_refuses_nonregular_or_oversized_inputs(tmp_path: Path) -> None:
    fifo = tmp_path / "approval.fifo"
    os.mkfifo(fifo)
    oversized = tmp_path / "approval.oversized"
    oversized.write_bytes(b"x" * (capture.inspection.MAX_REQUEST_BYTES + 1))
    target = tmp_path / "target.json"
    target.write_text("{}")
    link = tmp_path / "approval.link"
    link.symlink_to(target)

    for path in (fifo, oversized, link):
        with pytest.raises(capture.inspection.InspectionError, match="request is invalid"):
            capture.inspection._read_mapping(path)
