from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from collections.abc import Iterator
from concurrent.futures import Future
from pathlib import Path

import pytest
import uvicorn
from websockets.sync.client import connect as sync_connect

from planner.models import CommandOperation
from relay.app import RelayRuntime
from relay.autonomy import AutonomyConfig, create_autonomy_app
from relay.contracts import NodeType
from relay.observation_ingress import ObservationConfiguration
from relay.observations import FrameDeclaration, FrameRegistry, SourceBinding
from relay.settings import AdapterBackend, RelaySettings
from relay.tests.conftest import CONSOLE_KEY, SESSION
from tests.autonomy_fixtures import planning_config, safety_config

from .fake import FakeGroundDevice
from .runtime import GroundRuntimeConfig, OhmniRuntime, parse_args

GROUND_ID = 9
GROUND_KEY = b"ground-adapter-key-that-is-at-least-32-bytes"
WAIT_S = 5.0


class _RelayServer:
    def __init__(self, runtime: RelayRuntime, port: int, server: uvicorn.Server) -> None:
        self.runtime = runtime
        self.port = port
        self.server = server

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}"


def _wait_for(predicate, description: str) -> None:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + WAIT_S
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {description}")


def _observation_configuration() -> ObservationConfiguration:
    declarations = []
    bindings = []
    source_shapes = (
        ("ohmni-pose", ("odom", "body"), ("pose",)),
        ("ohmni-telemetry", ("odom",), ("telemetry",)),
        ("ohmni-status", ("odom",), ("status",)),
        ("ohmni-lidar", ("odom", "lidar"), ("range_scan",)),
        ("ohmni-camera", ("camera",), ("camera_frame",)),
    )
    kinds = {"odom": "odom", "body": "body", "lidar": "lidar", "camera": "camera"}
    axes = {
        "odom": "right_handed_z_up",
        "body": "forward_left_up",
        "lidar": "forward_left_up",
        "camera": "right_down_forward",
    }
    for source_id, frames, payload_kinds in source_shapes:
        bindings.append(
            SourceBinding(
                session=SESSION,
                device_id=GROUND_ID,
                connection_epoch=1,
                source_id=source_id,
                node_type="ground",
                allowed_frames=frames,
                allowed_payload_kinds=payload_kinds,
            )
        )
        declarations.extend(
            FrameDeclaration(
                frame_id=frame,
                kind=kinds[frame],  # type: ignore[arg-type]
                axis_convention=axes[frame],
                unit="m",
                session=SESSION,
                device_id=GROUND_ID,
                connection_epoch=1,
                source_id=source_id,
            )
            for frame in frames
        )
    return ObservationConfiguration(tuple(bindings), FrameRegistry(tuple(declarations)))


@pytest.fixture
def relay_server(tmp_path: Path) -> Iterator[_RelayServer]:
    settings = RelaySettings(
        relay_token=CONSOLE_KEY,
        adapter_keys={GROUND_ID: GROUND_KEY},
        node_types={GROUND_ID: NodeType.GROUND},
        log_dir=tmp_path,
        adapter_backend=AdapterBackend.REMOTE,
        node_watchdog_hold_ms=100,
        node_watchdog_failsafe_ms=200,
        observation_configuration=_observation_configuration(),
    )
    app, autonomy = create_autonomy_app(
        settings,
        AutonomyConfig(
            planning=planning_config(),
            safety=safety_config(),
        ),
    )
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    server = uvicorn.Server(
        uvicorn.Config(app, log_level="warning", lifespan="on", timeout_graceful_shutdown=2)
    )
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    _wait_for(lambda: server.started, "relay startup")
    try:
        yield _RelayServer(app.state.relay_runtime, listener.getsockname()[1], server)
    finally:
        server.should_exit = True
        thread.join(timeout=WAIT_S)
        if thread.is_alive():
            server.force_exit = True
            thread.join(timeout=WAIT_S)
        autonomy.close()


def _deliver(server: _RelayServer, frame: dict[str, object]) -> bool:
    assert server.runtime.loop is not None
    delivered: Future[bool] = asyncio.run_coroutine_threadsafe(
        server.runtime.deliver_to_node(SESSION, GROUND_ID, frame), server.runtime.loop
    )
    return delivered.result(timeout=WAIT_S)


def _receive_until(socket: object, predicate: object) -> dict[str, object]:
    deadline = time.monotonic() + WAIT_S
    while time.monotonic() < deadline:
        raw = socket.recv(timeout=WAIT_S)  # type: ignore[attr-defined]
        frame = json.loads(raw)
        if isinstance(frame, dict) and predicate(frame):  # type: ignore[operator]
            return frame
    raise AssertionError("timed out waiting for relay event")


def test_runtime_arguments_build_the_required_ground_identity() -> None:
    config = parse_args(
        [
            "--relay",
            "ws://relay.example/",
            "--session",
            "room-1",
            "--device-id",
            "9",
            "--token",
            GROUND_KEY.decode(),
        ]
    )

    assert config.relay_url == "ws://relay.example"
    assert config.adapter_id == "ohmni-9"


def test_ground_runtime_joins_becomes_ready_acks_velocity_and_stops_on_lease_loss(
    relay_server: _RelayServer,
) -> None:
    device = FakeGroundDevice()
    node = OhmniRuntime(
        GroundRuntimeConfig(
            relay_url=relay_server.url,
            session=SESSION,
            device_id=GROUND_ID,
            token=GROUND_KEY.decode(),
            adapter_id="fake-ohmni-9",
            heartbeat_hold_ms=100,
            heartbeat_failsafe_ms=200,
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
        _wait_for(
            lambda: (
                bool(session.current_state()["drones"])
                and session.current_state()["drones"][0]["membership"] == "ready"
            ),
            "ground readiness",
        )
        state = session.current_state()["drones"][0]
        assert state["node_type"] == "ground"
        assert state["telemetry"] is None
        assert device.enabled

        command = session.issue_command(
            command_id="ground-pulse",
            intent_id="ground-intent",
            roster_version=session.current_state()["roster_version"],
            drone_id=GROUND_ID,
            connection_epoch=state["connection_epoch"],
            operation=CommandOperation.GROUND_VELOCITY,
            args={"linear_mm_s": 100, "angular_mrad_s": 0, "duration_ms": 25},
            signing_key=GROUND_KEY,
        )
        assert _deliver(relay_server, command)
        statuses = []
        while not statuses or statuses[-1] != "completed":
            acknowledgement = session.await_command_acknowledgement(
                "ground-pulse", timeout_ms=WAIT_S * 1_000
            )
            assert acknowledgement is not None
            statuses.append(acknowledgement.status.value)
        assert statuses == ["accepted", "executing", "completed"]
        assert device.x > 0

        relay_server.runtime._fanout_failed_sessions.add(SESSION)
        _wait_for(lambda: node.watchdog_state == "failsafe", "ground watchdog failsafe")
        assert not device.enabled
        assert device.stopped
    finally:
        node.stop()


def test_confirmed_console_ground_velocity_uses_signed_relay_command_lifecycle(
    relay_server: _RelayServer,
) -> None:
    device = FakeGroundDevice()
    node = OhmniRuntime(
        GroundRuntimeConfig(
            relay_url=relay_server.url,
            session=SESSION,
            device_id=GROUND_ID,
            token=GROUND_KEY.decode(),
            adapter_id="fake-ohmni-9",
            heartbeat_hold_ms=100,
            heartbeat_failsafe_ms=200,
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
        _wait_for(
            lambda: (
                bool(session.current_state()["drones"])
                and session.current_state()["drones"][0]["membership"] == "ready"
            ),
            "ground readiness",
        )
        intent_id = "ground-velocity-e2e"
        with sync_connect(f"{relay_server.url}/ws/{SESSION}", proxy=None) as console:
            console.send(
                json.dumps(
                    {
                        "v": 1,
                        "type": "auth",
                        "source": "console",
                        "token": CONSOLE_KEY.decode(),
                    }
                )
            )
            assert json.loads(console.recv(timeout=WAIT_S))["type"] == "auth.accepted"
            assert json.loads(console.recv(timeout=WAIT_S))["type"] == "state"
            console.send(
                json.dumps(
                    {
                        "v": 1,
                        "t": int(time.time_ns() // 1_000_000),
                        "type": "intent",
                        "intent_id": intent_id,
                        "retry_of": None,
                        "source": "console",
                        "session": SESSION,
                        "name": "ground_velocity",
                        "args": {
                            "linear_mm_s": 100,
                            "angular_mrad_s": 0,
                            "duration_ms": 25,
                        },
                        "selection": [GROUND_ID],
                        "mode": "indoor",
                        "confirm": True,
                    }
                )
            )
            terminal = _receive_until(
                console,
                lambda frame: (
                    frame.get("type") == "acknowledgement"
                    and frame.get("intent_id") == intent_id
                    and frame.get("source") == "autonomy"
                    and frame.get("status") == "completed"
                ),
            )
        assert terminal["command_id"] is None
        assert device.x > 0
        records = [record["event"] for record in relay_server.runtime.replay(SESSION)["events"]]
        commands = [
            record
            for record in records
            if record["type"] == "command" and record["intent_id"] == intent_id
        ]
        assert len(commands) == 1
        assert commands[0]["operation"] == "ground_velocity"
        assert commands[0]["args"] == {
            "linear_mm_s": 100,
            "angular_mrad_s": 0,
            "duration_ms": 25,
        }
        assert "signature" not in commands[0]
        lifecycle = [
            (record["source"], record["status"])
            for record in records
            if record["type"] == "acknowledgement" and record.get("intent_id") == intent_id
        ]
        assert lifecycle == [
            ("relay", "accepted"),
            ("adapter", "accepted"),
            ("adapter", "executing"),
            ("adapter", "completed"),
            ("autonomy", "completed"),
        ]
    finally:
        node.stop()


@pytest.mark.parametrize(
    ("intent_name", "selection", "operation"),
    [
        ("hold", [GROUND_ID], CommandOperation.HOVER),
        ("estop", [], CommandOperation.ESTOP),
    ],
)
def test_console_stop_sends_a_signed_terminal_ground_stop_while_the_robot_is_moving(
    relay_server: _RelayServer,
    intent_name: str,
    selection: list[int],
    operation: CommandOperation,
) -> None:
    device = FakeGroundDevice()
    node = OhmniRuntime(
        GroundRuntimeConfig(
            relay_url=relay_server.url,
            session=SESSION,
            device_id=GROUND_ID,
            token=GROUND_KEY.decode(),
            adapter_id="fake-ohmni-9",
            heartbeat_hold_ms=100,
            heartbeat_failsafe_ms=200,
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
        _wait_for(
            lambda: (
                bool(session.current_state()["drones"])
                and session.current_state()["drones"][0]["membership"] == "ready"
            ),
            "ground readiness",
        )
        state = session.current_state()["drones"][0]
        motion = session.issue_command(
            command_id=f"moving-before-{intent_name}",
            intent_id=f"moving-before-{intent_name}",
            roster_version=session.current_state()["roster_version"],
            drone_id=GROUND_ID,
            connection_epoch=state["connection_epoch"],
            operation=CommandOperation.GROUND_VELOCITY,
            args={"linear_mm_s": 100, "angular_mrad_s": 0, "duration_ms": 500},
            signing_key=GROUND_KEY,
        )
        assert _deliver(relay_server, motion)
        _wait_for(lambda: device.status().state == "moving", "ground motion")

        intent_id = f"{intent_name}-ground-stop-e2e"
        with sync_connect(f"{relay_server.url}/ws/{SESSION}", proxy=None) as console:
            console.send(
                json.dumps(
                    {
                        "v": 1,
                        "type": "auth",
                        "source": "console",
                        "token": CONSOLE_KEY.decode(),
                    }
                )
            )
            assert json.loads(console.recv(timeout=WAIT_S))["type"] == "auth.accepted"
            assert json.loads(console.recv(timeout=WAIT_S))["type"] == "state"
            console.send(
                json.dumps(
                    {
                        "v": 1,
                        "t": int(time.time_ns() // 1_000_000),
                        "type": "intent",
                        "intent_id": intent_id,
                        "retry_of": None,
                        "source": "console",
                        "session": SESSION,
                        "name": intent_name,
                        "args": {},
                        "selection": selection,
                        "mode": "indoor",
                        "confirm": False,
                    }
                )
            )
            terminal = _receive_until(
                console,
                lambda frame: (
                    frame.get("type") == "acknowledgement"
                    and frame.get("intent_id") == intent_id
                    and frame.get("source") == "autonomy"
                    and frame.get("status") == "completed"
                ),
            )
        assert terminal["command_id"] is None
        assert device.status().state != "moving"
        records = [record["event"] for record in relay_server.runtime.replay(SESSION)["events"]]
        commands = [
            record
            for record in records
            if record["type"] == "command" and record["intent_id"] == intent_id
        ]
        assert [(command["drone_id"], command["operation"]) for command in commands] == [
            (GROUND_ID, operation.value)
        ]
        lifecycle = [
            (record["source"], record["status"])
            for record in records
            if record["type"] == "acknowledgement" and record.get("intent_id") == intent_id
        ]
        assert lifecycle == [
            ("relay", "accepted"),
            ("adapter", "accepted"),
            ("adapter", "executing"),
            ("adapter", "completed"),
            ("autonomy", "completed"),
        ]
    finally:
        node.stop()
