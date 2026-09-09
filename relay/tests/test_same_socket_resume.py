from __future__ import annotations

import time
from dataclasses import replace
from queue import Empty, Queue
from threading import Thread

from fastapi.testclient import TestClient

from planner.models import Geofence
from relay.auth import Principal
from relay.autonomy import AutonomyConfig, create_autonomy_app
from relay.settings import AdapterBackend, RelaySettings
from relay.tests.conftest import ADAPTER_KEY, CONSOLE_KEY, EventIds, MutableClock, telemetry_payload
from relay.tests.test_platform_navigation_execution import (
    LOCALIZATION_KEY,
    SESSION,
    _prepare_session,
    _preview,
    _projector,
)
from tests.autonomy_fixtures import planning_config, safety_config
from tools.loopback_demo_rehearsal import _three_destination_deployment


def test_late_completion_leaves_the_same_socket_free_to_acknowledge_the_next_goto(tmp_path):
    deployment = _three_destination_deployment(tmp_path)
    clock = MutableClock(100_000)
    settings = RelaySettings(
        relay_token=CONSOLE_KEY,
        adapter_keys={1: ADAPTER_KEY},
        localization_keys={1: LOCALIZATION_KEY},
        log_dir=tmp_path / "logs",
        adapter_backend=AdapterBackend.REMOTE,
    )
    config = AutonomyConfig(
        planning=replace(planning_config(), flight_speed_m_s=0.2),
        safety=replace(
            safety_config(), geofence=Geofence(-100, 100, -100, 100, -100, 100), ceiling_m=50
        ),
        control_localization_projector=_projector(deployment),
        navigation=deployment,
    )
    app, composition = create_autonomy_app(settings, config, clock=clock, event_ids=EventIds())
    try:
        with TestClient(app) as client:
            session, autonomy = _prepare_session(composition, deployment)
            preview = {**_preview(deployment), "destination": {"zoneId": "demo-west"}}
            execution = autonomy.preview_platform_navigation(preview)
            with client.websocket_connect(f"/ws/{SESSION}") as adapter:
                adapter.send_json(
                    {
                        "v": 1,
                        "type": "auth",
                        "source": "adapter",
                        "drone_id": 1,
                        "token": ADAPTER_KEY.decode(),
                    }
                )
                assert adapter.receive_json()["type"] == "auth.accepted"
                assert adapter.receive_json()["type"] == "state"
                received: Queue[dict | Exception] = Queue()
                observed = []

                def read_frames():
                    try:
                        while True:
                            received.put(adapter.receive_json())
                    except Exception as error:
                        received.put(error)

                def receive(predicate):
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        try:
                            frame = received.get(timeout=max(0.001, deadline - time.monotonic()))
                        except Empty as error:
                            raise AssertionError(observed) from error
                        if isinstance(frame, Exception):
                            raise frame
                        observed.append(frame)
                        if predicate(frame):
                            return frame
                    raise AssertionError(observed)

                def acknowledge(command, status):
                    adapter.send_json(
                        {
                            "v": 1,
                            "t": clock.value,
                            "type": "acknowledgement",
                            "event_id": f"{command['command_id']}:{status}",
                            "session": SESSION,
                            "intent_id": command["intent_id"],
                            "command_id": command["command_id"],
                            "status": status,
                            "drone_id": 1,
                            "connection_epoch": 1,
                            "roster_version": command["roster_version"],
                            "reason": None,
                            "detail": None,
                        }
                    )

                Thread(target=read_frames, daemon=True).start()
                autonomy.reserve_platform_navigation(
                    {**preview, "execution": execution["execution"]}
                )
                autonomy.dispatch_reserved_platform_navigation(preview["previewId"])
                first = receive(lambda frame: frame.get("type") == "command")
                assert first["operation"] == "goto"
                acknowledge(first, "accepted")
                acknowledge(first, "executing")
                deadline = time.monotonic() + 3
                while first["intent_id"] not in autonomy._awaiting and time.monotonic() < deadline:
                    time.sleep(0.01)
                assert first["intent_id"] in autonomy._awaiting

                clock.value += 3
                target = {axis: first["args"][f"{axis}_mm"] / 1000 for axis in ("x", "y", "z")}
                telemetry = telemetry_payload(
                    event_id="first-segment-arrival",
                    session=SESSION,
                    timestamp=clock.value,
                    state="hovering",
                )
                telemetry.update(target)
                session.process_telemetry(telemetry, Principal("adapter", 1, ADAPTER_KEY))
                with session._lock:
                    session._control_pose[1] = replace(
                        session._control_pose[1],
                        t=clock.value,
                        event_id="first-segment-arrival-pose",
                        pose_time_ms=clock.value - 2,
                        fix_time_ms=clock.value - 2,
                        **{f"{axis}_mm": first["args"][f"{axis}_mm"] for axis in ("x", "y", "z")},
                    )
                acknowledge(first, "completed")
                second = receive(lambda frame: frame.get("type") == "command")
                assert second["operation"] == "goto", observed
                assert second["command_id"] != first["command_id"]
                assert second["intent_id"] == first["intent_id"]
                acknowledge(second, "accepted")
                acknowledge(second, "executing")
                executing = receive(
                    lambda frame: (
                        frame.get("intent_id") == second["intent_id"]
                        and (
                            frame.get("status") in {"failed", "refused"}
                            or (
                                frame.get("command_id") == second["command_id"]
                                and frame.get("status") == "executing"
                            )
                        )
                    ),
                )
                assert executing["status"] == "executing", observed
                assert executing["source"] == "adapter"

                def awaiting_second_command():
                    with autonomy._lock:
                        owner = autonomy._awaiting.get(second["intent_id"])
                        return owner is not None and any(
                            acknowledgement.command_id == second["command_id"]
                            and acknowledgement.status.value == "executing"
                            for acknowledgement in owner.pending.acknowledgements
                        )

                deadline = time.monotonic() + 3
                while not awaiting_second_command() and time.monotonic() < deadline:
                    time.sleep(0.01)
                assert awaiting_second_command(), observed
    finally:
        composition.close()
