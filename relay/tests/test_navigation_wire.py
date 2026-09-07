from __future__ import annotations

import json
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from adapters.dji_mini3.remote import CommandRequest
from planner.models import Plan, Position
from planner.navigation import preview_evidence
from planner.navigation_authorization import NavigationApproval, content_digest
from planner.navigation_runtime import (
    NavigationExecutionConfig,
    NavigationFrame,
    NavigationRuntime,
    navigation_configuration_digest,
)
from planner.test_navigation import MOTION, PERMISSION, artifact
from relay.auth import sign_event, verify_event_signature
from relay.control_localization import ClockMapping, ControlLocalizationPins, ControlPose
from relay.intent_v1 import IntentName
from relay.navigation_wire import NavigationWireConfig, NavigationWirePublisher
from relay.tests.conftest import MutableClock
from tests.autonomy_fixtures import make_intent, make_snapshot, replace_aircraft

APPROVAL_KEY = b"navigation-wire-approval-key-00001"
NODE_KEY = b"navigation-wire-node-signing-key-001"
TEST_KEY_UTF8 = "navigation-wire-node-signing-key-001"
HASHES = {
    "map": "a" * 64,
    "geometry": "b" * 64,
    "calibration": "c" * 64,
    "extrinsics": "d" * 64,
    "transform": "e" * 64,
    "navigation": "f" * 64,
}
IDENTITY = (
    (1.0, 0.0, 0.0, 0.0),
    (0.0, 1.0, 0.0, 0.0),
    (0.0, 0.0, 1.0, 0.0),
    (0.0, 0.0, 0.0, 1.0),
)
FIXTURE = (
    Path(__file__).parents[2]
    / "adapters/dji_mini3/pilot-app/bridge-core/src/test/resources/navigation"
    / "python_route_pose_fixture.json"
)


def _wire_config() -> NavigationWireConfig:
    return NavigationWireConfig(
        clock_lease_id="relay-clock-lease-1",
        clock_lease_expires_at_ms=110_000,
        max_authorization_lifetime_ms=1_000,
        max_clock_error_ms=50,
        navigation_config_id="navigation-config-1",
        navigation_config_sha256=HASHES["navigation"],
        map_version="map-v2",
        map_sha256=HASHES["map"],
        geometry_sha256=HASHES["geometry"],
        camera_calibration_sha256=HASHES["calibration"],
        body_extrinsics_sha256=HASHES["extrinsics"],
        world_transform_sha256=HASHES["transform"],
        control_source_ids=("dji-telemetry", "tag-detector"),
        max_speed_mm_s=500,
        max_acceleration_mm_s2=300,
        max_deceleration_mm_s2=500,
        max_position_uncertainty_mm=30,
        max_cross_track_mm=100,
        arrival_horizontal_tolerance_mm=50,
        arrival_vertical_tolerance_mm=50,
        pose_freshness_ms=500,
        tracking_timeout_ms=5_000,
    )


def _control_pose(
    *, timestamp_ms: int = 99_950, x_mm: int = 500, y_mm: int = 1_500, z_mm: int = 1_000
) -> ControlPose:
    return ControlPose(
        t=timestamp_ms,
        event_id="control-pose-1",
        session="test-session",
        drone_id=1,
        connection_epoch=1,
        map_id="map-v2",
        geometry_id="geometry-v2",
        camera_calibration_id="camera-calibration-v1",
        body_extrinsics_id="body-extrinsics-v1",
        pose_time_ms=timestamp_ms,
        fix_time_ms=timestamp_ms,
        x_mm=x_mm,
        y_mm=y_mm,
        z_mm=z_mm,
        position_frame="map_enu",
        position_uncertainty_mm=20,
        status="ready",
    )


def _publisher() -> tuple[
    NavigationWirePublisher,
    Plan,
    list[object],
    list[object],
    MutableClock,
]:
    wire = _wire_config()
    clock = MutableClock(100_000)
    pose = [_control_pose()]
    config = NavigationExecutionConfig(
        "level_1",
        MOTION,
        0.2,
        0.05,
        500,
        0.5,
        5_000,
        (
            NavigationFrame(
                1,
                "measured-enu-world",
                IDENTITY,
                ControlLocalizationPins(
                    1,
                    "map-v2",
                    "geometry-v2",
                    "camera-calibration-v1",
                    "body-extrinsics-v1",
                    ("tag-detector", "dji-telemetry"),
                    ClockMapping(
                        "phone_snapshot_wall_ms",
                        "relay-wall-ms",
                        0.0,
                        0,
                        1_000,
                        50,
                        True,
                    ),
                ),
                HASHES["calibration"],
                HASHES["extrinsics"],
                HASHES["transform"],
            ),
        ),
        content_digest({"1": asdict(wire)}),
    )
    geometry = replace(artifact(), evidence=preview_evidence("measured"))
    approval_unsigned = {
        "v": 1,
        "type": "navigation_approval",
        "approval_id": "flight-approved-test",
        "session": "test-session",
        "mode": "flight",
        "configuration_sha256": navigation_configuration_digest(
            geometry, config, PERMISSION, "atrium"
        ),
        "issued_at_ms": 99_000,
        "expires_at_ms": 110_000,
        "epochs": [[1, 1]],
        "evidence_sha256": [HASHES["navigation"]],
    }
    approval = NavigationApproval.verify(
        {**approval_unsigned, "signature": sign_event(approval_unsigned, APPROVAL_KEY)},
        APPROVAL_KEY,
    )
    runtime = NavigationRuntime(
        lambda: geometry,
        config,
        PERMISSION,
        approval,
        session="test-session",
        home_zone_id="atrium",
        control_pose=lambda _drone_id: pose[0],
    )
    snapshot = [
        replace_aircraft(
            make_snapshot(1, selection=(1,), now_ms=100_000),
            1,
            pose=Position(0.5, 1.5, 1.0),
        )
    ]
    intent = make_intent(IntentName.COME_HOME, selection=(1,), confirm=True)
    plan = runtime.prepare(intent, snapshot[0])
    assert isinstance(plan, Plan)
    event_ids = iter(("route-event-1", "pose-event-1", "pose-event-2", "pose-event-3"))
    publisher = NavigationWirePublisher(
        runtime,
        {1: wire},
        session="test-session",
        signing_key=lambda _drone_id: NODE_KEY,
        event_ids=lambda: next(event_ids),
        clock=clock,
    )
    return publisher, plan, snapshot, pose, clock


def _request(plan: Plan) -> CommandRequest:
    command = plan.commands[0]
    return CommandRequest(
        command_id=command.command_id,
        intent_id=command.intent_id,
        roster_version=command.roster_version,
        drone_id=command.drone_id,
        connection_epoch=command.connection_epoch,
        operation=command.operation,
        args={
            "x_mm": round(float(command.parameters["x"]) * 1_000),
            "y_mm": round(float(command.parameters["y"]) * 1_000),
            "z_mm": round(float(command.parameters["z"]) * 1_000),
            "speed_mm_s": round(float(command.parameters["speed"]) * 1_000),
            "navigation_route_id": command.parameters["navigation_route_id"],
        },
    )


def _fixture_payload() -> dict[str, object]:
    publisher, plan, snapshot, _, _ = _publisher()
    with publisher.command_scope(plan, lambda: snapshot[0]):
        route, pose = publisher.prepare_request(_request(plan))
    return {
        "key_utf8": TEST_KEY_UTF8,
        "route_authorization": route,
        "navigation_pose": pose,
        "goto": _request(plan).args,
    }


def test_phone_wire_binds_a_flight_approved_frozen_segment_and_fresh_pose() -> None:
    publisher, plan, snapshot, poses, clock = _publisher()
    request = _request(plan)

    with publisher.command_scope(plan, lambda: snapshot[0]):
        frames = publisher.prepare_request(request)

    route, pose = frames
    assert route["type"] == "navigation_route_authorization"
    assert route["segments"] == [
        {
            "start_x_mm": 500,
            "start_y_mm": 1_500,
            "start_z_mm": 1_000,
            "end_x_mm": 6_500,
            "end_y_mm": 1_500,
            "end_z_mm": 1_000,
            "tube_radius_mm": 380,
        }
    ]
    assert pose["type"] == "navigation_pose"
    assert route["seq"] == 1
    assert pose["seq"] == 2
    assert publisher.update(poses[0]) == []
    for frame in frames:
        unsigned = {name: value for name, value in frame.items() if name != "signature"}
        assert verify_event_signature(unsigned, frame["signature"], NODE_KEY)
        assert frame["flight_approved"] is True

    publisher.activate(plan.commands[0].command_id)
    clock.advance(100)
    snapshot[0] = replace_aircraft(
        replace(snapshot[0], now_ms=100_100), 1, pose=Position(2.0, 1.5, 1.0)
    )
    poses[0] = _control_pose(timestamp_ms=100_050, x_mm=2_000)
    updates = publisher.update(poses[0])
    assert len(updates) == 1
    assert updates[0]["seq"] == 3

    hold = replace(poses[0], event_id="control-pose-hold", status="hold")
    poses[0] = hold
    hold_frame = publisher.update(hold)[0]
    assert hold_frame["status"] == "hold"
    assert all(
        hold_frame[name] is None
        for name in (
            "pose_time_ms",
            "fix_time_ms",
            "x_mm",
            "y_mm",
            "z_mm",
            "position_uncertainty_mm",
        )
    )
    publisher.retire(plan.commands[0].command_id)
    assert publisher.update(hold) == []


def test_phone_wire_requires_exact_bound_goto_and_flight_approval() -> None:
    publisher, plan, snapshot, _, _ = _publisher()
    request = _request(plan)
    request = replace(request, args={**request.args, "x_mm": request.args["x_mm"] + 1})
    with (
        publisher.command_scope(plan, lambda: snapshot[0]),
        pytest.raises(ValueError, match="differs"),
    ):
        publisher.prepare_request(request)

    wire = _wire_config()
    with pytest.raises(ValueError, match="SHA-256"):
        replace(wire, geometry_sha256="not-a-digest")


def test_python_generated_fixture_remains_a_signed_phone_contract() -> None:
    expected = _fixture_payload()
    assert json.loads(FIXTURE.read_text()) == expected
    for name in ("route_authorization", "navigation_pose"):
        frame = expected[name]
        assert isinstance(frame, dict)
        unsigned = {key: value for key, value in frame.items() if key != "signature"}
        assert verify_event_signature(unsigned, frame["signature"], NODE_KEY)


def test_per_drone_profiles_bind_each_frame_and_reject_incomplete_mappings() -> None:
    publisher, _, _, _, _ = _publisher()
    runtime = publisher.runtime
    first = _wire_config()
    second = replace(
        first,
        clock_lease_id="relay-clock-lease-2",
        navigation_config_id="navigation-config-2",
        camera_calibration_sha256="1" * 64,
        body_extrinsics_sha256="2" * 64,
        world_transform_sha256="3" * 64,
        control_source_ids=("tag-detector-2",),
    )
    second_frame = NavigationFrame(
        2,
        "measured-enu-world-2",
        IDENTITY,
        ControlLocalizationPins(
            2,
            "map-v2",
            "geometry-v2",
            "camera-calibration-v2",
            "body-extrinsics-v2",
            ("tag-detector-2",),
            ClockMapping("phone-2", "relay-wall-ms", 0.0, 0, 1_000, 50, True),
        ),
        second.camera_calibration_sha256,
        second.body_extrinsics_sha256,
        second.world_transform_sha256,
    )
    runtime.config = replace(
        runtime.config,
        frames=(*runtime.config.frames, second_frame),
        wire_config_sha256=content_digest({"1": asdict(first), "2": asdict(second)}),
    )

    with pytest.raises(ValueError, match="match every"):
        NavigationWirePublisher(
            runtime,
            {1: first},
            session="test-session",
            signing_key=lambda _drone_id: NODE_KEY,
            event_ids=lambda: "unused",
            clock=lambda: 100_000,
        )

    per_drone = NavigationWirePublisher(
        runtime,
        {1: first, 2: second},
        session="test-session",
        signing_key=lambda _drone_id: NODE_KEY,
        event_ids=lambda: "unused",
        clock=lambda: 100_000,
    )
    assert per_drone.wire_configs[2] is second
