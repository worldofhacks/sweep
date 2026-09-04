from __future__ import annotations

import asyncio
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, Lock, Timer

import pytest
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketTestSession
from starlette.websockets import WebSocket, WebSocketDisconnect

import relay.app as app_module
import relay.audit as audit_module
from relay.app import RelayRuntime, create_app
from relay.audit import AuditLogError, SessionAuditLog
from relay.auth import AuthenticationError, Principal
from relay.settings import RelaySettings
from relay.tests.conftest import (
    ADAPTER_KEY,
    CONSOLE_KEY,
    SESSION,
    EventIds,
    MutableClock,
    intent_payload,
    membership_payload,
    telemetry_payload,
)


@pytest.fixture
def app_settings(tmp_path: Path) -> RelaySettings:
    return RelaySettings(
        relay_token=CONSOLE_KEY,
        adapter_keys={1: ADAPTER_KEY},
        log_dir=tmp_path,
        intent_max_age_ms=5_000,
        transport_event_max_age_ms=5_000,
        future_clock_skew_ms=1_000,
        telemetry_freshness_ms=1_000,
    )


def _authenticate_console(
    socket: WebSocketTestSession,
) -> tuple[dict[str, object], dict[str, object]]:
    socket.send_json({"v": 1, "type": "auth", "source": "console", "token": CONSOLE_KEY.decode()})
    return socket.receive_json(), socket.receive_json()


def _authenticate_adapter(
    socket: WebSocketTestSession,
) -> tuple[dict[str, object], dict[str, object]]:
    socket.send_json(
        {
            "v": 1,
            "type": "auth",
            "source": "adapter",
            "drone_id": 1,
            "token": ADAPTER_KEY.decode(),
        }
    )
    return socket.receive_json(), socket.receive_json()


def _receive_type(
    socket: WebSocketTestSession, event_type: str, *, maximum: int = 30
) -> dict[str, object]:
    for _ in range(maximum):
        event = socket.receive_json()
        if event["type"] == event_type:
            return event
    raise AssertionError(f"did not receive {event_type!r} within {maximum} frames")


def test_first_frame_authentication_precedes_state_and_intent_results(
    app_settings: RelaySettings, clock: MutableClock, event_ids: EventIds
) -> None:
    app = create_app(
        app_settings,
        clock=clock,
        event_ids=event_ids,
        intent_sink_factory=lambda _session: lambda _intent, _state: None,
    )

    with TestClient(app) as client:
        with client.websocket_connect(f"/ws/{SESSION}") as socket:
            authenticated, state = _authenticate_console(socket)
            socket.send_json(intent_payload())
            acknowledgement = _receive_type(socket, "acknowledgement")

        replay = client.get(
            f"/session/{SESSION}",
            headers={"Authorization": f"Bearer {CONSOLE_KEY.decode()}"},
        )

    assert authenticated["v"] == 1
    assert authenticated["t"] == clock.value
    assert authenticated["type"] == "auth.accepted"
    assert authenticated["event_id"]
    assert authenticated["session"] == SESSION
    assert authenticated["source"] == "console"
    assert authenticated["drone_id"] is None
    assert state["type"] == "state"
    assert state["event_id"]
    assert acknowledgement["status"] == "accepted"
    assert acknowledgement["command_id"] is None
    assert replay.status_code == 200
    assert all(record["event"]["type"] != "auth.accepted" for record in replay.json()["events"])
    assert CONSOLE_KEY.decode() not in replay.text


def test_delayed_initial_delivery_cannot_put_a_new_snapshot_before_old_backlog(
    app_settings: RelaySettings, clock: MutableClock, event_ids: EventIds
) -> None:
    async def exercise_race() -> list[dict[str, object]]:
        runtime = RelayRuntime(app_settings, clock=clock, event_ids=event_ids)
        session = runtime.session(SESSION)
        adapter = Principal(source="adapter", drone_id=1, signing_key=ADAPTER_KEY)
        application = create_app(app_settings, clock=clock, event_ids=event_ids)
        application.state.relay_runtime = runtime
        route = next(
            route
            for route in application.routes
            if getattr(route, "path", None) == "/ws/{session_id}"
        )

        class RacingWebSocket:
            def __init__(self) -> None:
                self.app = application
                self.sent: list[dict[str, object]] = []
                self.receives = 0

            async def accept(self) -> None:
                return None

            async def receive_json(self) -> dict[str, object]:
                self.receives += 1
                if self.receives == 1:
                    return {
                        "v": 1,
                        "type": "auth",
                        "source": "console",
                        "token": CONSOLE_KEY.decode(),
                    }
                await asyncio.sleep(0.01)
                raise WebSocketDisconnect(code=1000)

            async def send_json(self, data: dict[str, object]) -> None:
                self.sent.append(data)
                if data["type"] != "auth.accepted":
                    return
                # Suspend initial delivery after activation. The old handler then read a
                # v2 snapshot before draining the v1 membership/state events queued here.
                frames = (
                    membership_payload(action="join", event_id="join-race"),
                    telemetry_payload(event_id="telemetry-race"),
                    membership_payload(action="readiness", event_id="ready-race"),
                )
                for frame in frames:
                    await runtime.publish(SESSION, session.process_frame(frame, adapter))

        websocket = RacingWebSocket()
        await route.endpoint(websocket, SESSION)
        return websocket.sent

    events = asyncio.run(exercise_race())

    assert [event["type"] for event in events[:2]] == ["auth.accepted", "state"]
    assert events[1]["roster_version"] == 0
    assert any(event["type"] == "state" and event["roster_version"] == 1 for event in events)
    state_rosters = [event["roster_version"] for event in events if event["type"] == "state"]
    assert state_rosters[-1] == 2
    assert state_rosters == sorted(state_rosters)


def test_session_operations_publish_whole_batches_in_mutation_order(
    app_settings: RelaySettings,
    clock: MutableClock,
    event_ids: EventIds,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise_race() -> tuple[list[int], int, int]:
        runtime = RelayRuntime(app_settings, clock=clock, event_ids=event_ids)
        session = runtime.session(SESSION)
        first = Principal(source="adapter", drone_id=1, signing_key=ADAPTER_KEY)
        second_key = b"adapter-key-2"
        second = Principal(source="adapter", drone_id=2, signing_key=second_key)
        console = Principal(source="console", drone_id=None, signing_key=CONSOLE_KEY)
        existing = await runtime.subscribe(SESSION, console)
        publish_started = asyncio.Event()
        resume_publish = asyncio.Event()
        real_publish = runtime.publish
        publish_count = 0

        async def paused_first_publish(session_id: str, events: list[dict[str, object]]) -> None:
            nonlocal publish_count
            publish_count += 1
            if publish_count == 1:
                publish_started.set()
                await resume_publish.wait()
            await real_publish(session_id, events)

        monkeypatch.setattr(runtime, "publish", paused_first_publish)
        first_operation = asyncio.create_task(
            runtime.process_and_publish(
                SESSION,
                lambda: session.process_frame(
                    membership_payload(action="join", event_id="join-1"), first
                ),
            )
        )
        await publish_started.wait()
        second_operation = asyncio.create_task(
            runtime.process_and_publish(
                SESSION,
                lambda: session.process_frame(
                    membership_payload(
                        action="join", event_id="join-2", drone_id=2, key=second_key
                    ),
                    second,
                ),
            )
        )
        await asyncio.sleep(0)
        joining_subscription = asyncio.create_task(runtime.subscribe(SESSION, console))
        resume_publish.set()
        await asyncio.gather(first_operation, second_operation)
        joined = await joining_subscription
        queued_versions = [int(existing.queue.get_nowait()["roster_version"]) for _ in range(4)]
        return (
            queued_versions,
            int(joined.initial_state["roster_version"]),
            joined.queue.qsize(),
        )

    queued_versions, initial_roster, new_backlog = asyncio.run(exercise_race())

    assert queued_versions == [1, 1, 2, 2]
    assert (initial_roster, new_backlog) == (2, 0)


def test_same_roster_operations_publish_in_mutation_order(
    app_settings: RelaySettings,
    clock: MutableClock,
    event_ids: EventIds,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise_race() -> list[bool]:
        runtime = RelayRuntime(app_settings, clock=clock, event_ids=event_ids)
        session = runtime.session(SESSION)
        subscription = await runtime.subscribe(
            SESSION, Principal(source="console", drone_id=None, signing_key=CONSOLE_KEY)
        )
        publish_started = asyncio.Event()
        resume_publish = asyncio.Event()
        real_publish = runtime.publish
        publish_count = 0

        async def paused_first_publish(session_id: str, events: list[dict[str, object]]) -> None:
            nonlocal publish_count
            publish_count += 1
            if publish_count == 1:
                publish_started.set()
                await resume_publish.wait()
            await real_publish(session_id, events)

        monkeypatch.setattr(runtime, "publish", paused_first_publish)
        older = asyncio.create_task(runtime.process_and_publish(SESSION, session.periodic_events))
        await publish_started.wait()
        newer = asyncio.create_task(
            runtime.process_and_publish(
                SESSION,
                lambda: [session.update_control_projection(estop=True)],
            )
        )
        await asyncio.sleep(0)
        assert not newer.done()
        resume_publish.set()
        await asyncio.gather(older, newer)
        return [
            bool(subscription.queue.get_nowait()["estop"]),
            bool(subscription.queue.get_nowait()["estop"]),
        ]

    assert asyncio.run(exercise_race()) == [False, True]


def test_cancelled_session_operation_finishes_publishing_before_releasing_order(
    app_settings: RelaySettings,
    clock: MutableClock,
    event_ids: EventIds,
) -> None:
    mutation_finished = Event()
    release_operation = Event()

    async def exercise() -> list[bool]:
        runtime = RelayRuntime(app_settings, clock=clock, event_ids=event_ids)
        session = runtime.session(SESSION)
        subscription = await runtime.subscribe(
            SESSION, Principal(source="console", drone_id=None, signing_key=CONSOLE_KEY)
        )

        def blocked_operation() -> list[dict[str, object]]:
            event = session.update_control_projection(estop=True)
            mutation_finished.set()
            assert release_operation.wait(timeout=2)
            return [event]

        cancelled = asyncio.create_task(runtime.process_and_publish(SESSION, blocked_operation))
        for _ in range(200):
            if mutation_finished.is_set():
                break
            await asyncio.sleep(0.01)
        assert mutation_finished.is_set()
        cancelled.cancel()
        release_operation.set()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        await runtime.process_and_publish(
            SESSION, lambda: [session.update_control_projection(estop=False)]
        )
        return [
            bool(subscription.queue.get_nowait()["estop"]),
            bool(subscription.queue.get_nowait()["estop"]),
        ]

    assert asyncio.run(exercise()) == [True, False]


def test_bad_authentication_is_refused_without_creating_a_session_log(
    app_settings: RelaySettings, clock: MutableClock, event_ids: EventIds
) -> None:
    app = create_app(app_settings, clock=clock, event_ids=event_ids)

    with TestClient(app) as client:
        with client.websocket_connect(f"/ws/{SESSION}") as socket:
            socket.send_json({"v": 1, "type": "auth", "source": "console", "token": "wrong"})
            refused = socket.receive_json()

    assert refused["type"] == "auth.refused"
    assert refused["reason"] == "authentication_failed"
    assert refused["event_id"]
    assert not list(app_settings.log_dir.glob("*.jsonl"))


def test_auth_accepted_distributes_node_thresholds_to_adapters_only(
    app_settings: RelaySettings, clock: MutableClock, event_ids: EventIds
) -> None:
    app = create_app(app_settings, clock=clock, event_ids=event_ids)

    with TestClient(app) as client:
        with client.websocket_connect(f"/ws/{SESSION}") as console:
            console_accepted, _ = _authenticate_console(console)
        with client.websocket_connect(f"/ws/{SESSION}") as adapter:
            adapter_accepted, _ = _authenticate_adapter(adapter)

    assert console_accepted["node"] is None
    assert adapter_accepted["node"] == {
        "command_ttl_ms": app_settings.command_ttl_ms,
        "virtual_stick_hz": app_settings.virtual_stick_hz,
        "watchdog_hold_ms": app_settings.node_watchdog_hold_ms,
        "watchdog_failsafe_ms": app_settings.node_watchdog_failsafe_ms,
    }
    assert set(adapter_accepted) == {
        "v",
        "t",
        "type",
        "event_id",
        "session",
        "source",
        "drone_id",
        "node",
    }


def test_second_adapter_connection_for_same_id_is_refused(
    app_settings: RelaySettings, clock: MutableClock, event_ids: EventIds
) -> None:
    app = create_app(app_settings, clock=clock, event_ids=event_ids)

    with TestClient(app) as client:
        with client.websocket_connect(f"/ws/{SESSION}") as first:
            first_auth, _ = _authenticate_adapter(first)
            with client.websocket_connect(f"/ws/{SESSION}") as second:
                second.send_json(
                    {
                        "v": 1,
                        "type": "auth",
                        "source": "adapter",
                        "drone_id": 1,
                        "token": ADAPTER_KEY.decode(),
                    }
                )
                refused = second.receive_json()

    assert first_auth["type"] == "auth.accepted"
    assert refused["type"] == "auth.refused"
    assert refused["reason"] == "adapter_already_connected"


def test_membership_telemetry_fanout_and_disconnect_share_one_ordered_contract(
    app_settings: RelaySettings, clock: MutableClock, event_ids: EventIds
) -> None:
    app = create_app(app_settings, clock=clock, event_ids=event_ids)

    with TestClient(app) as client:
        with client.websocket_connect(f"/ws/{SESSION}") as console:
            _authenticate_console(console)
            with client.websocket_connect(f"/ws/{SESSION}") as adapter:
                _authenticate_adapter(adapter)
                adapter.send_json(membership_payload(action="join", event_id="join-1"))
                joined = _receive_type(console, "membership")
                state_after_join = console.receive_json()
                adapter.send_json(telemetry_payload(event_id="telemetry-1"))
                telemetry = _receive_type(console, "telemetry")
                telemetry_state = console.receive_json()
            disconnected = _receive_type(console, "membership")
            disconnected_state = console.receive_json()

    assert joined["action"] == "join"
    assert joined["provenance"] == "adapter_signature"
    assert state_after_join["type"] == "state"
    assert state_after_join["roster_version"] == joined["roster_version"]
    assert telemetry["connection_epoch"] == 1
    assert telemetry_state["type"] == "state"
    assert telemetry_state["drones"][0]["flight_state"] == "hovering"
    assert disconnected["action"] == "unexpected_loss"
    assert disconnected["provenance"] == "relay_transport_attestation"
    assert disconnected_state["drones"][0]["membership"] == "disconnected"


def test_http_metrics_and_replay_require_bearer_authentication(
    app_settings: RelaySettings, clock: MutableClock, event_ids: EventIds
) -> None:
    app = create_app(app_settings, clock=clock, event_ids=event_ids)

    with TestClient(app) as client:
        assert client.get("/metrics").status_code == 401
        assert client.get(f"/session/{SESSION}").status_code == 401
        headers = {"Authorization": f"Bearer {CONSOLE_KEY.decode()}"}
        metrics = client.get("/metrics", headers=headers)
        replay = client.get(f"/session/{SESSION}?after_sequence=0", headers=headers)

    assert metrics.status_code == 200
    assert metrics.json()["type"] == "metrics"
    assert metrics.json()["event_id"]
    assert replay.status_code == 200
    assert replay.json()["type"] == "replay"
    assert replay.json()["event_id"]


def test_authenticated_console_receives_periodic_state_fanout_at_frozen_rate(
    app_settings: RelaySettings, clock: MutableClock, event_ids: EventIds
) -> None:
    app = create_app(app_settings, clock=clock, event_ids=event_ids)

    with TestClient(app) as client:
        with client.websocket_connect(f"/ws/{SESSION}") as socket:
            _authenticate_console(socket)
            periodic_state = _receive_type(socket, "state")

    assert app_settings.fanout_hz == 10
    assert periodic_state["type"] == "state"
    assert periodic_state["event_id"]


def test_restart_keeps_persisted_session_replay_only(
    app_settings: RelaySettings, clock: MutableClock, event_ids: EventIds
) -> None:
    def accepting_sink(_intent: object, _state: object) -> None:
        return None

    first_app = create_app(
        app_settings,
        clock=clock,
        event_ids=event_ids,
        intent_sink_factory=lambda _session: accepting_sink,
    )
    headers = {"Authorization": f"Bearer {CONSOLE_KEY.decode()}"}

    with TestClient(first_app) as client:
        with client.websocket_connect(f"/ws/{SESSION}") as socket:
            _authenticate_console(socket)
            socket.send_json(intent_payload())
            acknowledgement = _receive_type(socket, "acknowledgement")
        original_replay = client.get(f"/session/{SESSION}", headers=headers).json()

    assert acknowledgement["status"] == "accepted"
    assert original_replay["last_sequence"] > 0
    log_path = next(app_settings.log_dir.glob("*.jsonl"))
    original_log = log_path.read_bytes()

    second_app = create_app(app_settings, clock=clock, event_ids=event_ids)
    with TestClient(second_app) as client:
        replay = client.get(f"/session/{SESSION}", headers=headers)
        assert replay.status_code == 200
        assert replay.json()["events"] == original_replay["events"]
        assert replay.json()["last_sequence"] == original_replay["last_sequence"]
        assert SESSION not in second_app.state.relay_runtime.sessions

        with client.websocket_connect(f"/ws/{SESSION}") as closed_socket:
            closed_socket.send_json(
                {
                    "v": 1,
                    "type": "auth",
                    "source": "console",
                    "token": CONSOLE_KEY.decode(),
                }
            )
            refused = closed_socket.receive_json()

        assert refused["type"] == "auth.refused"
        assert refused["reason"] == "session_closed"
        assert SESSION not in second_app.state.relay_runtime.sessions
        assert log_path.read_bytes() == original_log

        with client.websocket_connect("/ws/new-session-after-restart") as new_socket:
            authenticated, state = _authenticate_console(new_socket)

        assert authenticated["type"] == "auth.accepted"
        assert state["type"] == "state"


def test_restart_recovers_torn_audit_tail_but_keeps_session_closed(
    app_settings: RelaySettings, clock: MutableClock, event_ids: EventIds
) -> None:
    log = SessionAuditLog(app_settings.log_dir, SESSION)
    first = log.append(
        {
            "v": 1,
            "t": clock.value,
            "type": "state",
            "event_id": "event-before-crash",
            "session": SESSION,
        }
    )
    log.path.write_bytes(log.path.read_bytes() + b'{"seq":2,"event":')
    app = create_app(app_settings, clock=clock, event_ids=event_ids)
    headers = {"Authorization": f"Bearer {CONSOLE_KEY.decode()}"}

    with TestClient(app) as client:
        replay = client.get(f"/session/{SESSION}", headers=headers)

    assert replay.status_code == 200
    assert replay.json()["events"] == [first]
    assert replay.json()["last_sequence"] == 1
    with pytest.raises(AuthenticationError, match="persisted sessions") as refusal:
        app.state.relay_runtime.session(SESSION)
    assert refusal.value.code == "session_closed"


def test_restart_keeps_torn_only_session_closed_after_replay_repair(
    app_settings: RelaySettings, clock: MutableClock, event_ids: EventIds
) -> None:
    log = SessionAuditLog(app_settings.log_dir, SESSION)
    log.path.write_bytes(b'{"seq":1,"event":')
    app = create_app(app_settings, clock=clock, event_ids=event_ids)
    headers = {"Authorization": f"Bearer {CONSOLE_KEY.decode()}"}

    with TestClient(app) as client:
        replay = client.get(f"/session/{SESSION}", headers=headers)
        assert replay.status_code == 200
        assert replay.json()["events"] == []
        assert replay.json()["last_sequence"] == 0

        with pytest.raises(AuthenticationError, match="persisted sessions") as refusal:
            app.state.relay_runtime.session(SESSION)

    assert refusal.value.code == "session_closed"
    assert log.path.read_bytes() == b""


def test_persisted_replay_cannot_race_live_session_activation(
    app_settings: RelaySettings,
    clock: MutableClock,
    event_ids: EventIds,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    constructor_started = Event()
    second_constructor_started = Event()
    resume_constructor = Event()
    recovery_complete = Event()
    partial_write = Event()
    constructor_lock = Lock()
    constructor_calls = 0
    real_write = os.write

    class PausingAuditLog(SessionAuditLog):
        def __init__(self, root: Path, session: str) -> None:
            nonlocal constructor_calls
            with constructor_lock:
                constructor_calls += 1
                call = constructor_calls
            if call == 1:
                constructor_started.set()
                assert resume_constructor.wait(timeout=2)
            else:
                second_constructor_started.set()
            super().__init__(root, session)

    monkeypatch.setattr(app_module, "SessionAuditLog", PausingAuditLog)
    runtime = RelayRuntime(app_settings, clock=clock, event_ids=event_ids)

    with ThreadPoolExecutor(max_workers=3) as executor:
        replay_future = executor.submit(runtime.replay, SESSION)
        assert constructor_started.wait(timeout=2)
        session_future = executor.submit(runtime.session, SESSION)
        raced = second_constructor_started.wait(timeout=0.2)

        if raced:
            session = session_future.result(timeout=2)
            first_write = True

            def pause_partial_write(descriptor: int, data: bytes | memoryview) -> int:
                nonlocal first_write
                if first_write:
                    first_write = False
                    written = real_write(descriptor, data[:5])
                    partial_write.set()
                    assert recovery_complete.wait(timeout=2)
                    return written
                return real_write(descriptor, data)

            monkeypatch.setattr(os, "write", pause_partial_write)
            append_future = executor.submit(session.update_control_projection, selection=())
            assert partial_write.wait(timeout=2)
            resume_constructor.set()
            replay_future.result(timeout=2)
            recovery_complete.set()
            append_future.result(timeout=2)
        else:
            resume_constructor.set()
            replay_future.result(timeout=2)
            session = session_future.result(timeout=2)
            session.update_control_projection(selection=())

    replay = runtime.replay(SESSION)
    assert [record["seq"] for record in replay["events"]] == [1]
    assert replay["last_sequence"] == 1


def test_persisted_replay_gate_does_not_block_another_session(
    app_settings: RelaySettings,
    clock: MutableClock,
    event_ids: EventIds,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    constructor_started = Event()
    resume_constructor = Event()

    class PausingAuditLog(SessionAuditLog):
        def __init__(self, root: Path, session: str) -> None:
            if session == "session-a":
                constructor_started.set()
                assert resume_constructor.wait(timeout=2)
            super().__init__(root, session)

    monkeypatch.setattr(app_module, "SessionAuditLog", PausingAuditLog)
    runtime = RelayRuntime(app_settings, clock=clock, event_ids=event_ids)

    with ThreadPoolExecutor(max_workers=2) as executor:
        replay_future = executor.submit(runtime.replay, "session-a")
        assert constructor_started.wait(timeout=2)
        session_future = executor.submit(runtime.session, "session-b")
        try:
            session = session_future.result(timeout=0.5)
        finally:
            resume_constructor.set()
        replay_future.result(timeout=2)

    assert session.session_id == "session-b"


def test_same_session_recovery_does_not_block_websocket_event_loop(
    app_settings: RelaySettings,
    clock: MutableClock,
    event_ids: EventIds,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    constructor_started = Event()
    resume_constructor = Event()

    class PausingAuditLog(SessionAuditLog):
        def __init__(self, root: Path, session: str) -> None:
            if session == SESSION:
                constructor_started.set()
                assert resume_constructor.wait(timeout=2)
            super().__init__(root, session)

    monkeypatch.setattr(app_module, "SessionAuditLog", PausingAuditLog)
    runtime = RelayRuntime(app_settings, clock=clock, event_ids=event_ids)
    application = create_app(app_settings, clock=clock, event_ids=event_ids)
    application.state.relay_runtime = runtime
    route = next(
        route for route in application.routes if getattr(route, "path", None) == "/ws/{session_id}"
    )

    class AuthenticatingWebSocket:
        def __init__(self) -> None:
            self.app = application
            self.receives = 0

        async def accept(self) -> None:
            return None

        async def receive_json(self) -> dict[str, object]:
            self.receives += 1
            if self.receives == 1:
                return {
                    "v": 1,
                    "type": "auth",
                    "source": "console",
                    "token": CONSOLE_KEY.decode(),
                }
            raise WebSocketDisconnect(code=1000)

        async def send_json(self, _data: dict[str, object]) -> None:
            return None

    async def exercise() -> tuple[float, int]:
        ticks = 0
        unrelated_finished = asyncio.Event()
        asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=20))

        async def ticker() -> None:
            nonlocal ticks
            while not unrelated_finished.is_set():
                ticks += 1
                await asyncio.sleep(0.01)

        with ThreadPoolExecutor(max_workers=1) as executor:
            replay_future = executor.submit(runtime.replay, SESSION)
            assert await asyncio.to_thread(constructor_started.wait, 2)
            timer = Timer(0.3, resume_constructor.set)
            timer.start()
            ticker_task = asyncio.create_task(ticker())
            waiters = [
                asyncio.create_task(route.endpoint(AuthenticatingWebSocket(), SESSION))
                for _ in range(20)
            ]
            await asyncio.sleep(0.05)
            started = time.monotonic()
            await route.endpoint(AuthenticatingWebSocket(), "session-b")
            elapsed = time.monotonic() - started
            unrelated_finished.set()
            await ticker_task
            resume_constructor.set()
            await asyncio.gather(*waiters)
            replay_future.result(timeout=2)
            timer.join(timeout=2)
            assert runtime._activation_tasks == {}
        return elapsed, ticks

    elapsed, ticks = asyncio.run(exercise())
    assert elapsed < 0.2
    assert ticks >= 5


def test_active_replay_does_not_block_same_session_or_unrelated_handshakes(
    app_settings: RelaySettings,
    clock: MutableClock,
    event_ids: EventIds,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = RelayRuntime(app_settings, clock=clock, event_ids=event_ids)
    active = runtime.session(SESSION)
    active.update_control_projection(selection=())
    replay_started = Event()
    resume_replay = Event()
    validate_record = audit_module._validate_record

    def pause_replay(record: object, expected: int, session: str, line: int) -> None:
        validate_record(record, expected, session, line)
        replay_started.set()
        assert resume_replay.wait(timeout=2)

    monkeypatch.setattr(audit_module, "_validate_record", pause_replay)

    async def exercise() -> tuple[int, str, str]:
        ticks = 0
        principal = Principal(source="console", drone_id=None, signing_key=CONSOLE_KEY)

        async def ticker() -> None:
            nonlocal ticks
            for _ in range(5):
                ticks += 1
                await asyncio.sleep(0.01)

        replay_task = asyncio.create_task(asyncio.to_thread(runtime.replay, SESSION))
        assert await asyncio.to_thread(replay_started.wait, 2)
        ticker_task = asyncio.create_task(ticker())
        same = await runtime.activate_session(SESSION)
        same_subscription = await runtime.subscribe(SESSION, principal)
        other = await runtime.activate_session("session-b")
        other_subscription = await runtime.subscribe("session-b", principal)
        await ticker_task
        await runtime.unsubscribe(SESSION, same_subscription)
        await runtime.unsubscribe("session-b", other_subscription)
        resume_replay.set()
        await replay_task
        return ticks, same.session_id, other.session_id

    ticks, same_id, other_id = asyncio.run(exercise())
    assert (ticks, same_id, other_id) == (5, SESSION, "session-b")


def test_fanout_isolates_audit_failure_to_affected_session(
    app_settings: RelaySettings,
    clock: MutableClock,
    event_ids: EventIds,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> tuple[int, bool, set[str]]:
        runtime = RelayRuntime(app_settings, clock=clock, event_ids=event_ids)
        failed = runtime.session("session-a")
        runtime.session("session-b")
        real_fsync = os.fsync

        def fail_fsync(_descriptor: int) -> None:
            raise OSError("injected fsync failure")

        monkeypatch.setattr(os, "fsync", fail_fsync)
        with pytest.raises(AuditLogError):
            failed.update_control_projection(selection=())
        monkeypatch.setattr(os, "fsync", real_fsync)
        subscription = await runtime.subscribe(
            "session-b", Principal(source="console", drone_id=None, signing_key=CONSOLE_KEY)
        )
        await runtime.start()
        await asyncio.sleep(0.25)
        running = runtime._fanout_task is not None and not runtime._fanout_task.done()
        await runtime.stop()
        return subscription.queue.qsize(), running, runtime._fanout_failed_sessions

    queued, running, failed_sessions = asyncio.run(exercise())
    assert running is True
    assert queued >= 1
    assert failed_sessions == {"session-a"}
    assert "session fan-out stopped after audit failure session=session-a" in caplog.text


def test_pending_wal_operation_keeps_session_closed_after_restart(
    app_settings: RelaySettings,
    clock: MutableClock,
    event_ids: EventIds,
) -> None:
    log = SessionAuditLog(app_settings.log_dir, SESSION)
    log.begin_operation()

    runtime = RelayRuntime(app_settings, clock=clock, event_ids=event_ids)
    with pytest.raises(AuthenticationError, match="persisted sessions are replay-only"):
        runtime.session(SESSION)
    with pytest.raises(AuditLogError, match="incomplete operation"):
        runtime.replay(SESSION)
    assert not log.pending_path.exists()


def test_join_close_failure_closes_socket_and_fences_live_projection(
    app_settings: RelaySettings,
    clock: MutableClock,
    event_ids: EventIds,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(app_settings, clock=clock, event_ids=event_ids)
    real_close = os.close
    closes = 0

    def fail_first_close(descriptor: int) -> None:
        nonlocal closes
        closes += 1
        real_close(descriptor)
        if closes == 1:
            raise OSError("injected close failure")

    with TestClient(app) as client:
        runtime = app.state.relay_runtime
        with client.websocket_connect(f"/ws/{SESSION}") as adapter:
            _authenticate_adapter(adapter)
            monkeypatch.setattr(os, "close", fail_first_close)
            adapter.send_json(membership_payload(action="join", event_id="join-close-error"))
            with pytest.raises(WebSocketDisconnect) as closed:
                adapter.receive_json()

        session = runtime.sessions[SESSION]
        assert runtime.connection_count() == 0

    assert closed.value.code == 1011
    with pytest.raises(AuditLogError, match="session is unusable"):
        session.replay()
    with pytest.raises(AuditLogError, match="session is unusable"):
        session.current_state()
    with pytest.raises(AuditLogError, match="session is unusable"):
        session.metrics()


@pytest.mark.parametrize("failure_point", ["recovery", "subscription"])
def test_handshake_audit_failure_closes_socket_without_success_frames(
    app_settings: RelaySettings,
    clock: MutableClock,
    event_ids: EventIds,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    runtime = RelayRuntime(app_settings, clock=clock, event_ids=event_ids)
    if failure_point == "recovery":

        class FailingAuditLog(SessionAuditLog):
            def __init__(self, root: Path, session: str) -> None:
                raise AuditLogError("injected recovery failure")

        monkeypatch.setattr(app_module, "SessionAuditLog", FailingAuditLog)
    else:
        runtime.session(SESSION)._projection_usable = False

    application = create_app(app_settings, clock=clock, event_ids=event_ids)
    application.state.relay_runtime = runtime
    route = next(
        route for route in application.routes if getattr(route, "path", None) == "/ws/{session_id}"
    )

    class HandshakeWebSocket:
        def __init__(self) -> None:
            self.app = application
            self.sent: list[dict[str, object]] = []
            self.closed: list[int] = []

        async def accept(self) -> None:
            return None

        async def receive_json(self) -> dict[str, object]:
            return {
                "v": 1,
                "type": "auth",
                "source": "console",
                "token": CONSOLE_KEY.decode(),
            }

        async def send_json(self, data: dict[str, object]) -> None:
            self.sent.append(data)

        async def close(self, code: int) -> None:
            self.closed.append(code)

    socket = HandshakeWebSocket()
    asyncio.run(route.endpoint(socket, SESSION))

    assert socket.sent == []
    assert socket.closed == [1011]
    assert runtime.connection_count() == 0


def test_initial_send_failure_releases_adapter_binding_and_records_loss(
    app_settings: RelaySettings,
    clock: MutableClock,
    event_ids: EventIds,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(app_settings, clock=clock, event_ids=event_ids)
    original_send_json = WebSocket.send_json
    failed = False

    async def fail_first_accepted(websocket: WebSocket, data: object, mode: str = "text") -> None:
        nonlocal failed
        if not failed and isinstance(data, dict) and data.get("type") == "auth.accepted":
            failed = True
            raise WebSocketDisconnect(code=1006)
        await original_send_json(websocket, data, mode=mode)

    with TestClient(app) as client:
        runtime = app.state.relay_runtime
        session = runtime.session(SESSION)
        principal = Principal(source="adapter", drone_id=1, signing_key=ADAPTER_KEY)
        session.process_membership(
            membership_payload(action="join", event_id="preconnected-join"), principal
        )
        monkeypatch.setattr(WebSocket, "send_json", fail_first_accepted)

        with client.websocket_connect(f"/ws/{SESSION}") as failed_socket:
            failed_socket.send_json(
                {
                    "v": 1,
                    "type": "auth",
                    "source": "adapter",
                    "drone_id": 1,
                    "token": ADAPTER_KEY.decode(),
                }
            )

        assert failed is True
        assert runtime.connection_count() == 0
        disconnected = session.current_state()["drones"][0]
        assert disconnected["membership"] == "disconnected"

        with client.websocket_connect(f"/ws/{SESSION}") as retry_socket:
            authenticated, state = _authenticate_adapter(retry_socket)
            assert authenticated["type"] == "auth.accepted"
            assert state["drones"][0]["membership"] == "disconnected"
            retry_socket.send_json(membership_payload(action="join", event_id="retry-join"))
            rejoined = _receive_type(retry_socket, "membership")

        assert rejoined["connection_epoch"] == 2
