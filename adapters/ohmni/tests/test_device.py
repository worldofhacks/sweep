from __future__ import annotations

import math
import time
from pathlib import Path

import pytest

from adapters.ohmni.camera import command, publish_url
from adapters.ohmni.device import Config, OhmniDevice
from adapters.ohmni.lidar import Lidar, discover, robot_bins
from adapters.ohmni.odometry import TICKS_PER_MM, Odometry, Pose, encoder_delta, encoder_pair
from adapters.ohmni.spike.rplidar_protocol import Measurement
from nodekit.device import Scan


class Shell:
    def __init__(self, _path=""):
        self.commands = []

    def command(self, text, **_kwargs):
        self.commands.append(text)
        return ""

    def close(self):
        pass


def device(*, lidar=False, **config):
    robot = OhmniDevice(
        Config(spotter_present=True, **config),
        shell_factory=Shell,
        lidar_discover=lambda: "/dev/test" if lidar else None,
        autostart=False,
    )
    robot.odometry.update((0, 0), time.monotonic())
    robot.enable()
    return robot


def test_encoder_wrap_and_measured_forward_signs():
    assert encoder_delta(16380, 20) == 24
    assert encoder_delta(20, 16380) == -24
    odom = Odometry(Shell(), (1.2, -0.4, 0))
    odom.update((16380, 20), 10.0)
    ticks = round(TICKS_PER_MM * 10)
    odom.update(((16380 + ticks) % 16384, (20 - ticks) % 16384), 10.1)
    assert odom.pose.x == pytest.approx(1.21, abs=0.0001)
    assert odom.pose.y == -0.4
    assert odom.pose.yaw_deg == 0
    # Both encoder-positive corresponds to clockwise; CCW yaw decreases.
    odom.update(((16380 + ticks * 2) % 16384, 20), 10.2)
    assert 350 < odom.pose.yaw_deg < 360


def test_encoder_reply_content_and_irrecoverable_sample_gap():
    assert encoder_pair("battery junk\napos 1 = 16382\nother reply\napos 0 = 12\n") == (12, 16382)
    assert encoder_pair("apos 0 = 16384\napos 1 = 0\n") is None
    odom = Odometry(Shell(), (0, 0, 0))
    odom.update((0, 0), 10.0)
    odom.update((20, 16364), 10.5)
    assert odom.lost and odom.snapshot(10.5).quality == 0
    odom.update((40, 16344), 10.6)
    assert odom.pose.x == 0


def test_encoder_and_drive_sockets_are_independent():
    robot = device(allow_spotted_without_lidar=True)
    assert robot.drive_shell is not robot.odometry.shell
    assert robot.drive_shell is not robot.battery_shell


def test_forward_and_ccw_use_manual_move_and_requested_speed_cap():
    robot = device(allow_spotted_without_lidar=True)
    identity = robot.move_to(1, 0, 0, 0.3)
    robot.step()
    assert robot.drive_shell.commands[-1] == "manual_move 250 -250"
    assert robot.motion_done(identity) is False
    robot.rotate_to(90, 30)
    robot.step()
    _, left, right = robot.drive_shell.commands[-1].split()
    assert int(left) < 0 and left == right
    assert all(not c.startswith(("pre_drive", "pre_rot")) for c in robot.drive_shell.commands)
    robot.stop()
    assert robot.drive_shell.commands[-1] == "manual_move 0 0"
    robot.disable()
    assert robot.drive_shell.commands[-1] == "sleep"


def test_owner_stall_stops_even_when_node_event_loop_cannot_run_watchdog():
    robot = device(allow_spotted_without_lidar=True)
    identity = robot.move_to(1, 0, 0, 0.18)
    robot.step(robot._last_owner_tick + 0.36)
    assert robot.motion is None
    assert robot.last_refusal == "owner_loop_stalled"
    assert robot.motion_done(identity) is None
    assert robot.drive_shell.commands[-1] == "manual_move 0 0"


def test_no_kit_requires_explicit_spotted_fallback_and_spotter_withdrawal_disables():
    robot = device()
    with pytest.raises(RuntimeError, match="lidar_missing"):
        robot.move_to(1, 0, 0, 0.18)
    fallback = device(allow_spotted_without_lidar=True)
    fallback.move_to(1, 0, 0, 0.18)
    fallback.set_spotter_present(False)
    assert not fallback.enabled and fallback.motion is None
    assert fallback.guard_reason() == "spotter_missing"


def install_scan(robot, bins=None):
    robot.lidar.scan = Scan(10, (0, 0, 0), 0, 1, 0.15, 12, bins or [300] * 360)
    robot.lidar.updated = time.monotonic()


def test_installed_kit_never_falls_back_on_unknown_alignment_stale_or_empty_scan():
    robot = device(lidar=True, allow_spotted_without_lidar=True)
    assert robot.guard_reason() == "lidar_calibration_required"
    robot.lidar.offset_deg, robot.lidar.angle_sign = 30, -1
    assert robot.guard_reason() == "lidar_stale"
    install_scan(robot, [0] * 360)
    assert robot.guard_reason() == "lidar_forward_coverage_missing"
    install_scan(robot)
    assert robot.guard_reason() is None
    assert robot.guard_reason(now=robot.lidar.updated + 0.51) == "lidar_stale"


def test_forward_guard_stops_the_motion_with_robot_frame_obstacle():
    robot = device(lidar=True, lidar_offset_deg=90, lidar_angle_sign=-1)
    install_scan(robot)
    identity = robot.move_to(1, 0, 0, 0.18)
    robot.lidar.scan.ranges_cm[0] = 30
    robot.step()
    assert robot.last_refusal == "obstacle_ahead"
    assert robot.motion_done(identity) is None
    assert robot.drive_shell.commands[-1] == "manual_move 0 0"


def test_mounting_rotation_and_handedness_apply_before_publish_and_keep_nearest_return():
    points = [
        Measurement(True, 20, 90, 500),
        Measurement(False, 20, 90.1, 300),
        Measurement(False, 20, 80, 1000),
        Measurement(False, 0, 100, 200),
    ]
    bins = robot_bins(points, 90, -1)
    assert bins[0] == 30 and bins[10] == 100 and bins[350] == 0
    scanner = Lidar(
        Shell(), "/unused", lambda: Pose(1, 2, 30, quality=0.6), offset_deg=None, angle_sign=None
    )
    scanner.publish(points, 10)
    assert scanner.scan is None
    scanner.offset_deg, scanner.angle_sign = 90, -1
    scanner.publish(points, 11)
    assert scanner.scan.pose == (1, 2, 30)
    assert scanner.scan.ranges_cm == bins
    assert scanner.scan.t_ms == 11000


def test_usb_discovery_does_not_open_wheel_bus(tmp_path: Path):
    sysfs = tmp_path / "usb-serial"
    sysfs.mkdir()
    for name, vendor, product in (("ttyUSB0", "0403", "6015"), ("ttyUSB2", "10c4", "ea60")):
        root = tmp_path / name
        child = root / "interface" / name
        child.mkdir(parents=True)
        (root / "idVendor").write_text(vendor)
        (root / "idProduct").write_text(product)
        (sysfs / name).symlink_to(child)
    assert discover(sysfs) == "/dev/ttyUSB2"
    (sysfs / "ttyUSB2").unlink()
    assert discover(sysfs) is None


def test_camera_is_uvc_h264_tcp_and_uses_class_media_credential():
    url = publish_url("media.local", 2, "test-key")
    assert url.startswith("rtsp://ground2:") and url.endswith("@media.local:8554/ground2")
    args = command("ffmpeg", url)
    assert args[args.index("-rtsp_transport") + 1] == "tcp"
    assert args[args.index("-input_format") + 1] == "uyvy422"
    assert args[args.index("-c:v") + 1] == "libx264"
    with pytest.raises(ValueError):
        publish_url("user@media", 1, "test-key")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_speed_m_s": 0.5},
        {"launch": (math.nan, 0, 0)},
        {"lidar_offset_deg": math.inf},
        {"lidar_angle_sign": 0},
    ],
)
def test_invalid_hardware_configuration_is_rejected(kwargs):
    with pytest.raises(ValueError):
        Config(**kwargs)


def test_failed_stop_and_sleep_are_retried_even_after_motion_is_cleared():
    robot = device(allow_spotted_without_lidar=True)
    robot.move_to(1, 0, 0, 0.18)
    original = robot.drive_shell.command
    attempted = []

    def broken(text, **kwargs):
        attempted.append(text)
        raise OSError("socket gone")

    robot.drive_shell.command = broken
    with pytest.raises(OSError):
        robot.disable()
    assert not robot.enabled and robot.motion is None
    assert robot._pending_stop and robot._pending_sleep
    assert attempted == ["manual_move 0 0", "sleep"]
    with pytest.raises(OSError):
        robot.step()
    assert attempted == ["manual_move 0 0", "sleep"] * 2
    robot.drive_shell.command = original
    robot.step()
    assert not robot._pending_stop and not robot._pending_sleep
    assert robot.drive_shell.commands[-2:] == ["manual_move 0 0", "sleep"]
