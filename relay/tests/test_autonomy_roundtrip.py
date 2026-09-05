"""The M2.0 workflow through the composed relay, the command wire, and two fake nodes."""

from __future__ import annotations

import socket
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
import uvicorn

from adapters.dji_mini3.fake_node import FakeNode, FakeNodeConfig
from relay.autonomy import LIFECYCLE_SOURCE, AutonomyConfig, create_autonomy_app
from relay.settings import AdapterBackend, RelaySettings
from relay.tests.conftest import ADAPTER_KEY, CONSOLE_KEY, SESSION
from relay.tests.test_bridge_roundtrip import ConsoleProbe, RelayServer
from tests.autonomy_fixtures import planning_config, safety_config

WAIT_S = 10.0
SECOND_ADAPTER_KEY = b"adapter-two-key-that-is-at-least-32"
HOMES = {1: (0.0, 0.0, 0.0), 2: (2.0, 0.0, 0.0)}
KEYS = {1: ADAPTER_KEY, 2: SECOND_ADAPTER_KEY}


@pytest.fixture
def relay_server(tmp_path: Path) -> Iterator[RelayServer]:
    settings = RelaySettings(
        relay_token=CONSOLE_KEY,
        adapter_keys=KEYS,
        log_dir=tmp_path,
        adapter_backend=AdapterBackend.REMOTE,
    )
    app, composition = create_autonomy_app(
        settings, AutonomyConfig(planning=planning_config(), safety=safety_config())
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
        composition.close()


def test_m20_workflow_reaches_two_fake_nodes_through_the_composition(
    relay_server: RelayServer,
) -> None:
    console = ConsoleProbe(relay_server.url)
    console.start()
    nodes = [
        FakeNode(
            FakeNodeConfig(
                relay_url=relay_server.url,
                session=SESSION,
                drone_id=drone_id,
                token=KEYS[drone_id].decode(),
                adapter_id=f"fake-node-{drone_id}",
                home=HOMES[drone_id],
            )
        )
        for drone_id in (1, 2)
    ]
    for node in nodes:
        node.start()

    def drones() -> dict[int, dict[str, object]]:
        session = relay_server.runtime.sessions.get(SESSION)
        if session is None:
            return {}
        return {drone["drone_id"]: drone for drone in session.current_state()["drones"]}

    def telemetry(drone_id: int, field: str) -> object:
        drone = drones().get(drone_id)
        return None if drone is None else drone["telemetry"][field]

    def run(
        name: str,
        *,
        selection: list[int],
        args: dict[str, object] | None = None,
        confirm: bool = False,
    ) -> tuple[str, dict[str, object]]:
        intent = _intent(name, selection=selection, args=args, confirm=confirm)
        console.send(intent)
        return intent["intent_id"], _outcome(console, intent["intent_id"])

    try:
        _wait_until(
            lambda: (
                all(drone["membership"] == "ready" for drone in drones().values())
                and set(drones()) == {1, 2}
            ),
            what="both fake nodes ready",
        )

        arm_id, arm = run("arm", selection=[])
        assert arm["status"] == "completed", arm
        console.wait_for("state", armed=True)

        select_id, select = run("select", selection=[], args={"ids": [1, 2]})
        assert select["status"] == "completed", select
        console.wait_for("state", selection=[1, 2])

        takeoff_id, takeoff = run("takeoff", selection=[1, 2], confirm=True)
        assert takeoff["status"] == "completed", takeoff
        _wait_until(
            lambda: telemetry(1, "state") == "hovering" and telemetry(2, "state") == "hovering",
            what="hovering telemetry from both nodes",
        )

        # A deliberate geofence violation is refused before any adapter command is sent.
        fence_id, fence = run("translate", selection=[1, 2], args={"dx": 100, "dy": 0})
        assert (fence["type"], fence["reason"]) == ("refusal", "geofence"), fence

        translate_id, translate = run("translate", selection=[1, 2], args={"dx": 1, "dy": 0})
        assert translate["status"] == "completed", translate
        _wait_until(
            lambda: telemetry(1, "x") == 0.5 and telemetry(2, "x") == 2.5,
            what="translated telemetry",
        )

        hold_id, hold = run("hold", selection=[1, 2])
        assert hold["status"] == "completed", hold

        home_id, home = run("come_home", selection=[1, 2])
        assert home["status"] == "completed", home
        _wait_until(
            lambda: telemetry(1, "x") == 0.0 and telemetry(2, "x") == 2.0,
            what="home telemetry",
        )

        land_id, land = run("land_all", selection=[], confirm=True)
        assert land["status"] == "completed", land
        _wait_until(
            lambda: telemetry(1, "state") == "landed" and telemetry(2, "state") == "landed",
            what="landed telemetry from both nodes",
        )

        estop_id, estop = run("estop", selection=[])
        assert estop["status"] == "completed", estop
        console.wait_for("state", estop=True)
    finally:
        for node in nodes:
            node.stop()
        console.stop()

    records = [record["event"] for record in relay_server.runtime.replay(SESSION)["events"]]
    commands = [record for record in records if record["type"] == "command"]
    expected = [
        ("takeoff", takeoff_id),
        ("goto", translate_id),
        ("hover", hold_id),
        ("goto", home_id),
        ("land", land_id),
        ("estop", estop_id),
    ]
    for drone_id in (1, 2):
        issued = [command for command in commands if command["drone_id"] == drone_id]
        assert [(command["operation"], command["intent_id"]) for command in issued] == expected
        assert [command["seq"] for command in issued] == list(range(1, len(expected) + 1))
    assert all("signature" not in command for command in commands)
    assert not any(command["intent_id"] == fence_id for command in commands)
    # Coordinated translation dispatches the leading aircraft first.
    assert [
        command["drone_id"] for command in commands if command["intent_id"] == translate_id
    ] == [2, 1]
    assert [command["drone_id"] for command in commands if command["intent_id"] == home_id] == [
        1,
        2,
    ]

    console_acks = [event for event in console.events if event["type"] == "acknowledgement"]
    for command in commands:
        node_acks = [
            event for event in console_acks if event["command_id"] == command["command_id"]
        ]
        assert [event["status"] for event in node_acks] == ["accepted", "executing", "completed"]
        assert {event["source"] for event in node_acks} == {"adapter"}
        assert {event["drone_id"] for event in node_acks} == {command["drone_id"]}
    for intent_id in (takeoff_id, translate_id, hold_id, home_id, land_id, estop_id):
        events = [event for event in console_acks if event["intent_id"] == intent_id]
        assert (events[0]["source"], events[0]["status"]) == ("relay", "accepted")
        assert {event["source"] for event in events[1:-1]} == {"adapter"}
        assert len(events[1:-1]) == 6, "three node acknowledgements per aircraft"
        assert (events[-1]["source"], events[-1]["status"], events[-1]["command_id"]) == (
            LIFECYCLE_SOURCE,
            "completed",
            None,
        )
    for intent_id in (arm_id, select_id):
        events = [event for event in console_acks if event["intent_id"] == intent_id]
        assert [(event["source"], event["status"]) for event in events] == [
            ("relay", "accepted"),
            (LIFECYCLE_SOURCE, "completed"),
        ]
    fence_events = [event for event in console.events if event.get("intent_id") == fence_id]
    assert [(event["type"], event["source"]) for event in fence_events] == [
        ("acknowledgement", "relay"),
        ("refusal", LIFECYCLE_SOURCE),
    ]
    assert "command" not in {event["type"] for event in console.events}
    assert not any("signature" in event for event in console.events)
    assert {
        record["intent_id"]: record["outcome"]
        for record in records
        if record["type"] == "intent_record"
    } == dict.fromkeys(
        (
            arm_id,
            select_id,
            takeoff_id,
            fence_id,
            translate_id,
            hold_id,
            home_id,
            land_id,
            estop_id,
        ),
        "accepted",
    )


def _intent(
    name: str,
    *,
    selection: list[int],
    args: dict[str, object] | None,
    confirm: bool,
) -> dict[str, object]:
    return {
        "v": 1,
        "t": _now_ms(),
        "type": "intent",
        "intent_id": f"{name}-{uuid.uuid4().hex[:8]}",
        "retry_of": None,
        "source": "console",
        "session": SESSION,
        "name": name,
        "args": args or {},
        "selection": selection,
        "mode": "indoor",
        "confirm": confirm,
    }


def _outcome(console: ConsoleProbe, intent_id: str) -> dict[str, object]:
    """Return the autonomy-owned result event for an intent once the console has it."""
    deadline = time.monotonic() + WAIT_S
    while time.monotonic() < deadline:
        for event in list(console.events):
            if (
                event["type"] in {"acknowledgement", "refusal"}
                and event.get("intent_id") == intent_id
                and event.get("source") == LIFECYCLE_SOURCE
            ):
                return event
        time.sleep(0.02)
    raise AssertionError(f"console never received an autonomy result for {intent_id}")


def _wait_until(predicate: Callable[[], bool], *, what: str) -> None:
    deadline = time.monotonic() + WAIT_S
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


def _now_ms() -> int:
    return time.time_ns() // 1_000_000
