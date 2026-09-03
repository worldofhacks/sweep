from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from adapters.sim.runtime import SimBridgeFactory, create_m14_sim_app
from planner.models import FlightState
from relay.auth import sign_event
from relay.settings import RelaySettings
from tests.autonomy_fixtures import make_snapshot, replace_aircraft

SESSION = "m14-button-sim"
CONSOLE_KEY = b"m14-console-key-that-is-at-least-32-bytes"
ADAPTER_KEYS = {
    1: b"m14-adapter-one-key-at-least-32-bytes",
    2: b"m14-adapter-two-key-at-least-32-bytes",
}


@dataclass(slots=True)
class Clock:
    value: int = 1_756_700_000_000

    def __call__(self) -> int:
        return self.value

    def advance(self, milliseconds: int) -> None:
        self.value += milliseconds


class EventIds:
    def __init__(self) -> None:
        self.value = 0

    def __call__(self) -> str:
        self.value += 1
        return f"m14-event-{self.value}"


class Harness:
    def __init__(self, tmp_path: Path, snapshot, *, auto_start_nodes: bool = False) -> None:  # type: ignore[no-untyped-def]
        self.clock = Clock()
        self.event_ids = EventIds()
        settings = RelaySettings(
            relay_token=CONSOLE_KEY,
            adapter_keys=ADAPTER_KEYS,
            log_dir=tmp_path,
        )

        self.app = create_m14_sim_app(
            settings,
            clock=self.clock,
            event_ids=self.event_ids,
            initial_snapshot=snapshot,
            auto_start_nodes=auto_start_nodes,
        )
        self.sequence = 0

    @property
    def factory(self) -> SimBridgeFactory:
        return self.app.state.sim_bridge_factory

    @property
    def flight(self):  # type: ignore[no-untyped-def]
        return self.factory.flights[SESSION]

    def next_id(self, prefix: str) -> str:
        self.sequence += 1
        return f"{prefix}-{self.sequence}"

    def intent(
        self,
        name: str,
        *,
        selection: list[int],
        args: dict[str, object] | None = None,
        confirm: bool = False,
        source: str = "console",
    ) -> dict[str, object]:
        return {
            "v": 1,
            "t": self.clock(),
            "type": "intent",
            "intent_id": self.next_id(f"intent-{name}"),
            "retry_of": None,
            "source": source,
            "session": SESSION,
            "name": name,
            "args": args or {},
            "selection": selection,
            "mode": "indoor",
            "confirm": confirm,
        }

    def membership(self, drone_id: int, action: str) -> dict[str, object]:
        event: dict[str, object] = {
            "v": 1,
            "t": self.clock(),
            "type": "membership",
            "event_id": self.next_id(f"membership-{drone_id}-{action}"),
            "session": SESSION,
            "drone_id": drone_id,
            "action": action,
        }
        if action == "join":
            event.update(adapter_id=f"sim-{drone_id}", capabilities=["flight"])
        else:
            event.update(
                connection_epoch=self.flight.aircraft[drone_id].connection_epoch,
                home_pose_confirmed=True,
                control_authority=True,
                rc_safety_operator_present=True,
            )
        event["signature"] = sign_event(event, ADAPTER_KEYS[drone_id])
        return event

    def telemetry(self, drone_id: int) -> dict[str, object]:
        aircraft = self.flight.aircraft[drone_id]
        return {
            "v": 1,
            "t": self.clock(),
            "type": "telemetry",
            "event_id": self.next_id(f"telemetry-{drone_id}"),
            "session": SESSION,
            "drone": drone_id,
            "connection_epoch": aircraft.connection_epoch,
            "x": aircraft.pose.x,
            "y": aircraft.pose.y,
            "z": aircraft.pose.z,
            "vx": 0.0,
            "vy": 0.0,
            "vz": 0.0,
            "battery": aircraft.battery,
            "state": aircraft.flight_state.value,
            "link": aircraft.link_quality,
            "pos_quality": aircraft.position_quality,
        }


def test_two_drone_button_mission_has_ordered_jsonl_evidence(tmp_path: Path) -> None:
    initial = make_snapshot(
        2,
        selection=(),
        flight_state=FlightState.DISARMED,
        armed=False,
        now_ms=Clock().value,
    )
    harness = Harness(tmp_path, initial)

    with TestClient(harness.app) as client, ExitStack() as stack:
        console = stack.enter_context(_connect(client, "console"))
        stack.enter_context(_connect(client, "keyboard"))
        adapters = {
            drone_id: stack.enter_context(_connect(client, "adapter", drone_id=drone_id))
            for drone_id in (1, 2)
        }
        _ready_aircraft(harness, adapters)

        mission = [
            harness.intent("arm", selection=[]),
            harness.intent("select", selection=[], args={"ids": [1, 2]}),
            harness.intent("takeoff", selection=[1, 2], confirm=True),
            harness.intent("translate", selection=[1, 2], args={"dx": 1, "dy": 0}),
            harness.intent("hold", selection=[1, 2]),
            harness.intent("come_home", selection=[1, 2]),
            harness.intent("land_all", selection=[], confirm=True),
        ]
        for intent in mission:
            terminal = _send_intent(console, intent)
            assert terminal["status"] == "completed"
            _sync_telemetry(harness, adapters)

        replay = client.get(
            f"/session/{SESSION}",
            headers={"Authorization": f"Bearer {CONSOLE_KEY.decode()}"},
        ).json()

    assert all(
        aircraft.flight_state is FlightState.LANDED for aircraft in harness.flight.aircraft.values()
    )
    records = replay["events"]
    assert [record["seq"] for record in records] == list(range(1, len(records) + 1))
    recorded_intents = [
        record["event"]["intent"]["name"]
        for record in records
        if record["event"]["type"] == "intent_record"
    ]
    assert recorded_intents == [intent["name"] for intent in mission]
    results = [
        record["event"] for record in records if record["event"]["type"] == "autonomy_result"
    ]
    assert [result["status"] for result in results] == ["completed"] * len(mission)
    assert [call.operation.value for call in harness.flight.calls] == [
        "takeoff",
        "takeoff",
        "goto",
        "goto",
        "hover",
        "hover",
        "goto",
        "goto",
        "land",
        "land",
    ]
    assert not _contains_key(records, "signature")
    assert not _contains_key(records, "token")


def test_geofence_refusal_precedes_sim_adapter_io(tmp_path: Path) -> None:
    initial = make_snapshot(2, selection=(), now_ms=Clock().value)
    initial = replace_aircraft(initial, 1, pose=initial.aircraft[1].pose.__class__(9.8, 0.0, 1.0))
    harness = Harness(tmp_path, initial)

    with TestClient(harness.app) as client, ExitStack() as stack:
        console = stack.enter_context(_connect(client, "console"))
        adapters = {
            drone_id: stack.enter_context(_connect(client, "adapter", drone_id=drone_id))
            for drone_id in (1, 2)
        }
        _ready_aircraft(harness, adapters)
        assert _send_intent(console, harness.intent("arm", selection=[]))["status"] == "completed"
        assert (
            _send_intent(
                console,
                harness.intent("select", selection=[], args={"ids": [1]}),
            )["status"]
            == "completed"
        )
        before = list(harness.flight.calls)

        refusal = _send_intent(
            console,
            harness.intent("translate", selection=[1], args={"dx": 1, "dy": 0}),
        )

    assert refusal["type"] == "refusal"
    assert refusal["reason"] == "geofence"
    assert harness.flight.calls == before


def test_keyboard_estop_and_epoch_bound_link_loss_fail_safe(tmp_path: Path) -> None:
    initial = make_snapshot(2, selection=(), now_ms=Clock().value)
    harness = Harness(tmp_path, initial)
    with TestClient(harness.app) as client, ExitStack() as stack:
        stack.enter_context(_connect(client, "console"))
        keyboard = stack.enter_context(_connect(client, "keyboard"))
        adapters = {
            drone_id: stack.enter_context(_connect(client, "adapter", drone_id=drone_id))
            for drone_id in (1, 2)
        }
        _ready_aircraft(harness, adapters)

        terminal = _send_intent(
            keyboard,
            harness.intent("estop", selection=[], source="keyboard"),
        )
        assert terminal["status"] == "completed"
        assert harness.flight.calls[-1].operation.value == "estop"
        assert harness.flight.calls[-1].drone_ids == (1, 2)
        assert harness.app.state.relay_runtime.session(SESSION).current_state()["estop"] is True

        adapters[1].close()
        relay_session = harness.app.state.relay_runtime.session(SESSION)
        loss = _receive_matching(
            keyboard,
            lambda event: (
                event.get("type") == "membership"
                and event.get("action") == "unexpected_loss"
                and event.get("drone_id") == 1
            ),
        )
        harness.clock.advance(2_000)
        adapters[2].send_json(harness.telemetry(2))
        _receive_matching(
            adapters[2],
            lambda event: event.get("type") == "telemetry" and event.get("drone") == 2,
        )
        hold = _receive_matching(
            keyboard,
            lambda event: event.get("type") == "safety_action" and event.get("drone_id") == 1,
        )
        assert hold["action"] == "hold"
        harness.clock.advance(8_000)
        adapters[2].send_json(harness.telemetry(2))
        _receive_matching(
            adapters[2],
            lambda event: event.get("type") == "telemetry" and event.get("drone") == 2,
        )
        failsafe = _receive_matching(
            keyboard,
            lambda event: (
                event.get("type") == "safety_action"
                and event.get("drone_id") == 1
                and event.get("action") == "failsafe"
            ),
        )
        assert failsafe["connection_epoch"] == loss["connection_epoch"]
        replay = relay_session.replay()

    assert harness.flight.aircraft[1].flight_state is FlightState.LANDED
    assert harness.flight.aircraft[1].pose == harness.flight.aircraft[1].home
    assert harness.flight.aircraft[2].flight_state is FlightState.HOVERING
    safety = [
        record["event"] for record in replay["events"] if record["event"]["type"] == "safety_action"
    ]
    assert [event["action"] for event in safety] == ["hold", "failsafe"]
    assert all(event["connection_epoch"] == 1 for event in safety)


def test_open_adapter_socket_link_loss_runs_watchdog_and_rejoin_uses_current_epoch(
    tmp_path: Path,
) -> None:
    initial = make_snapshot(
        2,
        selection=(),
        flight_state=FlightState.DISARMED,
        armed=False,
        now_ms=Clock().value,
    )
    harness = Harness(tmp_path, initial)

    with TestClient(harness.app) as client, ExitStack() as stack:
        console = stack.enter_context(_connect(client, "console"))
        adapter = stack.enter_context(_connect(client, "adapter", drone_id=1))
        _ready_aircraft(harness, {1: adapter})

        assert _send_intent(console, harness.intent("arm", selection=[]))["status"] == "completed"
        assert (
            _send_intent(console, harness.intent("select", selection=[], args={"ids": [1]}))[
                "status"
            ]
            == "completed"
        )
        assert (
            _send_intent(console, harness.intent("takeoff", selection=[1], confirm=True))["status"]
            == "completed"
        )

        # Keep the authenticated WebSocket open, then stop adapter activity.
        harness.clock.advance(2_000)
        hold = _periodic_safety_actions(harness)
        assert any(event["drone_id"] == 1 and event["action"] == "hold" for event in hold)
        assert harness.flight.aircraft[1].flight_state is FlightState.HOVERING

        harness.clock.advance(8_000)
        failsafe = _periodic_safety_actions(harness)
        assert any(event["drone_id"] == 1 and event["action"] == "failsafe" for event in failsafe)
        assert harness.flight.aircraft[1].flight_state is FlightState.LANDED

        adapter.close()
        replacement = stack.enter_context(_connect(client, "adapter", drone_id=1))
        replacement.send_json(harness.membership(1, "join"))
        rejoined = _receive_matching(
            replacement,
            lambda event: (
                event.get("type") == "membership"
                and event.get("action") == "join"
                and event.get("drone_id") == 1
            ),
        )
        assert rejoined["connection_epoch"] == 2
        replacement.send_json(harness.telemetry(1))
        _receive_matching(
            replacement,
            lambda event: event.get("type") == "telemetry" and event.get("drone") == 1,
        )
        replacement.send_json(harness.membership(1, "readiness"))
        _receive_matching(
            replacement,
            lambda event: (
                event.get("type") == "membership"
                and event.get("action") == "readiness"
                and event.get("membership") == "ready"
            ),
        )

        assert (
            _send_intent(console, harness.intent("select", selection=[], args={"ids": [1]}))[
                "status"
            ]
            == "completed"
        )
        resumed = _send_intent(console, harness.intent("takeoff", selection=[1], confirm=True))

    assert resumed["status"] == "completed"
    assert harness.flight.calls[-1].operation.value == "takeoff"
    assert harness.flight.aircraft[1].connection_epoch == 2


def test_deployed_simulator_nodes_stream_and_rejoin_without_stale_epoch_io(tmp_path: Path) -> None:
    initial = make_snapshot(
        2,
        selection=(),
        flight_state=FlightState.DISARMED,
        armed=False,
        now_ms=Clock().value,
    )
    harness = Harness(tmp_path, initial, auto_start_nodes=True)

    with TestClient(harness.app) as client, ExitStack() as stack:
        console = stack.enter_context(_connect(client, "console"))
        state = harness.app.state.relay_runtime.session(SESSION).current_state()
        assert [drone["membership"] for drone in state["drones"]] == ["ready", "ready"]

        assert _send_intent(console, harness.intent("arm", selection=[]))["status"] == "completed"
        assert (
            _send_intent(console, harness.intent("select", selection=[], args={"ids": [1]}))[
                "status"
            ]
            == "completed"
        )
        assert (
            _send_intent(console, harness.intent("takeoff", selection=[1], confirm=True))["status"]
            == "completed"
        )

        harness.factory.silence_node(SESSION, 1)
        harness.clock.advance(2_000)
        hold = _periodic_safety_actions(harness)
        assert any(event.get("drone_id") == 1 and event.get("action") == "hold" for event in hold)
        harness.clock.advance(8_000)
        failsafe = _periodic_safety_actions(harness)
        assert any(
            event.get("drone_id") == 1 and event.get("action") == "failsafe" for event in failsafe
        )

        harness.factory.disconnect_node(SESSION, 1)
        harness.factory.rejoin_node(SESSION, 1)
        assert harness.flight.aircraft[1].connection_epoch == 2
        assert (
            _send_intent(console, harness.intent("select", selection=[], args={"ids": [1]}))[
                "status"
            ]
            == "completed"
        )
        resumed = _send_intent(console, harness.intent("takeoff", selection=[1], confirm=True))
        replay = harness.app.state.relay_runtime.session(SESSION).replay()

    assert resumed["status"] == "completed"
    assert harness.flight.calls[-1].operation.value == "takeoff"
    assert not any(
        event["event"].get("reason") == "stale_connection_epoch" for event in replay["events"]
    )


def _periodic_safety_actions(harness: Harness) -> list[dict[str, object]]:
    session = harness.app.state.relay_runtime.session(SESSION)
    ingress = harness.factory.bridges[SESSION].periodic_ingress()
    relay_events = session.periodic_events()
    state = relay_events[-1]
    return ingress + harness.factory.bridges[SESSION].periodic_events(state)


def _connect(client: TestClient, source: str, *, drone_id: int | None = None):  # type: ignore[no-untyped-def]
    socket = client.websocket_connect(f"/ws/{SESSION}")
    token = CONSOLE_KEY if drone_id is None else ADAPTER_KEYS[drone_id]
    auth: dict[str, Any] = {"v": 1, "type": "auth", "source": source, "token": token.decode()}
    if drone_id is not None:
        auth["drone_id"] = drone_id
    session = socket.__enter__()
    session.send_json(auth)
    assert session.receive_json()["type"] == "auth.accepted"
    assert session.receive_json()["type"] == "state"

    class Connected:
        def __enter__(self):  # type: ignore[no-untyped-def]
            return session

        def __exit__(self, *args):  # type: ignore[no-untyped-def]
            return socket.__exit__(*args)

    return Connected()


def _ready_aircraft(harness: Harness, adapters: dict[int, Any]) -> None:
    for drone_id, socket in adapters.items():
        socket.send_json(harness.membership(drone_id, "join"))
        _receive_matching(
            socket,
            lambda event, drone_id=drone_id: (
                event.get("type") == "membership"
                and event.get("action") == "join"
                and event.get("drone_id") == drone_id
            ),
        )
        socket.send_json(harness.telemetry(drone_id))
        _receive_matching(
            socket,
            lambda event, drone_id=drone_id: (
                event.get("type") == "telemetry" and event.get("drone") == drone_id
            ),
        )
        socket.send_json(harness.membership(drone_id, "readiness"))
        ready = _receive_matching(
            socket,
            lambda event, drone_id=drone_id: (
                event.get("type") == "membership"
                and event.get("action") == "readiness"
                and event.get("drone_id") == drone_id
            ),
        )
        assert ready["membership"] == "ready"


def _sync_telemetry(harness: Harness, adapters: dict[int, Any]) -> None:
    for drone_id, socket in adapters.items():
        socket.send_json(harness.telemetry(drone_id))
        _receive_matching(
            socket,
            lambda event, drone_id=drone_id: (
                event.get("type") == "telemetry" and event.get("drone") == drone_id
            ),
        )


def _send_intent(socket, intent: dict[str, object]) -> dict[str, object]:  # type: ignore[no-untyped-def]
    socket.send_json(intent)
    intent_id = intent["intent_id"]
    return _receive_matching(
        socket,
        lambda event: (
            event.get("intent_id") == intent_id
            and (
                event.get("type") == "refusal"
                or event.get("status") in {"completed", "failed", "invalidated", "refused"}
            )
        ),
    )


def _receive_matching(socket, predicate):  # type: ignore[no-untyped-def]
    for _ in range(200):
        event = socket.receive_json()
        if predicate(event):
            return event
    raise AssertionError("matching relay event was not received")


def _contains_key(value: object, target: str) -> bool:
    if isinstance(value, dict):
        return target in value or any(_contains_key(item, target) for item in value.values())
    if isinstance(value, list):
        return any(_contains_key(item, target) for item in value)
    return False
