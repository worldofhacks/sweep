from __future__ import annotations

import asyncio
from dataclasses import asdict, replace
from itertools import count

from fastapi.testclient import TestClient

from adapters.dji_mini3.remote import CommandRequest
from planner.models import Command, CommandOperation, Plan, Position
from planner.navigation import preview_evidence
from planner.navigation_authorization import NavigationApproval, content_digest
from planner.navigation_runtime import (
    NavigationExecutionConfig,
    NavigationFrame,
    NavigationRuntime,
    navigation_configuration_digest,
)
from planner.planner import DeterministicPlanner
from planner.test_navigation import MOTION, PERMISSION, arrival, artifact
from relay.app import RelayRuntime, create_app
from relay.auth import Principal, sign_event, verify_event_signature
from relay.autonomy import relay_snapshot
from relay.control_localization import ClockMapping, ControlLocalizationPins, ControlPose
from relay.intent_v1 import IntentName
from relay.navigation_wire import NavigationWirePublisher
from relay.settings import AdapterBackend, RelaySettings
from relay.tests.conftest import MutableClock
from relay.tests.test_navigation_wire import (
    APPROVAL_KEY,
    HASHES,
    IDENTITY,
    NODE_KEY,
    _publisher,
    _request,
    _wire_config,
)
from tests.autonomy_fixtures import make_intent, planning_config, replace_aircraft


def test_remote_app_uses_the_explicit_physical_aircraft_limit(tmp_path) -> None:
    app = create_app(
        RelaySettings(
            relay_token=b"console-key-for-physical-limit-01",
            log_dir=tmp_path,
            adapter_backend=AdapterBackend.REMOTE,
            physical_aircraft_limit=1,
        )
    )

    with TestClient(app):
        session = app.state.relay_runtime.session("physical-limit")

    assert session.registry.aircraft_limit == 1


def test_retained_tracking_updates_reach_only_the_commanded_phone(tmp_path):
    publisher, plan, snapshots, poses, clock = _publisher()
    request = _request(plan)
    with publisher.command_scope(plan, lambda: snapshots[0]):
        publisher.prepare_request(request)
    publisher.activate(request.command_id)

    def navigation_events(
        _session_id: str, events: list[dict[str, object]]
    ) -> list[dict[str, object]]:
        output = []
        for event in events:
            if event.get("type") == "control_pose" and event.get("drone_id") == 1:
                output.extend(publisher.update(poses[0]))
            elif event.get("status") in {"completed", "failed", "refused", "invalidated"}:
                command_id = event.get("command_id")
                intent_id = event.get("intent_id")
                if isinstance(command_id, str):
                    publisher.retire(command_id)
                elif isinstance(intent_id, str):
                    publisher.retire_intent(intent_id)
        return output

    runtime = RelayRuntime(
        RelaySettings(
            relay_token=b"console-key-for-navigation-test-01",
            adapter_keys={1: NODE_KEY, 2: b"other-node-key-for-navigation-001"},
            log_dir=tmp_path,
        ),
        clock=clock,
        navigation_events=navigation_events,
    )
    runtime.session("test-session")
    poses[0] = replace(poses[0], t=100_000, x_mm=600)
    snapshots[0] = replace_aircraft(snapshots[0], 1, pose=Position(0.6, 1.5, 1.0))

    async def exercise():
        phone = await runtime.subscribe("test-session", Principal("adapter", 1, NODE_KEY))
        other = await runtime.subscribe(
            "test-session", Principal("adapter", 2, b"other-node-key-for-navigation-001")
        )
        console = await runtime.subscribe(
            "test-session", Principal("console", None, b"console-key-for-navigation-test-01")
        )
        event = {"type": "control_pose", "drone_id": 1}
        await runtime.publish("test-session", [event])
        assert phone.queue.get_nowait().event == event
        update = phone.queue.get_nowait().event
        assert update["type"] == "navigation_pose"
        assert update["command_id"] == request.command_id
        assert update["x_mm"] == 600
        assert verify_event_signature(
            {key: value for key, value in update.items() if key != "signature"},
            update["signature"],
            NODE_KEY,
        )
        assert other.queue.empty()
        assert console.queue.empty()
        await runtime.publish(
            "test-session",
            [
                {
                    "type": "lifecycle",
                    "status": "invalidated",
                    "intent_id": plan.intent_id,
                }
            ],
        )
        assert publisher.update(poses[0]) == []

    asyncio.run(exercise())


def test_three_aircraft_navigation_publishes_only_to_their_phones_and_excludes_ground_nodes(
    tmp_path,
) -> None:
    clock = MutableClock(100_000)
    profiles = {
        drone_id: replace(_wire_config(), navigation_config_id=f"navigation-config-{drone_id}")
        for drone_id in (1, 2, 3)
    }
    frames = tuple(
        NavigationFrame(
            drone_id,
            f"measured-enu-world-{drone_id}",
            IDENTITY,
            ControlLocalizationPins(
                drone_id,
                "map-v2",
                "geometry-v2",
                "camera-calibration-v1",
                "body-extrinsics-v1",
                ("tag-detector", "dji-telemetry"),
                ClockMapping("phone_snapshot_wall_ms", "relay-wall-ms", 0.0, 0, 1_000, 50, True),
            ),
            HASHES["calibration"],
            HASHES["extrinsics"],
            HASHES["transform"],
        )
        for drone_id in (1, 2, 3)
    )
    config = NavigationExecutionConfig(
        "level_1",
        MOTION,
        0.2,
        0.05,
        500,
        0.5,
        5_000,
        frames,
        content_digest({str(drone_id): asdict(profiles[drone_id]) for drone_id in profiles}),
        line_zone_id="atrium",
        max_aircraft=3,
    )
    geometry = replace(
        artifact(
            slots=tuple(arrival(f"line-{index}", 6.5, 0.5 + 0.8 * index) for index in range(3))
        ),
        evidence=preview_evidence("measured"),
    )
    approval_body = {
        "v": 1,
        "type": "navigation_approval",
        "approval_id": "three-phone-flight-approval",
        "session": "test-session",
        "mode": "flight",
        "configuration_sha256": navigation_configuration_digest(
            geometry, config, PERMISSION, "atrium"
        ),
        "issued_at_ms": 99_000,
        "expires_at_ms": 110_000,
        "epochs": [[drone_id, 1] for drone_id in (1, 2, 3)],
        "evidence_sha256": [HASHES["navigation"]],
    }
    approval = NavigationApproval.verify(
        {**approval_body, "signature": sign_event(approval_body, APPROVAL_KEY)}, APPROVAL_KEY
    )
    poses = {
        drone_id: ControlPose(
            t=99_950,
            event_id=f"control-pose-{drone_id}",
            session="test-session",
            drone_id=drone_id,
            connection_epoch=1,
            map_id="map-v2",
            geometry_id="geometry-v2",
            camera_calibration_id="camera-calibration-v1",
            body_extrinsics_id="body-extrinsics-v1",
            pose_time_ms=99_950,
            fix_time_ms=99_950,
            x_mm=500 + (drone_id - 1) * 1_000,
            y_mm=500,
            z_mm=1_000,
            position_frame="map_enu",
            position_uncertainty_mm=20,
            status="ready",
        )
        for drone_id in (1, 2, 3)
    }
    navigation_runtime = NavigationRuntime(
        lambda: geometry,
        config,
        PERMISSION,
        approval,
        session="test-session",
        home_zone_id="atrium",
        control_pose=lambda drone_id: poses[drone_id],
    )
    relay_state = {
        "t": 100_000,
        "roster_version": 7,
        "selection": [1, 2, 3],
        "armed": True,
        "estop": False,
        "formation": "none",
        "spacing": 0.8,
        "drones": [
            {
                "drone_id": drone_id,
                "node_type": "aircraft",
                "connection_epoch": 1,
                "membership": "ready",
                "readiness_reasons": [],
                "control_authority": True,
                "rc_safety_operator_present": True,
                "telemetry": {
                    "t": 100_000,
                    "state": "hovering",
                    "x": 0.5 + (drone_id - 1),
                    "y": 0.5,
                    "z": 1.0,
                    "battery": 0.9,
                    "link": 0.9,
                    "pos_quality": 0.9,
                },
            }
            for drone_id in (1, 2, 3)
        ]
        + [
            {"drone_id": 4, "node_type": "ground"},
            {"drone_id": 5, "node_type": "ground"},
        ],
    }
    snapshot = relay_snapshot(relay_state, operator_last_seen_ms=100_000)
    assert tuple(snapshot.aircraft) == (1, 2, 3)

    intent = make_intent(
        IntentName.FORMATION_SET,
        selection=(1, 2, 3),
        args={"name": "line"},
        confirm=True,
    )
    plan = DeterministicPlanner(planning_config(), navigation_runtime=navigation_runtime).plan(
        intent, snapshot
    )
    assert isinstance(plan, Plan)
    assert {command.drone_id for command in plan.commands} == {1, 2, 3}

    event_ids = count(1)
    keys = {
        drone_id: f"navigation-three-phone-node-key-{drone_id}-00001".encode()
        for drone_id in (1, 2, 3)
    }
    publisher = NavigationWirePublisher(
        navigation_runtime,
        profiles,
        session="test-session",
        signing_key=keys.get,
        event_ids=lambda: f"navigation-event-{next(event_ids)}",
        clock=clock,
    )

    def navigation_events(
        _session_id: str, events: list[dict[str, object]]
    ) -> list[dict[str, object]]:
        output = []
        for event in events:
            if event.get("type") == "control_pose":
                drone_id = event.get("drone_id")
                if isinstance(drone_id, int) and drone_id in poses:
                    output.extend(publisher.update(poses[drone_id]))
        return output

    relay = RelayRuntime(
        RelaySettings(
            relay_token=b"console-key-for-three-phone-test-01",
            adapter_keys=keys,
            log_dir=tmp_path,
        ),
        clock=clock,
        navigation_events=navigation_events,
    )
    relay.session("test-session")
    first_gotos = {
        drone_id: next(
            command
            for command in plan.commands
            if command.drone_id == drone_id and command.operation is CommandOperation.GOTO
        )
        for drone_id in (1, 2, 3)
    }
    routes_by_drone = {route.drone.drone_id: route for route in plan.navigation.route.routes}

    def request(command: Command) -> CommandRequest:
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

    async def exercise() -> None:
        nonlocal snapshot
        phones = {
            drone_id: await relay.subscribe(
                "test-session", Principal("adapter", drone_id, keys[drone_id])
            )
            for drone_id in (1, 2, 3)
        }
        for drone_id, command in first_gotos.items():
            with publisher.command_scope(plan, lambda current=snapshot: current):
                frames = publisher.prepare_request(request(command))
            await relay.publish("test-session", frames)
            publisher.activate(command.command_id)
            delivered = [phones[drone_id].queue.get_nowait().event for _ in frames]
            assert [frame["type"] for frame in delivered] == [
                "navigation_route_authorization",
                "navigation_pose",
            ]
            assert delivered[0]["device_id"] == drone_id
            assert delivered[1]["device_id"] == drone_id
            assert all(
                phones[other_id].queue.empty() for other_id in (1, 2, 3) if other_id != drone_id
            )
            poses[drone_id] = replace(
                poses[drone_id],
                t=clock(),
                event_id=f"control-pose-update-{drone_id}",
            )
            await relay.publish("test-session", [{"type": "control_pose", "drone_id": drone_id}])
            delivered = [phones[drone_id].queue.get_nowait().event for _ in range(2)]
            assert [frame["type"] for frame in delivered] == ["control_pose", "navigation_pose"]
            assert delivered[1]["device_id"] == drone_id
            assert all(
                phones[other_id].queue.empty() for other_id in (1, 2, 3) if other_id != drone_id
            )
            publisher.retire(command.command_id)
            slot = routes_by_drone[drone_id].arrival_slot.pose
            x, y, z = slot.xyz
            snapshot = replace_aircraft(
                snapshot,
                drone_id,
                pose=Position(*slot.xyz),
                position_last_seen_ms=clock(),
            )
            poses[drone_id] = replace(
                poses[drone_id],
                t=clock(),
                event_id=f"control-pose-arrived-{drone_id}",
                x_mm=round(x * 1_000),
                y_mm=round(y * 1_000),
                z_mm=round(z * 1_000),
            )

    asyncio.run(exercise())
