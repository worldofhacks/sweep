from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from websockets.sync.client import connect

from planner.ground_navigation import GroundNavigationDeployment
from planner.models import CommandOperation, LifecycleStatus
from planner.test_ground_navigation import KEY, deployment_file, pose
from relay.bridge import RelayNodeLink
from relay.intent_v1 import IntentName
from relay.tests.conftest import CONSOLE_KEY, SESSION
from tests.autonomy_fixtures import make_intent

from .dispatcher import GroundCommandDispatcher
from .fake import FakeGroundDevice
from .runtime import GroundRuntimeConfig, OhmniRuntime, parse_args
from .test_runtime import GROUND_ID, GROUND_KEY, _deliver, _receive_until, _wait_for
from .test_runtime import relay_server as relay_server


def test_node_cli_loads_the_signed_navigation_deployment_and_source_binding(tmp_path):
    path = deployment_file(tmp_path)
    key_path = tmp_path / "navigation.key"
    key_path.write_bytes(KEY)
    key_path.chmod(0o600)
    config = parse_args(
        [
            "--relay",
            "ws://relay.example",
            "--session",
            "session-a",
            "--device-id",
            "9",
            "--token",
            GROUND_KEY.decode(),
            "--odom-origin-id",
            "origin-a",
            "--navigation-config",
            str(path),
            "--navigation-key-file",
            str(key_path),
        ]
    )
    assert config.navigation.device(9).world_pose_source_id == "world-ohmni-pose"
    assert config.navigation.device(9).identity_source_id == "ohmni-status"


@pytest.mark.parametrize("hold_during_motion", [False, True])
def test_named_route_requires_fresh_arrival_and_stop_and_cannot_resume_or_replay(
    relay_server, tmp_path, hold_during_motion
):
    now_ms = time.time_ns() // 1_000_000
    path = deployment_file(tmp_path, now_ms=now_ms)
    host = GroundNavigationDeployment.load(path, KEY)
    device = FakeGroundDevice(x=7.75, y=-1.05, yaw_deg=90.0)
    node = OhmniRuntime(
        GroundRuntimeConfig(
            relay_server.url,
            SESSION,
            GROUND_ID,
            GROUND_KEY.decode(),
            "ground-9",
            navigation=GroundNavigationDeployment.load(path, KEY),
            odom_origin_id="origin-a",
            heartbeat_hold_ms=1000,
            heartbeat_failsafe_ms=2000,
            lidar_mount_x_m=0.0,
            lidar_mount_y_m=0.0,
            lidar_mount_z_m=0.25,
            lidar_mount_yaw_deg=0.0,
        ),
        device,
    )
    node.start()
    try:
        session = relay_server.runtime.sessions[SESSION]
        try:
            _wait_for(
                lambda: (
                    session.registry.ready_ground_identity(GROUND_ID, time.time_ns() // 1_000_000)
                    is not None
                ),
                "ground navigation readiness",
            )
        except AssertionError:
            raise AssertionError(session.current_state()) from None
        with connect(f"{relay_server.url}/ws/{SESSION}", proxy=None) as console:
            console.send(
                json.dumps(
                    {"v": 1, "type": "auth", "source": "console", "token": CONSOLE_KEY.decode()}
                )
            )
            identity = _receive_until(
                console,
                lambda frame: (
                    frame.get("type") == "observation"
                    and frame.get("payload", {}).get("code") == "ground_navigation_identity"
                ),
            )
            assert json.loads(identity["payload"]["detail"]) == {
                "odom_origin_id": "origin-a",
                "pose_source_id": "ohmni-pose",
                "registration_id": "fixture-transform",
                "configuration_sha256": host.configuration_sha256,
            }
            _wait_for(
                lambda: any(
                    row["drone_id"] == GROUND_ID and row["selectable"]
                    for row in session.current_state()["drones"]
                ),
                "selectable ground navigation node",
            )
            state = session.current_state()
            ground = next(row for row in state["drones"] if row["drone_id"] == GROUND_ID)
            assert "navigate" in ground["adapter_capabilities"]
            now_ms = time.time_ns() // 1_000_000
            plan = host.prepare(
                "lobby",
                (pose(9, 1.95, 2.25, t_ms=now_ms),),
                (9,),
                session=SESSION,
                roster_version=state["roster_version"],
                now_ms=now_ms,
            )
            dispatcher = GroundCommandDispatcher(
                RelayNodeLink(relay_server.runtime, SESSION, delivery_timeout_ms=1000),
                acknowledgement_timeout_ms=8000,
                command_deadline_ms=10_000,
            )
            intent = make_intent(
                IntentName.NAVIGATE,
                selection=(9,),
                args={"zone_id": "lobby"},
                intent_id="ground-navigation",
                confirm=True,
            )
            route_id = f"{plan.plan_id}:9"
            record = host.route_record(plan, 9, now_ms=now_ms)
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(
                    dispatcher.dispatch_navigation,
                    intent,
                    state,
                    route_id=route_id,
                    navigation_route=record,
                )
                if hold_during_motion:
                    _wait_for(lambda: device.status().state == "moving", "navigation motion")
                    hold = session.issue_command(
                        command_id="hold-navigation",
                        intent_id="hold-navigation",
                        roster_version=state["roster_version"],
                        drone_id=GROUND_ID,
                        connection_epoch=ground["connection_epoch"],
                        operation=CommandOperation.HOVER,
                        args={},
                        signing_key=GROUND_KEY,
                    )
                    assert _deliver(relay_server, hold)
                result = future.result(timeout=12)
            expected = LifecycleStatus.FAILED if hold_during_motion else LifecycleStatus.COMPLETED
            assert result.status is expected, result
            assert device.stop_confirmed()
            assert device.status().t_ms is not None
            if not hold_during_motion:
                assert device.status().y >= -0.80
                assert session.current_state()["roster_version"] == state["roster_version"]
            _wait_for(
                lambda: (
                    session.registry.ready_ground_identity(GROUND_ID, time.time_ns() // 1_000_000)
                    is not None
                ),
                "fresh heartbeat after route ended",
            )
            stopped_pose = device.status()
            replay = dispatcher.dispatch_navigation(
                make_intent(
                    IntentName.NAVIGATE,
                    selection=(9,),
                    args={"zone_id": "lobby"},
                    intent_id="ground-navigation-replay",
                    confirm=True,
                ),
                session.current_state(),
                route_id=route_id,
                navigation_route=record,
            )
            assert replay.status is LifecycleStatus.FAILED, replay
            time.sleep(0.3)
            assert device.stop_confirmed()
            assert device.status().x == pytest.approx(stopped_pose.x)
            assert device.status().y == pytest.approx(stopped_pose.y)
    finally:
        node.stop()
