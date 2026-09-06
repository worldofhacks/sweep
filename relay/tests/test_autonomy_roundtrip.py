"""The M2.0 workflow through the composed relay, the command wire, and fake nodes."""

from __future__ import annotations

import socket
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path

import pytest
import uvicorn

import relay.autonomy as autonomy_module
from adapters.dji_mini3.fake_node import FakeNode, FakeNodeConfig
from relay.audit import AuditLogError
from relay.autonomy import (
    LIFECYCLE_SOURCE,
    PREEMPTED_BY_ESTOP,
    PREEMPTED_BY_HOLD,
    AutonomyConfig,
    create_autonomy_app,
)
from relay.settings import AdapterBackend, RelaySettings
from relay.tests.conftest import ADAPTER_KEY, CONSOLE_KEY, SESSION
from relay.tests.test_bridge_roundtrip import ConsoleProbe, RelayServer
from tests.autonomy_fixtures import planning_config, safety_config

WAIT_S = 10.0
SECOND_ADAPTER_KEY = b"adapter-two-key-that-is-at-least-32"
THIRD_ADAPTER_KEY = b"adapter-three-key-that-is-at-least-32"
HOMES = {1: (0.0, 0.0, 0.0), 2: (2.0, 0.0, 0.0), 3: (4.0, 0.0, 0.0)}
KEYS = {1: ADAPTER_KEY, 2: SECOND_ADAPTER_KEY, 3: THIRD_ADAPTER_KEY}
STALL_S = 1.5


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


def _node(server: RelayServer, drone_id: int, **options: object) -> FakeNode:
    return FakeNode(
        FakeNodeConfig(
            relay_url=server.url,
            session=SESSION,
            drone_id=drone_id,
            token=KEYS[drone_id].decode(),
            adapter_id=f"fake-node-{drone_id}",
            home=HOMES[drone_id],
            **options,  # type: ignore[arg-type]
        )
    )


class _Fleet:
    """A console probe and fake nodes against one relay, with the checkpoint helpers."""

    def __init__(self, server: RelayServer, options: Mapping[int, Mapping[str, object]]) -> None:
        self.server = server
        self.console = ConsoleProbe(server.url)
        self.ids = sorted(options)
        self.nodes = [_node(server, drone_id, **options[drone_id]) for drone_id in self.ids]

    def start(self) -> None:
        self.console.start()
        for node in self.nodes:
            node.start()
        _wait_until(
            lambda: (
                set(self.drones()) == set(self.ids)
                and all(drone["membership"] == "ready" for drone in self.drones().values())
            ),
            what="fake nodes ready",
        )

    def stop(self) -> None:
        for node in self.nodes:
            node.stop()
        self.console.stop()

    def drones(self) -> dict[int, dict[str, object]]:
        session = self.server.runtime.sessions.get(SESSION)
        if session is None:
            return {}
        return {drone["drone_id"]: drone for drone in session.current_state()["drones"]}

    def telemetry(self, drone_id: int, field: str) -> object:
        drone = self.drones().get(drone_id)
        return None if drone is None else drone["telemetry"][field]

    def roster_version(self) -> int:
        return self.server.runtime.sessions[SESSION].registry.roster_version

    def send(
        self,
        name: str,
        *,
        selection: list[int],
        args: dict[str, object] | None = None,
        confirm: bool = False,
    ) -> str:
        intent = _intent(name, selection=selection, args=args, confirm=confirm)
        self.console.send(intent)
        return intent["intent_id"]

    def run(
        self,
        name: str,
        *,
        selection: list[int],
        args: dict[str, object] | None = None,
        confirm: bool = False,
    ) -> tuple[str, dict[str, object]]:
        intent_id = self.send(name, selection=selection, args=args, confirm=confirm)
        return intent_id, _outcome(self.console, intent_id)

    def airborne(self) -> str:
        """Arm, select every node, and take off; return the takeoff intent id."""
        _, arm = self.run("arm", selection=[])
        assert arm["status"] == "completed", arm
        self.console.wait_for("state", armed=True)
        _, select = self.run("select", selection=[], args={"ids": self.ids})
        assert select["status"] == "completed", select
        self.console.wait_for("state", selection=self.ids)
        takeoff_id, takeoff = self.run("takeoff", selection=self.ids, confirm=True)
        assert takeoff["status"] == "completed", takeoff
        _wait_until(
            lambda: all(self.telemetry(drone_id, "state") == "hovering" for drone_id in self.ids),
            what="hovering telemetry from every node",
        )
        return takeoff_id

    def commands_for(self, drone_id: int) -> list[tuple[str, str]]:
        """The wire commands the relay issued to one aircraft, in sequence order."""
        records = [record["event"] for record in self.server.runtime.replay(SESSION)["events"]]
        issued = [
            record
            for record in records
            if record["type"] == "command" and record["drone_id"] == drone_id
        ]
        assert [record["seq"] for record in issued] == list(range(1, len(issued) + 1))
        return [(record["operation"], record["intent_id"]) for record in issued]

    def command_is_replayable(self, drone_id: int, operation: str, intent_id: str) -> bool:
        """Return false while an atomic audit append intentionally blocks replay."""
        try:
            return (operation, intent_id) in self.commands_for(drone_id)
        except AuditLogError:
            return False

    def node_acks(self, intent_id: str, drone_id: int) -> list[str]:
        return [
            event["status"]
            for event in self.console.events
            if event["type"] == "acknowledgement"
            and event.get("intent_id") == intent_id
            and event.get("source") == "adapter"
            and event.get("drone_id") == drone_id
        ]


def test_m20_workflow_reaches_two_fake_nodes_through_the_composition(
    relay_server: RelayServer,
) -> None:
    console = ConsoleProbe(relay_server.url)
    console.start()
    nodes = [_node(relay_server, drone_id) for drone_id in (1, 2)]
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


def test_estop_preempts_a_running_plan_before_its_remaining_commands(
    relay_server: RelayServer,
) -> None:
    fleet = _Fleet(
        relay_server,
        {1: {}, 2: {"slow_operations": ("goto",), "slow_ack_delay_s": STALL_S}},
    )
    fleet.start()
    try:
        takeoff_id = fleet.airborne()
        # Drone 2 leads the translation; its node acknowledges executing, then stalls.
        translate_id = fleet.send("translate", selection=[1, 2], args={"dx": 1, "dy": 0})
        fleet.console.wait_for(
            "acknowledgement",
            intent_id=translate_id,
            source="adapter",
            drone_id=2,
            status="executing",
        )
        started = time.monotonic()
        estop_id, estop = fleet.run("estop", selection=[])
        estop_elapsed = time.monotonic() - started
        translate = _outcome(fleet.console, translate_id)
        fleet.console.wait_for("state", estop=True)
    finally:
        fleet.stop()

    assert estop["status"] == "completed", estop
    assert estop_elapsed < STALL_S / 2, "the stop did not wait for the stalled goto"
    assert (translate["status"], translate["reason"]) == ("invalidated", PREEMPTED_BY_ESTOP)
    assert translate["command_id"] is None
    # Each node received the stop before any remaining command of the plan: drone 1's
    # goto was never issued, and nothing followed drone 2's in-flight goto but the stop.
    assert fleet.commands_for(1) == [("takeoff", takeoff_id), ("estop", estop_id)]
    assert fleet.commands_for(2) == [
        ("takeoff", takeoff_id),
        ("goto", translate_id),
        ("estop", estop_id),
    ]
    for drone_id in (1, 2):
        assert fleet.node_acks(estop_id, drone_id) == ["accepted", "executing", "completed"]


def test_estop_reaches_responsive_nodes_at_once_while_a_node_stays_silent(
    relay_server: RelayServer,
) -> None:
    fleet = _Fleet(relay_server, {1: {"silent_operations": ("goto", "estop")}, 2: {}})
    fleet.start()
    try:
        takeoff_id = fleet.airborne()
        # dx < 0 makes drone 1 lead; its node swallows the goto, so the plan waits on it.
        translate_id = fleet.send("translate", selection=[1, 2], args={"dx": -1, "dy": 0})
        _wait_until(
            lambda: fleet.command_is_replayable(1, "goto", translate_id),
            what="the goto issued to the silent node",
        )
        started = time.monotonic()
        estop_id = fleet.send("estop", selection=[])
        fleet.console.wait_for(
            "acknowledgement",
            intent_id=estop_id,
            source="adapter",
            drone_id=2,
            status="completed",
        )
        responsive_elapsed = time.monotonic() - started
        estop = _outcome(fleet.console, estop_id)
        translate = _outcome(fleet.console, translate_id)
        fleet.console.wait_for("state", estop=True)
    finally:
        fleet.stop()

    ttl_s = relay_server.runtime.settings.command_ttl_ms / 1000
    assert responsive_elapsed < ttl_s / 4, responsive_elapsed
    assert (estop["status"], estop["reason"], estop["drone_id"]) == ("failed", "adapter_failure", 1)
    assert estop["detail"].startswith("adapter_timeout")
    assert (translate["status"], translate["reason"]) == ("invalidated", PREEMPTED_BY_ESTOP)
    assert fleet.commands_for(1) == [
        ("takeoff", takeoff_id),
        ("goto", translate_id),
        ("estop", estop_id),
    ]
    assert fleet.commands_for(2) == [("takeoff", takeoff_id), ("estop", estop_id)]
    assert fleet.node_acks(estop_id, 1) == []
    assert fleet.node_acks(estop_id, 2) == ["accepted", "executing", "completed"]


def test_hold_preempts_a_running_motion_plan_but_queues_behind_land_all(
    relay_server: RelayServer,
) -> None:
    fleet = _Fleet(
        relay_server,
        {1: {}, 2: {"slow_operations": ("goto", "land"), "slow_ack_delay_s": STALL_S}},
    )
    fleet.start()
    try:
        takeoff_id = fleet.airborne()
        translate_id = fleet.send("translate", selection=[1, 2], args={"dx": 1, "dy": 0})
        fleet.console.wait_for(
            "acknowledgement",
            intent_id=translate_id,
            source="adapter",
            drone_id=2,
            status="executing",
        )
        started = time.monotonic()
        hold_id, hold = fleet.run("hold", selection=[1, 2])
        hold_elapsed = time.monotonic() - started
        translate = _outcome(fleet.console, translate_id)
        # The lanes recover: the next operator plan runs normally.
        home_id, home = fleet.run("come_home", selection=[1, 2])
        # A hold during land_all queues behind the safety plan instead of interrupting it.
        land_id = fleet.send("land_all", selection=[], confirm=True)
        fleet.console.wait_for(
            "acknowledgement", intent_id=land_id, source="adapter", drone_id=2, status="executing"
        )
        late_hold_id, late_hold = fleet.run("hold", selection=[1, 2])
        land = _outcome(fleet.console, land_id)
    finally:
        fleet.stop()

    assert hold["status"] == "completed", hold
    assert hold_elapsed < STALL_S / 2, "the hold did not wait for the stalled goto"
    assert (translate["status"], translate["reason"]) == ("invalidated", PREEMPTED_BY_HOLD)
    assert home["status"] == "completed", home
    assert land["status"] == "completed", land
    assert (late_hold["type"], late_hold["reason"]) == ("refusal", "invalid_state"), late_hold
    assert fleet.commands_for(1) == [
        ("takeoff", takeoff_id),
        ("hover", hold_id),
        ("goto", home_id),
        ("land", land_id),
    ]
    assert fleet.commands_for(2) == [
        ("takeoff", takeoff_id),
        ("goto", translate_id),
        ("hover", hold_id),
        ("goto", home_id),
        ("land", land_id),
    ]
    assert not any(late_hold_id == intent_id for _, intent_id in fleet.commands_for(2))


def test_acknowledgement_timeout_degrades_and_holds_only_the_silent_aircraft(
    relay_server: RelayServer,
) -> None:
    fleet = _Fleet(relay_server, {1: {"silent_operations": ("goto",)}, 2: {}})
    fleet.start()
    try:
        takeoff_id = fleet.airborne()
        # dx > 0 makes drone 2 lead and complete; drone 1's node then swallows its goto.
        translate_id, translate = fleet.run("translate", selection=[1, 2], args={"dx": 1, "dy": 0})
        # Others continue: the fleet still answers a hold once the silent aircraft was held.
        hold_id, hold = fleet.run("hold", selection=[1, 2])
    finally:
        fleet.stop()

    assert (translate["status"], translate["reason"], translate["drone_id"]) == (
        "failed",
        "adapter_timeout",
        1,
    )
    assert hold["status"] == "completed", hold
    assert fleet.commands_for(2) == [
        ("takeoff", takeoff_id),
        ("goto", translate_id),
        ("hover", hold_id),
    ]
    assert fleet.commands_for(1) == [
        ("takeoff", takeoff_id),
        ("goto", translate_id),
        ("hover", translate_id),
        ("hover", hold_id),
    ]
    # The unanswered goto produced no acknowledgements; the best-effort hold did.
    assert fleet.node_acks(translate_id, 1) == ["accepted", "executing", "completed"]


def test_terminal_acknowledgement_after_timeout_resumes_the_original_plan(
    relay_server: RelayServer,
) -> None:
    delay_s = relay_server.runtime.settings.command_ttl_ms / 1000 + 0.4
    fleet = _Fleet(
        relay_server,
        {1: {"slow_operations": ("goto",), "slow_ack_delay_s": delay_s}},
    )
    fleet.start()
    try:
        takeoff_id = fleet.airborne()
        translate_id = fleet.send("translate", selection=[1], args={"dx": 1, "dy": 0})
        waiting = fleet.console.wait_for(
            "acknowledgement",
            intent_id=translate_id,
            source=LIFECYCLE_SOURCE,
            status="executing",
        )
        projected = relay_server.runtime.sessions[SESSION].current_state()["accepted_plan"]
        completed = fleet.console.wait_for(
            "acknowledgement",
            intent_id=translate_id,
            source=LIFECYCLE_SOURCE,
            status="completed",
        )
        _wait_until(lambda: fleet.telemetry(1, "x") == 0.5, what="late completed telemetry")
    finally:
        fleet.stop()

    assert waiting["status"] == "executing"
    assert projected is not None and projected["intent_id"] == translate_id
    assert completed["status"] == "completed"
    assert fleet.commands_for(1) == [("takeoff", takeoff_id), ("goto", translate_id)]
    assert fleet.node_acks(translate_id, 1) == ["accepted", "executing", "completed"]


def test_node_disconnect_mid_plan_refuses_the_rest_as_stale_roster(
    relay_server: RelayServer,
) -> None:
    fleet = _Fleet(
        relay_server,
        {1: {}, 2: {"slow_operations": ("goto",), "slow_ack_delay_s": STALL_S}},
    )
    fleet.start()
    try:
        takeoff_id = fleet.airborne()
        translate_id = fleet.send("translate", selection=[1, 2], args={"dx": 1, "dy": 0})
        fleet.console.wait_for(
            "acknowledgement",
            intent_id=translate_id,
            source="adapter",
            drone_id=2,
            status="executing",
        )
        fleet.nodes[1].stop()  # drone 2's node drops off the wire mid-command
        fleet.console.wait_for("membership", drone_id=2, action="unexpected_loss")
        translate = _outcome(fleet.console, translate_id)
        membership = fleet.drones()[2]["membership"]
    finally:
        fleet.stop()

    assert (translate["type"], translate["reason"]) == ("refusal", "stale_roster"), translate
    assert membership == "disconnected"
    assert fleet.commands_for(1) == [("takeoff", takeoff_id)]
    assert fleet.commands_for(2) == [("takeoff", takeoff_id), ("goto", translate_id)]


def test_roster_change_during_a_plan_refuses_its_next_step_and_holds_the_moved_aircraft(
    relay_server: RelayServer,
) -> None:
    fleet = _Fleet(
        relay_server,
        {1: {}, 2: {"slow_operations": ("goto",), "slow_ack_delay_s": STALL_S}},
    )
    fleet.start()
    newcomer = _node(relay_server, 3)
    try:
        takeoff_id = fleet.airborne()
        roster_before = fleet.roster_version()
        translate_id = fleet.send("translate", selection=[1, 2], args={"dx": 1, "dy": 0})
        fleet.console.wait_for(
            "acknowledgement",
            intent_id=translate_id,
            source="adapter",
            drone_id=2,
            status="executing",
        )
        newcomer.start()  # a third aircraft joins while drone 2's goto is in flight
        _wait_until(lambda: 3 in fleet.drones(), what="the third node to join")
        translate = _outcome(fleet.console, translate_id)
        roster_after = fleet.roster_version()
    finally:
        newcomer.stop()
        fleet.stop()

    assert roster_after > roster_before
    assert (translate["type"], translate["reason"]) == ("refusal", "stale_roster"), translate
    assert fleet.commands_for(1) == [("takeoff", takeoff_id)]
    assert fleet.commands_for(2) == [
        ("takeoff", takeoff_id),
        ("goto", translate_id),
        ("hover", translate_id),
    ]
    assert fleet.node_acks(translate_id, 2)[-3:] == ["accepted", "executing", "completed"]


def test_a_composition_failure_still_latches_the_network_stop(
    relay_server: RelayServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    fleet = _Fleet(relay_server, {1: {}, 2: {}})
    fleet.start()
    original = autonomy_module.build_dispatcher

    def broken(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("injected composition failure")

    try:
        fleet.airborne()
        monkeypatch.setattr(autonomy_module, "build_dispatcher", broken)
        estop_id, estop = fleet.run("estop", selection=[])
        fleet.console.wait_for("state", estop=True)
        monkeypatch.setattr(autonomy_module, "build_dispatcher", original)
        _, translate = fleet.run("translate", selection=[1, 2], args={"dx": 1, "dy": 0})
    finally:
        fleet.stop()

    assert (estop["status"], estop["reason"]) == ("failed", "adapter_failure"), estop
    assert (translate["type"], translate["reason"]) == ("refusal", "estop_active"), translate
    assert not any(intent_id == estop_id for _, intent_id in fleet.commands_for(1))
    assert {operation for operation, _ in fleet.commands_for(1) + fleet.commands_for(2)} == {
        "takeoff"
    }


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
