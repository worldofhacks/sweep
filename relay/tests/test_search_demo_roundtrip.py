from __future__ import annotations

import socket
import threading
import time
import uuid
from collections.abc import Iterator

import httpx
import pytest
import uvicorn

from adapters.dji_mini3.fake_node import FakeNode, FakeNodeConfig
from adapters.sim.search_demo import SearchDemo, search_demo
from relay.autonomy import create_autonomy_app
from relay.settings import AdapterBackend, RelaySettings
from relay.tests.conftest import ADAPTER_KEY, CONSOLE_KEY, SESSION
from relay.tests.test_bridge_roundtrip import ConsoleProbe, RelayServer

WAIT_S = 10.0


def _wait_until(predicate, *, what: str) -> None:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + WAIT_S
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


def _outcome(console: ConsoleProbe, intent_id: str) -> dict[str, object]:
    deadline = time.monotonic() + WAIT_S
    while time.monotonic() < deadline:
        for event in list(console.events):
            if (
                event.get("intent_id") == intent_id
                and event.get("source") == "autonomy"
                and event.get("type") in {"acknowledgement", "refusal"}
                and event.get("status") in {"completed", "failed", "refused", "invalidated"}
            ):
                return event
        time.sleep(0.02)
    raise AssertionError(f"missing terminal outcome for {intent_id}")


def _intent(
    name: str,
    *,
    selection: list[int],
    args: dict[str, object] | None = None,
    confirm: bool = False,
) -> dict[str, object]:
    return {
        "v": 1,
        "t": time.time_ns() // 1_000_000,
        "type": "intent",
        "intent_id": f"search-demo-{uuid.uuid4().hex}",
        "retry_of": None,
        "source": "console",
        "session": SESSION,
        "name": name,
        "args": args or {},
        "selection": selection,
        "mode": "indoor",
        "confirm": confirm,
    }


@pytest.fixture
def search_demo_server(tmp_path) -> Iterator[tuple[RelayServer, SearchDemo]]:
    demo = search_demo()
    settings = RelaySettings(
        relay_token=CONSOLE_KEY,
        adapter_keys={1: ADAPTER_KEY},
        log_dir=tmp_path,
        adapter_backend=AdapterBackend.REMOTE,
        telemetry_freshness_ms=5_000,
    )
    app, composition = create_autonomy_app(
        settings,
        demo.config,
        detection_stream_factory=demo.stream_factory,
        detection_detector_factory=demo.detector_factory,
        detection_pose_provider_factory=demo.pose_provider_factory,
        detection_camera_provider_factory=demo.camera_provider_factory,
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
    _wait_until(lambda: server.started, what="search demo relay startup")
    try:
        yield RelayServer(runtime=app.state.relay_runtime, port=port), demo
    finally:
        server.should_exit = True
        thread.join(timeout=WAIT_S)
        if thread.is_alive():
            server.force_exit = True
            thread.join(timeout=WAIT_S)
        composition.close()


def test_synthetic_search_demo_runs_from_http_preview_through_fake_node(
    search_demo_server: tuple[RelayServer, SearchDemo]
) -> None:
    server, demo = search_demo_server
    console = ConsoleProbe(server.url)
    node = FakeNode(
        FakeNodeConfig(
            relay_url=server.url,
            session=SESSION,
            drone_id=1,
            token=ADAPTER_KEY.decode(),
            adapter_id="synthetic-search-node",
            home=(0.5, 1.5, 0.0),
            telemetry_hz=10,
        )
    )
    http_url = server.url.replace("ws://", "http://", 1)
    headers = {"Authorization": f"Bearer {CONSOLE_KEY.decode()}"}
    console.start()
    node.start()
    try:
        def drone() -> dict[str, object] | None:
            session = server.runtime.sessions.get(SESSION)
            if session is None:
                return None
            return next(
                (item for item in session.current_state()["drones"] if item["drone_id"] == 1), None
            )

        _wait_until(
            lambda: (item := drone()) is not None and item["membership"] == "ready",
            what="synthetic search node readiness",
        )
        arm = _intent("arm", selection=[])
        console.send(arm)
        assert _outcome(console, arm["intent_id"])["status"] == "completed"
        select = _intent("select", selection=[], args={"ids": [1]})
        console.send(select)
        assert _outcome(console, select["intent_id"])["status"] == "completed"
        takeoff = _intent("takeoff", selection=[1], confirm=True)
        console.send(takeoff)
        assert _outcome(console, takeoff["intent_id"])["status"] == "completed"
        _wait_until(
            lambda: (item := drone()) is not None
            and item["telemetry"] is not None
            and item["telemetry"]["state"] == "hovering",
            what="synthetic search node hover telemetry",
        )

        search = _intent(
            "search",
            selection=[1],
            args={"zone_id": "atrium", "target_class": "person"},
            confirm=True,
        )
        preview = httpx.post(
            f"{http_url}/session/{SESSION}/search/preview",
            headers=headers,
            json={"intent": search},
            timeout=WAIT_S,
        )
        assert preview.status_code == 200, preview.text
        assert preview.json()["session"] == SESSION
        console.send(search)
        for _ in range(30):
            demo.publish_frame()
            time.sleep(0.04)
        outcome = _outcome(console, search["intent_id"])
        assert outcome["status"] == "completed", outcome

        status_url = f"{http_url}/session/{SESSION}/search/{search['intent_id']}"
        _wait_until(
            lambda: bool(
                (
                    payload := httpx.get(status_url, headers=headers, timeout=WAIT_S).json()
                )["candidates"]
            )
            and payload["candidates"][0]["position"] is not None,
            what="localized synthetic search candidate",
        )
        status = httpx.get(status_url, headers=headers, timeout=WAIT_S).json()
        candidate = status["candidates"][0]
        commands_before = len(
            [
                record
                for record in server.runtime.replay(SESSION)["events"]
                if record["event"]["type"] == "command"
            ]
        )
        acknowledged = httpx.post(
            f"{status_url}/findings/{candidate['sighting_id']}/ack",
            headers=headers,
            timeout=WAIT_S,
        )
        assert acknowledged.status_code == 200, acknowledged.text
        commands_after = len(
            [
                record
                for record in server.runtime.replay(SESSION)["events"]
                if record["event"]["type"] == "command"
            ]
        )
    finally:
        node.stop()
        console.stop()

    assert status["session"] == SESSION
    assert status["tasks"][0]["covered_cells"] > 0
    assert candidate["position"]["zone_id"] == "atrium"
    assert candidate["frame"]["source_id"] == "synthetic-search-camera-1"
    assert candidate["frame"]["frame_sequence"] >= 5
    assert acknowledged.json()["session"] == SESSION
    assert acknowledged.json()["candidates"][0]["acknowledged"]
    assert commands_after == commands_before
