from __future__ import annotations

import asyncio
import json

import pytest

from relay.app import RelayRuntime
from relay.auth import Principal
from relay.contracts import NodeType
from relay.media import MediaMonitor, MediaPathObservation
from relay.settings import RelaySettings, SettingsError
from relay.tests.conftest import ADAPTER_KEY, CONSOLE_KEY, SESSION, MutableClock, membership_payload
from relay.tests.test_media import FakePathClient


def test_explicit_ground_unit_preserves_wire_identity_and_disabled_control(tmp_path):
    settings = RelaySettings.from_env(
        {
            "SWEEP_RELAY_TOKEN": CONSOLE_KEY.decode(),
            "SWEEP_ADAPTER_KEYS_JSON": json.dumps({"11": ADAPTER_KEY.decode()}),
            "SWEEP_NODE_TYPES_JSON": '{"11":"ground"}',
            "SWEEP_DEVICE_UNITS_JSON": '{"11":1}',
            "SWEEP_MEDIA_STREAMS_JSON": '{"11":"ground1"}',
            "SWEEP_SESSION_LOG_DIR": str(tmp_path),
            "SWEEP_ADAPTER_BACKEND": "remote",
        }
    )
    runtime = RelayRuntime(settings, clock=MutableClock())
    session = runtime.session(SESSION)
    session.process_frame(
        membership_payload(
            action="join",
            event_id="g01-join",
            drone_id=11,
            node_type="ground",
            capabilities=["ground_drive"],
        ),
        Principal("adapter", 11, ADAPTER_KEY),
    )
    state = session.current_state()
    assert [(d["drone_id"], d["device_class"], d["unit"]) for d in state["drones"]] == [
        (11, "ground_vehicle", 1)
    ]
    assert state["armed"] is False
    assert state["selection"] == []
    assert session.intent_sink is None
    assert settings.media_streams == {11: "ground1"}
    assert state["drones"][0]["cameras"] == [
        {
            "camera_id": "primary",
            "label": "Primary camera",
            "stream": "ground1",
            **state["drones"][0]["video"],
        }
    ]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"device_units": {12: 1}},
        {"device_units": {11: True}},
        {"device_units": {11: 0}},
        {"media_streams": {12: "ground1"}},
        {"media_streams": {11: "../other"}},
        {"media_streams": {11: "ground1?token=wrong"}},
    ],
)
def test_device_mapping_rejects_ambiguous_or_unconfigured_identity(kwargs):
    with pytest.raises(SettingsError):
        RelaySettings(relay_token=CONSOLE_KEY, adapter_keys={11: ADAPTER_KEY}, **kwargs)


def test_duplicate_units_within_class_cannot_alias_two_robots():
    with pytest.raises(SettingsError, match="unique"):
        RelaySettings(
            relay_token=CONSOLE_KEY,
            adapter_keys={11: ADAPTER_KEY, 12: ADAPTER_KEY + b"different"},
            node_types={11: NodeType.GROUND, 12: NodeType.GROUND},
            device_units={11: 1, 12: 1},
        )


def test_media_evidence_stays_bound_to_configured_device_and_expires():
    clock = MutableClock()
    client = FakePathClient()
    monitor = MediaMonitor(client, clock=clock, drone_ids=(11,), streams={11: "ground1"})
    client.paths["ground1"] = MediaPathObservation(online=True, inbound_bytes=100)
    asyncio.run(monitor.poll_once())
    assert client.calls == ["ground1"]
    assert monitor.evidence(1, clock()) is None
    first = monitor.evidence(11, clock()).last_frame_at
    clock.advance(1000)
    client.paths["ground1"] = MediaPathObservation(online=True, inbound_bytes=200)
    asyncio.run(monitor.poll_once())
    assert monitor.evidence(11, clock()).last_frame_at > first
    clock.advance(3001)
    assert monitor.evidence(11, clock()).fresh is False


@pytest.mark.parametrize(
    "kwargs",
    [
        {"device_units": {11: 1}},
        {"media_streams": {11: "drone1"}},
    ],
)
def test_override_cannot_alias_another_devices_default_identity(kwargs):
    with pytest.raises(SettingsError, match="unique"):
        RelaySettings(
            relay_token=CONSOLE_KEY,
            adapter_keys={1: ADAPTER_KEY, 11: ADAPTER_KEY + b"different"},
            node_types={1: NodeType.GROUND, 11: NodeType.GROUND},
            **kwargs,
        )
