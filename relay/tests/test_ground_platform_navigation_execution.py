from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import uvicorn
from mcap.reader import make_reader
from websockets.sync.client import connect

from adapters.dji_mini3.fake_node import FakeNode, FakeNodeConfig
from adapters.dji_mini3.remote import CommandRequest
from adapters.ohmni.fake import FakeGroundDevice
from adapters.ohmni.runtime import GroundRuntimeConfig, OhmniRuntime
from adapters.ohmni.test_runtime import (
    GROUND_ID,
    GROUND_KEY,
    _observation_configuration,
    _receive_until,
    _wait_for,
)
from adapters.protocols import AdapterError
from language.relay_compiler import RelayTranscriptCompiler
from language.test_semantic_reviews import CapturingTransport
from planner.ground_navigation import GroundNavigationDeployment
from planner.models import CommandOperation, LifecycleStatus
from planner.test_ground_navigation import KEY, deployment_file
from relay.auth import sign_event
from relay.autonomy import AutonomyConfig, create_autonomy_app
from relay.bridge import RelayNodeLink
from relay.contracts import NodeType
from relay.ground_navigation_execution import _GroundGuardedLink
from relay.map_authoring import MapAuthoringStore
from relay.observation_ingress import ObservationConfiguration
from relay.observations import ClockMapping, FrameDeclaration, FrameRegistry, SourceBinding
from relay.settings import AdapterBackend, RelaySettings
from relay.tests.conftest import ADAPTER_KEY, CONSOLE_KEY, SESSION
from relay.tests.test_voice import FixedTranscriptionTransport, fixed_audio_duration
from relay.voice import TranscriptService
from tests.autonomy_fixtures import planning_config, safety_config
from tools.world_replay import export_audit, read_replay

LOCALIZATION_KEY = b"ground-qualified-localization-test-key-32"


@pytest.fixture
def world_confidence():
    return [1.0]


@pytest.fixture
def ground_platform(tmp_path: Path, monkeypatch, request, world_confidence):
    with_aircraft = getattr(request, "param", False)
    path = deployment_file(tmp_path, now_ms=time.time_ns() // 1_000_000)
    document = json.loads(path.read_text())
    source_store = MapAuthoringStore(tmp_path / "maps.sqlite")
    draft = source_store.load("session-a", document["approved_map"]["reference"])["draft"]
    log_dir = tmp_path / "relay"
    maps = MapAuthoringStore(log_dir / "platform" / "maps.sqlite3")
    reference = maps.save(SESSION, draft, None, "operator")
    validation = maps.validate(SESSION, reference, "operator")
    maps.approve(SESSION, reference, validation["validationId"], "operator")
    document["approved_map"] = maps.approved_bundle(SESSION, reference)
    unsigned = {key: value for key, value in document.items() if key != "signature"}
    path.write_text(json.dumps({**unsigned, "signature": sign_event(unsigned, KEY)}))
    deployment = GroundNavigationDeployment.load(path, KEY)
    device_config = deployment.device(GROUND_ID)
    manifest = document["approved_map"]["bundle"]["manifest"]
    monkeypatch.setenv(
        "SWEEP_WORLD_OBSERVATION_SOURCES",
        json.dumps(
            {
                "registrations": {
                    device_config.world_pose_source_id: {
                        "reference": reference,
                        "mapVersion": deployment.map_version,
                        "floorId": deployment.floor_id,
                        "sourceFrame": manifest["registration"]["sourceFrame"],
                        "transformId": device_config.registration_id,
                        "qualifiedWorldPose": True,
                    },
                },
            }
        ),
    )
    settings = RelaySettings(
        relay_token=CONSOLE_KEY,
        adapter_keys={GROUND_ID: GROUND_KEY, **({1: ADAPTER_KEY} if with_aircraft else {})},
        localization_keys={GROUND_ID: LOCALIZATION_KEY},
        node_types={
            GROUND_ID: NodeType.GROUND,
            **({1: NodeType.AIRCRAFT} if with_aircraft else {}),
        },
        log_dir=log_dir,
        adapter_backend=AdapterBackend.REMOTE,
        node_watchdog_hold_ms=1000,
        node_watchdog_failsafe_ms=2000,
        observation_configuration=ObservationConfiguration(
            bindings=(
                *_observation_configuration().bindings,
                SourceBinding(
                    SESSION,
                    GROUND_ID,
                    1,
                    device_config.world_pose_source_id,
                    "ground",
                    ("world", "body"),
                    ("pose",),
                    reference["bundleId"],
                    deployment.map_version,
                    f"ground-navigation-{device_config.registration_id}",
                    ("host-clock",),
                    producer_role="localization",
                ),
            ),
            frames=FrameRegistry(
                (
                    *_observation_configuration().frames.declarations,
                    FrameDeclaration(
                        "world",
                        "world",
                        "right_handed_z_up",
                        "m",
                        map_id=reference["bundleId"],
                        map_version=deployment.map_version,
                        physical_datum=f"ground-navigation-{device_config.registration_id}",
                    ),
                    FrameDeclaration(
                        "body",
                        "body",
                        "forward_left_up",
                        "m",
                        SESSION,
                        GROUND_ID,
                        1,
                        device_config.world_pose_source_id,
                    ),
                )
            ),
            clock_mappings=(
                ClockMapping(
                    "host-clock",
                    "host",
                    "ms",
                    time.time_ns() // 1_000_000,
                    time.time_ns() // 1_000_000,
                    1,
                    1,
                    100,
                ),
            ),
        ),
    )
    app, composition = create_autonomy_app(
        settings,
        AutonomyConfig(
            planning=planning_config(),
            safety=safety_config(),
            ground_navigation=deployment,
        ),
        transcript_service_factory=lambda runtime: TranscriptService(
            transcription=FixedTranscriptionTransport("Go to the lobby."),
            duration_probe=fixed_audio_duration,
            compiler=RelayTranscriptCompiler(
                sessions=runtime.sessions.get,
                transport=CapturingTransport(
                    {"kind": "review", "review": {"kind": "navigate", "destination_id": "lobby"}}
                ),
            ),
        ),
    )
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", timeout_graceful_shutdown=2))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    _wait_for(lambda: server.started, "qualified ground relay startup")
    device = FakeGroundDevice(x=7.75, y=-1.05, yaw_deg=90.0)
    node_path = tmp_path / "node-ground-navigation.json"
    node_path.write_bytes(path.read_bytes())
    node = OhmniRuntime(
        GroundRuntimeConfig(
            f"ws://127.0.0.1:{port}",
            SESSION,
            GROUND_ID,
            GROUND_KEY.decode(),
            "ground-9",
            navigation=GroundNavigationDeployment.load(node_path, KEY),
            odom_origin_id="origin-a",
            heartbeat_hold_ms=1000,
            heartbeat_failsafe_ms=2000,
            lidar_mount_x_m=0.0,
            lidar_mount_y_m=0.0,
            lidar_mount_z_m=0.25,
            lidar_mount_yaw_deg=0.0,
        ),
        device,
    )
    node.start()
    aircraft = None
    if with_aircraft:
        aircraft = FakeNode(
            FakeNodeConfig(
                relay_url=f"ws://127.0.0.1:{port}",
                session=SESSION,
                drone_id=1,
                token=ADAPTER_KEY.decode(),
                adapter_id="other-hold-target",
            )
        )
        aircraft.start()
    stop_source = threading.Event()
    errors = []
    source_thread = None
    try:
        _wait_for(lambda: SESSION in composition.runtime.sessions, "ground session")
        session = composition.runtime.sessions[SESSION]
        if with_aircraft:
            _wait_for(
                lambda: any(
                    row["drone_id"] == 1 and row["membership"] == "ready"
                    for row in session.current_state()["drones"]
                ),
                "other HOLD target readiness",
            )
        _wait_for(
            lambda: (
                session.registry.ready_ground_identity(GROUND_ID, time.time_ns() // 1_000_000)
                is not None
            ),
            "ground platform readiness",
        )
        session.update_control_projection(selection=(GROUND_ID,))
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5) as client:
            headers = {"Authorization": f"Bearer {CONSOLE_KEY.decode()}"}
            selected = client.post(
                f"/api/sessions/{SESSION}/navigation/select-map",
                headers=headers,
                json={"reference": reference},
            )
            assert selected.status_code == 200, selected.text

            def publish_world():
                with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=2) as producer:
                    while not stop_source.is_set():
                        status = device.status()
                        now = time.time_ns() // 1_000_000
                        response = producer.post(
                            f"/api/sessions/{SESSION}/observations",
                            headers={
                                "Authorization": f"Bearer {LOCALIZATION_KEY.decode()}",
                                "X-Sweep-Source": "localization",
                                "X-Sweep-Device-Id": str(GROUND_ID),
                            },
                            json={
                                "v": 1,
                                "type": "observation",
                                "event_id": f"world-{uuid4().hex}",
                                "session": SESSION,
                                "device_id": GROUND_ID,
                                "connection_epoch": node.connection_epoch,
                                "source_id": device_config.world_pose_source_id,
                                "node_type": "ground",
                                "t_capture": {"clock_id": "host", "unit": "ms", "value": now},
                                "t_source_receipt": {
                                    "clock_id": "host",
                                    "unit": "ms",
                                    "value": now,
                                },
                                "clock_mapping_id": "host-clock",
                                "frame": "world",
                                "confidence": world_confidence[0],
                                "payload": {
                                    "kind": "pose",
                                    "pose": {
                                        "parent_frame": "world",
                                        "child_frame": "body",
                                        "x_m": status.y + 3.0,
                                        "y_m": 10.0 - status.x,
                                        "z_m": 0.0,
                                        "qx": 0.0,
                                        "qy": 0.0,
                                        "qz": 0.0,
                                        "qw": 1.0,
                                    },
                                },
                            },
                        )
                        if response.status_code != 200:
                            errors.append(response.text)
                        stop_source.wait(0.06)

            def start_source():
                nonlocal source_thread
                source_thread = threading.Thread(target=publish_world, daemon=True)
                source_thread.start()
                _wait_for(
                    lambda: bool(
                        app.state.platform_services.observations.positions(
                            SESSION,
                            {
                                "reference": reference,
                                "mapVersion": deployment.map_version,
                                "floorId": deployment.floor_id,
                            },
                            session.current_state(),
                        )["observations"]
                    ),
                    "qualified world pose",
                )

            yield client, headers, session, device, start_source, stop_source, port, errors, path
    finally:
        stop_source.set()
        if source_thread is not None:
            source_thread.join(timeout=3)
        node.stop()
        if aircraft is not None:
            aircraft.stop()
        server.should_exit = True
        thread.join(timeout=5)
        composition.close()


def test_zero_confidence_world_pose_cannot_authorize_ground_navigation(
    ground_platform, world_confidence
):
    client, headers, session, device, start_source, _, _, _, _ = ground_platform
    world_confidence[0] = 0.0
    start_source()

    response = client.post(
        f"/api/sessions/{SESSION}/navigation/compile",
        headers=headers,
        json={"intentId": "zero-confidence", "query": "lobby"},
    )

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "navigation_execution_unavailable"
    assert "confidence" in response.json()["detail"]
    assert device.stop_confirmed()
    assert not any(
        row["event"].get("operation") == "ground_navigate" for row in session.replay()["events"]
    )


def test_world_confidence_loss_stops_a_running_ground_route(ground_platform, world_confidence):
    client, headers, session, device, start_source, _, _, _, _ = ground_platform
    start_source()
    base = f"/api/sessions/{SESSION}/navigation"
    response = client.post(
        base + "/compile",
        headers=headers,
        json={"intentId": "lose-world-confidence", "query": "lobby"},
    )
    assert response.status_code == 200, response.text
    envelope = response.json()
    preview = envelope["preview"]
    response = client.post(
        base + "/confirm",
        headers=headers,
        json={
            "previewId": preview["previewId"],
            "intentId": preview["intentId"],
            "previewHash": envelope["previewHash"],
        },
    )
    assert response.status_code == 200 and response.json()["status"] == "accepted", response.text
    _wait_for(lambda: device.status().state == "moving", "ground route before confidence loss")

    world_confidence[0] = 0.0
    _wait_for(
        lambda: any(
            row["event"].get("operation") == "hover" and row["event"].get("drone_id") == GROUND_ID
            for row in session.replay()["events"]
        ),
        "STOP after world confidence loss",
    )
    _wait_for(device.stop_confirmed, "world-confidence STOP confirmation")
    stopped = device.status()
    time.sleep(0.4)
    assert device.status().x == pytest.approx(stopped.x)
    assert device.status().y == pytest.approx(stopped.y)


@pytest.mark.parametrize("ground_platform", [True], indirect=True)
def test_hold_for_another_device_stops_the_preempted_ground_route(ground_platform):
    client, headers, session, device, start_source, _, port, _, _ = ground_platform
    start_source()
    with connect(f"ws://127.0.0.1:{port}/ws/{SESSION}", proxy=None) as console:
        console.send(
            json.dumps({"v": 1, "type": "auth", "source": "console", "token": CONSOLE_KEY.decode()})
        )
        _receive_until(console, lambda frame: frame.get("type") == "state")
        base = f"/api/sessions/{SESSION}/navigation"
        response = client.post(
            base + "/compile",
            headers=headers,
            json={"intentId": "ground-before-other-hold", "query": "lobby"},
        )
        assert response.status_code == 200, response.text
        envelope = response.json()
        preview = envelope["preview"]
        response = client.post(
            base + "/confirm",
            headers=headers,
            json={
                "previewId": preview["previewId"],
                "intentId": preview["intentId"],
                "previewHash": envelope["previewHash"],
            },
        )
        assert response.status_code == 200 and response.json()["status"] == "accepted", (
            response.text
        )
        _wait_for(lambda: device.status().state == "moving", "active reviewed ground route")
        ground_intent_id = next(
            row["event"]["intent_id"]
            for row in session.replay()["events"]
            if row["event"].get("operation") == "ground_navigate"
        )
        console.send(
            json.dumps(
                {
                    "v": 1,
                    "t": time.time_ns() // 1_000_000,
                    "type": "intent",
                    "intent_id": "hold-aircraft-only",
                    "retry_of": None,
                    "source": "console",
                    "session": SESSION,
                    "name": "hold",
                    "args": {},
                    "selection": [1],
                    "mode": "indoor",
                    "confirm": False,
                }
            )
        )
        _receive_until(
            console,
            lambda frame: (
                frame.get("source") == "autonomy"
                and frame.get("intent_id") == ground_intent_id
                and frame.get("status") == "invalidated"
            ),
        )
        _wait_for(
            lambda: any(
                row["event"].get("operation") == "hover"
                and row["event"].get("drone_id") == GROUND_ID
                for row in session.replay()["events"]
            ),
            "independent STOP for the cancelled ground route",
        )
        stopped_command = next(
            row["event"]
            for row in session.replay()["events"]
            if row["event"].get("operation") == "hover"
            and row["event"].get("drone_id") == GROUND_ID
        )
        _wait_for(
            lambda: any(
                row["event"].get("source") == "adapter"
                and row["event"].get("command_id") == stopped_command["command_id"]
                and row["event"].get("status") == "completed"
                for row in session.replay()["events"]
            ),
            "independent STOP acknowledgement",
        )
        _wait_for(device.stop_confirmed, "cancelled ground STOP confirmation")
        stopped = device.status()
        time.sleep(0.4)
        assert device.status().x == pytest.approx(stopped.x)
        assert device.status().y == pytest.approx(stopped.y)
        events = [row["event"] for row in session.replay()["events"]]
        assert stopped_command["intent_id"] != ground_intent_id
        assert not any(
            event.get("source") == "autonomy"
            and event.get("intent_id") == ground_intent_id
            and event.get("status") == "completed"
            for event in events
        )


def test_uncertain_delivery_stops_a_route_that_already_reached_the_ground_node(
    ground_platform, monkeypatch
):
    client, headers, session, device, start_source, _, port, _, _ = ground_platform
    deliver = RelayNodeLink._deliver

    def delivery_loses_its_confirmation(link, loop, device_id, frame):
        delivered = deliver(link, loop, device_id, frame)
        if delivered and frame.get("operation") == "ground_navigate":
            _wait_for(lambda: device.status().state == "moving", "route delivered before timeout")
            return False
        return delivered

    monkeypatch.setattr(RelayNodeLink, "_deliver", delivery_loses_its_confirmation)
    start_source()
    with connect(f"ws://127.0.0.1:{port}/ws/{SESSION}", proxy=None) as console:
        console.send(
            json.dumps({"v": 1, "type": "auth", "source": "console", "token": CONSOLE_KEY.decode()})
        )
        _receive_until(console, lambda frame: frame.get("type") == "state")
        base = f"/api/sessions/{SESSION}/navigation"
        response = client.post(
            base + "/compile",
            headers=headers,
            json={"intentId": "ground-delivery-timeout", "query": "lobby"},
        )
        assert response.status_code == 200, response.text
        envelope = response.json()
        preview = envelope["preview"]
        response = client.post(
            base + "/confirm",
            headers=headers,
            json={
                "previewId": preview["previewId"],
                "intentId": preview["intentId"],
                "previewHash": envelope["previewHash"],
            },
        )
        assert response.status_code == 200 and response.json()["status"] == "accepted", (
            response.text
        )
        terminal = _receive_until(
            console,
            lambda frame: frame.get("source") == "autonomy" and frame.get("status") == "failed",
        )
        assert "could not be delivered" in terminal["detail"]
        _wait_for(
            lambda: any(
                row["event"].get("operation") == "hover"
                and row["event"].get("drone_id") == GROUND_ID
                for row in session.replay()["events"]
            ),
            "STOP after uncertain route delivery",
        )
        stopped_command = next(
            row["event"]
            for row in session.replay()["events"]
            if row["event"].get("operation") == "hover"
            and row["event"].get("drone_id") == GROUND_ID
        )
        _wait_for(
            lambda: any(
                row["event"].get("command_id") == stopped_command["command_id"]
                and row["event"].get("source") == "adapter"
                and row["event"].get("status") == "completed"
                for row in session.replay()["events"]
            ),
            "STOP completion after uncertain delivery",
        )
        assert device.stop_confirmed()
        stopped = device.status()
        time.sleep(0.4)
        assert device.status().x == pytest.approx(stopped.x)
        assert device.status().y == pytest.approx(stopped.y)


@pytest.mark.parametrize("reply", [None, SimpleNamespace(status=LifecycleStatus.FAILED)])
def test_missing_or_failed_stop_ack_is_reported_and_its_waiter_released(reply):
    class StopSession:
        registry = SimpleNamespace(roster_version=4)

        def __init__(self):
            self.discarded = []

        def await_command_acknowledgement(self, _command_id, **_kwargs):
            return reply

        def discard_command_waiter(self, command_id):
            self.discarded.append(command_id)

    session = StopSession()
    stopped = []
    request = CommandRequest(
        command_id="navigate-command",
        intent_id="navigate-intent",
        roster_version=4,
        drone_id=GROUND_ID,
        connection_epoch=1,
        operation=CommandOperation.GROUND_NAVIGATE,
        args={},
    )
    guarded = _GroundGuardedLink(
        None, session, lambda _moving: None, stopped.append, stop_timeout_ms=10
    )
    guarded.request = request

    with pytest.raises(AdapterError, match="independent ground STOP was not confirmed"):
        guarded._stop()

    assert len(stopped) == 1
    assert stopped[0].operation is CommandOperation.HOVER
    assert stopped[0].intent_id != request.intent_id
    assert session.discarded == [stopped[0].command_id]


@pytest.mark.parametrize("withdraw_world_pose", [False, "world", "approval_file"])
def test_named_ground_preview_confirm_and_guard_reach_the_real_node(
    ground_platform, withdraw_world_pose, tmp_path
):
    client, headers, session, device, start_source, stop_source, port, errors, path = (
        ground_platform
    )
    base = f"/api/sessions/{SESSION}/navigation"
    missing = client.post(
        base + "/compile", headers=headers, json={"intentId": "missing-world", "query": "lobby"}
    )
    assert missing.status_code == 409, missing.text
    assert "world pose" in missing.text
    start_source()
    with connect(f"ws://127.0.0.1:{port}/ws/{SESSION}", proxy=None) as console:
        console.send(
            json.dumps({"v": 1, "type": "auth", "source": "console", "token": CONSOLE_KEY.decode()})
        )
        _receive_until(console, lambda frame: frame.get("type") == "state")
        voice = client.post(
            f"/api/sessions/{SESSION}/transcripts",
            headers={
                **headers,
                "Content-Type": "audio/webm",
                "X-Sweep-Correlation-Id": "ground-voice-review",
            },
            content=b"isolated voice upload",
        )
        assert voice.status_code == 200, voice.text
        assert voice.json()["plan"]["kind"] == "review", voice.json()
        assert voice.json()["emissions"] == []
        destination = voice.json()["plan"]["review"]["destination_id"]
        compiled = client.post(
            base + "/compile",
            headers=headers,
            json={"intentId": "ground-reviewed", "query": destination},
        )
        assert compiled.status_code == 200, compiled.text
        envelope = compiled.json()
        assert envelope["kind"] == "review", envelope
        preview = envelope["preview"]
        assert preview["dispatchEligible"] is True
        assert preview["routes"][0]["holdBehavior"] == "stop"
        confirmed = client.post(
            base + "/confirm",
            headers=headers,
            json={
                "previewId": preview["previewId"],
                "intentId": preview["intentId"],
                "previewHash": envelope["previewHash"],
            },
        )
        assert confirmed.status_code == 200, confirmed.text
        assert confirmed.json()["status"] == "accepted", confirmed.text

        def commands():
            return [
                record["event"]
                for record in session.replay()["events"]
                if record["event"].get("operation") == "ground_navigate"
            ]

        _wait_for(lambda: bool(commands()), "audited ground route command")
        command = commands()[0]
        assert command["operation"] == "ground_navigate"
        stop_started = threading.Event()
        release_stop = threading.Event()
        terminal_ready = threading.Event()
        terminal_frames = []
        if withdraw_world_pose == "world":
            stop_source.set()
        elif withdraw_world_pose == "approval_file":
            stop = device.stop

            def block_stop_confirmation():
                stop_started.set()
                assert release_stop.wait(timeout=5), "test did not release the ground STOP"
                stop()

            device.stop = block_stop_confirmation
            path.rename(tmp_path / "retired-ground-navigation.json")
        if withdraw_world_pose == "approval_file":

            def receive_terminal():
                terminal_frames.append(
                    _receive_until(
                        console,
                        lambda frame: (
                            frame.get("source") == "autonomy"
                            and frame.get("intent_id") == command["intent_id"]
                            and frame.get("status")
                            in {"completed", "failed", "refused", "invalidated"}
                        ),
                    )
                )
                terminal_ready.set()

            terminal_thread = threading.Thread(target=receive_terminal, daemon=True)
            terminal_thread.start()
            assert stop_started.wait(timeout=5), "independent STOP was not sent"
            assert not terminal_ready.wait(timeout=0.2), (
                "terminal result preceded STOP confirmation"
            )
            release_stop.set()
            terminal_thread.join(timeout=5)
            assert terminal_ready.is_set(), "terminal result did not follow STOP confirmation"
            terminal = terminal_frames[0]
        else:
            terminal = _receive_until(
                console,
                lambda frame: (
                    frame.get("source") == "autonomy"
                    and frame.get("intent_id") == command["intent_id"]
                    and frame.get("status") in {"completed", "failed", "refused", "invalidated"}
                ),
            )
        if withdraw_world_pose:
            assert terminal["status"] != "completed", terminal
            if withdraw_world_pose == "world":
                assert "pose" in terminal.get("detail", ""), terminal
        else:
            assert terminal["status"] == "completed", terminal
            assert device.status().y >= -0.80
        assert device.stop_confirmed()
        assert not errors
        repeat = client.post(
            base + "/confirm",
            headers=headers,
            json={
                "previewId": preview["previewId"],
                "intentId": preview["intentId"],
                "previewHash": envelope["previewHash"],
            },
        )
        assert repeat.json()["code"] == "confirmation_consumed"
        output = tmp_path / "ground-session.mcap"
        export_audit(session.audit_log.path, SESSION, output)
        events = [record["event"] for record in read_replay(output, SESSION)]
        route = next(event for event in events if event.get("operation") == "ground_navigate")
        assert route["args"] == command["args"]
        map_identity = next(event for event in events if event["type"] == "map_identity")
        registration = next(event for event in events if event["type"] == "registration")
        assert map_identity["map_sha256"] == preview["execution"]["mapPin"]["contentSha256"]
        assert registration["registration_id"] == "fixture-transform"
        assert any(event["type"] == "world_observation" for event in events)
        with output.open("rb") as stream:
            scenes = [
                json.loads(message.data)
                for _, channel, message in make_reader(stream).iter_messages()
                if channel.topic == "/sweep/scene"
            ]
        assert any(
            entity["frame_id"] == "world" and entity["spheres"]
            for scene in scenes
            for entity in scene["entities"]
        )
        assert any(
            entity["frame_id"] == "world" and entity["lines"]
            for scene in scenes
            for entity in scene["entities"]
        )
