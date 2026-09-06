from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketTestSession

from relay.app import RelayRuntime
from relay.audit import AuditLogError
from relay.auth import Principal, verify_event_signature
from relay.autonomy import AutonomyComposition, AutonomyConfig, create_autonomy_app
from relay.control_frames import ControlLocalizationFrame, sign_localization_frame
from relay.control_localization import (
    ClockMapping,
    ControlLocalizationWire,
    ControlProvenance,
    to_wire_payload,
)
from relay.control_runtime import ControlRuntimeConfig
from relay.session import RelaySession
from relay.settings import AdapterBackend, RelaySettings
from relay.tests.conftest import (
    ADAPTER_KEY,
    CONSOLE_KEY,
    SESSION,
    EventIds,
    MutableClock,
    membership_payload,
    telemetry_payload,
)
from relay.tests.test_control_localization import fresh_snapshot
from tests.autonomy_fixtures import camera_config, planning_config, safety_config

LOCALIZATION_KEY = b"localization-test-key-32-characters"


@dataclass
class ControlRelay:
    client: TestClient
    runtime: RelayRuntime
    session: RelaySession
    composition: AutonomyComposition
    clock: MutableClock


def _control_config(clock: MutableClock) -> ControlRuntimeConfig:
    return ControlRuntimeConfig.from_mapping(
        {
            "limits": {
                "max_clock_error_ms": 5,
                "max_fix_age_ms": 500,
                "max_position_uncertainty_m": 0.4,
                "land_after_fix_age_ms": 2_000,
            },
            "drones": [
                {
                    "drone_id": 1,
                    "connection_epoch": 1,
                    "map_id": "map-sha",
                    "geometry_id": "geometry-sha",
                    "camera_calibration_id": "camera-calibration-sha",
                    "body_extrinsics_id": "body-extrinsics-sha",
                    "capture_clock_id": "camera-clock",
                    "relay_clock_id": "relay-clock",
                    "source_ids": ["tag-camera", "msdk-velocity", "tof-height"],
                    "clock_mapping": {
                        "capture_clock_id": "camera-clock",
                        "relay_clock_id": "relay-clock",
                        "capture_reference_s": 0,
                        "relay_reference_ms": clock.value - 1_000,
                        "milliseconds_per_capture_second": 1_000,
                        "max_error_ms": 5,
                        "measured": True,
                    },
                }
            ],
        },
    )


@pytest.fixture
def control_relay(tmp_path: Path, clock: MutableClock, event_ids: EventIds) -> ControlRelay:
    app, composition = create_autonomy_app(
        RelaySettings(
            relay_token=CONSOLE_KEY,
            adapter_keys={1: ADAPTER_KEY},
            localization_keys={1: LOCALIZATION_KEY},
            log_dir=tmp_path,
            adapter_backend=AdapterBackend.SIM,
        ),
        AutonomyConfig(
            planning=planning_config(),
            safety=safety_config(),
            sim_camera=camera_config(),
            control_localization=_control_config(clock),
        ),
        clock=clock,
        event_ids=event_ids,
    )
    try:
        with TestClient(app) as client:
            runtime = app.state.relay_runtime
            session = runtime.session(SESSION)
            adapter = Principal(source="adapter", drone_id=1, signing_key=ADAPTER_KEY)
            session.process_membership(
                membership_payload(action="join", event_id="join-1"), adapter
            )
            session.process_telemetry(telemetry_payload(event_id="telemetry-1"), adapter)
            session.process_membership(
                membership_payload(action="readiness", event_id="ready-1"), adapter
            )
            yield ControlRelay(client, runtime, session, composition, clock)
    finally:
        composition.close()


def _localization_frame(
    relay: ControlRelay,
    *,
    event_id: str,
    epoch: int = 1,
    map_id: str | None = None,
) -> dict[str, object]:
    mapping = ClockMapping(
        "camera-clock", "relay-clock", 0.0, relay.clock.value - 1_000, 1_000.0, 5, True
    )
    wire = ControlLocalizationWire.from_mapping(
        to_wire_payload(fresh_snapshot(), mapping, "fuser-evidence", event_id)
    )
    wire = replace(
        wire,
        connection_epoch=epoch,
        map_id=wire.map_id if map_id is None else map_id,
    )
    return sign_localization_frame(
        wire,
        timestamp_ms=relay.clock.value,
        event_id=event_id,
        session=SESSION,
        signing_key=LOCALIZATION_KEY,
    )


def _authenticate(socket: WebSocketTestSession, *, source: str) -> None:
    frame: dict[str, object] = {
        "v": 1,
        "type": "auth",
        "source": source,
        "drone_id": 1,
        "token": LOCALIZATION_KEY.decode() if source == "localization" else ADAPTER_KEY.decode(),
    }
    socket.send_json(frame)
    assert socket.receive_json()["type"] == "auth.accepted"
    assert socket.receive_json()["type"] == "state"


def _receive_until(
    socket: WebSocketTestSession,
    predicate: Callable[[dict[str, object]], bool],
    *,
    maximum: int = 200,
) -> dict[str, object]:
    for _ in range(maximum):
        event = socket.receive_json()
        if predicate(event):
            return event
    raise AssertionError("expected matching WebSocket event")


def _send_localization(relay: ControlRelay, payload: dict[str, object]) -> dict[str, object]:
    with relay.client.websocket_connect(f"/ws/{SESSION}") as socket:
        _authenticate(socket, source="localization")
        socket.send_json(payload)
        return _receive_until(
            socket,
            lambda event: event["type"] in {"control_localization", "refusal"},
        )


def test_authenticated_localization_projects_provenance_and_forwards_signed_phone_pose(
    control_relay: ControlRelay,
) -> None:
    with control_relay.client.websocket_connect(f"/ws/{SESSION}") as adapter:
        _authenticate(adapter, source="adapter")
        submitted = _localization_frame(control_relay, event_id="localization-ready")
        accepted = _send_localization(control_relay, submitted)

        assert accepted["type"] == "control_localization"
        retained = control_relay.session.control_localization(1)
        assert retained == ControlLocalizationFrame.parse(submitted)

        snapshot = control_relay.composition.session(SESSION).snapshot(
            control_relay.session.current_state()
        )
        aircraft = snapshot.aircraft[1]
        expected_pose = fresh_snapshot().position_map_enu_m
        assert aircraft.pose.to_dict() == pytest.approx(
            {"x": expected_pose[0], "y": expected_pose[1], "z": expected_pose[2]}
        )
        assert aircraft.position_quality == 1.0
        assert isinstance(aircraft.control_provenance, ControlProvenance)
        provenance = aircraft.control_provenance
        assert provenance.to_dict() == {
            "map_id": "map-sha",
            "geometry_id": "geometry-sha",
            "camera_calibration_id": "camera-calibration-sha",
            "body_extrinsics_id": "body-extrinsics-sha",
            "capture_clock_id": "camera-clock",
            "relay_clock_id": "relay-clock",
            "source_ids": ["tag-camera", "msdk-velocity", "tof-height"],
            "capture_time_s": 0.9,
            "conversion_error_ms": 5,
            "reason": "fresh_verified_measurements",
            "evaluated_at_relay_ms": control_relay.clock.value,
            "position_uncertainty_m": pytest.approx(0.10053400624739406),
        }

        packet = _receive_until(adapter, lambda event: event["type"] == "control_pose")

    unsigned = {key: value for key, value in packet.items() if key != "signature"}
    assert packet["status"] == "ready"
    assert (packet["x_mm"], packet["y_mm"], packet["z_mm"]) == tuple(
        round(value * 1_000) for value in fresh_snapshot().position_map_enu_m
    )
    assert verify_event_signature(unsigned, packet["signature"], ADAPTER_KEY)


@pytest.mark.parametrize(
    ("payload", "expected_type", "expected_reason"),
    [
        (
            lambda relay: _localization_frame(relay, event_id="wrong-epoch", epoch=2),
            "refusal",
            "stale_connection_epoch",
        ),
        (
            lambda relay: _localization_frame(relay, event_id="wrong-pin", map_id="wrong-map"),
            "control_localization",
            None,
        ),
        (
            lambda relay: {
                **_localization_frame(relay, event_id="forged"),
                "position_map_enu_m": [99, 99, 99],
            },
            "refusal",
            "invalid_signature",
        ),
    ],
)
def test_invalid_localization_inputs_never_become_ready_control_evidence(
    control_relay: ControlRelay,
    payload: Callable[[ControlRelay], dict[str, object]],
    expected_type: str,
    expected_reason: str | None,
) -> None:
    received = _send_localization(control_relay, payload(control_relay))

    assert received["type"] == expected_type
    if expected_reason is not None:
        assert received["reason"] == expected_reason
    snapshot = control_relay.composition.session(SESSION).snapshot(
        control_relay.session.current_state()
    )
    aircraft = snapshot.aircraft[1]
    assert aircraft.position_quality == 0.0
    assert aircraft.control_provenance is not None
    if expected_type == "refusal":
        assert control_relay.session.control_localization(1) is None
    else:
        assert control_relay.session.control_localization(1) is not None


def test_stale_localization_loses_readiness_and_emits_a_hold_packet(
    control_relay: ControlRelay,
) -> None:
    accepted = _send_localization(
        control_relay, _localization_frame(control_relay, event_id="stale-localization")
    )
    assert accepted["type"] == "control_localization"
    initial = control_relay.composition.session(SESSION).periodic_events(
        control_relay.session.current_state()
    )
    assert any(packet["type"] == "control_pose" for packet in initial)

    control_relay.clock.advance(501)
    snapshot = control_relay.composition.session(SESSION).snapshot(
        control_relay.session.current_state()
    )
    assert snapshot.aircraft[1].position_quality == 0.0
    assert snapshot.aircraft[1].control_provenance.reason == "localization_missing"

    packets = control_relay.composition.session(SESSION).periodic_events(
        control_relay.session.current_state()
    )
    assert [packet["status"] for packet in packets if packet["type"] == "control_pose"] == ["hold"]


def test_a_failed_localization_audit_cannot_enter_retention_or_control_runtime(
    control_relay: ControlRelay, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _localization_frame(control_relay, event_id="audit-failure")

    def fail(*_args: object, **_kwargs: object) -> None:
        raise AuditLogError("disk failed")

    monkeypatch.setattr(control_relay.session.audit_log, "append_batch", fail)
    producer = Principal(source="localization", drone_id=1, signing_key=LOCALIZATION_KEY)
    with pytest.raises(AuditLogError):
        control_relay.session.process_frame(payload, producer)

    assert control_relay.session._control_localization == {}
    assert payload["event_id"] not in control_relay.session._seen_transport_event_ids
    assert control_relay.composition.session(SESSION).control.store._patches == {}
