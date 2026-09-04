from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import uvicorn
from websockets.asyncio.client import connect

from adapters.dji_mini3.fake_node import FakeNode, FakeNodeConfig
from adapters.dji_mini3.remote import RemoteBridgeAdapter
from relay.app import RelayRuntime, create_app
from relay.bridge import RelayNodeLink
from relay.settings import RelaySettings
from relay.tests.conftest import ADAPTER_KEY, CONSOLE_KEY, SESSION, intent_payload

WAIT_S = 10.0


@dataclass(slots=True)
class RelayServer:
    runtime: RelayRuntime
    port: int

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}"


@dataclass(slots=True)
class ConsoleProbe:
    """Authenticated console client collecting every fanned-out event on its own loop."""

    url: str
    events: list[dict[str, object]] = field(default_factory=list)
    outbound: list[dict[str, object]] = field(default_factory=list)
    _stop: threading.Event = field(default_factory=threading.Event)
    _ready: threading.Event = field(default_factory=threading.Event)
    _thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=lambda: asyncio.run(self._run()), daemon=True)
        self._thread.start()
        assert self._ready.wait(WAIT_S), "console probe did not authenticate"

    def send(self, frame: dict[str, object]) -> None:
        self.outbound.append(frame)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=WAIT_S)

    def wait_for(self, event_type: str, **fields: object) -> dict[str, object]:
        deadline = time.monotonic() + WAIT_S
        while time.monotonic() < deadline:
            for event in list(self.events):
                if event.get("type") == event_type and all(
                    event.get(key) == value for key, value in fields.items()
                ):
                    return event
            time.sleep(0.02)
        raise AssertionError(f"console never received {event_type} {fields}")

    async def _run(self) -> None:
        async with connect(f"{self.url}/ws/{SESSION}") as ws:
            await ws.send(
                json.dumps(
                    {"v": 1, "type": "auth", "source": "console", "token": CONSOLE_KEY.decode()}
                )
            )
            accepted = json.loads(await ws.recv())
            assert accepted["type"] == "auth.accepted"
            self._ready.set()
            sent = 0
            while not self._stop.is_set():
                while sent < len(self.outbound):
                    await ws.send(json.dumps(self.outbound[sent]))
                    sent += 1
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=0.05)
                except TimeoutError:
                    continue
                self.events.append(json.loads(raw))


def _wait_until(predicate, *, what: str) -> None:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + WAIT_S
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


@pytest.fixture
def relay_server(tmp_path: Path) -> Iterator[RelayServer]:
    settings = RelaySettings(
        relay_token=CONSOLE_KEY,
        adapter_keys={1: ADAPTER_KEY},
        log_dir=tmp_path,
    )
    app = create_app(settings, intent_sink_factory=lambda _session: lambda _intent, _state: None)
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


def test_hover_round_trips_through_the_node_socket_and_remote_adapter(
    relay_server: RelayServer,
) -> None:
    console = ConsoleProbe(relay_server.url)
    console.start()
    node = FakeNode(
        FakeNodeConfig(
            relay_url=relay_server.url,
            session=SESSION,
            drone_id=1,
            token=ADAPTER_KEY.decode(),
            adapter_id="fake-node-1",
        )
    )
    node.start()
    try:
        _wait_until(
            lambda: (
                SESSION in relay_server.runtime.sessions
                and any(
                    drone["membership"] == "ready"
                    for drone in relay_server.runtime.sessions[SESSION].current_state()["drones"]
                )
            ),
            what="fake node readiness",
        )
        assert node.node_settings == relay_server.runtime.settings.node_settings()
        console.send(intent_payload(timestamp=_now_ms()))
        console.wait_for("acknowledgement", intent_id="intent-1", status="accepted")
        session = relay_server.runtime.sessions[SESSION]
        state = session.current_state()
        drone = state["drones"][0]
        link = RelayNodeLink(relay_server.runtime, SESSION, delivery_timeout_ms=2_000)
        adapter = RemoteBridgeAdapter(
            link,
            epochs={1: drone["connection_epoch"]},
            acknowledgement_timeout_ms=2_000,
            command_ids=iter(["wire-hover", "wire-stale"]).__next__,
        )

        with adapter.for_intent("intent-1", state["roster_version"]):
            (acknowledgement,) = adapter.hover([1])
        with adapter.for_intent("intent-1", state["roster_version"] + 100):
            (stale,) = adapter.hover([1])
    finally:
        node.stop()
        console.stop()

    assert acknowledgement.status.value == "completed"
    assert acknowledgement.connection_epoch == drone["connection_epoch"]
    assert acknowledgement.detail == ""
    assert stale.status.value == "failed"
    assert stale.detail.startswith("stale_command")
    assert drone["camera_capabilities"]["aircraft_model"] == "fake-mini3"
    assert drone["node_status"]["watchdog_state"] == "nominal"
    assert drone["telemetry"]["state"] == "landed"

    records = [record["event"] for record in relay_server.runtime.replay(SESSION)["events"]]
    commands = [record for record in records if record["type"] == "command"]
    assert [command["command_id"] for command in commands] == ["wire-hover", "wire-stale"]
    assert commands[0]["operation"] == "hover"
    assert commands[0]["seq"] == 1
    assert commands[1]["seq"] == 2
    assert all("signature" not in command for command in commands)
    hover_acks = [
        (record["status"], record["reason"])
        for record in records
        if record["type"] == "acknowledgement" and record.get("command_id") == "wire-hover"
    ]
    assert hover_acks == [("accepted", None), ("executing", None), ("completed", None)]
    stale_acks = [
        (record["status"], record["reason"])
        for record in records
        if record["type"] == "acknowledgement" and record.get("command_id") == "wire-stale"
    ]
    assert stale_acks == [("failed", "stale_command")]
    assert {record["type"] for record in records} >= {
        "membership",
        "telemetry",
        "capabilities",
        "node_status",
        "capture_readiness",
        "intent_record",
    }
    console_types = {event["type"] for event in console.events}
    assert {"capabilities", "node_status", "capture_readiness", "state"} <= console_types
    assert "command" not in console_types
    assert not any("signature" in event for event in console.events)


def _now_ms() -> int:
    return time.time_ns() // 1_000_000
