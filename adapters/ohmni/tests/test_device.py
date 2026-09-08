from __future__ import annotations

import math
import time
from pathlib import Path

import pytest

from adapters.ohmni.device import Config, OhmniDevice, battery_charge_band, from_environment
from adapters.ohmni.lidar import Lidar, discover, robot_bins
from adapters.ohmni.odometry import TICKS_PER_MM, Odometry, Pose, encoder_delta, encoder_pair
from adapters.ohmni.spike.rplidar_protocol import Measurement


class Shell:
    def __init__(self, _path=""):
        self.commands = []

    def command(self, text, **_kwargs):
        self.commands.append(text)
        return ""

    def close(self):
        pass


def device(*, lidar=True, **config):
    robot = OhmniDevice(
        Config(spotter_present=True, **config),
        shell_factory=Shell,
        lidar_discover=lambda: "/dev/test" if lidar else None,
        autostart=False,
    )
    robot.odometry.update((0, 0), time.monotonic())
    if lidar:
        robot.lidar._reader._port = "/dev/test"
        install_scan(robot)
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
    robot = device()
    assert robot.drive_shell is not robot.odometry.shell
    assert robot.drive_shell is not robot.battery_shell


def test_forward_and_ccw_use_manual_move_and_requested_speed_cap():
    robot = device()
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
    robot = device()
    identity = robot.move_to(1, 0, 0, 0.18)
    robot.step(robot._last_owner_tick + 0.36)
    assert robot.motion is None
    assert robot.last_refusal == "owner_loop_stalled"
    assert robot.motion_done(identity) is None
    assert robot.drive_shell.commands[-1] == "manual_move 0 0"


def test_missing_lidar_is_mandatory_and_spotter_withdrawal_disables():
    robot = device(lidar=False)
    assert robot.lidar is not None  # Owner remains available for USB recovery.
    assert robot.lidar._reader._configured_port is None
    assert not robot.enabled
    with pytest.raises(RuntimeError, match="lidar_missing"):
        robot.move_to(1, 0, 0, 0.18)
    with pytest.raises(ValueError, match="LiDAR is mandatory"):
        Config(allow_spotted_without_lidar=True)
    active = device()
    active.move_to(1, 0, 0, 0.18)
    active.set_spotter_present(False)
    assert not active.enabled and active.motion is None
    assert active.guard_reason() == "spotter_missing"


def install_scan(robot, bins=None):
    ranges = [300] * 360 if bins is None else bins
    robot.lidar.publish(
        [
            Measurement(index == 0, 20 if value else 0, float(index), value * 10)
            for index, value in enumerate(ranges)
        ],
        time.monotonic(),
    )


def test_raw_guard_requires_coverage_and_freshness_but_map_requires_calibration():
    robot = device()
    assert not robot.lidar.calibrated
    assert robot.guard_reason() is None
    assert robot.latest_scan() is None
    install_scan(robot, [0] * 360)
    assert robot.guard_reason() == "lidar_coverage_missing"
    install_scan(robot)
    assert robot.guard_reason() is None
    assert robot.guard_reason(now=robot.lidar.updated + 0.51) == "lidar_stale"
    assert robot.guard_reason(now=robot.lidar.updated - 0.01) == "lidar_stale"


def test_forward_guard_stops_the_motion_with_robot_frame_obstacle():
    robot = device(lidar=True, lidar_offset_deg=90, lidar_angle_sign=-1)
    install_scan(robot)
    identity = robot.move_to(1, 0, 0, 0.18)
    bins = [300] * 360
    bins[0] = 30
    install_scan(robot, bins)
    robot.step()
    assert robot.last_refusal == "obstacle_nearby"
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
    robot = device()
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


@pytest.mark.parametrize("angle", [0, 90, 180, 270, 359])
@pytest.mark.parametrize("distance_cm", [1, 30, 45])
def test_all_around_clearance_blocks_drive_and_turn_including_very_close_hits(angle, distance_cm):
    robot = device()
    ranges = [300] * 360
    ranges[angle] = distance_cm
    install_scan(robot, ranges)
    with pytest.raises(RuntimeError, match="obstacle_nearby"):
        robot.move_to(1, 0, 0, 0.18)
    with pytest.raises(RuntimeError, match="obstacle_nearby"):
        robot.rotate_to(90, 30)
    assert robot.drive_shell.commands[-1] == "manual_move 0 0"


@pytest.mark.parametrize("sector", range(12))
def test_unknown_sector_refuses_rotational_clearance(sector):
    robot = device()
    ranges = [300] * 360
    ranges[sector * 30 : (sector + 1) * 30] = [0] * 30
    install_scan(robot, ranges)
    assert robot.guard_reason() == "lidar_coverage_missing"
    with pytest.raises(RuntimeError, match="lidar_coverage_missing"):
        robot.rotate_to(90, 30)


def test_nearby_return_during_turn_stops_and_recovered_scan_never_resumes_old_motion():
    robot = device()
    motion = robot.rotate_to(90, 30)
    robot.step()
    assert robot.drive_shell.commands[-1] != "manual_move 0 0"
    ranges = [300] * 360
    ranges[180] = 17
    install_scan(robot, ranges)
    robot.step()
    assert robot.motion is None and robot.motion_done(motion) is None
    assert robot.last_refusal == "obstacle_nearby"
    assert robot.drive_shell.commands[-1] == "manual_move 0 0"
    install_scan(robot)
    before = list(robot.drive_shell.commands)
    robot.step()
    assert robot.drive_shell.commands == before


def test_enable_and_reconnect_never_reset_an_initialized_lidar_owner():
    robot = device()
    assert robot.drive_shell.commands.count("init") == 1
    robot.disable()
    assert robot.enable()
    robot.lidar._reader._release_vendor()
    assert robot.drive_shell.commands.count("init") == 1
    assert robot.drive_shell.commands.count("stop_collision_detection") == 1
    assert robot.lidar.shell.commands[-2:] == ["lidar_stop", "lidar_release"]


def test_vendor_initialization_happens_before_reader_handoff_and_adapts_timeout():
    robot = device(lidar=False)
    events = []
    original = robot.drive_shell.command

    def drive(text, **kwargs):
        events.append((text, kwargs))
        return original(text, **kwargs)

    def lidar(text, **kwargs):
        events.append((text, kwargs))
        return ""

    robot.drive_shell.command = drive
    robot.lidar.shell.command = lidar
    robot.lidar._reader._release_vendor()
    names = [name for name, _ in events]
    assert names.index("init") < names.index("stop_collision_detection") < names.index("lidar_stop")
    assert events[-2:] == [("lidar_stop", {"timeout": 0.2}), ("lidar_release", {"timeout": 0.2})]


@pytest.mark.parametrize(
    "minimum,expected", [(3149, 20), (3150, 50), (3249, 50), (3250, 80), (3379, 80), (3380, 100)]
)
def test_battery_matches_installed_vendor_min_cell_bands(minimum, expected):
    assert battery_charge_band([3400, 3400, minimum, 3400, 3400]) == expected


def test_actual_five_cell_reply_preserves_cells_voltage_coarse_charge_and_freshness():
    robot = device()
    now = time.monotonic()
    robot.update_battery("Last battery: [ 3269, 3282, 3305, 3285, 3259 ]\nLast docked: 0\n", now)
    status = robot.status()
    assert status.battery == 0.8
    assert status.extras["battery"]["cells_mv"] == [3269, 3282, 3305, 3285, 3259]
    assert status.extras["battery"]["voltage"] == pytest.approx(16.4)
    assert status.extras["battery"]["charge_source"] == "vendor_min_cell_band"
    assert status.extras["battery"]["charge_estimated"] is True
    robot.battery_updated -= 31
    stale = robot.status()
    assert stale.battery == 0.0
    assert stale.extras["battery"]["charge_percent"] is None
    assert stale.extras["battery"]["fresh"] is False


@pytest.mark.parametrize("cells", [[3300] * 4, [3300] * 6, [1999] * 5, [4301] * 5])
def test_unknown_battery_shape_is_not_mistaken_for_a_safe_charge(cells):
    with pytest.raises(ValueError):
        battery_charge_band(cells)
    robot = device()
    robot.update_battery(f"Last battery: {cells}\n", time.monotonic())
    assert robot.status().battery == 0
    assert robot.status().extras["battery"]["charge_percent"] is None


def test_missing_lidar_bypass_environment_is_rejected(monkeypatch):
    monkeypatch.setenv("SWEEP_ALLOW_NO_LIDAR", "1")
    with pytest.raises(ValueError, match="LiDAR is mandatory"):
        from_environment()


def test_custom_telemetry_keeps_raw_uncalibrated_scan_and_is_bounded_json():
    from nodekit.telemetry import device_telemetry_payload

    robot = device()
    status = robot.status()
    payload = device_telemetry_payload(status.extras)
    assert payload["lidar"]["frame"] == "lidar_raw"
    assert payload["lidar"]["calibrated"] is False
    assert payload["lidar"]["map_available"] is False
    assert payload["lidar"]["ranges_cm"] == [300] * 360
    assert payload["lidar"]["valid_bins"] == 360
    assert payload["lidar"]["sectors"] == 12
    assert payload["lidar"]["nearest_m"] == 3.0
    assert payload["safety"]["blocked"] is False
    assert payload["position"]["yaw_deg"] == 0
    assert payload["odometry"]["encoders"] == [0, 0]


def test_raw_reader_disconnect_withdraws_authority_and_later_detection_recovers_only_sensing():
    robot = device(lidar=False)
    scanner = robot.lidar
    scanner._running = True
    raw = scanner._reader
    raw._port = "/dev/ttyUSB2"
    raw.info = {"model": "RPLIDAR A2M8", "firmware": "1.29", "health": "good", "baudrate": 115200}
    raw.motor_pwm = 660
    raw._publish((300,) * 360, time.monotonic())
    assert robot.guard_reason() is None
    assert robot.enabled is False and robot.motion is None
    assert robot.enable()
    diagnostic = robot.status().extras["lidar"]
    assert diagnostic["health"] == "good"
    assert diagnostic["motor_pwm_requested"] == 660
    assert diagnostic["port"] == "/dev/ttyUSB2"
    motion = robot.rotate_to(90, 30)
    raw._invalidate("serial disconnected")
    assert robot.status().control_authority is False
    assert robot.status().extras["lidar"]["health"] is None
    robot.step()
    assert robot.motion_done(motion) is None and robot.motion is None
    assert robot.drive_shell.commands[-1] == "manual_move 0 0"
    raw._publish((300,) * 360, time.monotonic())
    robot.step()
    assert robot.motion is None
    assert robot.drive_shell.commands[-1] == "manual_move 0 0"


def test_platform_identity_reads_current_host_and_preserves_unknowns(monkeypatch):
    from types import SimpleNamespace

    def run(args, **kwargs):
        assert kwargs["timeout"] <= 2
        output = {
            "ro.product.model": "Ohmni-5-2\n",
            "ro.build.version.release": "7.1.2\n",
            "com.ohmnilabs.telebot_rtc": "  versionName=4.2.0\n",
        }[args[-1]]
        return SimpleNamespace(stdout=output)

    monkeypatch.setattr("adapters.ohmni.device.subprocess.run", run)
    profile = OhmniDevice._read_hardware_profile()
    assert profile["aircraft_model"] == "Ohmni-5-2"
    assert profile["aircraft_firmware"] == "telebot 4.2.0"
    assert profile["phone_model"] == "unreported"
    assert device().hardware_profile()["aircraft_firmware"] == "unreported"


def test_enable_cannot_claim_initial_signed_authority_without_odometry():
    robot = device()
    robot.odometry.lost = True
    assert robot.enable() is False
    assert robot.last_refusal == "wheel_odometry_unavailable"
    assert robot.status().control_authority is False


def test_failed_sensor_close_does_not_skip_independent_remaining_cleanup():
    from types import SimpleNamespace

    robot = device()
    closed = []

    def broken():
        closed.append("lidar")
        raise RuntimeError("worker did not stop")

    robot.lidar.close = broken
    robot.camera = SimpleNamespace(close=lambda: closed.append("camera"))
    robot.peripherals.close = lambda: closed.append("peripherals")
    robot.drive_shell.close = lambda: closed.append("drive")
    robot.battery_shell.close = lambda: closed.append("battery")
    with pytest.raises(OSError, match="resources failed to close"):
        robot.close()
    assert closed == ["lidar", "camera", "peripherals", "drive", "battery"]


def test_launcher_screen_bind_failure_closes_started_device(monkeypatch):
    from types import SimpleNamespace

    from adapters.ohmni import __main__ as launcher

    closed = []
    monkeypatch.delenv("SWEEP_NODE_KEY_FILE", raising=False)
    for key, value in {
        "SWEEP_NODE_KEY": "test-key",
        "SWEEP_DEVICE_ID": "11",
        "SWEEP_RELAY_URL": "ws://localhost",
        "SWEEP_SESSION_ID": "test",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(
        launcher, "from_environment", lambda **_: SimpleNamespace(close=lambda: closed.append(True))
    )
    monkeypatch.setattr(launcher, "Node", lambda *_: object())

    def fail_screen(*_):
        raise OSError("screen address already used")

    monkeypatch.setattr(launcher, "serve_screen", fail_screen)
    with pytest.raises(OSError, match="screen address"):
        launcher.main()
    assert closed == [True]
