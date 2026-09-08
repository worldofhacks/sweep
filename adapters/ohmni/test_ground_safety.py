"""Offline safety regressions: no hardware sockets, threads or motor writes."""

import asyncio
import json
import math
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from planner.models import CommandOperation
from relay.auth import sign_event
from relay.contracts import command_event, parse_command

from . import device as device_module
from .device import Config, LidarCoverage, Motion, OhmniDevice
from .fake import FakeGroundDevice
from .models import RangeScan
from .odometry import Pose
from .runtime import GroundRuntimeConfig, OhmniRuntime

KEY = "offline-test-key-with-at-least-32-characters"


class Shell:
    def __init__(self, path):
        self.commands = []
        self.fail_stop = False

    def command(self, text):
        self.commands.append(text)
        if self.fail_stop and text == "manual_move 0 0":
            raise OSError("test STOP failure")

    def close(self):
        pass


def configured(**overrides):
    values = dict(
        spotter_present=True,
        footprint_radius_m=0.3,
        stopping_distance_m=0.1,
        clearance_margin_m=0.1,
        lidar_mount_x_m=0.08,
        lidar_mount_y_m=0.02,
        lidar_mount_z_m=0.25,
        lidar_offset_deg=0.0,
        lidar_angle_sign=1,
    )
    values.update(overrides)
    device = OhmniDevice(
        Config(**values), shell_factory=Shell, lidar_discover=lambda: None, autostart=False
    )
    device.odometry = SimpleNamespace(snapshot=lambda *args: Pose(0, 0, 0, quality=0.6), lost=False)
    device.lidar = SimpleNamespace(
        calibrated=True,
        updated=time.monotonic(),
        scan=RangeScan(int(time.monotonic() * 1000), (0, 0, 0), 0, 1, 0.15, 12, [400] * 360),
        error=None,
    )
    return device


@pytest.mark.parametrize("angle", [0, 20, 90, 180, 270, 359])
@pytest.mark.parametrize("forward", [False, True])
def test_clearance_covers_every_direction_and_yaw(angle, forward):
    device = configured()
    device.lidar.scan.ranges_cm[angle] = 30
    assert device.guard_reason(forward=forward) == "obstacle_within_clearance"
    assert not device.enable()
    assert "init" not in device.drive_shell.commands


@pytest.mark.parametrize(
    "field",
    [
        "footprint_radius_m",
        "stopping_distance_m",
        "clearance_margin_m",
        "lidar_mount_x_m",
        "lidar_mount_y_m",
        "lidar_mount_z_m",
    ],
)
def test_missing_measured_clearance_refuses_before_enable(field):
    device = configured(**{field: None})
    assert device.guard_reason() == "ground_clearance_unconfigured"
    assert not device.enable()
    assert device.drive_shell.commands == []


def test_direct_lidar_guard_reports_unconfigured_clearance_before_sensor_state():
    device = configured(lidar_mount_x_m=None)

    evidence = device.lidar_guard_evidence()

    assert evidence.coverage is LidarCoverage.INVALID
    assert evidence.reason == "ground_clearance_unconfigured"
    assert device.guard_reason() == "ground_clearance_unconfigured"
    assert device.status().extras["lidar_guard"]["reason"] == "ground_clearance_unconfigured"


@pytest.mark.parametrize("invalid", [-1, True, float("nan")])
def test_invalid_scan_bin_cannot_be_inferred_clear(invalid):
    device = configured()
    device.lidar.scan.ranges_cm[181] = invalid
    evidence = device.lidar_guard_evidence()
    assert evidence.coverage is LidarCoverage.INVALID
    assert evidence.reason == "lidar_scan_invalid"
    assert evidence.invalid_bins == (181,)
    assert evidence.raw_ranges_cm[181] is invalid


def test_empty_and_sparse_scan_coverage_stay_stopped_with_their_raw_bins():
    device = configured()
    device.lidar.scan.ranges_cm[:] = [0] * 360
    missing = device.lidar_guard_evidence()
    assert missing.coverage is LidarCoverage.MISSING
    assert missing.reason == "lidar_scan_coverage_missing"
    assert missing.valid_bins == 0
    assert missing.missing_bins == tuple(range(360))
    assert missing.raw_ranges_cm == (0,) * 360
    assert not device.enable()

    device.lidar.scan.ranges_cm[:] = [400] * 360
    device.lidar.scan.ranges_cm[180] = 0
    sparse = device.lidar_guard_evidence()
    assert sparse.coverage is LidarCoverage.SPARSE
    assert sparse.reason == "lidar_scan_coverage_sparse"
    assert sparse.valid_bins == 359
    assert sparse.missing_bins == (180,)
    assert not device.enable()


@pytest.mark.parametrize("age", [-0.01, 0.51])
def test_scan_from_future_or_past_refuses(age):
    device = configured()
    evidence = device.lidar_guard_evidence(now=device.lidar.updated + age)
    assert evidence.coverage is LidarCoverage.STALE
    assert evidence.reason == "lidar_scan_stale"


def test_lidar_read_error_is_distinct_from_missing_scan_coverage():
    device = configured()
    device.lidar.error = "lidar_disconnected"
    evidence = device.lidar_guard_evidence()
    assert evidence.coverage is LidarCoverage.READ_ERROR
    assert evidence.reason == "lidar_read_error"
    assert evidence.read_error == "lidar_disconnected"
    assert not device.enable()


def test_operator_report_truncates_lidar_errors_to_the_status_contract_limit():
    device = configured()
    device.lidar.error = "x" * 10_000

    report = device.lidar_guard_evidence().operator_report()
    detail = json.dumps(report, sort_keys=True, separators=(",", ":"))

    assert len(detail) <= 512
    assert isinstance(report["read_error"], str)
    assert 0 < len(report["read_error"]) < 10_000
    node = node_for(device)
    node._publish_observations()
    reports = [
        event["payload"]
        for event in (node._outbound.get_nowait() for _ in range(node._outbound.qsize()))
        if event["type"] == "observation" and event["payload"]["kind"] == "status"
    ]
    emitted = next(event for event in reports if event["code"] == "ground_lidar_guard")
    assert len(emitted["detail"]) <= 512


def test_new_complete_scan_needs_fresh_validation_after_a_guard_stop():
    device = configured()
    assert device.enable()
    motion = device.drive_velocity(0.1, 0, 0.5)
    device.lidar.scan.ranges_cm[0] = 0
    device.step()
    assert device.motion_done(motion) is None
    assert device.status().state == "idle"
    fault = device.last_lidar_guard_fault
    assert fault is not None
    assert fault.reason == "lidar_scan_coverage_sparse"
    assert fault.raw_ranges_cm[0] == 0

    device.lidar.scan.ranges_cm[:] = [400] * 360
    device.lidar.updated = time.monotonic()
    assert device.guard_reason() is None
    assert device.lidar_guard_evidence().coverage is LidarCoverage.CLEAR
    resumed = device.drive_velocity(0.1, 0, 0.5)
    assert resumed != motion
    device.stop()

    device.lidar.updated = time.monotonic() - device.config.scan_max_age_s - 0.01
    assert device.guard_reason() == "lidar_scan_stale"
    with pytest.raises(RuntimeError, match="lidar_scan_stale"):
        device.drive_velocity(0.1, 0, 0.5)


def test_runtime_preserves_guard_reason_and_operator_evidence_for_a_sparse_scan():
    device = configured()
    assert device.enable()
    device.lidar.scan.ranges_cm[7] = 0
    node = node_for(device)
    node._ready = True
    node._watchdog_state = "nominal"
    node._last_heartbeat_expires_at = node._relay_now_ms() + 1_000
    now = int(time.time() * 1_000)
    frame = command_event(
        t=now,
        event_id="sparse-scan-command",
        session="offline-safety",
        command_id="sparse-scan-command",
        intent_id="sparse-scan-intent",
        roster_version=2,
        drone_id=11,
        connection_epoch=1,
        seq=1,
        issued_at=now,
        ttl_ms=1_000,
        operation=CommandOperation.GROUND_VELOCITY,
        args={"linear_mm_s": 100, "angular_mrad_s": 0, "duration_ms": 250},
    )
    frame["signature"] = sign_event(frame, KEY.encode())
    node._on_command(frame)
    events = [node._outbound.get_nowait() for _ in range(node._outbound.qsize())]
    acknowledgement = [event for event in events if event["type"] == "acknowledgement"][-1]
    status = next(event for event in events if event["type"] == "node_status")
    assert acknowledgement["reason"] == "lidar_scan_coverage_sparse"
    assert acknowledgement["detail"] == "local guard stopped the ground pulse"
    assert status["authority_change_reason"] == "lidar_scan_coverage_sparse"

    node._publish_observations()
    reports = [
        event["payload"]
        for event in (node._outbound.get_nowait() for _ in range(node._outbound.qsize()))
        if event["type"] == "observation" and event["payload"]["kind"] == "status"
    ]
    report = next(event for event in reports if event["code"] == "ground_lidar_guard")
    assert json.loads(report["detail"]) == {
        "coverage": "sparse",
        "invalid_bin_count": 0,
        "invalid_bins": [],
        "missing_bin_count": 1,
        "missing_bins": [7],
        "read_error": None,
        "reason": "lidar_scan_coverage_sparse",
        "scan_age_ms": pytest.approx(json.loads(report["detail"])["scan_age_ms"], abs=1),
        "scan_t_ms": device.lidar.scan.t_ms,
        "scan_updated_ms": pytest.approx(int(device.lidar.updated * 1_000), abs=1),
        "valid_bins": 359,
    }

    device.lidar.scan.ranges_cm[:] = [0] * 360
    node._publish_observations()
    reports = [
        event["payload"]
        for event in (node._outbound.get_nowait() for _ in range(node._outbound.qsize()))
        if event["type"] == "observation" and event["payload"]["kind"] == "status"
    ]
    empty_report = next(event for event in reports if event["code"] == "ground_lidar_guard")
    detail = json.loads(empty_report["detail"])
    assert len(empty_report["detail"]) <= 512
    assert detail["missing_bin_count"] == 360
    assert detail["missing_bins"] == list(range(32))


def test_sensor_offset_expands_required_clearance():
    device = configured(lidar_mount_x_m=1.0)
    device.lidar.scan.ranges_cm[:] = [120] * 360
    assert device.guard_reason() == "obstacle_within_clearance"


def test_valid_pose_required_before_any_init():
    device = configured()
    device.odometry.snapshot = lambda *args: Pose(0, 0, 0)
    assert not device.enable()
    assert device.last_refusal == "wheel_odometry_unavailable"
    assert device.drive_shell.commands == []


def test_positive_clearance_and_pose_enable_with_no_nonzero_drive():
    device = configured()
    assert device.enable()
    assert all(
        text == "manual_move 0 0"
        for text in device.drive_shell.commands
        if text.startswith("manual_move")
    )


def test_reported_authority_withdraws_immediately_when_clearance_fails():
    device = configured()
    assert device.enable()
    assert device.status().drive_authority
    device.lidar.scan.ranges_cm[180] = 20
    assert not device.status().drive_authority


def test_stop_failure_never_retains_successful_motion_completion():
    device = configured()
    assert device.enable()
    device.motion = Motion("test-motion", None, 0, 0.1, time.monotonic())
    device.drive_shell.fail_stop = True
    with pytest.raises(OSError):
        device._finish(True)
    assert device.motion_done("test-motion") is None
    assert not device.stop_confirmed()
    device.drive_shell.fail_stop = False
    device.step()
    assert device.stop_confirmed()
    assert device.motion_done("test-motion") is None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"allow_spotted_without_lidar": True},
        {"owner_timeout_s": 1},
        {"owner_timeout_s": float("nan")},
        {"footprint_radius_m": True},
        {"stopping_distance_m": 0},
    ],
)
def test_unsafe_configuration_rejected(kwargs):
    with pytest.raises(ValueError):
        Config(**kwargs)


class StopFailure(FakeGroundDevice):
    def stop(self):
        raise OSError("STOP failed")

    def disable(self):
        self.disable_attempted = True
        self.enabled = False


def node_for(device, *, mount=True):
    config = GroundRuntimeConfig(
        "ws://localhost",
        "offline-safety",
        11,
        KEY,
        "ohmni-test",
        **(
            dict(
                lidar_mount_x_m=0.08,
                lidar_mount_y_m=0.02,
                lidar_mount_z_m=0.25,
                lidar_mount_yaw_deg=0.0,
            )
            if mount
            else {}
        ),
    )
    node = OhmniRuntime(config, device)
    node._epoch = 1
    node._roster_version = 2
    node._outbound = asyncio.Queue()
    return node


def command(operation):
    now = int(time.time() * 1000)
    frame = command_event(
        t=now,
        event_id="test-command",
        session="offline-safety",
        command_id="test-command",
        intent_id="test-intent",
        roster_version=2,
        drone_id=11,
        connection_epoch=1,
        seq=1,
        issued_at=now,
        ttl_ms=1000,
        operation=operation,
        args={},
    )
    frame["signature"] = sign_event(frame, KEY.encode())
    return frame


@pytest.mark.parametrize("operation", [CommandOperation.HOVER, CommandOperation.ESTOP])
def test_stop_acknowledgement_is_failed_when_writes_fail(operation):
    device = StopFailure(enabled=True)
    node = node_for(device)
    node._ready = True
    node._on_command(command(operation))
    events = [node._outbound.get_nowait() for _ in range(node._outbound.qsize())]
    acks = [event for event in events if event["type"] == "acknowledgement"]
    assert [ack["status"] for ack in acks] == ["accepted", "executing", "failed"]
    assert acks[-1]["reason"] == "local_stop_unconfirmed"
    assert not node._ready
    assert not node._local_stop_ready
    if operation is CommandOperation.ESTOP:
        assert device.disable_attempted
        assert not device.enabled


def test_motion_result_true_cannot_bypass_final_stop_confirmation():
    device = StopFailure(enabled=True, stopped=False)
    node = node_for(device)
    asyncio.run(
        node._complete_motion(parse_command(command(CommandOperation.HOVER)), "test-motion")
    )
    events = [node._outbound.get_nowait() for _ in range(node._outbound.qsize())]
    ack = [event for event in events if event["type"] == "acknowledgement"][-1]
    assert ack["status"] == "failed"
    assert ack["reason"] == "local_stop_unconfirmed"
    assert device.disable_attempted


class InvalidPose(FakeGroundDevice):
    enable_attempted = False

    def status(self):
        return replace(super().status(), pos_quality=0.0)

    def enable(self):
        self.enable_attempted = True
        return True


def heartbeat(node):
    now = node._relay_now_ms()
    frame = dict(
        v=1,
        t=now,
        type="control_heartbeat",
        event_id="test-heartbeat",
        session=node.config.session,
        source="relay",
        drone_id=11,
        connection_epoch=1,
        roster_version=2,
        seq=1,
        issued_at=now,
        expires_at=now + 1000,
        hold_after_ms=node.config.heartbeat_hold_ms,
        failsafe_after_ms=node.config.heartbeat_failsafe_ms,
    )
    frame["signature"] = sign_event(frame, KEY.encode())
    return frame


def test_zero_confidence_pose_cannot_enable_on_fresh_signed_heartbeat():
    device = InvalidPose()
    node = node_for(device)
    node._on_heartbeat(heartbeat(node))
    assert not node._ready
    assert not device.enable_attempted
    assert node._watchdog_state == "failsafe"
    events = [node._outbound.get_nowait() for _ in range(node._outbound.qsize())]
    assert all(not event["control_authority"] for event in events if event["type"] == "node_status")


def test_missing_mount_is_read_only_even_with_valid_fake_pose():
    device = FakeGroundDevice()
    node = node_for(device, mount=False)
    node._on_heartbeat(heartbeat(node))
    assert not node._ready and not device.enabled


@pytest.mark.parametrize(("wheel_diameter", "expected"), [(None, 150.5), ("152.4", 152.4)])
def test_environment_wheel_model_reaches_integrated_wheel_distance(
    monkeypatch, wheel_diameter, expected
):
    received = []

    class EnvironmentDevice:
        def __init__(self, config, *, camera=None):
            received.append(config)

    monkeypatch.setattr(device_module, "OhmniDevice", EnvironmentDevice)
    monkeypatch.delenv("SWEEP_MEDIA_HOST", raising=False)
    monkeypatch.delenv("SWEEP_ALLOW_NO_LIDAR", raising=False)
    if wheel_diameter is None:
        monkeypatch.delenv("SWEEP_WHEEL_DIAMETER_MM", raising=False)
    else:
        monkeypatch.setenv("SWEEP_WHEEL_DIAMETER_MM", wheel_diameter)
    device_module.from_environment()
    device = OhmniDevice(
        received[0], shell_factory=Shell, lidar_discover=lambda: None, autostart=False
    )
    try:
        device.odometry.update((1000, 1000), 1.0)
        device.odometry.update((0, 2000), 1.1)
        pose = device.odometry.snapshot(1.1)
        assert pose.x == pytest.approx(math.pi * expected / (16384 * (30 / 11)))
        assert pose.y == pytest.approx(0.0)
        assert pose.yaw_deg == pytest.approx(0.0)
        assert pose.quality > 0
        assert not device.enabled
    finally:
        device.close()


@pytest.mark.parametrize("invalid", [True, False, 0, -1, float("nan"), float("inf"), "152.4"])
def test_invalid_wheel_model_is_refused_before_device_construction(invalid):
    with pytest.raises(ValueError, match="wheel"):
        Config(wheel_diameter_mm=invalid)
