"""Production entry points require explicit identities and operator evidence."""

import asyncio

import pytest

from nodekit.cli import load_device, parse_args, resolve_key
from nodekit.fake import FakeAircraft, FakeGroundVehicle
from nodekit.node import Node, NodeConfig


def test_operator_and_home_attestations_are_not_invented_by_default():
    config = NodeConfig("ws://localhost", "isolated", 11, "test-key", "test")
    assert config.home_pose_confirmed is False
    assert config.safety_operator_present is False


@pytest.mark.parametrize("field", ["home_pose_confirmed", "safety_operator_present"])
def test_string_settings_cannot_be_truthy_operator_attestations(field):
    with pytest.raises(ValueError, match="explicit booleans"):
        NodeConfig("ws://localhost", "isolated", 11, "test-key", "test", **{field: "0"})


@pytest.mark.parametrize("home,operator", [(False, False), (False, True), (True, False)])
def test_join_for_diagnostics_keeps_device_disabled_without_both_attestations(home, operator):
    device = FakeGroundVehicle()
    node = Node(
        NodeConfig(
            "ws://localhost",
            "isolated",
            11,
            "test-key",
            "test",
            home_pose_confirmed=home,
            safety_operator_present=operator,
        ),
        device,
    )
    node._outbound = asyncio.Queue()
    node._anchor_clock(100_000)
    node._handle_membership({"action": "join", "connection_epoch": 1, "roster_version": 1})
    assert not device.enabled
    assert not device.status().control_authority


@pytest.mark.parametrize("device", ["ground", "aircraft", "nodekit.fake:FakeGroundVehicle"])
def test_generic_production_launcher_cannot_create_a_synthetic_device(device):
    with pytest.raises(SystemExit, match="fixtures belong in isolated tests"):
        load_device(device, None)


@pytest.mark.parametrize("device", [FakeAircraft, FakeGroundVehicle])
def test_fixture_origin_marker_survives_custom_capability_lists(device):
    assert "test:synthetic" in device(capabilities=()).capabilities


def test_relay_and_session_have_no_demo_fallback(monkeypatch):
    for key in ("SWEEP_RELAY_URL", "SWEEP_SESSION_ID"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(SystemExit):
        parse_args(["--device-id", "11", "--device", "adapters.ohmni.device:build"])


def test_a_console_credential_cannot_be_used_as_an_implicit_node_key(monkeypatch):
    monkeypatch.delenv("SWEEP_NODE_KEY", raising=False)
    monkeypatch.setenv("SWEEP_RELAY_TOKEN", "isolated-console-key")
    monkeypatch.setenv("SWEEP_ADAPTER_KEYS_JSON", '{"11":"isolated-other-config"}')
    with pytest.raises(SystemExit, match="no credential"):
        resolve_key(11, None)
