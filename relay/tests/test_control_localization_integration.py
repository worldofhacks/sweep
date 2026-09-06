from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketTestSession

from relay.app import RelayRuntime
from relay.audit import AuditLogError
from relay.auth import Principal, verify_event_signature
from relay.autonomy import AutonomyComposition, AutonomyConfig, create_autonomy_app
from relay.control_config import ControlRuntimeConfig
from relay.control_frames import sign_localization_frame
from relay.control_localization import ControlLocalizationPins
from relay.navigation_control import NavigationControl, NavigationControlConfig
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
from relay.tests.test_control_localization import mapping, projector, snapshot, wire
from tests.autonomy_fixtures import camera_config, planning_config, safety_config

LOCALIZATION_KEY = b"localization-test-key-32-characters"


@dataclass
class ControlRelay:
    client: TestClient
    runtime: RelayRuntime
    session: RelaySession
    composition: AutonomyComposition
    clock: MutableClock
    control_config: ControlRuntimeConfig


def _control_config() -> ControlRuntimeConfig:
    pin = ControlLocalizationPins(
        drone_id=1,
        map_id="map-id",
        geometry_id="geometry-id",
        camera_calibration_id="camera-calibration-id",
        body_extrinsics_id="body-extrinsics-id",
        source_ids=("tag-camera", "msdk-velocity", "tof-height"),
        clock_mapping=mapping(),
    )
    return ControlRuntimeConfig(
        pins={1: pin},
        max_clock_error_ms=5,
        max_fix_age_ms=500,
        max_velocity_age_ms=200,
        max_height_age_ms=200,
        max_position_uncertainty_p95_m=0.3,
    )


@pytest.fixture
def control_relay(tmp_path: Path, clock: MutableClock, event_ids: EventIds) -> ControlRelay:
    control_config = _control_config()
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
            control_localization=control_config,
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
            yield ControlRelay(client, runtime, session, composition, clock, control_config)
    finally:
        composition.close()


def _localization_frame(
    relay: ControlRelay, *, event_id: str, **wire_changes: object
) -> dict[str, object]:
    return sign_localization_frame(
        wire(**wire_changes),
        timestamp_ms=relay.clock(),
        event_id=event_id,
        session=SESSION,
        signing_key=LOCALIZATION_KEY,
    )


def _authenticate(socket: WebSocketTestSession, *, source: str) -> None:
    token = LOCALIZATION_KEY if source == "localization" else ADAPTER_KEY
    socket.send_json(
        {
            "v": 1,
            "type": "auth",
            "source": source,
            "drone_id": 1,
            "token": token.decode(),
        }
    )
    assert socket.receive_json()["type"] == "auth.accepted"
    assert socket.receive_json()["type"] == "state"


def _receive_until(
    socket: WebSocketTestSession,
    event_type: str,
    *,
    maximum: int = 200,
) -> dict[str, object]:
    for _ in range(maximum):
        event = socket.receive_json()
        if event["type"] == event_type:
            return event
    raise AssertionError(f"expected {event_type} WebSocket event")


def _publish_localization(
    relay: ControlRelay, *, event_id: str = "producer-pose-1", **wire_changes: object
) -> None:
    with relay.client.websocket_connect(f"/ws/{SESSION}") as producer:
        _authenticate(producer, source="localization")
        producer.send_json(_localization_frame(relay, event_id=event_id, **wire_changes))


def test_signed_producer_emits_only_a_diagnostic_control_pose_to_the_node(
    control_relay: ControlRelay,
) -> None:
    with control_relay.client.websocket_connect(f"/ws/{SESSION}") as adapter:
        _authenticate(adapter, source="adapter")
        _publish_localization(control_relay)
        packet = _receive_until(adapter, "control_pose")
        retained = control_relay.session.control_pose(1)
        assert retained is not None
        assert control_relay.session.control_localization_projector.pins == projector().pins
        assert retained.flight_approved is False
        assert packet["flight_approved"] is False
        assert packet["status"] == "ready"
        assert (packet["x_mm"], packet["y_mm"], packet["z_mm"]) == tuple(
            round(value * 1_000) for value in snapshot().position_map_enu_m
        )
        assert "position_map_enu_m" not in packet
        assert "covariance_map_enu_m2" not in packet
        unsigned = {key: value for key, value in packet.items() if key != "signature"}
        assert verify_event_signature(unsigned, packet["signature"], ADAPTER_KEY)
        assert not hasattr(control_relay.session, "_control_localization")
        assert control_relay.session.control_localization(1) == retained


def test_diagnostic_pose_cannot_project_into_navigation_without_explicit_approval(
    control_relay: ControlRelay,
) -> None:
    _publish_localization(control_relay)

    state = control_relay.session.current_state()
    projected = control_relay.composition.session(SESSION).snapshot(state)

    assert control_relay.composition.config.enable_localized_navigation is False
    assert projected.aircraft[1].pose.to_dict() == {"x": 1.0, "y": 2.0, "z": 0.5}
    assert projected.aircraft[1].control_provenance is None
    assert control_relay.session.apply_control_localization(projected) == projected


def test_localization_audit_failure_keeps_diagnostic_pose_unretained(
    control_relay: ControlRelay, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _localization_frame(control_relay, event_id="audit-failure")

    def fail(*_args: object, **_kwargs: object) -> None:
        raise AuditLogError("disk failed")

    monkeypatch.setattr(control_relay.session.audit_log, "append_batch", fail)
    producer = Principal(source="localization", drone_id=1, signing_key=LOCALIZATION_KEY)
    with pytest.raises(AuditLogError):
        control_relay.session.process_frame(payload, producer)

    assert control_relay.session.control_pose(1) is None
    assert payload["event_id"] not in control_relay.session._seen_transport_event_ids


def _prepared_navigation(relay: ControlRelay):
    from planner.models import CommandOperation
    from planner.test_navigation_runtime import stack

    controller, _, _, base_snapshot, _, _, intent = stack()
    navigation = controller.planner.navigation
    navigation.require_phone_authorization = True
    now = relay.clock()
    current = replace(
        base_snapshot,
        now_ms=now,
        operator_last_seen_ms=now,
        aircraft={
            drone_id: replace(
                aircraft,
                link_last_seen_ms=now,
                position_last_seen_ms=now,
            )
            for drone_id, aircraft in base_snapshot.aircraft.items()
        },
    )
    prepared = controller.prepare(intent, current, current_snapshot=lambda: current)
    assert prepared.plan is not None, prepared
    command = next(
        item for item in prepared.plan.commands if item.operation is CommandOperation.GOTO
    )
    return navigation, prepared, command, current


_NAVIGATION_COVARIANCE = (
    (0.00001, 0.0, 0.0),
    (0.0, 0.00001, 0.0),
    (0.0, 0.0, 0.00001),
)


def test_authorized_route_receives_initial_and_updated_poses_until_localization_expires(
    control_relay: ControlRelay,
) -> None:
    from adapters.dji_mini3.remote import CommandRequest
    from relay.bridge import RelayNodeLink

    with control_relay.client.websocket_connect(f"/ws/{SESSION}") as adapter:
        _authenticate(adapter, source="adapter")
        _publish_localization(
            control_relay,
            covariance_map_enu_m2=_NAVIGATION_COVARIANCE,
        )
        _receive_until(adapter, "control_pose")
        navigation, prepared, command, current = _prepared_navigation(control_relay)
        control = NavigationControl(
            NavigationControlConfig(
                navigation,
                control_relay.control_config,
                "approved-navigation-config",
                {1: ADAPTER_KEY},
            )
        )
        approved = control.approved_snapshot(current, control_relay.session)
        control_relay.composition.session(SESSION).navigation_control = control
        link = RelayNodeLink(
            control_relay.runtime,
            SESSION,
            delivery_timeout_ms=100,
            navigation_control=control,
        )
        request = {
            "x_mm": round(float(command.parameters["x"]) * 1_000),
            "y_mm": round(float(command.parameters["y"]) * 1_000),
            "z_mm": round(float(command.parameters["z"]) * 1_000),
            "speed_mm_s": round(float(command.parameters["speed"]) * 1_000),
            "navigation_route_id": prepared.plan.intent_id,
        }

        asyncio.run(asyncio.to_thread(link.authorize_navigation, prepared.plan, command, approved))
        asyncio.run(
            asyncio.to_thread(
                link.send,
                CommandRequest(
                    command.command_id,
                    command.intent_id,
                    command.roster_version,
                    command.drone_id,
                    command.connection_epoch,
                    command.operation,
                    request,
                ),
            )
        )

        authorization = _receive_until(adapter, "navigation_route_authorization")
        initial_pose = _receive_until(adapter, "navigation_pose")
        goto = _receive_until(adapter, "command")
        control_relay.clock.advance(100)
        _publish_localization(
            control_relay,
            event_id="producer-pose-2",
            evaluated_at_s=1.1,
            position_map_enu_m=(2.01, 3.0, 1.0),
            covariance_map_enu_m2=((0.0001, 0.0, 0.0), (0.0, 0.0001, 0.0), (0.0, 0.0, 0.0001)),
        )
        for _ in range(20):
            updated_pose = _receive_until(adapter, "navigation_pose")
            if updated_pose["pose_time_ms"] > initial_pose["pose_time_ms"]:
                break
        else:
            raise AssertionError("the active route did not receive the new measured pose")
        unsigned = {key: value for key, value in updated_pose.items() if key != "signature"}
        assert verify_event_signature(unsigned, updated_pose["signature"], ADAPTER_KEY)
        assert updated_pose["seq"] > initial_pose["seq"]
        assert updated_pose["command_id"] == command.command_id
        assert updated_pose["x_mm"] == 2010
        assert control_relay.session.control_pose(1).flight_approved is False
        control_relay.clock.advance(1_000)
        events = control_relay.runtime.periodic_events(control_relay.session)
        assert not any(
            event.get("type") == "navigation_pose" and event.get("status") == "ready"
            for event in events
        )

    assert authorization["flight_approved"] is True
    assert initial_pose["flight_approved"] is True
    assert initial_pose["command_id"] == command.command_id
    assert goto["operation"] == "goto"
    assert goto["args"] == request


def test_navigation_authorization_delivery_failure_blocks_the_goto(
    control_relay: ControlRelay, monkeypatch: pytest.MonkeyPatch
) -> None:
    from adapters.protocols import AdapterError
    from relay.bridge import RelayNodeLink

    _publish_localization(
        control_relay,
        covariance_map_enu_m2=_NAVIGATION_COVARIANCE,
    )
    navigation, prepared, command, current = _prepared_navigation(control_relay)
    control = NavigationControl(
        NavigationControlConfig(
            navigation,
            control_relay.control_config,
            "approved-navigation-config",
            {1: ADAPTER_KEY},
        )
    )
    approved = control.approved_snapshot(current, control_relay.session)

    async def undeliver(*_args: object, **_kwargs: object) -> bool:
        return False

    monkeypatch.setattr(control_relay.runtime, "deliver_to_node", undeliver)
    link = RelayNodeLink(
        control_relay.runtime,
        SESSION,
        delivery_timeout_ms=100,
        navigation_control=control,
    )

    with pytest.raises(AdapterError, match="authorization could not be delivered"):
        asyncio.run(asyncio.to_thread(link.authorize_navigation, prepared.plan, command, approved))
    assert control_relay.session.metrics()["commands_issued"] == 0


@pytest.mark.parametrize("event_type", ["navigation_pose", "navigation_route_authorization"])
def test_navigation_packets_reach_only_the_authorized_aircraft(
    tmp_path: Path, clock: MutableClock, event_ids: EventIds, event_type: str
) -> None:
    runtime = RelayRuntime(
        RelaySettings(relay_token=CONSOLE_KEY, adapter_keys={1: ADAPTER_KEY}, log_dir=tmp_path),
        clock=clock,
        event_ids=event_ids,
    )
    runtime.session(SESSION)

    async def exercise() -> None:
        recipients = [
            await runtime.subscribe(SESSION, principal)
            for principal in (
                Principal("adapter", 1, ADAPTER_KEY),
                Principal("adapter", 2, b"other-aircraft-credential-32bytes"),
                Principal("console", signing_key=CONSOLE_KEY),
                Principal("localization", 1, LOCALIZATION_KEY),
            )
        ]
        packet = {"type": event_type, "drone_id": 1, "event_id": "route-packet"}
        await runtime.publish(SESSION, [packet])
        assert recipients[0].queue.get_nowait().event == packet
        assert all(recipient.queue.empty() for recipient in recipients[1:])

    asyncio.run(exercise())
