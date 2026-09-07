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

from adapters.dji_mini3.fake_node import FakeNode, FakeNodeConfig
from planner.models import CommandOperation
from relay.app import RelayRuntime
from relay.auth import sign_event
from relay.autonomy import AutonomyConfig, create_autonomy_app
from relay.contracts import NodeType, command_event, parse_command
from relay.observation_ingress import ObservationConfiguration
from relay.observations import FrameDeclaration, FrameRegistry, SourceBinding
from relay.settings import AdapterBackend, RelaySettings
from relay.tests.conftest import ADAPTER_KEY, CONSOLE_KEY, SESSION
from tests.autonomy_fixtures import planning_config, safety_config

from .fake import FakeGroundDevice
from .return_controller import ApprovedReturnRoute, ReturnPoint, ReturnSegment, WorldToOdom
from .runtime import GroundRuntimeConfig, OhmniRuntime, parse_args

AIRCRAFT_ID = 1
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
        adapter_keys={AIRCRAFT_ID: ADAPTER_KEY, GROUND_ID: GROUND_KEY},
        node_types={AIRCRAFT_ID: NodeType.AIRCRAFT, GROUND_ID: NodeType.GROUND},
        log_dir=tmp_path,
        adapter_backend=AdapterBackend.REMOTE,
        node_watchdog_hold_ms=1_000,
        node_watchdog_failsafe_ms=2_000,
        ground_return_id="room-a-return",
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


def _deliver(server: _RelayServer, frame: dict[str, object], *, drone_id: int = GROUND_ID) -> bool:
    assert server.runtime.loop is not None
    delivered: Future[bool] = asyncio.run_coroutine_threadsafe(
        server.runtime.deliver_to_node(SESSION, drone_id, frame), server.runtime.loop
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


def test_numeric_dial_address_preserves_the_tls_hostname(monkeypatch):
    from . import runtime as module

    calls = []

    def refuse_connection(uri, **kwargs):
        calls.append((uri, kwargs))
        raise OSError("probe complete")

    monkeypatch.setattr(module, "connect", refuse_connection)
    node = OhmniRuntime(
        GroundRuntimeConfig(
            "wss://relay.example/field",
            SESSION,
            GROUND_ID,
            GROUND_KEY.decode(),
            "ground-9",
            relay_connect_host="192.0.2.5",
        ),
        FakeGroundDevice(),
    )
    with pytest.raises(OSError, match="probe complete"):
        asyncio.run(node.run())
    assert calls == [
        (f"wss://relay.example/field/ws/{SESSION}", {"host": "192.0.2.5", "proxy": None})
    ]


def test_measured_clock_correction_applies_to_envelopes_and_lease_expiry(monkeypatch):
    monkeypatch.setattr(time, "time_ns", lambda: 131_000_000_000)
    node = OhmniRuntime(
        GroundRuntimeConfig(
            "ws://relay.example",
            SESSION,
            GROUND_ID,
            GROUND_KEY.decode(),
            "ground-9",
            relay_clock_offset_ms=-31_000,
        ),
        FakeGroundDevice(),
    )
    assert node._envelope("membership")["t"] == 100_000
    node._last_heartbeat_expires_at = 100_001
    assert not node._lease_expired()
    node._last_heartbeat_expires_at = 100_000
    assert node._lease_expired()
    assert node.config.source_clock_id == "ohmni-monotonic"


def _clock_corrected_runtime() -> OhmniRuntime:
    return OhmniRuntime(
        GroundRuntimeConfig(
            "ws://relay.example",
            SESSION,
            GROUND_ID,
            GROUND_KEY.decode(),
            "ground-9",
            relay_clock_offset_ms=-31_000,
        ),
        FakeGroundDevice(),
    )


def _signed_heartbeat(*, seq: int, issued_at: int, expires_at: int) -> dict[str, object]:
    frame: dict[str, object] = {
        "v": 1,
        "t": issued_at,
        "type": "control_heartbeat",
        "event_id": f"heartbeat-clock-boundary-{seq}",
        "session": SESSION,
        "source": "relay",
        "drone_id": GROUND_ID,
        "connection_epoch": 1,
        "roster_version": 2,
        "seq": seq,
        "issued_at": issued_at,
        "expires_at": expires_at,
        "hold_after_ms": 2_000,
        "failsafe_after_ms": 10_000,
    }
    frame["signature"] = sign_event(frame, GROUND_KEY)
    return frame


def test_relay_clock_correction_sets_heartbeat_and_command_boundaries(monkeypatch):
    monkeypatch.setattr(time, "time_ns", lambda: 131_000_000_000)
    node = _clock_corrected_runtime()
    node._epoch = 1
    node._roster_version = 2

    node._on_heartbeat(_signed_heartbeat(seq=1, issued_at=100_000, expires_at=100_001))
    assert node._last_heartbeat_seq == 1
    assert node._last_heartbeat_expires_at == 100_001

    node._on_heartbeat(_signed_heartbeat(seq=2, issued_at=100_000, expires_at=100_000))
    assert node._last_heartbeat_seq == 1
    assert node._last_heartbeat_expires_at == 100_001

    node._on_heartbeat(_signed_heartbeat(seq=3, issued_at=100_001, expires_at=100_002))
    assert node._last_heartbeat_seq == 1
    assert node._last_heartbeat_expires_at == 100_001

    at_expiry = command_event(
        t=99_999,
        event_id="command-clock-boundary",
        session=SESSION,
        command_id="command-clock-boundary",
        intent_id="command-clock-boundary",
        roster_version=2,
        drone_id=GROUND_ID,
        connection_epoch=1,
        seq=1,
        issued_at=99_999,
        ttl_ms=1,
        operation=CommandOperation.HOVER,
        args={},
    )
    at_expiry["signature"] = sign_event(at_expiry, GROUND_KEY)
    assert node._command_failure(parse_command(at_expiry)) is None

    monkeypatch.setattr(time, "time_ns", lambda: 131_001_000_000)
    expired = node._command_failure(parse_command(at_expiry))
    assert expired is not None
    assert expired[0] == "stale_command"


def test_relay_clock_correction_never_changes_sensor_monotonic_receipt(monkeypatch):
    monkeypatch.setattr(time, "time_ns", lambda: 131_000_000_000)
    monkeypatch.setattr(time, "monotonic_ns", lambda: 555_000_000)
    node = _clock_corrected_runtime()
    node._epoch = 1
    node._outbound = asyncio.Queue()

    node._publish_observations()

    observations = [node._outbound.get_nowait() for _ in range(node._outbound.qsize())]
    assert len(observations) == 3
    assert all(
        frame["t_source_receipt"]
        == {
            "clock_id": "ohmni-monotonic",
            "unit": "ns",
            "value": 555_000_000,
        }
        for frame in observations
    )


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
            heartbeat_hold_ms=1_000,
            heartbeat_failsafe_ms=2_000,
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
            heartbeat_hold_ms=1_000,
            heartbeat_failsafe_ms=2_000,
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
            heartbeat_hold_ms=1_000,
            heartbeat_failsafe_ms=2_000,
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


class _AckSilentGroundRuntime(OhmniRuntime):
    def _enqueue(self, frame: dict[str, object]) -> None:
        if frame.get("type") != "acknowledgement":
            super()._enqueue(frame)


def _mixed_ready(session: object) -> bool:
    state = session.current_state()  # type: ignore[attr-defined]
    drones = {drone["drone_id"]: drone for drone in state["drones"]}
    return set(drones) == {AIRCRAFT_ID, GROUND_ID} and all(
        drone["membership"] == "ready" for drone in drones.values()
    )


def _console_intent(*, intent_id: str, name: str, selection: list[int]) -> dict[str, object]:
    return {
        "v": 1,
        "t": int(time.time_ns() // 1_000_000),
        "type": "intent",
        "intent_id": intent_id,
        "retry_of": None,
        "source": "console",
        "session": SESSION,
        "name": name,
        "args": {},
        "selection": selection,
        "mode": "indoor",
        "confirm": False,
    }


def _authenticate_console(socket: object) -> None:
    socket.send(  # type: ignore[attr-defined]
        json.dumps({"v": 1, "type": "auth", "source": "console", "token": CONSOLE_KEY.decode()})
    )
    assert json.loads(socket.recv(timeout=WAIT_S))["type"] == "auth.accepted"  # type: ignore[attr-defined]
    assert json.loads(socket.recv(timeout=WAIT_S))["type"] == "state"  # type: ignore[attr-defined]


def test_ground_only_hold_in_a_mixed_roster_does_not_dispatch_an_empty_aircraft_plan(
    relay_server: _RelayServer,
) -> None:
    device = FakeGroundDevice()
    ground = OhmniRuntime(
        GroundRuntimeConfig(
            relay_url=relay_server.url,
            session=SESSION,
            device_id=GROUND_ID,
            token=GROUND_KEY.decode(),
            adapter_id="fake-ohmni-9",
            heartbeat_hold_ms=1_000,
            heartbeat_failsafe_ms=2_000,
            lidar_mount_x_m=0.0,
            lidar_mount_y_m=0.0,
            lidar_mount_z_m=0.25,
            lidar_mount_yaw_deg=0.0,
        ),
        device,
    )
    aircraft = FakeNode(
        FakeNodeConfig(
            relay_url=relay_server.url,
            session=SESSION,
            drone_id=AIRCRAFT_ID,
            token=ADAPTER_KEY.decode(),
            adapter_id="fake-aircraft-1",
        )
    )
    ground.start()
    aircraft.start()
    try:
        session = relay_server.runtime.sessions[SESSION]
        _wait_for(lambda: _mixed_ready(session), "mixed readiness")
        time.sleep(0.5)
        roster_version = session.current_state()["roster_version"]
        time.sleep(0.5)
        state_after_ticks = session.current_state()
        assert state_after_ticks["roster_version"] == roster_version
        state = {drone["drone_id"]: drone for drone in state_after_ticks["drones"]}
        _wait_for(lambda: aircraft._roster_version == roster_version, "aircraft roster refresh")
        aircraft_command = session.issue_command(
            command_id="mixed-aircraft-command",
            intent_id="mixed-aircraft-command",
            roster_version=roster_version,
            drone_id=AIRCRAFT_ID,
            connection_epoch=state[AIRCRAFT_ID]["connection_epoch"],
            operation=CommandOperation.HOVER,
            args={},
            signing_key=ADAPTER_KEY,
        )
        assert _deliver(relay_server, aircraft_command, drone_id=AIRCRAFT_ID)
        _wait_for(lambda: aircraft._last_seq == 1, "aircraft command delivery")
        motion = session.issue_command(
            command_id="mixed-ground-motion",
            intent_id="mixed-ground-motion",
            roster_version=session.current_state()["roster_version"],
            drone_id=GROUND_ID,
            connection_epoch=state[GROUND_ID]["connection_epoch"],
            operation=CommandOperation.GROUND_VELOCITY,
            args={"linear_mm_s": 100, "angular_mrad_s": 0, "duration_ms": 500},
            signing_key=GROUND_KEY,
        )
        assert _deliver(relay_server, motion)
        _wait_for(lambda: device.status().state == "moving", "mixed ground motion")

        intent_id = "mixed-ground-only-hold"
        with sync_connect(f"{relay_server.url}/ws/{SESSION}", proxy=None) as console:
            _authenticate_console(console)
            console.send(
                json.dumps(_console_intent(intent_id=intent_id, name="hold", selection=[GROUND_ID]))
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
        assert [(record["drone_id"], record["operation"]) for record in commands] == [
            (GROUND_ID, CommandOperation.HOVER.value)
        ]
    finally:
        aircraft.stop()
        ground.stop()


def test_aircraft_estop_reaches_the_live_node_before_a_silent_ground_ack_times_out(
    relay_server: _RelayServer,
) -> None:
    device = FakeGroundDevice()
    ground = _AckSilentGroundRuntime(
        GroundRuntimeConfig(
            relay_url=relay_server.url,
            session=SESSION,
            device_id=GROUND_ID,
            token=GROUND_KEY.decode(),
            adapter_id="silent-ack-ohmni-9",
            heartbeat_hold_ms=1_000,
            heartbeat_failsafe_ms=2_000,
            lidar_mount_x_m=0.0,
            lidar_mount_y_m=0.0,
            lidar_mount_z_m=0.25,
            lidar_mount_yaw_deg=0.0,
        ),
        device,
    )
    aircraft = FakeNode(
        FakeNodeConfig(
            relay_url=relay_server.url,
            session=SESSION,
            drone_id=AIRCRAFT_ID,
            token=ADAPTER_KEY.decode(),
            adapter_id="fake-aircraft-1",
        )
    )
    ground.start()
    aircraft.start()
    try:
        session = relay_server.runtime.sessions[SESSION]
        _wait_for(lambda: _mixed_ready(session), "mixed readiness")
        intent_id = "mixed-estop-silent-ground"
        with sync_connect(f"{relay_server.url}/ws/{SESSION}", proxy=None) as console:
            _authenticate_console(console)
            began = time.monotonic()
            console.send(
                json.dumps(_console_intent(intent_id=intent_id, name="estop", selection=[]))
            )
            aircraft_completed = _receive_until(
                console,
                lambda frame: (
                    frame.get("type") == "acknowledgement"
                    and frame.get("intent_id") == intent_id
                    and frame.get("source") == "adapter"
                    and frame.get("drone_id") == AIRCRAFT_ID
                    and frame.get("status") == "completed"
                ),
            )
            timeout_s = relay_server.runtime.settings.command_deadline_ms / 1_000
            assert time.monotonic() - began < timeout_s
            terminal = _receive_until(
                console,
                lambda frame: (
                    frame.get("type") == "acknowledgement"
                    and frame.get("intent_id") == intent_id
                    and frame.get("source") == "autonomy"
                    and frame.get("status") == "failed"
                ),
            )
        assert aircraft_completed["command_id"] is not None
        assert terminal["command_id"] is None
        assert device.stopped
    finally:
        aircraft.stop()
        ground.stop()


def _approved_return() -> ApprovedReturnRoute:
    return ApprovedReturnRoute(
        return_id="room-a-return",
        approval_id="approval-17",
        approval_signer="map-operator",
        source_registration_id="registration-9",
        pose_source_id="ohmni-pose",
        odom_frame="odom",
        world_to_odom=WorldToOdom(0.0, 0.0, 0.0, "registration-9"),
        start=ReturnPoint(0.0, 0.0),
        segments=(
            ReturnSegment(
                ReturnPoint(0.05, 0.0),
                (
                    ReturnPoint(-0.1, -0.2),
                    ReturnPoint(0.2, -0.2),
                    ReturnPoint(0.2, 0.2),
                    ReturnPoint(-0.1, 0.2),
                ),
            ),
        ),
        footprint_radius_m=0.25,
        arrival_tolerance_m=0.03,
        geometry_sha256="0" * 64,
    )


def test_confirmed_console_come_home_executes_the_approved_ground_return(
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
            heartbeat_hold_ms=1_000,
            heartbeat_failsafe_ms=2_000,
            lidar_mount_x_m=0.0,
            lidar_mount_y_m=0.0,
            lidar_mount_z_m=0.25,
            lidar_mount_yaw_deg=0.0,
            return_approval=_approved_return(),
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
        intent_id = "ground-return-e2e"
        with sync_connect(f"{relay_server.url}/ws/{SESSION}", proxy=None) as console:
            console.send(
                json.dumps(
                    {"v": 1, "type": "auth", "source": "console", "token": CONSOLE_KEY.decode()}
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
                        "name": "come_home",
                        "args": {},
                        "selection": [GROUND_ID],
                        "mode": "indoor",
                        "confirm": True,
                    }
                )
            )
            _wait_for(
                lambda: any(
                    record["event"].get("type") == "acknowledgement"
                    and record["event"].get("intent_id") == intent_id
                    and record["event"].get("source") == "autonomy"
                    and record["event"].get("status") == "completed"
                    for record in relay_server.runtime.replay(SESSION)["events"]
                ),
                "ground return completion",
            )
        records = [record["event"] for record in relay_server.runtime.replay(SESSION)["events"]]
        commands = [
            record
            for record in records
            if record["type"] == "command" and record["intent_id"] == intent_id
        ]
        assert [(command["operation"], command["args"]) for command in commands] == [
            ("ground_return", {"return_id": "room-a-return"})
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
        assert device.x >= 0.024
    finally:
        node.stop()
