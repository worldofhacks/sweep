"""Acceptance of the isolated launcher through real relay and node WebSockets."""

from __future__ import annotations

import json
import socket
import time
import uuid
from pathlib import Path

import httpx
import pytest
from websockets.sync.client import connect

import relay.voice as voice_module
from adapters.sim.demo import DemoConfig, FleetDemo
from relay.autonomy import AutonomyConfig
from relay.settings import RelaySettings


@pytest.mark.parametrize("count", [0, 5, 6, -1, True, 2.5])
def test_demo_rejects_invalid_fleet_count(count: object) -> None:
    with pytest.raises(ValueError, match="count"):
        DemoConfig(count=count)  # type: ignore[arg-type]


@pytest.mark.parametrize("session", ["../live", "demo/live", "demo?query", "", "å"])
def test_demo_session_is_confined_to_one_url_component(session: str) -> None:
    with pytest.raises(ValueError, match="session"):
        DemoConfig(session=session)


def _outcome(websocket: object, intent_id: str) -> dict[str, object]:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        event = json.loads(websocket.recv(timeout=10))  # type: ignore[attr-defined]
        if (
            event.get("intent_id") == intent_id
            and event.get("source") == "autonomy"
            and event.get("status") in {"completed", "failed", "refused", "invalidated"}
        ):
            return event
    raise AssertionError("no terminal autonomy outcome arrived")


def _command(
    demo: FleetDemo,
    websocket: object,
    name: str,
    selection: list[int],
    *,
    args: dict[str, object] | None = None,
    confirm: bool = False,
) -> tuple[str, dict[str, object]]:
    intent_id = str(uuid.uuid4())
    websocket.send(  # type: ignore[attr-defined]
        json.dumps(
            {
                "v": 1,
                "type": "intent",
                "t": time.time_ns() // 1_000_000,
                "intent_id": intent_id,
                "retry_of": None,
                "source": "console",
                "session": demo.config.session,
                "name": name,
                "selection": selection,
                "args": args or {},
                "mode": "indoor",
                "confirm": confirm,
            }
        )
    )
    return intent_id, _outcome(websocket, intent_id)


def _commands(demo: FleetDemo, intent_id: str) -> list[dict[str, object]]:
    return [
        record["event"]
        for record in demo.runtime.replay(demo.config.session)["events"]
        if record["event"].get("type") == "command"
        and record["event"].get("intent_id") == intent_id
    ]


def test_four_node_demo_dispatches_selection_and_fleet_stops_over_signed_wire(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unexpected_environment(*_: object) -> None:
        raise AssertionError("the isolated demo must never read deployment configuration")

    monkeypatch.setattr(RelaySettings, "from_env", unexpected_environment)
    monkeypatch.setattr(AutonomyConfig, "from_env", unexpected_environment)
    with FleetDemo(DemoConfig(log_dir=tmp_path)) as demo:
        assert set(demo.drones()) == {1, 2, 3, 4}
        assert {drone["telemetry"]["x"] for drone in demo.drones().values()} == {0, 2, 4, 6}
        assert all(drone["membership"] == "ready" for drone in demo.drones().values())
        with connect(f"{demo.ws_url}/ws/{demo.config.session}") as websocket:
            websocket.send(
                json.dumps({"v": 1, "type": "auth", "source": "console", "token": demo.token})
            )
            assert json.loads(websocket.recv(timeout=10))["type"] == "auth.accepted"
            commands = [
                ("arm", [], {}, False, []),
                ("select", [], {"ids": [1, 2, 3, 4]}, False, []),
                ("takeoff", [1, 2, 3, 4], {}, True, [1, 2, 3, 4]),
                ("select", [1, 2, 3, 4], {"ids": [1, 3]}, False, []),
                ("translate", [1, 3], {"dx": 1, "dy": 0}, False, [3, 1]),
                ("hold", [1, 3], {}, False, [1, 3]),
                ("land_all", [], {}, True, [1, 2, 3, 4]),
                ("estop", [], {}, False, [1, 2, 3, 4]),
            ]
            for name, selection, args, confirm, expected_ids in commands:
                intent_id, outcome = _command(
                    demo, websocket, name, selection, args=args, confirm=confirm
                )
                assert outcome["status"] == "completed", outcome
                issued = _commands(demo, intent_id)
                assert [command["drone_id"] for command in issued] == expected_ids
                assert all(command["connection_epoch"] == 1 for command in issued)
                assert all("signature" not in command for command in issued)
        port = demo.port
        thread = demo._thread
        assert demo.token not in json.dumps(demo.status())
    assert thread is not None and not thread.is_alive()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        assert probe.connect_ex(("127.0.0.1", port)) != 0
    assert list(tmp_path.glob("*.jsonl")), "the evidence log should survive demo shutdown"


def test_demo_bootstrap_and_loss_rejoin_are_isolated_and_authenticated(tmp_path: Path) -> None:
    with FleetDemo(DemoConfig(count=2, log_dir=tmp_path)) as demo:
        with httpx.Client(base_url=demo.http_url) as client:
            bootstrap = client.get("/relay-bootstrap.json")
            assert bootstrap.status_code == 200
            assert bootstrap.headers["cache-control"] == "no-store"
            assert bootstrap.json() == demo.bootstrap()
            assert client.post("/demo/nodes/2/disconnect").status_code == 401
            headers = {"Authorization": f"Bearer {demo.token}"}
            disconnected = client.post("/demo/nodes/2/disconnect", headers=headers)
            assert disconnected.status_code == 200
            assert demo.drones()[2]["membership"] == "disconnected"
            rejoined = client.post("/demo/nodes/2/rejoin", headers=headers)
            assert rejoined.status_code == 200
            assert demo.drones()[2]["membership"] == "ready"
            assert demo.drones()[2]["connection_epoch"] == 2
            assert demo.drones()[1]["connection_epoch"] == 1
            assert client.post("/demo/nodes/2/rejoin", headers=headers).status_code == 409
            assert client.post("/demo/nodes/9/disconnect", headers=headers).status_code == 409


def test_demo_never_initializes_environment_backed_voice_tracing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []

    def unexpected_trace_sink(*_: object, **__: object) -> None:
        calls.append("environment trace initialization")
        raise AssertionError("demo construction must not initialize the default trace sink")

    monkeypatch.setattr(voice_module, "get_default_voice_trace_sink", unexpected_trace_sink)
    with FleetDemo(DemoConfig(count=1, log_dir=tmp_path)) as demo:
        assert demo.drones()[1]["membership"] == "ready"
    assert calls == []


def test_built_console_shares_the_bootstrap_origin(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html><body>demo console</body></html>")
    with FleetDemo(DemoConfig(count=1, console_dist=dist, log_dir=tmp_path / "logs")) as demo:
        response = httpx.get(demo.http_url)
        assert response.status_code == 200
        assert "demo console" in response.text
        assert httpx.get(f"{demo.http_url}/demo/status").json()["count"] == 1
        assert demo.status()["console_url"] == demo.http_url


def test_busy_port_failure_preserves_the_existing_listener(tmp_path: Path) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as existing:
        existing.bind(("127.0.0.1", 0))
        existing.listen()
        port = existing.getsockname()[1]
        demo = FleetDemo(DemoConfig(port=port, log_dir=tmp_path))
        with pytest.raises(OSError):
            demo.start()
        demo.stop()
        assert not demo.nodes
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            connection, _ = existing.accept()
            connection.close()
