from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Thread
from time import monotonic, sleep
from typing import Any

import pytest
from fastapi.testclient import TestClient

from adapters.sim.runtime import SimBridgeFactory, create_m14_sim_app
from planner.models import FlightState
from relay.auth import Principal, sign_event
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

        profile = harness.factory.capability_profile
        runtime = harness.app.state.relay_runtime
        session = runtime.session(SESSION)
        bridge = harness.factory.bridges[SESSION]
        assert runtime.capability_profile is profile
        assert session.capability_profile is profile
        assert bridge.capability_profile is profile
        assert bridge.controller.planner.capability_profile is profile
        assert "altitude" not in session.current_state()["enabled_intent_names"]

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
            harness.clock.advance(501)
            intent["t"] = harness.clock()
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
        record["event"]
        for record in replay["events"]
        if record["event"]["type"] == "safety_action" and record["event"]["drone_id"] == 1
    ]
    assert [event["action"] for event in safety] == ["hold", "failsafe"]
    assert all(event["connection_epoch"] == 1 for event in safety)


@pytest.mark.parametrize(
    ("name", "selection", "args", "confirm", "operation"),
    [
        ("hold", [1, 2], {}, False, "hover"),
        ("land_all", [], {}, True, "land"),
    ],
)
def test_delivered_recovery_action_survives_undelivered_estop(
    tmp_path: Path,
    name: str,
    selection: list[int],
    args: dict[str, object],
    confirm: bool,
    operation: str,
) -> None:
    harness = Harness(
        tmp_path,
        make_snapshot(2, selection=(1, 2), now_ms=Clock().value),
        auto_start_nodes=False,
    )
    with TestClient(harness.app) as client, ExitStack() as stack:
        console = stack.enter_context(_connect(client, "console"))
        adapters = {
            drone_id: stack.enter_context(_connect(client, "adapter", drone_id=drone_id))
            for drone_id in (1, 2)
        }
        _ready_aircraft(harness, adapters)
        assert (
            _send_intent(
                console,
                harness.intent("select", selection=[], args={"ids": [1, 2]}),
            )["status"]
            == "completed"
        )
        session = harness.app.state.relay_runtime.session(SESSION)
        recovery = harness.intent(name, selection=selection, args=args, confirm=confirm)
        session.process_intent(recovery, Principal("console", None, CONSOLE_KEY))
        harness.clock.advance(100)
        estop = harness.intent("estop", selection=[], source="keyboard")
        session.process_intent(estop, Principal("keyboard", None, CONSOLE_KEY))

        session.mark_pending_intent_delivered(recovery["intent_id"])
        completed = session.execute_pending_intent(recovery["intent_id"])
        session.fail_pending_intent(
            estop["intent_id"],
            reason="acceptance_delivery_failed",
            detail="the acknowledgement socket closed before delivery",
        )

    assert completed[-1]["status"] == "completed"
    assert any(call.operation.value == operation for call in harness.flight.calls)
    assert session.current_state()["estop"] is False


def test_delivered_hold_tombstones_older_undelivered_motion(tmp_path: Path) -> None:
    harness = Harness(
        tmp_path,
        make_snapshot(2, selection=(1, 2), now_ms=Clock().value),
        auto_start_nodes=False,
    )
    with TestClient(harness.app) as client, ExitStack() as stack:
        console = stack.enter_context(_connect(client, "console"))
        adapters = {
            drone_id: stack.enter_context(_connect(client, "adapter", drone_id=drone_id))
            for drone_id in (1, 2)
        }
        _ready_aircraft(harness, adapters)
        assert (
            _send_intent(
                console,
                harness.intent("select", selection=[], args={"ids": [1, 2]}),
            )["status"]
            == "completed"
        )
        session = harness.app.state.relay_runtime.session(SESSION)
        motion = harness.intent("translate", selection=[1, 2], args={"dx": 0.5, "dy": 0})
        session.process_intent(motion, Principal("console", None, CONSOLE_KEY))
        harness.clock.advance(600)
        hold = harness.intent("hold", selection=[1, 2])
        session.process_intent(hold, Principal("console", None, CONSOLE_KEY))

        session.mark_pending_intent_delivered(hold["intent_id"])
        hold_events = session.execute_pending_intent(hold["intent_id"])
        session.mark_pending_intent_delivered(motion["intent_id"])
        motion_events = session.execute_pending_intent(motion["intent_id"])

    assert hold_events[-1]["status"] == "completed"
    assert motion_events[-1]["status"] == "invalidated"
    assert [call.operation.value for call in harness.flight.calls] == ["hover", "hover"]


@pytest.mark.parametrize(
    ("coordinator_index", "deliver_all_before_execution"),
    [(index, delivered) for index in range(3) for delivered in (False, True)],
)
def test_chained_motion_conflict_is_independent_of_delivery_coordinator(
    tmp_path: Path, coordinator_index: int, deliver_all_before_execution: bool
) -> None:
    harness = Harness(
        tmp_path,
        make_snapshot(2, selection=(1, 2), now_ms=Clock().value),
        auto_start_nodes=False,
    )
    with TestClient(harness.app) as client, ExitStack() as stack:
        console = stack.enter_context(_connect(client, "console"))
        adapters = {
            drone_id: stack.enter_context(_connect(client, "adapter", drone_id=drone_id))
            for drone_id in (1, 2)
        }
        _ready_aircraft(harness, adapters)
        assert (
            _send_intent(
                console,
                harness.intent("select", selection=[], args={"ids": [1, 2]}),
            )["status"]
            == "completed"
        )
        session = harness.app.state.relay_runtime.session(SESSION)
        principal = Principal(source="console", drone_id=None, signing_key=CONSOLE_KEY)
        motions = []
        for dx, dy in ((1, 0), (0, 1), (-1, 0)):
            motion = harness.intent("translate", selection=[1, 2], args={"dx": dx, "dy": dy})
            assert session.process_frame(motion, principal)[0]["status"] == "accepted"
            motions.append(motion)
            if len(motions) < 3:
                harness.clock.advance(400)

        coordinator = motions[coordinator_index]
        delivered = motions if deliver_all_before_execution else (coordinator,)
        for motion in delivered:
            session.mark_pending_intent_delivered(motion["intent_id"])
        outcomes = {
            coordinator["intent_id"]: session.execute_pending_intent(coordinator["intent_id"])
        }
        for motion in motions:
            if motion is coordinator:
                continue
            session.mark_pending_intent_delivered(motion["intent_id"])
            outcomes[motion["intent_id"]] = session.execute_pending_intent(motion["intent_id"])

    assert {events[-1]["reason"] for events in outcomes.values()} == {"conflicting_motion"}
    assert all(call.operation.value != "goto" for call in harness.flight.calls)
    assert [call.operation.value for call in harness.flight.calls] == ["hover", "hover"]


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
        _wait_for_safety_audit(harness, 1, "hold")
        assert harness.flight.aircraft[1].flight_state is FlightState.HOVERING

        harness.clock.advance(8_000)
        _wait_for_safety_audit(harness, 1, "failsafe")
        assert harness.flight.aircraft[1].flight_state is FlightState.LANDED

        replay = harness.app.state.relay_runtime.session(SESSION).replay()
        audited_safety = [
            record["event"]
            for record in replay["events"]
            if record["event"].get("type") == "safety_action"
            and record["event"].get("drone_id") == 1
        ]
        assert [event["action"] for event in audited_safety] == ["hold", "failsafe"]

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
        harness.clock.advance(501)
        state_after_takeoff = harness.app.state.relay_runtime.session(SESSION).current_state()
        assert state_after_takeoff["drones"][0]["flight_state"] == "hovering"
        translated = _send_intent(
            console,
            harness.intent("translate", selection=[1], args={"dx": 1, "dy": 0}),
        )
        assert translated["status"] == "completed"
        assert harness.flight.aircraft[1].pose.x == 0.5

        harness.factory.silence_node(SESSION, 1)
        harness.clock.advance(2_000)
        _wait_for_safety_audit(harness, 1, "hold")
        harness.clock.advance(8_000)
        _wait_for_safety_audit(harness, 1, "failsafe")

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


def test_simulator_link_controls_require_console_authority(tmp_path: Path) -> None:
    harness = Harness(tmp_path, make_snapshot(1), auto_start_nodes=True)

    with TestClient(harness.app) as client, _connect(client, "console"):
        path = f"/sim/{SESSION}/nodes/1/silence"
        assert client.post(path).status_code == 401
        response = client.post(
            path,
            headers={"Authorization": f"Bearer {CONSOLE_KEY.decode()}"},
        )

    assert response.status_code == 200
    assert response.json() == {"status": "silenced", "session": SESSION, "drone_id": 1}


def test_production_bridge_refuses_concurrent_motion_and_holds_the_fleet(tmp_path: Path) -> None:
    initial = make_snapshot(
        2,
        selection=(),
        flight_state=FlightState.HOVERING,
        armed=True,
        now_ms=Clock().value,
    )
    harness = Harness(tmp_path, initial, auto_start_nodes=True)

    with TestClient(harness.app) as client:
        with _connect(client, "console") as console:
            selected = _send_intent(
                console,
                harness.intent("select", selection=[], args={"ids": [1, 2]}),
            )
            assert selected["status"] == "completed"

        session = harness.app.state.relay_runtime.session(SESSION)
        principal = Principal(source="console", drone_id=None, signing_key=CONSOLE_KEY)
        first = harness.intent("translate", selection=[1, 2], args={"dx": 1, "dy": 0})
        second = harness.intent("translate", selection=[1, 2], args={"dx": 0, "dy": 1})
        assert session.process_intent(first, principal)[0]["status"] == "accepted"
        assert session.process_intent(second, principal)[0]["status"] == "accepted"
        results: dict[str, list[dict[str, object]]] = {}

        threads = [
            Thread(
                target=lambda intent_id=intent_id: results.setdefault(
                    intent_id, session.execute_pending_intent(intent_id)
                )
            )
            for intent_id in (first["intent_id"], second["intent_id"])
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)

    assert all(not thread.is_alive() for thread in threads)
    assert {events[-1]["reason"] for events in results.values()} == {"conflicting_motion"}
    assert all(call.operation.value != "goto" for call in harness.flight.calls)
    assert [call.operation.value for call in harness.flight.calls[-2:]] == ["hover", "hover"]
    assert [call.drone_ids for call in harness.flight.calls[-2:]] == [(1,), (2,)]


def test_simulator_node_watchdog_holds_then_failsafes_without_relay_fanout(
    tmp_path: Path,
) -> None:
    initial = make_snapshot(
        2,
        selection=(),
        flight_state=FlightState.AIRBORNE,
        armed=True,
        now_ms=Clock().value,
    )
    harness = Harness(tmp_path, initial, auto_start_nodes=True)

    with TestClient(harness.app) as client:
        with _connect(client, "console"):
            pass
        runtime = harness.app.state.relay_runtime
        assert client.portal is not None
        client.portal.call(runtime.stop)
        harness.factory.silence_node(SESSION, 1)

        harness.clock.advance(2_000)
        _wait_for_flight_state(harness, 1, FlightState.HOVERING)
        harness.clock.advance(8_000)
        _wait_for_flight_state(harness, 1, FlightState.LANDED)


def test_estop_cannot_be_overwritten_by_an_already_running_motion(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    initial = make_snapshot(
        1,
        selection=(),
        flight_state=FlightState.DISARMED,
        armed=False,
        now_ms=Clock().value,
    )
    harness = Harness(tmp_path, initial, auto_start_nodes=True)
    entered = Event()
    estop_entered = Event()
    release = Event()

    with TestClient(harness.app) as client, _connect(client, "console") as console:
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
        harness.clock.advance(501)

        original_goto = harness.flight.goto

        def delayed_goto(*args, **kwargs):  # type: ignore[no-untyped-def]
            entered.set()
            assert release.wait(timeout=2)
            return original_goto(*args, **kwargs)

        original_estop = harness.flight.estop

        def observed_estop(*args, **kwargs):  # type: ignore[no-untyped-def]
            estop_entered.set()
            return original_estop(*args, **kwargs)

        monkeypatch.setattr(harness.flight, "goto", delayed_goto)
        monkeypatch.setattr(harness.flight, "estop", observed_estop)
        outcomes: dict[str, dict[str, object]] = {}
        motion = harness.intent("translate", selection=[1], args={"dx": 1, "dy": 0})
        motion_thread = Thread(
            target=lambda: outcomes.setdefault("motion", _send_intent(console, motion))
        )
        motion_thread.start()
        assert entered.wait(timeout=1)

        with _connect(client, "keyboard") as keyboard:
            stopped = _send_intent(
                keyboard,
                harness.intent("estop", selection=[], source="keyboard"),
            )
        assert estop_entered.is_set()
        release.set()
        motion_thread.join(timeout=2)

    assert stopped["status"] == "completed"
    assert outcomes["motion"]["status"] != "completed"
    assert harness.flight.aircraft[1].pose.x == 0.0


def test_land_all_remains_available_after_completed_estop(tmp_path: Path) -> None:
    initial = make_snapshot(
        1,
        selection=(),
        flight_state=FlightState.DISARMED,
        armed=False,
        now_ms=Clock().value,
    )
    harness = Harness(tmp_path, initial, auto_start_nodes=True)

    with TestClient(harness.app) as client, _connect(client, "console") as console:
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
        with _connect(client, "keyboard") as keyboard:
            stopped = _send_intent(
                keyboard,
                harness.intent("estop", selection=[], source="keyboard"),
            )
        harness.clock.advance(501)
        landed = _send_intent(
            console,
            harness.intent("land_all", selection=[], confirm=True),
        )

    assert stopped["status"] == "completed"
    assert landed["status"] == "completed"
    assert harness.flight.aircraft[1].flight_state is FlightState.LANDED
    assert harness.flight.aircraft[1].armed is False


def test_node_failsafe_cannot_be_overwritten_by_an_already_running_motion(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    initial = make_snapshot(
        1,
        selection=(),
        flight_state=FlightState.DISARMED,
        armed=False,
        now_ms=Clock().value,
    )
    harness = Harness(tmp_path, initial, auto_start_nodes=True)
    entered = Event()
    release = Event()

    with TestClient(harness.app) as client, _connect(client, "console") as console:
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
        harness.clock.advance(501)
        original_goto = harness.flight.goto

        def delayed_goto(*args, **kwargs):  # type: ignore[no-untyped-def]
            entered.set()
            assert release.wait(timeout=2)
            return original_goto(*args, **kwargs)

        monkeypatch.setattr(harness.flight, "goto", delayed_goto)
        outcome: dict[str, dict[str, object]] = {}
        motion = harness.intent("translate", selection=[1], args={"dx": 1, "dy": 0})
        motion_thread = Thread(
            target=lambda: outcome.setdefault("motion", _send_intent(console, motion))
        )
        motion_thread.start()
        assert entered.wait(timeout=1)
        harness.factory.silence_node(SESSION, 1)
        harness.clock.advance(10_000)
        _wait_for_flight_state(harness, 1, FlightState.LANDED)
        release.set()
        motion_thread.join(timeout=2)

    assert outcome["motion"]["status"] != "completed"
    assert harness.flight.aircraft[1].flight_state is FlightState.LANDED
    assert harness.flight.aircraft[1].pose == harness.flight.aircraft[1].home


def test_prior_epoch_motion_cannot_resume_after_estop_and_rejoin(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    initial = make_snapshot(
        1,
        selection=(),
        flight_state=FlightState.DISARMED,
        armed=False,
        now_ms=Clock().value,
    )
    harness = Harness(tmp_path, initial, auto_start_nodes=True)
    entered = Event()
    release = Event()

    with TestClient(harness.app) as client, _connect(client, "console") as console:
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
        harness.clock.advance(501)
        original_goto = harness.flight.goto

        def delayed_goto(*args, **kwargs):  # type: ignore[no-untyped-def]
            entered.set()
            assert release.wait(timeout=2)
            return original_goto(*args, **kwargs)

        monkeypatch.setattr(harness.flight, "goto", delayed_goto)
        outcome: dict[str, dict[str, object]] = {}
        motion = harness.intent("translate", selection=[1], args={"dx": 1, "dy": 0})
        motion_thread = Thread(
            target=lambda: outcome.setdefault("motion", _send_intent(console, motion))
        )
        motion_thread.start()
        assert entered.wait(timeout=1)

        with _connect(client, "keyboard") as keyboard:
            stopped = _send_intent(
                keyboard,
                harness.intent("estop", selection=[], source="keyboard"),
            )
        harness.factory.disconnect_node(SESSION, 1)
        rejoin = Thread(target=lambda: harness.factory.rejoin_node(SESSION, 1))
        rejoin.start()
        release.set()
        motion_thread.join(timeout=2)
        rejoin.join(timeout=2)

    assert stopped["status"] == "completed"
    assert outcome["motion"]["status"] != "completed"
    assert harness.flight.aircraft[1].connection_epoch == 2
    assert harness.flight.aircraft[1].pose.x == 0.0


def test_back_to_back_websocket_motion_frames_trigger_conflict_hold(tmp_path: Path) -> None:
    initial = make_snapshot(2, selection=(), now_ms=Clock().value)
    harness = Harness(tmp_path, initial, auto_start_nodes=True)

    with TestClient(harness.app) as client, _connect(client, "console") as console:
        assert _send_intent(console, harness.intent("arm", selection=[]))["status"] == "completed"
        assert (
            _send_intent(console, harness.intent("select", selection=[], args={"ids": [1, 2]}))[
                "status"
            ]
            == "completed"
        )
        first = harness.intent("translate", selection=[1, 2], args={"dx": 1, "dy": 0})
        second = harness.intent("translate", selection=[1, 2], args={"dx": 0, "dy": 1})
        console.send_json(first)
        console.send_json(second)
        terminals = {
            event["intent_id"]: event
            for event in (
                _receive_matching(
                    console,
                    lambda item: (
                        item.get("intent_id") in {first["intent_id"], second["intent_id"]}
                        and item.get("type") == "refusal"
                    ),
                ),
                _receive_matching(
                    console,
                    lambda item: (
                        item.get("intent_id") in {first["intent_id"], second["intent_id"]}
                        and item.get("type") == "refusal"
                    ),
                ),
            )
        }

    assert set(terminals) == {first["intent_id"], second["intent_id"]}
    assert {event["reason"] for event in terminals.values()} == {"conflicting_motion"}
    assert all(call.operation.value != "goto" for call in harness.flight.calls)


def test_three_motion_burst_refuses_every_intent_and_holds_once(tmp_path: Path) -> None:
    initial = make_snapshot(2, selection=(), now_ms=Clock().value)
    harness = Harness(tmp_path, initial, auto_start_nodes=True)

    with TestClient(harness.app) as client, _connect(client, "console") as console:
        assert _send_intent(console, harness.intent("arm", selection=[]))["status"] == "completed"
        assert (
            _send_intent(console, harness.intent("select", selection=[], args={"ids": [1, 2]}))[
                "status"
            ]
            == "completed"
        )
        motions = [
            harness.intent("translate", selection=[1, 2], args={"dx": dx, "dy": dy})
            for dx, dy in ((1, 0), (0, 1), (-1, 0))
        ]
        for motion in motions:
            console.send_json(motion)
        terminals = [
            _receive_matching(
                console,
                lambda item: (
                    item.get("intent_id") in {motion["intent_id"] for motion in motions}
                    and item.get("type") == "refusal"
                ),
            )
            for _ in motions
        ]

    assert {event["intent_id"] for event in terminals} == {
        motion["intent_id"] for motion in motions
    }
    assert {event["reason"] for event in terminals} == {"conflicting_motion"}
    assert all(call.operation.value != "goto" for call in harness.flight.calls)
    assert [call.operation.value for call in harness.flight.calls].count("hover") == 2


def test_hold_supersedes_timestamp_conflicting_motion_before_adapter_io(tmp_path: Path) -> None:
    initial = make_snapshot(1, selection=(), now_ms=Clock().value)
    harness = Harness(tmp_path, initial, auto_start_nodes=True)

    with TestClient(harness.app) as client, _connect(client, "console") as console:
        assert _send_intent(console, harness.intent("arm", selection=[]))["status"] == "completed"
        assert (
            _send_intent(console, harness.intent("select", selection=[], args={"ids": [1]}))[
                "status"
            ]
            == "completed"
        )
        motion = harness.intent("translate", selection=[1], args={"dx": 1, "dy": 0})
        hold = harness.intent("hold", selection=[1])
        console.send_json(motion)
        console.send_json(hold)
        terminals = {
            event["intent_id"]: event
            for event in (
                _receive_matching(
                    console,
                    lambda item: (
                        item.get("intent_id") in {motion["intent_id"], hold["intent_id"]}
                        and item.get("status") in {"completed", "invalidated"}
                    ),
                ),
                _receive_matching(
                    console,
                    lambda item: (
                        item.get("intent_id") in {motion["intent_id"], hold["intent_id"]}
                        and item.get("status") in {"completed", "invalidated"}
                    ),
                ),
            )
        }

    assert terminals[motion["intent_id"]]["status"] == "invalidated"
    assert terminals[hold["intent_id"]]["status"] == "completed"
    assert all(call.operation.value != "goto" for call in harness.flight.calls)


def test_conflict_uses_admission_time_even_when_second_acceptance_delivery_is_delayed(
    tmp_path: Path,
) -> None:
    initial = make_snapshot(1, selection=(1,), now_ms=Clock().value)
    harness = Harness(tmp_path, initial, auto_start_nodes=True)

    with TestClient(harness.app):
        session = harness.app.state.relay_runtime.session(SESSION)
        principal = Principal(source="console", drone_id=None, signing_key=CONSOLE_KEY)
        first = harness.intent("translate", selection=[1], args={"dx": 1, "dy": 0})
        second = harness.intent("translate", selection=[1], args={"dx": 0, "dy": 1})
        assert session.process_frame(first, principal)[0]["status"] == "accepted"
        assert session.process_frame(second, principal)[0]["status"] == "accepted"
        session.mark_pending_intent_delivered(first["intent_id"])
        outcomes: dict[str, list[dict[str, object]]] = {}
        first_execution = Thread(
            target=lambda: outcomes.setdefault(
                "first", session.execute_pending_intent(first["intent_id"])
            )
        )
        first_execution.start()
        first_execution.join(timeout=2)
        assert not first_execution.is_alive()

        session.mark_pending_intent_delivered(second["intent_id"])
        second_execution = Thread(
            target=lambda: outcomes.setdefault(
                "second", session.execute_pending_intent(second["intent_id"])
            )
        )
        second_execution.start()
        first_execution.join(timeout=2)
        second_execution.join(timeout=2)

    assert not first_execution.is_alive() and not second_execution.is_alive()
    assert outcomes["first"][-1]["reason"] == "conflicting_motion"
    assert outcomes["second"][-1]["reason"] == "conflicting_motion"
    assert all(call.operation.value != "goto" for call in harness.flight.calls)


def test_delivered_hold_does_not_wait_for_an_undelivered_motion(tmp_path: Path) -> None:
    initial = make_snapshot(
        1,
        selection=(1,),
        flight_state=FlightState.HOVERING,
        armed=True,
        now_ms=Clock().value,
    )
    harness = Harness(tmp_path, initial, auto_start_nodes=True)

    with TestClient(harness.app):
        session = harness.app.state.relay_runtime.session(SESSION)
        principal = Principal(source="console", drone_id=None, signing_key=CONSOLE_KEY)
        arm = harness.intent("arm", selection=[])
        assert session.process_frame(arm, principal)[0]["status"] == "accepted"
        session.mark_pending_intent_delivered(arm["intent_id"])
        assert session.execute_pending_intent(arm["intent_id"])[-1]["status"] == "completed"
        selection = harness.intent("select", selection=[], args={"ids": [1]})
        assert session.process_frame(selection, principal)[0]["status"] == "accepted"
        session.mark_pending_intent_delivered(selection["intent_id"])
        assert session.execute_pending_intent(selection["intent_id"])[-1]["status"] == "completed"
        motion = harness.intent("translate", selection=[1], args={"dx": 1, "dy": 0})
        hold = harness.intent("hold", selection=[1])
        assert session.process_frame(motion, principal)[0]["status"] == "accepted"
        assert session.process_frame(hold, principal)[0]["status"] == "accepted"
        session.mark_pending_intent_delivered(hold["intent_id"])

        hold_events = session.execute_pending_intent(hold["intent_id"])
        session.mark_pending_intent_delivered(motion["intent_id"])
        motion_events = session.execute_pending_intent(motion["intent_id"])

    assert hold_events[-1]["status"] == "completed"
    assert motion_events[-1]["status"] == "invalidated"
    assert all(call.operation.value != "goto" for call in harness.flight.calls)


def test_estop_admission_preempts_motion_before_its_acceptance_delivery(tmp_path: Path) -> None:
    initial = make_snapshot(
        1,
        selection=(1,),
        flight_state=FlightState.HOVERING,
        armed=True,
        now_ms=Clock().value,
    )
    harness = Harness(tmp_path, initial, auto_start_nodes=True)

    with TestClient(harness.app):
        session = harness.app.state.relay_runtime.session(SESSION)
        console = Principal(source="console", drone_id=None, signing_key=CONSOLE_KEY)
        keyboard = Principal(source="keyboard", drone_id=None, signing_key=CONSOLE_KEY)
        selection = harness.intent("select", selection=[], args={"ids": [1]})
        assert session.process_frame(selection, console)[0]["status"] == "accepted"
        session.mark_pending_intent_delivered(selection["intent_id"])
        assert session.execute_pending_intent(selection["intent_id"])[-1]["status"] == "completed"
        motion = harness.intent("translate", selection=[1], args={"dx": 1, "dy": 0})
        estop = harness.intent("estop", selection=[], source="keyboard")
        assert session.process_frame(motion, console)[0]["status"] == "accepted"
        assert session.process_frame(estop, keyboard)[0]["status"] == "accepted"
        session.mark_pending_intent_delivered(motion["intent_id"])

        motion_events = session.execute_pending_intent(motion["intent_id"])
        session.mark_pending_intent_delivered(estop["intent_id"])
        estop_events = session.execute_pending_intent(estop["intent_id"])

    assert motion_events[-1]["status"] == "invalidated"
    assert estop_events[-1]["status"] == "completed"
    assert all(call.operation.value != "goto" for call in harness.flight.calls)


def test_select_coordinator_cannot_dispatch_motion_near_undelivered_estop(tmp_path: Path) -> None:
    initial = make_snapshot(
        1,
        selection=(1,),
        flight_state=FlightState.HOVERING,
        armed=True,
        now_ms=Clock().value,
    )
    harness = Harness(tmp_path, initial, auto_start_nodes=True)

    with TestClient(harness.app):
        session = harness.app.state.relay_runtime.session(SESSION)
        console = Principal(source="console", drone_id=None, signing_key=CONSOLE_KEY)
        keyboard = Principal(source="keyboard", drone_id=None, signing_key=CONSOLE_KEY)
        selection = harness.intent("select", selection=[], args={"ids": [1]})
        assert session.process_frame(selection, console)[0]["status"] == "accepted"
        session.mark_pending_intent_delivered(selection["intent_id"])
        assert session.execute_pending_intent(selection["intent_id"])[-1]["status"] == "completed"
        early = harness.intent("select", selection=[1], args={"ids": [1]})
        assert session.process_frame(early, console)[0]["status"] == "accepted"
        session.mark_pending_intent_delivered(early["intent_id"])
        harness.clock.advance(300)
        motion = harness.intent("translate", selection=[1], args={"dx": 1, "dy": 0})
        harness.clock.advance(400)
        estop = harness.intent("estop", selection=[], source="keyboard")
        assert session.process_frame(motion, console)[0]["status"] == "accepted"
        assert session.process_frame(estop, keyboard)[0]["status"] == "accepted"
        session.mark_pending_intent_delivered(motion["intent_id"])

        early_events = session.execute_pending_intent(early["intent_id"])
        motion_events = session.execute_pending_intent(motion["intent_id"])
        session.mark_pending_intent_delivered(estop["intent_id"])
        estop_events = session.execute_pending_intent(estop["intent_id"])

    assert early_events[-1]["status"] == "completed"
    assert motion_events[-1]["status"] == "invalidated"
    assert estop_events[-1]["status"] == "completed"
    assert all(call.operation.value != "goto" for call in harness.flight.calls)


def test_out_of_window_coordinator_seed_cannot_orphan_an_earlier_intent(tmp_path: Path) -> None:
    initial = make_snapshot(
        1,
        selection=(1,),
        flight_state=FlightState.HOVERING,
        armed=True,
        now_ms=Clock().value,
    )
    harness = Harness(tmp_path, initial, auto_start_nodes=True)

    with TestClient(harness.app):
        session = harness.app.state.relay_runtime.session(SESSION)
        principal = Principal(source="console", drone_id=None, signing_key=CONSOLE_KEY)
        arm = harness.intent("arm", selection=[])
        assert session.process_frame(arm, principal)[0]["status"] == "accepted"
        session.mark_pending_intent_delivered(arm["intent_id"])
        assert session.execute_pending_intent(arm["intent_id"])[-1]["status"] == "completed"
        selection = harness.intent("select", selection=[], args={"ids": [1]})
        assert session.process_frame(selection, principal)[0]["status"] == "accepted"
        session.mark_pending_intent_delivered(selection["intent_id"])
        assert session.execute_pending_intent(selection["intent_id"])[-1]["status"] == "completed"
        earlier = harness.intent("translate", selection=[1], args={"dx": 1, "dy": 0})
        harness.clock.advance(501)
        later = harness.intent("translate", selection=[1], args={"dx": 0, "dy": 1})
        assert session.process_frame(earlier, principal)[0]["status"] == "accepted"
        assert session.process_frame(later, principal)[0]["status"] == "accepted"
        session.mark_pending_intent_delivered(earlier["intent_id"])
        session.mark_pending_intent_delivered(later["intent_id"])
        outcomes: dict[str, list[dict[str, object]]] = {}
        later_thread = Thread(
            target=lambda: outcomes.setdefault(
                "later", session.execute_pending_intent(later["intent_id"])
            )
        )
        earlier_thread = Thread(
            target=lambda: outcomes.setdefault(
                "earlier", session.execute_pending_intent(earlier["intent_id"])
            )
        )
        later_thread.start()
        sleep(0.05)
        earlier_thread.start()
        later_thread.join(timeout=2)
        earlier_thread.join(timeout=2)

    assert not later_thread.is_alive() and not earlier_thread.is_alive()
    assert outcomes["earlier"][-1]["status"] == "completed"
    assert outcomes["later"][-1]["status"] == "completed", outcomes


def test_later_selection_wins_even_when_older_acceptance_arrives_last(tmp_path: Path) -> None:
    initial = make_snapshot(2, selection=(), now_ms=Clock().value)
    harness = Harness(tmp_path, initial, auto_start_nodes=True)

    with TestClient(harness.app):
        session = harness.app.state.relay_runtime.session(SESSION)
        principal = Principal(source="console", drone_id=None, signing_key=CONSOLE_KEY)
        older = harness.intent("select", selection=[], args={"ids": [1]})
        harness.clock.advance(1)
        newer = harness.intent("select", selection=[], args={"ids": [2]})
        assert session.process_frame(older, principal)[0]["status"] == "accepted"
        assert session.process_frame(newer, principal)[0]["status"] == "accepted"
        session.mark_pending_intent_delivered(newer["intent_id"])
        newer_events = session.execute_pending_intent(newer["intent_id"])
        session.mark_pending_intent_delivered(older["intent_id"])
        older_events = session.execute_pending_intent(older["intent_id"])
        state = session.current_state()

    assert newer_events[-1]["status"] == "completed"
    assert older_events[-1]["status"] == "invalidated"
    assert state["selection"] == [2]


def test_disconnect_does_not_postpone_the_existing_node_failsafe_deadline(tmp_path: Path) -> None:
    initial = make_snapshot(
        1, selection=(), flight_state=FlightState.AIRBORNE, now_ms=Clock().value
    )
    harness = Harness(tmp_path, initial, auto_start_nodes=True)

    with TestClient(harness.app):
        harness.app.state.relay_runtime.session(SESSION)
        harness.factory.silence_node(SESSION, 1)
        harness.clock.advance(9_000)
        harness.factory.disconnect_node(SESSION, 1)
        harness.clock.advance(1_000)
        _wait_for_flight_state(harness, 1, FlightState.LANDED)


def test_delayed_pre_command_telemetry_cannot_rollback_post_command_state(tmp_path: Path) -> None:
    initial = make_snapshot(
        1,
        selection=(),
        flight_state=FlightState.DISARMED,
        armed=False,
        now_ms=Clock().value,
    )
    harness = Harness(tmp_path, initial, auto_start_nodes=True)

    with TestClient(harness.app) as client, _connect(client, "console") as console:
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
        harness.clock.advance(501)
        node = harness.factory.nodes[SESSION]
        delayed = node._telemetry(1)
        assert (
            _send_intent(
                console, harness.intent("translate", selection=[1], args={"dx": 1, "dy": 0})
            )["status"]
            == "completed"
        )
        events = node._process(delayed, 1)
        state = harness.app.state.relay_runtime.session(SESSION).current_state()

    assert any(event.get("type") == "refusal" for event in events)
    assert state["drones"][0]["telemetry"]["x"] == 0.5


def test_concurrent_simulator_frames_receive_unique_ordered_timestamps(tmp_path: Path) -> None:
    harness = Harness(tmp_path, make_snapshot(1, now_ms=Clock().value), auto_start_nodes=False)

    with TestClient(harness.app):
        harness.app.state.relay_runtime.session(SESSION)
        node = harness.factory.nodes[SESSION]
        with ThreadPoolExecutor(max_workers=8) as executor:
            timestamps = list(executor.map(lambda _: node._telemetry(1)["t"], range(32)))

    assert len(set(timestamps)) == 32
    assert sorted(timestamps) == list(range(min(timestamps), min(timestamps) + 32))


def test_frame_started_before_silence_cannot_refresh_the_watchdog_afterward(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    initial = make_snapshot(
        1, selection=(), flight_state=FlightState.AIRBORNE, now_ms=Clock().value
    )
    harness = Harness(tmp_path, initial, auto_start_nodes=True)

    with TestClient(harness.app):
        harness.app.state.relay_runtime.session(SESSION)
        node = harness.factory.nodes[SESSION]
        original = node.session.process_frame
        entered = Event()
        release = Event()

        def delayed_process(frame, principal):  # type: ignore[no-untyped-def]
            if frame.get("type") == "telemetry":
                entered.set()
                assert release.wait(timeout=2)
            return original(frame, principal)

        monkeypatch.setattr(node.session, "process_frame", delayed_process)
        periodic = Thread(target=node.periodic_events)
        periodic.start()
        assert entered.wait(timeout=1)
        node.silence(1)
        harness.clock.advance(10_000)
        release.set()
        periodic.join(timeout=2)
        assert not periodic.is_alive()
        _wait_for_flight_state(harness, 1, FlightState.LANDED)


def _wait_for_flight_state(
    harness: Harness, drone_id: int, expected: FlightState, *, timeout_s: float = 0.5
) -> None:
    deadline = monotonic() + timeout_s
    while monotonic() < deadline:
        if harness.flight.aircraft[drone_id].flight_state is expected:
            return
        sleep(0.01)
    assert harness.flight.aircraft[drone_id].flight_state is expected


def _wait_for_safety_audit(
    harness: Harness, drone_id: int, action: str, *, timeout_s: float = 1.0
) -> None:
    session = harness.app.state.relay_runtime.session(SESSION)
    deadline = monotonic() + timeout_s
    while monotonic() < deadline:
        events = [record["event"] for record in session.replay()["events"]]
        if any(
            event.get("type") == "safety_action"
            and event.get("drone_id") == drone_id
            and event.get("action") == action
            for event in events
        ):
            return
        sleep(0.01)
    raise AssertionError(f"safety action {action} for drone {drone_id} was not audited")


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
