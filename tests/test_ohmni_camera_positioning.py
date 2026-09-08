import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

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

    def stop(self) -> None:
        self.stop_calls += 1

    def disable(self) -> None:
        self.disable_calls += 1

    def enable(self) -> bool:
        self.enable_calls += 1
        return True

    def guard_reason(self, *, forward=False, now=None):
        return None

    def calibration_drive_velocity(self, _speed, _yaw, _duration, *, host_lease):
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

    assert instance.forward_pulses_completed == 11
    assert instance.forward_pulses_completed <= capture.FORWARD_MAX_PULSES
    assert instance.forward_distance_m == pytest.approx(0.22)


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
    assert 0.2 <= runner.forward_distance_m <= 0.26
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
    assert 0.2 <= runner.forward_distance_m <= 0.26
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
    assert motion["velocity_m_s"] == 0.04
    assert motion["pulse_duration_s"] == 0.5
    assert motion["maximum_pulses"] == 28
    assert motion["pulses_completed"] == runner.forward_pulses_completed
    assert 0.2 <= motion["measured_distance_m"] <= 0.26
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
    runner._capture_stage = lambda: {"revolutions": []}
    runner._settle = lambda: None

    with pytest.raises(capture.CameraPosePaused, match="obstacle_within_clearance"):
        runner.run()

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


def test_forward_resume_continues_from_the_measured_pause_pose(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    initial, simulation = _simulated_capture_runner(
        monkeypatch, tmp_path, sleep_factory=_obstacle_after_motion_sleep
    )
    initial._capture_stage = lambda: {"revolutions": []}
    initial._settle = lambda: None

    with pytest.raises(capture.CameraPosePaused, match="obstacle_within_clearance"):
        initial.run()

    paused_path = tmp_path / "capture.json.paused.json"
    paused = capture._read_pause(paused_path)
    simulation.forward_scan_cm = 100
    _set_forward_scan(simulation)

    def sleep(delay: float) -> None:
        simulation.sleep(delay)
        _set_forward_scan(simulation)

    resumed = capture.CameraPoseCaptureRunner(
        simulation.device,
        simulation.lease,
        tmp_path / "capture.json",
        mode="forward",
        resume=paused,
        boot_id="boot",
        executed_bundle_source_sha256="source",
        monotonic=simulation.clock,
        sleep=sleep,
    )
    capture_calls: list[str] = []
    resumed._capture_stage = lambda: capture_calls.append("after") or {"revolutions": []}
    resumed._settle = lambda: None

    resumed.run()

    document = __import__("json").loads((tmp_path / "capture.json").read_text())
    assert capture_calls == ["after"]
    assert document["stages"]["before_motion"] == {"revolutions": []}
    assert document["motion"]["measured_distance_m"] >= capture.FORWARD_TARGET_M
    assert document["resume_decision"]["pause_evidence_sha256"] == paused["pause_evidence_sha256"]
    assert document["resume_decision"]["remaining_distance_m"] < capture.FORWARD_TARGET_M
    assert (
        document["resume_decision"]["expires_at_monotonic_s"]
        > document["resume_decision"]["issued_at_monotonic_s"]
    )
    assert simulation.device.motion is None
    assert not simulation.device.enabled


def test_forward_resume_refuses_a_changed_pause_pose_without_motion(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    initial, simulation = _simulated_capture_runner(
        monkeypatch, tmp_path, sleep_factory=_obstacle_after_motion_sleep
    )
    initial._capture_stage = lambda: {"revolutions": []}
    initial._settle = lambda: None
    with pytest.raises(capture.CameraPosePaused):
        initial.run()

    paused = capture._read_pause(tmp_path / "capture.json.paused.json")
    simulation.forward_scan_cm = 100
    simulation.clock.value += 0.1
    _set_forward_scan(simulation)
    ticks = 0.04 * 1000 * TICKS_PER_MM
    simulation.left -= ticks
    simulation.right += ticks
    simulation._sample()
    resumed = capture.CameraPoseCaptureRunner(
        simulation.device,
        simulation.lease,
        tmp_path / "capture.json",
        mode="forward",
        resume=paused,
        boot_id="boot",
        executed_bundle_source_sha256="source",
        monotonic=simulation.clock,
        sleep=simulation.sleep,
    )
    resumed._capture_stage = lambda: pytest.fail("resume recaptured before admission")

    with pytest.raises(CalibrationError, match="camera_pose_resume_pose_changed"):
        resumed.run()

    assert simulation.device.motion is None
    assert not simulation.device.enabled
