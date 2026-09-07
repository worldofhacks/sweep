from __future__ import annotations

import pytest

from relay.app import RelayRuntime
from relay.audit import AuditLogError, SessionAuditLog
from relay.auth import Principal
from relay.capabilities import C1_CAPABILITY_PROFILE, C2_CAPABILITY_PROFILE
from relay.contracts import NodeType, parse_membership_request
from relay.session import (
    CapabilityBoundIntentSink,
    RelayLimits,
    RelaySession,
    _material_state_projection,
)
from relay.settings import RelaySettings, SettingsError
from relay.state import FleetRegistry, RegistryError
from relay.tests.conftest import (
    ADAPTER_KEY,
    CONSOLE_KEY,
    SESSION,
    EventIds,
    MutableClock,
    intent_payload,
    membership_payload,
)

GROUND_KEY = b"ground-adapter-key-that-is-at-least-32-bytes"


def _ground_join(device_id: int, event_id: str) -> object:
    return parse_membership_request(
        membership_payload(
            action="join",
            event_id=event_id,
            drone_id=device_id,
            node_type="ground",
            capabilities=["ground_drive"],
        )
    )


def test_host_node_type_controls_authenticated_join_and_state(tmp_path):
    session = RelaySession(
        session_id=SESSION,
        audit_log=SessionAuditLog(tmp_path, SESSION),
        limits=RelayLimits(5_000, 5_000, 1_000, 1_000),
        clock=MutableClock(),
        event_ids=EventIds(),
        node_types={9: NodeType.GROUND},
    )
    principal = Principal("adapter", 9, GROUND_KEY)

    accepted = session.process_membership(
        membership_payload(
            action="join",
            event_id="ground-join",
            drone_id=9,
            key=GROUND_KEY,
            node_type="ground",
            capabilities=["ground_drive"],
        ),
        principal,
    )
    state = session.current_state()

    assert accepted[0]["node_type"] == "ground"
    assert state["drones"][0]["drone_id"] == 9
    assert state["drones"][0]["node_type"] == "ground"
    assert state["drones"][0]["adapter_capabilities"] == ["ground_drive"]

    refused = session.process_membership(
        membership_payload(
            action="join",
            event_id="wrong-class",
            drone_id=9,
            key=GROUND_KEY,
            capabilities=["ground_drive"],
        ),
        principal,
    )
    assert refused[0]["reason"] == "node_type_mismatch"


def test_ground_capacity_is_separate_from_aircraft_capacity():
    registry = FleetRegistry(
        telemetry_freshness_ms=1_000,
        node_types={device_id: NodeType.GROUND for device_id in range(9, 13)},
    )
    for device_id in range(1, 5):
        registry.apply_join(
            parse_membership_request(
                membership_payload(
                    action="join", event_id=f"aircraft-{device_id}", drone_id=device_id
                )
            )
        )
    for device_id in range(9, 12):
        registry.apply_join(_ground_join(device_id, f"ground-{device_id}"))

    with pytest.raises(RegistryError) as error:
        registry.apply_join(_ground_join(12, "ground-overflow"))
    assert error.value.code == "fleet_capacity"


def test_mixed_c2_fleet_state_audit_accepts_nine_nodes_and_rejects_ten():
    registry = FleetRegistry(
        telemetry_freshness_ms=1_000,
        capability_profile=C2_CAPABILITY_PROFILE,
        node_types={device_id: NodeType.GROUND for device_id in range(7, 10)},
    )
    for device_id in range(1, 7):
        registry.apply_join(
            parse_membership_request(
                membership_payload(
                    action="join", event_id=f"aircraft-{device_id}", drone_id=device_id
                )
            )
        )
    for device_id in range(7, 10):
        registry.apply_join(_ground_join(device_id, f"ground-{device_id}"))

    state = registry.state_event(session=SESSION, t=1_000, event_id="mixed-state")
    _material_state_projection(state)

    state["drones"].append(state["drones"][0])
    with pytest.raises(AuditLogError, match="bounded mixed-node list"):
        _material_state_projection(state)


def test_aircraft_only_intent_refuses_ground_target_before_sink(tmp_path):
    dispatched: list[object] = []
    session = RelaySession(
        session_id=SESSION,
        audit_log=SessionAuditLog(tmp_path, SESSION),
        limits=RelayLimits(5_000, 5_000, 1_000, 1_000),
        clock=MutableClock(),
        event_ids=EventIds(),
        node_types={9: NodeType.GROUND},
        intent_sink=CapabilityBoundIntentSink(
            lambda intent, _state: dispatched.append(intent), C1_CAPABILITY_PROFILE
        ),
    )
    raw = {
        **intent_payload(intent_id="ground-takeoff"),
        "name": "takeoff",
        "selection": [9],
        "confirm": True,
    }

    refused = session.process_intent(raw, Principal("console", None, CONSOLE_KEY))

    assert refused[0]["reason"] == "ground_intent_not_supported"
    assert dispatched == []


def test_node_type_settings_require_an_authenticated_adapter_key(tmp_path):
    settings = RelaySettings.from_env(
        {
            "SWEEP_RELAY_TOKEN": CONSOLE_KEY.decode(),
            "SWEEP_ADAPTER_KEYS_JSON": f'{{"9":"{GROUND_KEY.decode()}"}}',
            "SWEEP_NODE_TYPES_JSON": '{"9":"ground"}',
        }
    )
    assert settings.node_types == {9: NodeType.GROUND}
    session = RelayRuntime(settings, clock=MutableClock(), event_ids=EventIds()).session(SESSION)
    assert session.registry.node_type(9) is NodeType.GROUND

    with pytest.raises(SettingsError, match="configured adapter IDs"):
        RelaySettings(
            relay_token=CONSOLE_KEY,
            adapter_keys={1: ADAPTER_KEY},
            node_types={9: NodeType.GROUND},
            log_dir=tmp_path,
        )
