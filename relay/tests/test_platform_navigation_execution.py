from __future__ import annotations

import asyncio
import json
import threading
import time
from dataclasses import asdict, replace
from hashlib import sha256
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from planner.models import Geofence
from planner.navigation import MotionConfig
from planner.navigation_authorization import content_digest
from planner.navigation_deployment import NavigationDeployment, load_navigation_deployment
from planner.navigation_runtime import PrecisionReturnBinding, navigation_configuration_digest
from planner.test_navigation_deployment import _flight_deployment_files
from planner.test_navigation_runtime import KEY
from relay.auth import Principal, sign_event
from relay.autonomy import AutonomyComposition, AutonomyConfig, create_autonomy_app
from relay.control_localization import ControlLocalizationProjector, ControlPose
from relay.intent_v1 import IntentName, IntentV1, Mode
from relay.navigation_service import NavigationService
from relay.navigation_wire import NavigationTrackingError
from relay.platform import _FlightExecutionAdapter
from relay.settings import AdapterBackend, RelaySettings
from relay.tests.conftest import (
    ADAPTER_KEY,
    CONSOLE_KEY,
    EventIds,
    MutableClock,
    membership_payload,
    telemetry_payload,
)
from tests.autonomy_fixtures import planning_config, safety_config

SESSION = "flight-session"
LOCALIZATION_KEY = b"localization-test-key-32-characters"


def _deployment(tmp_path: Path, *, precision_return: bool = False) -> NavigationDeployment:
    path, _, _ = _flight_deployment_files(tmp_path)
    original = load_navigation_deployment(path)
    artifact = original.artifact()
    document = json.loads(path.read_text())
    limits = document["wire_profiles"]["1"]
    limits.update(
        max_position_uncertainty_mm=5,
        max_cross_track_mm=5,
        arrival_horizontal_tolerance_mm=5,
        arrival_vertical_tolerance_mm=5,
        max_deceleration_mm_s2=4_000,
    )
    tuning_path = tmp_path / document["wire_navigation_files"]["1"]
    tuning = json.loads(tuning_path.read_text())
    tuning["limits"].update(
        max_position_uncertainty_mm=5,
        max_cross_track_mm=5,
        arrival_horizontal_tolerance_mm=5,
        arrival_vertical_tolerance_mm=5,
        max_deceleration_mm_s2=4_000,
    )
    encoded_tuning = json.dumps(tuning, sort_keys=True, separators=(",", ":")).encode()
    tuning_path.write_bytes(encoded_tuning)
    limits["navigation_config_sha256"] = sha256(encoded_tuning).hexdigest()
    config = replace(
        original.config,
        motion=MotionConfig(0.005, 0.005, 0.001, 0.005, 0.005, 0.01, 0.2),
        position_tolerance_m=0.005,
        wire_config_sha256=content_digest({"1": limits}),
        precision_returns=(
            (PrecisionReturnBinding(1, 1, "lobby", "flight-home"),) if precision_return else ()
        ),
    )
    document["execution"] = asdict(config)
    path.write_text(json.dumps(document))
    approval_path = tmp_path / "approval.json"
    approval = json.loads(approval_path.read_text())
    approval["epochs"] = [[1, 1]]
    approval["configuration_sha256"] = navigation_configuration_digest(
        artifact, config, original.permission, original.home_zone_id
    )
    approval_unsigned = {key: value for key, value in approval.items() if key != "signature"}
    approval["signature"] = sign_event(approval_unsigned, KEY)
    approval_path.write_text(json.dumps(approval))
    return load_navigation_deployment(path)


def test_precision_return_uses_its_marked_slot_and_refuses_a_changed_aircraft_identity(
    tmp_path: Path,
) -> None:
    deployment = _deployment(tmp_path, precision_return=True)
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
        with TestClient(app):
            _prepare_session(composition, deployment)
            preview = _preview(deployment)
            planned = composition.preview_platform_navigation(SESSION, preview)
            assert planned["routes"][0]["arrivalSlot"]["slotId"] == "flight-home"
            assert planned["routes"][0]["holdBehavior"] == "hover"
            changed = {**preview, "selected": [{"id": 1, "deviceClass": "aircraft", "epoch": 2}]}
            with pytest.raises(ValueError, match="precision return"):
                composition.preview_platform_navigation(SESSION, changed)
    finally:
        composition.close()


def _projector(deployment: NavigationDeployment) -> ControlLocalizationProjector:
    pins = deployment.config.frames[0].control_pins
    assert pins is not None
    return ControlLocalizationProjector(
        {1: pins},
        relay_clock_id=pins.clock_mapping.relay_clock_id,
        max_clock_error_ms=2,
        max_fix_age_ms=500,
        max_velocity_age_ms=200,
        max_height_age_ms=200,
        max_position_uncertainty_p95_m=0.3,
    )


def _preview(deployment: NavigationDeployment) -> dict[str, object]:
    artifact = deployment.artifact()
    return {
        "previewId": "platform-preview-1",
        "intentId": "platform-intent-1",
        "expiresAt": 115_000,
        "destination": {"zoneId": "lobby"},
        "selected": [{"id": 1, "deviceClass": "aircraft", "epoch": 1}],
        "map": {
            "mapPin": {
                "version": artifact.map_pin.version,
                "contentSha256": artifact.map_pin.content_sha256,
            }
        },
    }


def _prepare_session(
    composition: AutonomyComposition, deployment: NavigationDeployment
) -> tuple[object, object]:
    runtime = composition.runtime
    session = runtime.session(SESSION)
    adapter = Principal("adapter", 1, ADAPTER_KEY)
    session.process_membership(
        membership_payload(
            action="join",
            event_id="platform-join",
            session=SESSION,
            timestamp=100_000,
            capabilities=["flight", "pano_360", "navigate"],
        ),
        adapter,
    )
    telemetry = telemetry_payload(
        event_id="platform-telemetry", session=SESSION, timestamp=100_000, state="hovering"
    )
    telemetry.update(x=-20.0, y=9.8, z=-29.0)
    session.process_telemetry(telemetry, adapter)
    session.process_membership(
        membership_payload(
            action="readiness", event_id="platform-ready", session=SESSION, timestamp=100_000
        ),
        adapter,
    )
    session.update_control_projection(selection=(1,), armed=True)
    pins = deployment.config.frames[0].control_pins
    assert pins is not None
    session._control_pose[1] = ControlPose(
        t=100_000,
        event_id="platform-control-pose",
        session=SESSION,
        drone_id=1,
        connection_epoch=1,
        map_id=pins.map_id,
        geometry_id=pins.geometry_id,
        camera_calibration_id=pins.camera_calibration_id,
        body_extrinsics_id=pins.body_extrinsics_id,
        pose_time_ms=99_998,
        fix_time_ms=99_998,
        x_mm=-20_000,
        y_mm=9_800,
        z_mm=-29_000,
        position_frame="map_enu",
        position_uncertainty_mm=1,
        status="ready",
    )
    autonomy = composition.session(SESSION)
    autonomy._operator_last_seen_ms = 100_000
    return session, autonomy


@pytest.mark.parametrize("terminal_status", ["completed", "failed"])
def test_platform_confirmation_runs_the_retained_route_through_the_real_flight_wire(
    tmp_path: Path, terminal_status: str
) -> None:
    deployment = _deployment(tmp_path)
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
            preview = _preview(deployment)
            execution = autonomy.preview_platform_navigation(preview)
            callback_results: list[tuple[str, str, str, str]] = []
            composition.set_multiview_listener(
                lambda session_id, intent_id, name, status: callback_results.append(
                    (session_id, intent_id, name, status)
                )
            )
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
                reserved = autonomy.reserve_platform_navigation(
                    {**preview, "execution": execution["execution"]}
                )
                assert reserved["status"] == "accepted"
                confirmation = autonomy.dispatch_reserved_platform_navigation(preview["previewId"])
                assert confirmation["status"] == "accepted"
                command = None
                frames = []
                for _ in range(64):
                    frame = adapter.receive_json()
                    frames.append(frame)
                    if frame.get("type") == "command":
                        command = frame
                        break
                assert {frame["type"] for frame in frames} >= {
                    "acknowledgement",
                    "navigation_route_authorization",
                    "navigation_pose",
                    "command",
                }
                assert command is not None
                if terminal_status == "completed":
                    clock.value = 100_003
                    telemetry = telemetry_payload(
                        event_id="platform-arrived",
                        session=SESSION,
                        timestamp=100_003,
                        state="hovering",
                    )
                    telemetry.update(x=-20.0, y=10.0, z=-29.0)
                    session.process_telemetry(telemetry, Principal("adapter", 1, ADAPTER_KEY))
                    pose = session.control_pose(1)
                    assert pose is not None
                    session._control_pose[1] = replace(
                        pose,
                        t=100_003,
                        event_id="platform-arrived-pose",
                        pose_time_ms=100_001,
                        fix_time_ms=100_001,
                        y_mm=10_000,
                    )
                adapter.send_json(
                    {
                        "v": 1,
                        "t": 100_003 if terminal_status == "completed" else 100_000,
                        "type": "acknowledgement",
                        "event_id": f"platform-{terminal_status}",
                        "session": SESSION,
                        "intent_id": command["intent_id"],
                        "command_id": command["command_id"],
                        "status": terminal_status,
                        "drone_id": 1,
                        "connection_epoch": 1,
                        "roster_version": command["roster_version"],
                        "reason": None if terminal_status == "completed" else "adapter_failed",
                        "detail": None,
                    }
                )
                wire_lifecycle = []
                for _ in range(64):
                    frame = adapter.receive_json()
                    wire_lifecycle.append(frame)
                    if terminal_status == "completed" and frame.get("type") == "command":
                        clock.value += 3
                        telemetry = telemetry_payload(
                            event_id=f"platform-arrived-{frame['command_id']}",
                            session=SESSION,
                            timestamp=clock.value,
                            state="hovering",
                        )
                        telemetry.update(x=-20.0, y=10.0, z=-29.0)
                        session.process_telemetry(telemetry, Principal("adapter", 1, ADAPTER_KEY))
                        pose = session.control_pose(1)
                        assert pose is not None
                        session._control_pose[1] = replace(
                            pose,
                            t=clock.value,
                            event_id=f"platform-arrived-pose-{frame['command_id']}",
                            pose_time_ms=clock.value - 2,
                            fix_time_ms=clock.value - 2,
                            y_mm=10_000,
                        )
                        adapter.send_json(
                            {
                                "v": 1,
                                "t": clock.value,
                                "type": "acknowledgement",
                                "event_id": f"platform-completed-{frame['command_id']}",
                                "session": SESSION,
                                "intent_id": frame["intent_id"],
                                "command_id": frame["command_id"],
                                "status": "completed",
                                "drone_id": 1,
                                "connection_epoch": 1,
                                "roster_version": frame["roster_version"],
                                "reason": None,
                                "detail": None,
                            }
                        )
                    if (
                        frame.get("source") == "autonomy"
                        and frame.get("intent_id") == command["intent_id"]
                    ):
                        break
                terminal = wire_lifecycle[-1]
                if terminal_status == "completed":
                    assert terminal["status"] == "completed", json.dumps(wire_lifecycle)
                else:
                    assert terminal["status"] == "refused", json.dumps(wire_lifecycle)
                    assert terminal["reason"] == "adapter_failure"
                expected_status = "completed" if terminal_status == "completed" else "refused"
                expected_callback = (SESSION, "platform-intent-1", "navigate", expected_status)
                deadline = time.monotonic() + 1
                while expected_callback not in callback_results and time.monotonic() < deadline:
                    time.sleep(0.01)
                assert expected_callback in callback_results
                assert session.audit_log.root.exists()
    finally:
        composition.close()


def test_tracking_disagreement_terminates_the_active_platform_route(tmp_path: Path) -> None:
    deployment = _deployment(tmp_path)
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
            preview = _preview(deployment)
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
                autonomy.confirm_platform_navigation(
                    {**preview, "execution": execution["execution"]}
                )
                command = next(
                    frame
                    for _ in range(64)
                    if (frame := adapter.receive_json()).get("type") == "command"
                )
                for status in ("accepted", "executing"):
                    adapter.send_json(
                        {
                            "v": 1,
                            "t": 100_000,
                            "type": "acknowledgement",
                            "event_id": f"tracking-{status}",
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
                deadline = threading.Event()
                for _ in range(300):
                    if command["intent_id"] in autonomy._awaiting:
                        deadline.set()
                        break
                    time.sleep(0.01)
                assert deadline.is_set()
                clock.value = 100_001
                telemetry = telemetry_payload(
                    event_id="tracking-disagreement-telemetry",
                    session=SESSION,
                    timestamp=clock(),
                    state="hovering",
                )
                telemetry.update(x=-19.9, y=9.8, z=-29.0)
                session.process_telemetry(telemetry, Principal("adapter", 1, ADAPTER_KEY))
                pose = session.control_pose(1)
                assert pose is not None
                session._control_pose[1] = replace(
                    pose,
                    t=clock() - 2,
                    event_id="tracking-disagreement-pose",
                    pose_time_ms=clock() - 2,
                    fix_time_ms=clock() - 2,
                )
                asyncio.run_coroutine_threadsafe(
                    composition.runtime.publish(
                        SESSION,
                        [{**session._control_pose[1].unsigned_event(), "signature": "test"}],
                    ),
                    composition.runtime.loop,
                ).result(timeout=2)
                terminal = next(
                    frame
                    for _ in range(64)
                    if (frame := adapter.receive_json()).get("intent_id") == command["intent_id"]
                    and frame.get("source") == "autonomy"
                    and frame.get("status") == "failed"
                )
                assert terminal["status"] == "failed"
                assert terminal["reason"] == "invalid_plan"
                assert "disagrees with adapter ENU telemetry" in terminal["detail"]
                assert command["intent_id"] not in autonomy._awaiting
                assert session.current_state()["accepted_plan"] is None
                hold = next(
                    frame
                    for _ in range(64)
                    if (frame := adapter.receive_json()).get("type") == "command"
                )
                assert hold["operation"] == "hover"
                assert hold["intent_id"].startswith("safety:navigation-tracking:")
    finally:
        composition.close()


def test_tracking_failure_before_awaiting_execution_still_stops_the_platform_route(
    tmp_path: Path,
) -> None:
    deployment = _deployment(tmp_path)
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
            preview = _preview(deployment)
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
                autonomy.confirm_platform_navigation(
                    {**preview, "execution": execution["execution"]}
                )
                command = next(
                    frame
                    for _ in range(64)
                    if (frame := adapter.receive_json()).get("type") == "command"
                )
                active = None
                for _ in range(100):
                    active = next(iter(autonomy.navigation_wire._active.values()), None)
                    if active is not None:
                        break
                    time.sleep(0.01)
                assert active is not None
                events = autonomy.fail_navigation_tracking(
                    NavigationTrackingError(active, "control pose lost before execution wait")
                )
                asyncio.run_coroutine_threadsafe(
                    composition.runtime.publish(SESSION, events), composition.runtime.loop
                ).result(timeout=2)
                terminal = next(
                    frame
                    for _ in range(64)
                    if (frame := adapter.receive_json()).get("intent_id") == command["intent_id"]
                    and frame.get("source") == "autonomy"
                    and frame.get("status") == "failed"
                )
                assert terminal["reason"] == "invalid_plan"
                assert command["intent_id"] not in autonomy._awaiting
                assert session.current_state()["accepted_plan"] is None
                hold = next(
                    frame
                    for _ in range(64)
                    if (frame := adapter.receive_json()).get("type") == "command"
                )
                assert hold["operation"] == "hover"
                assert hold["intent_id"].startswith("safety:navigation-tracking:")
    finally:
        composition.close()


def test_platform_confirmation_refreshes_only_the_internal_admission_timestamp(
    tmp_path: Path,
) -> None:
    deployment = _deployment(tmp_path)
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
            preview = _preview(deployment)
            execution = autonomy.preview_platform_navigation(preview)
            clock.value = 106_000
            telemetry = telemetry_payload(
                event_id="platform-fresh-confirmation-telemetry",
                session=SESSION,
                timestamp=106_000,
                state="hovering",
            )
            telemetry.update(x=-20.0, y=9.8, z=-29.0)
            session.process_telemetry(telemetry, Principal("adapter", 1, ADAPTER_KEY))
            pose = session.control_pose(1)
            assert pose is not None
            session._control_pose[1] = replace(
                pose,
                t=106_000,
                event_id="platform-fresh-confirmation-pose",
                pose_time_ms=105_998,
                fix_time_ms=105_998,
            )
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
                confirmation = autonomy.confirm_platform_navigation(
                    {**preview, "execution": execution["execution"]}
                )
                assert confirmation["status"] == "accepted"
                command = next(
                    frame
                    for _ in range(64)
                    if (frame := adapter.receive_json()).get("type") == "command"
                )
                assert command["intent_id"] == "platform:platform-preview-1"
                assert command["issued_at"] == 106_000
                with pytest.raises(ValueError, match="retained navigation plan is unavailable"):
                    autonomy.confirm_platform_navigation(
                        {**preview, "execution": execution["execution"]}
                    )
    finally:
        composition.close()


def test_hold_between_reserved_admission_and_execution_never_starts_a_goto(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deployment = _deployment(tmp_path)
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
        with TestClient(app):
            session, autonomy = _prepare_session(composition, deployment)
            preview = _preview(deployment)
            execution = autonomy.preview_platform_navigation(preview)
            reserved = {**preview, "execution": execution["execution"]}
            assert autonomy.reserve_platform_navigation(reserved)["status"] == "accepted"

            admitted = threading.Event()
            release_execution = threading.Event()
            original_publish = autonomy._publish
            publishes = 0

            def block_after_admission(runtime, operation):
                nonlocal publishes
                original_publish(runtime, operation)
                publishes += 1
                if publishes == 1:
                    admitted.set()
                    assert release_execution.wait(2)

            monkeypatch.setattr(autonomy, "_publish", block_after_admission)
            execute_pending_calls = 0
            original_execute_pending = session.execute_pending_intent

            def count_execute_pending(*args, **kwargs):
                nonlocal execute_pending_calls
                execute_pending_calls += 1
                return original_execute_pending(*args, **kwargs)

            monkeypatch.setattr(session, "execute_pending_intent", count_execute_pending)
            result: list[BaseException] = []

            def dispatch() -> None:
                try:
                    autonomy.dispatch_reserved_platform_navigation("platform-preview-1")
                except BaseException as error:
                    result.append(error)

            worker = threading.Thread(target=dispatch)
            worker.start()
            assert admitted.wait(2)
            autonomy.submit(
                IntentV1(
                    v=1,
                    t=clock(),
                    type="intent",
                    intent_id="barrier-hold",
                    retry_of=None,
                    source="console",
                    session=SESSION,
                    name=IntentName.HOLD,
                    args={},
                    selection=(1,),
                    mode=Mode.INDOOR,
                    confirm=False,
                ),
                session.current_state(),
            )
            release_execution.set()
            worker.join(2)
            assert not worker.is_alive()
            assert len(result) == 1
            assert isinstance(result[0], ValueError)
            assert execute_pending_calls == 0
    finally:
        composition.close()


def test_console_hold_preempts_a_running_platform_route_before_another_wire_command(
    tmp_path: Path,
) -> None:
    deployment = _deployment(tmp_path)
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
            _session, autonomy = _prepare_session(composition, deployment)
            preview = _preview(deployment)
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
                autonomy.confirm_platform_navigation(
                    {**preview, "execution": execution["execution"]}
                )
                command = next(
                    frame
                    for _ in range(64)
                    if (frame := adapter.receive_json()).get("type") == "command"
                )
                with client.websocket_connect(f"/ws/{SESSION}") as console:
                    console.send_json(
                        {"v": 1, "type": "auth", "source": "console", "token": CONSOLE_KEY.decode()}
                    )
                    assert console.receive_json()["type"] == "auth.accepted"
                    assert console.receive_json()["type"] == "state"
                    console.send_json(
                        {
                            "v": 1,
                            "t": 100_000,
                            "type": "intent",
                            "intent_id": "platform-route-hold",
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
                    acknowledged = next(
                        frame
                        for _ in range(64)
                        if (frame := console.receive_json()).get("type") == "acknowledgement"
                    )
                    assert acknowledged["intent_id"] == "platform-route-hold"
                    invalidated = next(
                        frame
                        for _ in range(64)
                        if (
                            (frame := adapter.receive_json()).get("intent_id")
                            == command["intent_id"]
                            and frame.get("status") == "invalidated"
                        )
                    )
                    assert invalidated["reason"] == "preempted_by_hold"
                    assert autonomy._platform_dispatch == {}
            replay = client.get(
                f"/session/{SESSION}",
                headers={"Authorization": f"Bearer {CONSOLE_KEY.decode()}"},
            )
            assert replay.status_code == 200
            events = [record["event"] for record in replay.json()["events"]]
            assert any(
                event.get("intent_id") == command["intent_id"]
                and event.get("status") == "invalidated"
                for event in events
            )
    finally:
        composition.close()


def _approved_bundle(deployment: NavigationDeployment) -> dict[str, object]:
    artifact = deployment.artifact()
    reference = {
        "bundleId": "qualified-flight-map",
        "revision": "1",
        "contentHash": artifact.map_pin.content_sha256,
    }
    return {
        "reference": reference,
        "approval": {"reference": reference, "auditId": "qualified-flight-map-approval"},
        "bundle": {
            "manifest": {
                "frame": "world",
                "floorId": "level_1",
                "mapVersion": artifact.map_pin.version,
            },
            "zones": [{"id": "lobby", "name": "Lobby", "aliases": []}],
            "corridors": [],
            "obstacles": [],
            "geofence": [],
        },
    }


def test_map_change_invalidates_a_real_flight_execution_review_before_admission(
    tmp_path: Path,
) -> None:
    deployment = _deployment(tmp_path)
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
    navigation: NavigationService | None = None
    try:
        with TestClient(app):
            session, autonomy = _prepare_session(composition, deployment)
            flight_execution = _FlightExecutionAdapter(composition)
            navigation = NavigationService(
                tmp_path / "navigation.sqlite3",
                clock_ms=clock,
                approved_bundle=lambda _session: _approved_bundle(deployment),
                state=lambda _session: session.current_state(),
                motion_config=lambda _session: {"source": "qualified-flight-deployment"},
                flight_execution=flight_execution,
            )
            catalog = navigation.catalog(SESSION)["catalog"]
            preview = navigation.preview(
                SESSION,
                {
                    "session": SESSION,
                    "intentId": "map-change-intent",
                    "zoneId": "lobby",
                    "rosterVersion": session.registry.roster_version,
                    "selected": [{"id": 1, "deviceClass": "aircraft", "epoch": 1}],
                    **{
                        name: catalog[name]
                        for name in ("catalogVersion", "map", "configVersion", "motionConfig")
                    },
                },
            )
            assert preview["preview"]["dispatchEligible"] is True
            preview_id = preview["preview"]["previewId"]
            assert flight_execution.reserve(SESSION, preview["preview"])["status"] == "accepted"
            assert preview_id in autonomy._platform_navigation_reservations
            assert flight_execution.discard_reserved(SESSION, preview_id) == {"status": "discarded"}
            assert preview_id not in autonomy._platform_navigation_reservations
            assert preview_id not in autonomy._platform_navigation_intents_by_preview
            navigation.invalidate(SESSION)
            confirmation = navigation.confirm(
                SESSION,
                {
                    "previewId": preview["preview"]["previewId"],
                    "intentId": "map-change-intent",
                    "previewHash": preview["previewHash"],
                },
            )
            assert confirmation["status"] == "invalidated"
            assert confirmation["code"] == "frozen_inputs_changed"
            assert autonomy._platform_dispatch == {}
    finally:
        if navigation is not None:
            navigation.close()
        composition.close()


def test_platform_preview_uses_a_prior_qualified_arrival_as_its_next_route_start(
    tmp_path: Path,
) -> None:
    deployment = _deployment(tmp_path)
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
        with TestClient(app):
            _, autonomy = _prepare_session(composition, deployment)
            preview = _preview(deployment)
            planned = autonomy.preview_platform_navigation(
                {
                    **preview,
                    "trustedStart": {
                        "target": preview["selected"][0],
                        "position": {
                            "xM": 0.0,
                            "yM": 0.0,
                            "zM": 1.0,
                            "floorId": "level_1",
                            "frame": "world",
                        },
                    },
                }
            )
            route = planned["routes"][0]
            assert route["waypoints"][0] == {
                "xM": 0.0,
                "yM": 0.0,
                "zM": 1.0,
                "floorId": "level_1",
                "frame": "world",
            }
    finally:
        composition.close()
