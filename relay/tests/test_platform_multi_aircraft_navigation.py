from __future__ import annotations

import copy
import json
import time
from dataclasses import asdict, replace
from hashlib import sha256
from pathlib import Path

from fastapi.testclient import TestClient
from starlette.testclient import WebSocketTestSession

from planner.models import Geofence
from planner.navigation import ArrivalSlot, MotionConfig, Pose
from planner.navigation_authorization import content_digest
from planner.navigation_deployment import NavigationDeployment, load_navigation_deployment
from planner.navigation_runtime import NavigationFrame, navigation_configuration_digest
from planner.test_navigation_deployment import _flight_deployment_files
from planner.test_navigation_runtime import KEY
from relay.auth import Principal, sign_event
from relay.autonomy import AutonomyComposition, AutonomyConfig, create_autonomy_app
from relay.control_localization import (
    ControlLocalizationPins,
    ControlLocalizationProjector,
    ControlPose,
)
from relay.settings import AdapterBackend, RelaySettings
from relay.tests.conftest import (
    ADAPTER_KEY,
    CONSOLE_KEY,
    EventIds,
    MutableClock,
    membership_payload,
    telemetry_payload,
)
from tests.autonomy_fixtures import planning_config, safety_config
from tools.world_replay import export_audit, read_replay

SESSION = "flight-session"
ADAPTER_KEYS = {
    1: ADAPTER_KEY,
    2: b"adapter-two-key-that-is-at-least-32",
}
LOCALIZATION_KEYS = {
    1: b"localization-test-key-32-characters",
    2: b"localization-two-key-that-is-at-least-32",
}


def _deployment(tmp_path: Path) -> NavigationDeployment:
    path, _, _ = _flight_deployment_files(tmp_path)
    document = json.loads(path.read_text())
    original = load_navigation_deployment(path)
    original_artifact = original.artifact()
    world_path = tmp_path / document["world_localization_file"]
    world = json.loads(world_path.read_text())
    publisher = copy.deepcopy(world["publisher"]["drones"][0])
    publisher["key_environment"] = "LOCALIZATION_KEY_2"
    publisher["fuser"]["drone_id"] = 2
    world["publisher"]["drones"].append(publisher)
    device = copy.deepcopy(world["devices"][0])
    device["pins"]["drone_id"] = 2
    world["devices"].append(device)
    world_path.write_text(json.dumps(world))

    first_tuning_path = tmp_path / document["wire_navigation_files"]["1"]
    tuning = json.loads(first_tuning_path.read_text())
    tuning["limits"].update(
        max_position_uncertainty_mm=5,
        max_cross_track_mm=5,
        arrival_horizontal_tolerance_mm=5,
        arrival_vertical_tolerance_mm=5,
        max_deceleration_mm_s2=4_000,
    )
    first_encoded_tuning = json.dumps(tuning, sort_keys=True, separators=(",", ":")).encode()
    first_tuning_path.write_bytes(first_encoded_tuning)
    tuning["device_id"] = 2
    tuning["navigation_config_id"] = "wire-navigation-2"
    second_tuning_path = tmp_path / "device-2-navigation.json"
    encoded_tuning = json.dumps(tuning, sort_keys=True, separators=(",", ":")).encode()
    second_tuning_path.write_bytes(encoded_tuning)

    first_frame = original.config.frames[0]
    assert first_frame.control_pins is not None
    second_frame = NavigationFrame(
        2,
        first_frame.transform_id,
        first_frame.world_from_enu,
        replace(first_frame.control_pins, drone_id=2),
        first_frame.camera_calibration_sha256,
        first_frame.body_extrinsics_sha256,
        first_frame.world_transform_sha256,
    )
    first_profile = replace(
        original.wire_profiles[1],
        navigation_config_sha256=sha256(first_encoded_tuning).hexdigest(),
        max_position_uncertainty_mm=5,
        max_cross_track_mm=5,
        arrival_horizontal_tolerance_mm=5,
        arrival_vertical_tolerance_mm=5,
        max_deceleration_mm_s2=4_000,
    )
    second_profile = replace(
        first_profile,
        navigation_config_id="wire-navigation-2",
        navigation_config_sha256=sha256(encoded_tuning).hexdigest(),
    )
    config = replace(
        original.config,
        motion=MotionConfig(0.005, 0.005, 0.001, 0.005, 0.005, 0.01, 0.2),
        position_tolerance_m=0.005,
        frames=(first_frame, second_frame),
        max_aircraft=2,
        wire_config_sha256=content_digest(
            {"1": asdict(first_profile), "2": asdict(second_profile)}
        ),
    )
    slots = (
        ArrivalSlot("flight-home-1", "lobby", Pose(0, 0, 1, "level_1"), 0.05, 0.05),
        ArrivalSlot("flight-home-2", "lobby", Pose(0.2, 0, 1, "level_1"), 0.05, 0.05),
    )
    artifact = original_artifact.from_geometry_directory(
        tmp_path / document["bundle_directory"],
        tmp_path / document["geometry_directory"],
        document["accepted_map_versions"],
        slots,
        authoring=tmp_path / document["geometry_authoring"],
    )
    artifact = replace(
        artifact,
        zones=tuple(
            replace(zone, owner_approved=zone.zone_id in original.permission.permitted_zone_ids)
            for zone in artifact.zones
        ),
    )
    document["arrival_slots"] = [asdict(slot) for slot in slots]
    document["execution"] = asdict(config)
    document["wire_profiles"] = {"1": asdict(first_profile), "2": asdict(second_profile)}
    document["wire_navigation_files"]["2"] = second_tuning_path.name
    path.write_text(json.dumps(document))
    approval_path = tmp_path / document["approval_file"]
    approval = json.loads(approval_path.read_text())
    unsigned = {name: value for name, value in approval.items() if name != "signature"}
    unsigned["epochs"] = [[1, 1], [2, 1]]
    unsigned["configuration_sha256"] = navigation_configuration_digest(
        artifact, config, original.permission, original.home_zone_id
    )
    approval_path.write_text(json.dumps({**unsigned, "signature": sign_event(unsigned, KEY)}))
    return load_navigation_deployment(path)


def _projector(deployment: NavigationDeployment) -> ControlLocalizationProjector:
    pins = {frame.drone_id: frame.control_pins for frame in deployment.config.frames}
    assert all(isinstance(pin, ControlLocalizationPins) for pin in pins.values())
    first = pins[1]
    assert first is not None
    return ControlLocalizationProjector(
        pins,
        relay_clock_id=first.clock_mapping.relay_clock_id,
        max_clock_error_ms=2,
        max_fix_age_ms=500,
        max_velocity_age_ms=200,
        max_height_age_ms=200,
        max_position_uncertainty_p95_m=0.3,
    )


def _preview(deployment: NavigationDeployment) -> dict[str, object]:
    artifact = deployment.artifact()
    return {
        "previewId": "platform-fleet-preview-1",
        "intentId": "platform-fleet-intent-1",
        "expiresAt": 115_000,
        "destination": {"zoneId": "lobby"},
        "selected": [
            {"id": drone_id, "deviceClass": "aircraft", "epoch": 1} for drone_id in (1, 2)
        ],
        "map": {
            "mapPin": {
                "version": artifact.map_pin.version,
                "contentSha256": artifact.map_pin.content_sha256,
            }
        },
    }


def _prepare_session(
    composition: AutonomyComposition, deployment: NavigationDeployment, clock: MutableClock
):
    session = composition.runtime.session(SESSION)
    for drone_id, x in ((1, -20.0), (2, -19.8)):
        adapter = Principal("adapter", drone_id, ADAPTER_KEYS[drone_id])
        session.process_membership(
            membership_payload(
                action="join",
                event_id=f"fleet-join-{drone_id}",
                timestamp=clock.value,
                drone_id=drone_id,
                session=SESSION,
                key=ADAPTER_KEYS[drone_id],
                capabilities=["flight", "pano_360", "navigate"],
            ),
            adapter,
        )
        telemetry = telemetry_payload(
            event_id=f"fleet-telemetry-{drone_id}",
            timestamp=clock.value,
            drone_id=drone_id,
            session=SESSION,
        )
        telemetry.update(x=x, y=9.8, z=-29.0)
        session.process_telemetry(telemetry, adapter)
        session.process_membership(
            membership_payload(
                action="readiness",
                event_id=f"fleet-ready-{drone_id}",
                timestamp=clock.value,
                drone_id=drone_id,
                session=SESSION,
                key=ADAPTER_KEYS[drone_id],
            ),
            adapter,
        )
        pins = deployment.config.frame(drone_id).control_pins
        assert pins is not None
        session._control_pose[drone_id] = ControlPose(
            t=clock.value,
            event_id=f"fleet-control-pose-{drone_id}",
            session=SESSION,
            drone_id=drone_id,
            connection_epoch=1,
            map_id=pins.map_id,
            geometry_id=pins.geometry_id,
            camera_calibration_id=pins.camera_calibration_id,
            body_extrinsics_id=pins.body_extrinsics_id,
            pose_time_ms=clock.value - 2,
            fix_time_ms=clock.value - 2,
            x_mm=round(x * 1_000),
            y_mm=9_800,
            z_mm=-29_000,
            position_frame="map_enu",
            position_uncertainty_mm=1,
            status="ready",
        )
    session.update_control_projection(selection=(1, 2), armed=True, spacing=0.1)
    autonomy = composition.session(SESSION)
    autonomy._operator_last_seen_ms = clock.value
    return session, autonomy


def _next_command(
    adapter: WebSocketTestSession,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    frames = []
    for _ in range(12):
        frame = adapter.receive_json()
        frames.append(frame)
        if frame.get("type") == "command":
            return frame, frames
    raise AssertionError(f"navigation command was not delivered: {frames}")


def _complete_command(session, clock: MutableClock, command: dict[str, object]) -> None:
    drone_id = command["drone_id"]
    assert isinstance(drone_id, int)
    args = command["args"]
    assert isinstance(args, dict)
    pose = session.control_pose(drone_id)
    assert pose is not None
    clock.advance(3)
    x_mm = args.get("x_mm", pose.x_mm)
    y_mm = args.get("y_mm", pose.y_mm)
    z_mm = args.get("z_mm", pose.z_mm)
    assert all(isinstance(value, int) for value in (x_mm, y_mm, z_mm))
    telemetry = telemetry_payload(
        event_id=f"fleet-arrived-{command['command_id']}",
        timestamp=clock.value,
        drone_id=drone_id,
        session=SESSION,
    )
    telemetry.update(x=x_mm / 1_000, y=y_mm / 1_000, z=z_mm / 1_000)
    session.process_telemetry(telemetry, Principal("adapter", drone_id, ADAPTER_KEYS[drone_id]))
    session._control_pose[drone_id] = replace(
        pose,
        t=clock.value,
        event_id=f"fleet-arrived-pose-{command['command_id']}",
        pose_time_ms=clock.value - 2,
        fix_time_ms=clock.value - 2,
        x_mm=x_mm,
        y_mm=y_mm,
        z_mm=z_mm,
    )


def _acknowledgement(
    command: dict[str, object], status: str, clock: MutableClock
) -> dict[str, object]:
    return {
        "v": 1,
        "t": clock.value,
        "type": "acknowledgement",
        "event_id": f"fleet-{status}-{command['command_id']}",
        "session": SESSION,
        "intent_id": command["intent_id"],
        "command_id": command["command_id"],
        "status": status,
        "drone_id": command["drone_id"],
        "connection_epoch": 1,
        "roster_version": command["roster_version"],
        "reason": None if status == "completed" else "adapter_failed",
        "detail": None,
    }


def _issued_drones(session) -> list[int]:
    return [
        event["drone_id"]
        for record in session.replay()["events"]
        if (event := record["event"]).get("type") == "command"
        and event["intent_id"] == "platform:platform-fleet-preview-1"
    ]


def _composition(tmp_path: Path):
    deployment = _deployment(tmp_path)
    clock = MutableClock(100_000)
    app, composition = create_autonomy_app(
        RelaySettings(
            relay_token=CONSOLE_KEY,
            adapter_keys=ADAPTER_KEYS,
            localization_keys=LOCALIZATION_KEYS,
            log_dir=tmp_path / "logs",
            adapter_backend=AdapterBackend.REMOTE,
        ),
        AutonomyConfig(
            planning=replace(planning_config(), flight_speed_m_s=0.2),
            safety=replace(
                safety_config(),
                geofence=Geofence(-100, 100, -100, 100, -100, 100),
                ceiling_m=50,
                min_spacing_m=0.1,
            ),
            control_localization_projector=_projector(deployment),
            navigation=deployment,
        ),
        clock=clock,
        event_ids=EventIds(),
    )
    return deployment, clock, app, composition


def test_platform_fleet_routes_are_qualified_and_sent_one_aircraft_at_a_time(tmp_path: Path):
    deployment, clock, app, composition = _composition(tmp_path)
    try:
        with TestClient(app) as client:
            session, autonomy = _prepare_session(composition, deployment, clock)
            preview = _preview(deployment)
            qualified = autonomy.preview_platform_navigation(preview)
            assert [route["target"]["id"] for route in qualified["routes"]] == [1, 2]
            assert [outcome["code"] for outcome in qualified["outcomes"]] == [
                "route_qualified",
                "route_qualified",
            ]
            with (
                client.websocket_connect(f"/ws/{SESSION}") as first,
                client.websocket_connect(f"/ws/{SESSION}") as second,
            ):
                for socket, drone_id in ((first, 1), (second, 2)):
                    socket.send_json(
                        {
                            "v": 1,
                            "type": "auth",
                            "source": "adapter",
                            "drone_id": drone_id,
                            "token": ADAPTER_KEYS[drone_id].decode(),
                        }
                    )
                    assert socket.receive_json()["type"] == "auth.accepted"
                    assert socket.receive_json()["type"] == "state"
                assert (
                    autonomy.confirm_platform_navigation(
                        {**preview, "execution": qualified["execution"]}
                    )["status"]
                    == "accepted"
                )
                command, frames = _next_command(first)
                assert {frame["type"] for frame in frames} >= {
                    "navigation_route_authorization",
                    "navigation_pose",
                    "command",
                }
                assert command["drone_id"] == 1
                assert _issued_drones(session) == [1]
                first_route_commands = len(qualified["routes"][0]["waypoints"])
                for index in range(first_route_commands):
                    assert command["drone_id"] == 1
                    _complete_command(session, clock, command)
                    first.send_json(_acknowledgement(command, "completed", clock))
                    if index + 1 < first_route_commands:
                        command, _ = _next_command(first)
                command, _ = _next_command(second)
                assert command["drone_id"] == 2
                assert _issued_drones(session) == [1, 1, 2]
            output = tmp_path / "platform-flight.mcap"
            assert (
                export_audit(session.audit_log.path, SESSION, output)
                == session.audit_log.last_sequence
            )
            records = list(read_replay(output, SESSION))
            assert records == session.audit_log.replay()
            routes = [
                record["event"]
                for record in records
                if record["event"]["type"] == "navigation_route_authorization"
            ]
            assert {route["device_id"] for route in routes} == {1, 2}
            for route in routes:
                profile = deployment.wire_profiles[route["device_id"]]
                assert route["map_sha256"] == profile.map_sha256
                assert route["geometry_sha256"] == profile.geometry_sha256
                assert route["world_transform_sha256"] == profile.world_transform_sha256
            poses = [
                record["event"]
                for record in records
                if record["event"]["type"] == "navigation_pose"
            ]
            assert {pose["device_id"] for pose in poses} == {1, 2}
            for pose in poses:
                profile = deployment.wire_profiles[pose["device_id"]]
                assert pose["position_frame"] == "map_enu"
                assert pose["map_sha256"] == profile.map_sha256
                assert pose["geometry_sha256"] == profile.geometry_sha256
                assert pose["world_transform_sha256"] == profile.world_transform_sha256
            acknowledgements = [
                record["event"]
                for record in records
                if record["event"]["type"] == "acknowledgement"
            ]
            assert {acknowledgement["command_id"] for acknowledgement in acknowledgements} >= {
                route["command_id"] for route in routes if route["device_id"] == 1
            }
    finally:
        composition.close()


def test_platform_fleet_first_wire_failure_prevents_the_second_aircraft_send(tmp_path: Path):
    deployment, clock, app, composition = _composition(tmp_path)
    try:
        with TestClient(app) as client:
            session, autonomy = _prepare_session(composition, deployment, clock)
            preview = _preview(deployment)
            qualified = autonomy.preview_platform_navigation(preview)
            with (
                client.websocket_connect(f"/ws/{SESSION}") as first,
                client.websocket_connect(f"/ws/{SESSION}") as second,
            ):
                for socket, drone_id in ((first, 1), (second, 2)):
                    socket.send_json(
                        {
                            "v": 1,
                            "type": "auth",
                            "source": "adapter",
                            "drone_id": drone_id,
                            "token": ADAPTER_KEYS[drone_id].decode(),
                        }
                    )
                    assert socket.receive_json()["type"] == "auth.accepted"
                    assert socket.receive_json()["type"] == "state"
                autonomy.confirm_platform_navigation(
                    {**preview, "execution": qualified["execution"]}
                )
                command, _ = _next_command(first)
                assert command["operation"] == "goto"
                first.send_json(_acknowledgement(command, "failed", clock))
                hold, _ = _next_command(first)
                assert hold["operation"] == "hover"
                assert hold["drone_id"] == 1
                first.send_json(_acknowledgement(hold, "completed", clock))
                deadline = time.monotonic() + 2
                refused = False
                while time.monotonic() < deadline:
                    refused = any(
                        event.get("status") == "refused"
                        and event.get("intent_id") == command["intent_id"]
                        for record in session.replay()["events"]
                        if (event := record["event"]).get("type") == "refusal"
                    )
                    if refused:
                        break
                    time.sleep(0.01)
                assert refused
                assert _issued_drones(session) == [1, 1]
    finally:
        composition.close()
