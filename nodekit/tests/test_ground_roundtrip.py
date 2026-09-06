"""A ground vehicle on the real relay: join, ready, drive, scan, stop, and lose the link.

The relay runs in-process on the ``remote`` backend and the robot is
``nodekit.fake.FakeGroundVehicle`` behind ``nodekit.node.Node``; every command travels the
relay's own signing, sequencing, and delivery path (``relay.bridge.RelayNodeLink``).
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn

from adapters.dji_mini3.remote import CommandRequest
from nodekit.fake import FakeGroundVehicle
from nodekit.node import Node, NodeConfig
from planner.models import CommandOperation, DeviceClass
from relay.app import create_app
from relay.bridge import RelayNodeLink
from relay.capabilities import C1_CAPABILITY_PROFILE
from relay.session import CapabilityBoundIntentSink
from relay.settings import AdapterBackend, RelaySettings
from relay.tests.conftest import CONSOLE_KEY, SESSION
from relay.tests.test_bridge_roundtrip import ConsoleProbe, RelayServer

WAIT_S = 10.0
GROUND_ID = 11
GROUND_KEY = b"ground-vehicle-key-that-is-at-least-32"
# Only the deadman test shortens the watchdog. Every other test keeps the relay's own
# defaults, so a slow runner cannot starve a healthy node into a hold it never earned.
QUICK_HOLD_MS = 400
QUICK_FAILSAFE_MS = 1_200


def _wait_until(predicate, *, what: str) -> None:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + WAIT_S
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


def build_settings(
    tmp_path: Path, *, hold_ms: int = 2_000, failsafe_ms: int = 10_000
) -> RelaySettings:
    return RelaySettings(
        relay_token=CONSOLE_KEY,
        adapter_keys={GROUND_ID: GROUND_KEY},
        device_classes={GROUND_ID: DeviceClass.GROUND_VEHICLE},
        log_dir=tmp_path,
        adapter_backend=AdapterBackend.REMOTE,
        node_watchdog_hold_ms=hold_ms,
        node_watchdog_failsafe_ms=failsafe_ms,
    )


def serve(settings: RelaySettings) -> Iterator[RelayServer]:
    app = create_app(
        settings,
        intent_sink_factory=lambda _session: CapabilityBoundIntentSink(
            lambda _intent, _state: None,
            C1_CAPABILITY_PROFILE,
        ),
    )
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, log_level="warning", lifespan="on", timeout_graceful_shutdown=2)
    )
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    _wait_until(lambda: server.started, what="relay startup")
    try:
        yield RelayServer(runtime=app.state.relay_runtime, port=port)
    finally:
        server.should_exit = True
        thread.join(timeout=WAIT_S)
        if thread.is_alive():
            server.force_exit = True
            thread.join(timeout=WAIT_S)


@pytest.fixture
def relay_server(tmp_path: Path) -> Iterator[RelayServer]:
    yield from serve(build_settings(tmp_path))


@pytest.fixture
def quick_watchdog_relay(tmp_path: Path) -> Iterator[RelayServer]:
    """A relay whose hold and failsafe windows are short enough to watch."""
    yield from serve(build_settings(tmp_path, hold_ms=QUICK_HOLD_MS, failsafe_ms=QUICK_FAILSAFE_MS))


class GroundFleet:
    """One robot, one node, one console, and the relay they share."""

    def __init__(self, server: RelayServer, **device: object) -> None:
        self.server = server
        self.console = ConsoleProbe(server.url)
        self.robot = FakeGroundVehicle(**device)  # type: ignore[arg-type]
        self.node = Node(
            NodeConfig(
                relay_url=server.url,
                session=SESSION,
                device_id=GROUND_ID,
                token=GROUND_KEY.decode(),
                adapter_id="fake-ground-1",
                telemetry_hz=10.0,
                sensor_hz=5.0,
            ),
            self.robot,
        )
        self._commands = 0

    def start(self) -> None:
        self.console.start()
        self.node.start()
        # Readiness is the third frame of the join burst, not the last: wait for the whole
        # burst so a snapshot cannot catch the projection between readiness and node_status.
        _wait_until(self.joined, what="the ground vehicle to reach ready and report itself")

    def joined(self) -> bool:
        drone = self.drone()
        return (
            drone is not None
            and drone["membership"] == "ready"
            and drone["camera_capabilities"] is not None
            and drone["node_status"] is not None
        )

    def stop(self) -> None:
        self.node.stop()
        self.console.stop()

    def drone(self) -> dict[str, object] | None:
        session = self.server.runtime.sessions.get(SESSION)
        if session is None:
            return None
        for drone in session.current_state()["drones"]:
            if drone["drone_id"] == GROUND_ID:
                return drone
        return None

    def telemetry(self, field: str) -> object:
        drone = self.drone()
        return None if drone is None else drone["telemetry"][field]

    def send(self, operation: CommandOperation, **args: object) -> str:
        """Issue one command through the relay's own signing and delivery; do not wait."""
        session = self.server.runtime.sessions[SESSION]
        link = RelayNodeLink(self.server.runtime, SESSION, delivery_timeout_ms=2_000)
        self._commands += 1
        command_id = f"cmd-{self._commands}"
        epoch = link.connection_epoch(GROUND_ID)
        assert epoch is not None
        link.send(
            CommandRequest(
                command_id=command_id,
                intent_id=f"intent-{self._commands}",
                roster_version=session.registry.roster_version,
                drone_id=GROUND_ID,
                connection_epoch=epoch,
                operation=operation,
                args=args,  # type: ignore[arg-type]
            )
        )
        return command_id

    def acknowledgements(self, command_id: str) -> list[dict[str, object]]:
        return [
            event
            for event in list(self.console.events)
            if event.get("type") == "acknowledgement" and event.get("command_id") == command_id
        ]

    def terminal(self, command_id: str) -> dict[str, object]:
        """The node's terminal acknowledgement as a console subscriber sees it."""
        deadline = time.monotonic() + WAIT_S
        while time.monotonic() < deadline:
            for event in self.acknowledgements(command_id):
                if event.get("status") in {"completed", "failed", "invalidated"}:
                    return event
            time.sleep(0.02)
        raise AssertionError(f"{command_id} was never answered")

    def run(self, operation: CommandOperation, **args: object) -> dict[str, object]:
        return self.terminal(self.send(operation, **args))


def test_a_ground_vehicle_joins_ready_with_its_class_and_a_floor_pose(
    relay_server: RelayServer,
) -> None:
    fleet = GroundFleet(relay_server, start=(0.0, 0.0, 0.0))
    fleet.start()
    try:
        drone = fleet.drone()
        assert drone is not None
        capabilities = set(drone["adapter_capabilities"])
        scan = fleet.console.wait_for("sensor", drone_id=GROUND_ID)
        state = relay_server.runtime.sessions[SESSION].current_state()
    finally:
        fleet.stop()

    assert drone["readiness_reasons"] == []
    assert drone["device_class"] == "ground_vehicle"
    assert drone["unit"] == 1
    assert capabilities == {"ground_drive", "lidar", "camera", "class:ground_vehicle"}
    assert drone["telemetry"]["z"] == 0.0
    assert drone["telemetry"]["vz"] == 0.0
    assert drone["telemetry"]["state"] in {"idle", "moving", "stopped"}
    assert drone["node_status"]["watchdog_state"] == "nominal"
    assert drone["node_status"]["virtual_stick_enabled"] is False
    assert drone["camera_capabilities"]["aircraft_model"] == "fake-ground-vehicle"

    # The scan reaches a console subscriber unchanged, and the projection remembers it.
    assert scan["kind"] == "lidar_scan"
    assert (scan["angle_increment_deg"], len(scan["ranges_cm"])) == (1.0, 360)
    assert scan["pose"] == {"x": 0.0, "y": 0.0, "yaw_deg": 0.0}
    assert scan["ranges_cm"][0] == 400
    projected = next(item for item in state["drones"] if item["drone_id"] == GROUND_ID)
    assert projected["sensor"]["kind"] == "lidar_scan"
    assert projected["sensor"]["last_scan_at"] is not None


def test_the_ground_command_set_drives_the_robot_and_refuses_what_it_cannot_do(
    relay_server: RelayServer,
) -> None:
    fleet = GroundFleet(relay_server, start=(0.0, 0.0, 0.0))
    fleet.start()
    try:
        goto = fleet.run(CommandOperation.GOTO, x_mm=300, y_mm=0, z_mm=0, speed_mm_s=600)
        after_goto = (fleet.robot.x, fleet.robot.y)

        rotate = fleet.run(CommandOperation.ROTATE_TO, yaw_mdeg=90_000, speed_mdeg_s=180_000)
        after_rotate = fleet.robot.yaw_deg

        hover = fleet.run(CommandOperation.HOVER)
        after_hover = fleet.robot.state

        takeoff = fleet.run(CommandOperation.TAKEOFF, z_mm=1_200)
        land = fleet.run(CommandOperation.LAND)
        capture = fleet.run(CommandOperation.CAPTURE_PANORAMA, capture_id="cap-1")

        estop = fleet.run(CommandOperation.ESTOP)
        after_estop = fleet.robot.status()
    finally:
        fleet.stop()

    assert goto["status"] == "completed", goto["detail"]
    assert after_goto == (pytest.approx(0.3), pytest.approx(0.0))
    assert rotate["status"] == "completed", rotate["detail"]
    assert after_rotate == pytest.approx(90.0)
    assert hover["status"] == "completed"
    assert after_hover == "stopped"

    for refused, operation in ((takeoff, "takeoff"), (land, "land"), (capture, "capture_panorama")):
        assert refused["status"] == "failed"
        assert refused["reason"] == "unsupported_operation"
        assert refused["detail"] == f"{operation} is not available for a ground_vehicle"

    assert estop["status"] == "completed"
    assert after_estop.control_authority is False
    assert after_estop.extras["wheels_enabled"] is False
    assert after_estop.state == "stopped"


def test_the_deadman_holds_and_then_disables_when_the_control_lease_stops(
    quick_watchdog_relay: RelayServer,
) -> None:
    relay_server = quick_watchdog_relay
    fleet = GroundFleet(relay_server, start=(0.0, 0.0, 0.0))
    fleet.start()
    try:
        # A slow, long drive so the motion is still under way when the leases stop.
        drive = fleet.send(CommandOperation.GOTO, x_mm=2_000, y_mm=0, z_mm=0, speed_mm_s=100)
        _wait_until(lambda: fleet.robot.state == "moving", what="the robot to start driving")

        # Stop the relay's periodic fan-out for this session, which is what carries the
        # control heartbeat. The socket stays up and the node keeps sending, so this is
        # link loss as the node experiences it: leases stop, nothing else does.
        relay_server.runtime._fanout_failed_sessions.add(SESSION)

        hold = fleet.console.wait_for("node_status", drone_id=GROUND_ID, watchdog_state="hold")
        held = fleet.robot.state
        failsafe = fleet.console.wait_for(
            "node_status", drone_id=GROUND_ID, watchdog_state="failsafe"
        )
        after = fleet.robot.status()
        interrupted = fleet.terminal(drive)
    finally:
        fleet.stop()

    assert (interrupted["status"], interrupted["reason"]) == ("failed", "motion_failed")
    assert held == "stopped"
    assert hold["authority_change_reason"] == "watchdog_hold"
    assert failsafe["authority_change_reason"] == "watchdog_failsafe"
    assert failsafe["control_authority"] is False
    assert after.extras["wheels_enabled"] is False
    assert fleet.node.watchdog_state == "failsafe"


def test_withdrawing_the_spotter_degrades_readiness_while_the_node_runs(
    relay_server: RelayServer,
) -> None:
    fleet = GroundFleet(relay_server, start=(0.0, 0.0, 0.0))
    fleet.start()
    try:
        # The robot's screen toggle: the spotter steps away, then comes back.
        fleet.node.set_safety_operator_present(False)
        _wait_until(
            lambda: "rc_safety_operator_missing" in fleet.drone()["readiness_reasons"],
            what="the relay to drop readiness without a spotter",
        )
        without = fleet.drone()["membership"]

        fleet.node.set_safety_operator_present(True)
        _wait_until(
            lambda: fleet.drone()["readiness_reasons"] == [],
            what="the relay to restore readiness when the spotter returns",
        )
        with_spotter = fleet.drone()["membership"]
    finally:
        fleet.stop()

    assert without == "degraded"
    assert with_spotter == "ready"


def test_the_node_retries_until_the_relay_answers(tmp_path: Path) -> None:
    """A robot is switched on before the relay is up; backoff is how it gets in."""
    app = create_app(
        build_settings(tmp_path),
        intent_sink_factory=lambda _session: CapabilityBoundIntentSink(
            lambda _intent, _state: None,
            C1_CAPABILITY_PROFILE,
        ),
    )
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))  # bound but not listening: connections are refused
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, log_level="warning", lifespan="on", timeout_graceful_shutdown=2)
    )

    def serve_after_a_pause() -> None:
        time.sleep(0.8)
        server.run(sockets=[listener])

    thread = threading.Thread(target=serve_after_a_pause, daemon=True)
    thread.start()
    node = Node(
        NodeConfig(
            relay_url=f"ws://127.0.0.1:{port}",
            session=SESSION,
            device_id=GROUND_ID,
            token=GROUND_KEY.decode(),
            adapter_id="fake-ground-1",
        ),
        FakeGroundVehicle(),
    )
    try:
        node.start()
        _wait_until(lambda: node.connection_epoch is not None, what="the node to join")
        epoch = node.connection_epoch
    finally:
        node.stop()
        server.should_exit = True
        thread.join(timeout=WAIT_S)
        if thread.is_alive():
            server.force_exit = True
            thread.join(timeout=WAIT_S)

    assert epoch == 1
