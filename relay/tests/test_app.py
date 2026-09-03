from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketTestSession
from starlette.websockets import WebSocket, WebSocketDisconnect

from relay.app import RelayRuntime, create_app
from relay.auth import Principal
from relay.contracts import parse_membership_request
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


def test_refresh_media_projection_updates_the_session_state_envelope(
    app_settings: RelaySettings, clock: MutableClock, event_ids: EventIds
) -> None:
    class Observer:
        async def observe(self, *, observed_at_ms: int) -> dict[int, dict[str, object]]:
            assert observed_at_ms == clock.value
            return {1: {"status": "live", "last_frame_at": observed_at_ms}}

    runtime = RelayRuntime(
        app_settings,
        clock=clock,
        event_ids=event_ids,
        media_observer=Observer(),  # type: ignore[arg-type]
    )
    session = runtime.session(SESSION)
    session.registry.apply_join(
        parse_membership_request(membership_payload(action="join", event_id="join-media"))
    )

    asyncio.run(runtime.refresh_media_projection())

    state = session.current_state()
    assert state["type"] == "state"
    assert state["drones"][0]["video"] == {
        "status": "live",
        "last_frame_at": clock.value,
    }


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
