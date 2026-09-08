"""Real Ohmni adapter joins diagnostic-only without synthetic calibration claims."""

import asyncio

import pytest

from adapters.ohmni.device import Config, OhmniDevice, from_environment
from adapters.ohmni.tests.test_device import Shell, device
from nodekit.node import Node, NodeConfig
from nodekit.telemetry import device_telemetry_payload


def test_unconfirmed_robot_reports_diagnostics_without_enabling_or_fabricating_camera_values():
    robot = device()
    node = Node(NodeConfig("ws://localhost", "isolated", 11, "test-key", "test"), robot)
    node._outbound = asyncio.Queue()
    node._anchor_clock(100_000)
    node._handle_membership({"action": "join", "connection_epoch": 1, "roster_version": 1})
    frames = []
    while not node._outbound.empty():
        frames.append(node._outbound.get_nowait())
    assert [frame["type"] for frame in frames] == ["telemetry", "membership", "node_status"]
    assert not robot.enabled
    readiness = frames[1]
    assert readiness["home_pose_confirmed"] is False
    assert readiness["rc_safety_operator_present"] is False
    assert readiness["control_authority"] is False
    custom = frames[2]["device_telemetry"]
    assert device_telemetry_payload(custom) == custom
    assert custom["link"]["radio_quality"] is None
    assert custom["link"]["legacy_value_semantics"] == "authenticated_transport_liveness"
    assert custom["position"]["frame"] == "launch_wheel_odometry"
    assert custom["cameras"] == []
    assert "horizontal_fov_deg" not in custom["identity"]


def test_vendor_reinitialization_retains_stop_and_disable_for_diagnostic_startup():
    robot = OhmniDevice(Config(), shell_factory=Shell, lidar_discover=lambda: None, autostart=False)
    robot._initialize_vendor()
    assert robot.drive_shell.commands[-2:] == ["manual_move 0 0", "sleep"]
    assert not robot.enabled
    assert not robot._pending_stop and not robot._pending_sleep


@pytest.mark.parametrize("missing", ["X", "Y", "YAW_DEG"])
def test_home_confirmation_requires_all_explicit_measured_coordinates(monkeypatch, missing):
    monkeypatch.delenv("SWEEP_ALLOW_NO_LIDAR", raising=False)
    monkeypatch.setenv("SWEEP_HOME_CONFIRMED", "1")
    for axis in ("X", "Y", "YAW_DEG"):
        monkeypatch.setenv(f"SWEEP_HOME_{axis}", "0")
    monkeypatch.delenv(f"SWEEP_HOME_{missing}")
    with pytest.raises(ValueError, match="explicit measured X, Y and yaw"):
        from_environment(key="isolated-test-key")
