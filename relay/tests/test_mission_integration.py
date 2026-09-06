from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from perception.detection_publisher import DetectionPublisher, DetectionPublisherConfig
from perception.object_detection import (
    DecodedFrame,
    DetectionCandidate,
    FrameIdentity,
    LiveDetectionWorker,
    ProcessedFrameEvent,
    SightingEvent,
)
from perception.search_events import CameraPolicy
from planner.control_provenance import ControlProvenance
from planner.navigation import ArrivalSlot, NavigationPermission
from planner.navigation_acceptance import NavigationDispatchAcceptance
from planner.navigation_runtime import NavigationExecutionConfig, NavigationRuntime
from planner.search import SearchArea
from planner.test_navigation import MOTION, artifact, pose
from relay.auth import Principal
from relay.autonomy import AutonomyConfig, create_autonomy_app
from relay.control_config import ControlRuntimeConfig
from relay.control_localization import ClockMapping
from relay.search_bridge import SearchBridge
from relay.search_runtime import SearchMissionPreview, SearchRuntime, SearchRuntimeConfig
from relay.settings import AdapterBackend, RelaySettings
from relay.tests.conftest import (
    ADAPTER_KEY,
    CONSOLE_KEY,
    SESSION,
    EventIds,
    MutableClock,
    capabilities_payload,
    capture_readiness_payload,
    membership_payload,
    telemetry_payload,
)
from tests.autonomy_fixtures import camera_config, planning_config, safety_config

PERCEPTION_KEY = b"perception-key-that-is-at-least-32"
_DETECTOR_CONFIG_SHA256 = "a" * 64


def _settings(log_dir: Path) -> RelaySettings:
    return RelaySettings(
        relay_token=CONSOLE_KEY,
        adapter_keys={1: ADAPTER_KEY},
        perception_key=PERCEPTION_KEY,
        log_dir=log_dir,
        adapter_backend=AdapterBackend.SIM,
    )


def _navigation() -> NavigationRuntime:
    return NavigationRuntime(
        lambda: artifact(slots=(ArrivalSlot("atrium-1", "atrium", pose(6.5, 1.5), 0.5, 0.5),)),
        NavigationExecutionConfig("level_1", MOTION, 0.5, 0.05, 500, 0.5, 5_000),
        NavigationPermission(frozenset({"atrium"})),
        dispatch_acceptance=lambda plan, current: NavigationDispatchAcceptance(
            "sim-test",
            current.map_pin,
            current.geometry_pin,
            current.navigation_pin,
            plan.plan_revision,
        ),
    )


def _intent(
    name: str,
    intent_id: str,
    *,
    selection: list[int],
    args: dict[str, object] | None = None,
    confirm: bool = False,
) -> dict[str, object]:
    return {
        "v": 1,
        "t": 1_756_700_000_000,
        "type": "intent",
        "intent_id": intent_id,
        "retry_of": None,
        "source": "console",
        "session": SESSION,
        "name": name,
        "args": args or {},
        "selection": selection,
        "mode": "indoor",
        "confirm": confirm,
    }


def _authenticate(socket, source: str) -> None:
    token = PERCEPTION_KEY if source == "perception" else CONSOLE_KEY
    frame = {"v": 1, "type": "auth", "source": source, "token": token.decode()}
    if source in {"adapter", "localization"}:
        frame.update(drone_id=1, token=ADAPTER_KEY.decode())
    socket.send_json(frame)
    assert socket.receive_json()["type"] == "auth.accepted"
    assert socket.receive_json()["type"] == "state"


def _until(socket, predicate):
    for _ in range(100):
        event = socket.receive_json()
        if predicate(event):
            return event
    raise AssertionError("expected relay event")


def _outcome(socket, intent_id: str):
    return _until(
        socket,
        lambda event: (
            event.get("intent_id") == intent_id
            and event["type"] in {"acknowledgement", "refusal"}
            and event.get("source") == "autonomy"
        ),
    )


@pytest.mark.parametrize("mission_name", ["navigate", "search"])
def test_simulated_navigation_preview_and_confirmation_complete_the_frozen_route(
    tmp_path: Path, clock: MutableClock, event_ids: EventIds, mission_name: str
) -> None:
    navigation = _navigation()
    search = (
        SearchRuntimeConfig(
            areas={"atrium": SearchArea("atrium", "level_1", ((2, 1), (6, 1), (6, 3), (2, 3)))},
            map_pin=navigation.artifact().map_pin,
            camera=CameraPolicy(90, 90, 1, -90, -90, 0, 0.25),
            calibration_id="camera",
            source_by_drone={1: "camera-1"},
            permission=navigation.permission,
            floor_z_m=0,
            camera_offset_z_m=0,
        )
        if mission_name == "search"
        else None
    )
    app, composition = create_autonomy_app(
        _settings(tmp_path),
        AutonomyConfig(
            planning=planning_config(),
            safety=safety_config(),
            sim_camera=camera_config(),
            navigation=navigation,
            search=search,
        ),
        clock=clock,
        event_ids=event_ids,
    )
    try:
        with TestClient(app) as client:
            with (
                client.websocket_connect(f"/ws/{SESSION}") as console,
                client.websocket_connect(f"/ws/{SESSION}") as adapter,
            ):
                _authenticate(console, "console")
                _authenticate(adapter, "adapter")
                adapter.send_json(membership_payload(action="join", event_id="join"))
                adapter.send_json(telemetry_payload(event_id="telemetry", state="landed"))
                adapter.send_json(capabilities_payload(event_id="capabilities"))
                adapter.send_json(capture_readiness_payload(event_id="readiness"))
                adapter.send_json(membership_payload(action="readiness", event_id="ready"))
                _until(
                    console,
                    lambda event: (
                        event["type"] == "state" and event["drones"][0]["membership"] == "ready"
                    ),
                )

                for name, intent_id, selection, args, confirm in (
                    ("arm", "arm", [], {}, False),
                    ("select", "select", [], {"ids": [1]}, False),
                    ("takeoff", "takeoff", [1], {}, True),
                ):
                    console.send_json(
                        _intent(name, intent_id, selection=selection, args=args, confirm=confirm)
                    )
                    assert _outcome(console, intent_id)["status"] == "completed"

                args = {"zone_id": "atrium"}
                if mission_name == "search":
                    args["target_class"] = "backpack"
                draft = _intent(mission_name, "route", selection=[1], args=args)
                console.send_json({"v": 1, "type": "navigation_preview_request", "intent": draft})
                preview = _until(
                    console, lambda event: event["type"] in {"navigation_preview", "refusal"}
                )
                assert preview["type"] == "navigation_preview", preview
                assert preview["plan"]["navigation"]["route"]["routes"]

                console.send_json({**draft, "confirm": True})
                outcome = _outcome(console, "route")
                assert outcome["status"] == "completed", str(outcome)
                target = preview["plan"]["navigation"]["route"]["routes"][-1]["arrival_slot"][
                    "pose"
                ]
                state = _until(
                    console,
                    lambda event: (
                        event["type"] == "state"
                        and event["drones"][0]["telemetry"]["x"] == target["x_m"]
                    ),
                )
                assert state["drones"][0]["flight_state"] == "hovering"
                if mission_name == "search":
                    status = composition.session(SESSION).search.status("route")
                    assert status.state == "incomplete"
                    assert sum(task.covered_cells for task in status.tasks) == 0
                    assert preview["plan"]["commands"][0]["operation"] == "set_gimbal_pitch"
    finally:
        composition.close()


def _clock_mapping(now_ms: int) -> ClockMapping:
    return ClockMapping("camera-clock", "relay-monotonic", 0, now_ms - 1_000, 1_000, 5, True)


def _control_config(now_ms: int) -> ControlRuntimeConfig:
    return ControlRuntimeConfig.from_mapping(
        {
            "limits": {
                "max_clock_error_ms": 5,
                "max_fix_age_ms": 500,
                "max_position_uncertainty_p95_m": 0.3,
                "max_velocity_age_ms": 200,
                "max_height_age_ms": 200,
            },
            "drones": [
                {
                    "drone_id": 1,
                    "map_id": "map",
                    "geometry_id": "geometry",
                    "camera_calibration_id": "camera",
                    "body_extrinsics_id": "body",
                    "source_ids": ["tag", "velocity", "height"],
                    "clock_mapping": _clock_mapping(now_ms).to_mapping(),
                }
            ],
        },
    )


def _active_bridge() -> tuple[SearchBridge, SearchRuntime, object, object, ClockMapping]:
    from relay.tests.test_search_runtime import _at_first_coverage, _prepared

    _, _, _, snapshot, _, intent, runtime, preview = _prepared("bridge")
    assert isinstance(preview, SearchMissionPreview)
    route = preview.task_routes[0]
    active_snapshot = _at_first_coverage(snapshot, preview)
    provenance = ControlProvenance(
        "map",
        "geometry",
        "camera",
        "body",
        "camera-clock",
        "relay-monotonic",
        ("tag",),
        1,
        5,
        "ready",
        100_000,
        0.1,
    )
    aircraft = active_snapshot.aircraft[1]
    active_snapshot = replace(
        active_snapshot,
        now_ms=100_000,
        aircraft={
            1: replace(
                aircraft,
                position_last_seen_ms=100_000,
                link_last_seen_ms=100_000,
                control_provenance=provenance,
            )
        },
    )
    runtime.start(intent.intent_id, active_snapshot)
    runtime.on_command(intent.intent_id, route.gimbal_command_id, active_snapshot)
    runtime.on_command(intent.intent_id, route.camera_ready_command_id, active_snapshot)
    runtime.on_command(intent.intent_id, route.first_coverage_command_id, active_snapshot)
    mapping = _clock_mapping(100_000)
    bridge = SearchBridge(
        SESSION,
        runtime,
        _control_config(100_000),
        {1: "camera-serial-1"},
    )
    bridge.observe_snapshot(active_snapshot)
    return bridge, runtime, preview, active_snapshot, mapping


def test_search_bridge_requires_an_accepted_requested_class_frame_before_sighting() -> None:
    bridge, runtime, preview, _, mapping = _active_bridge()
    publisher = DetectionPublisher(
        DetectionPublisherConfig(
            SESSION,
            "bridge",
            preview.search.mission.frame_mission_id,
            1,
            1,
            "camera-1",
            "camera-serial-1",
            mapping,
            mapping,
        ),
        PERCEPTION_KEY,
    )
    identity = FrameIdentity("camera-1", preview.search.mission.frame_mission_id, "test-run", 1)
    processed = publisher.enqueue(
        ProcessedFrameEvent(
            identity,
            100,
            100,
            100,
            "detections",
            1,
            ("backpack",),
            _DETECTOR_CONFIG_SHA256,
            capture_time_verified=True,
            received_at_s=100,
        )
    )
    principal = Principal("perception", None, PERCEPTION_KEY)

    accepted = bridge.consume(processed, principal, 100_000)
    matching = bridge.consume(
        publisher.enqueue(
            SightingEvent(
                "sighting-1",
                identity,
                100,
                100,
                100,
                100,
                DetectionCandidate("backpack", 24, 0.9, (0, 0, 1, 1)),
                1,
                _DETECTOR_CONFIG_SHA256,
            )
        ),
        principal,
        100_000,
    )
    wrong_target = bridge.consume(
        publisher.enqueue(
            SightingEvent(
                "sighting-2",
                identity,
                100,
                100,
                100,
                100,
                DetectionCandidate("bottle", 39, 0.9, (0, 0, 1, 1)),
                1,
                _DETECTOR_CONFIG_SHA256,
            )
        ),
        principal,
        100_000,
    )
    unrelated = bridge.consume(
        publisher.enqueue(
            SightingEvent(
                "sighting-3",
                FrameIdentity("camera-1", preview.search.mission.frame_mission_id, "unverified", 1),
                100,
                100,
                100,
                100,
                DetectionCandidate("backpack", 24, 0.9, (0, 0, 1, 1)),
                1,
                _DETECTOR_CONFIG_SHA256,
            )
        ),
        principal,
        100_000,
    )

    assert accepted["accepted"] is True
    assert matching["accepted"] is True
    assert matching["candidate"]["label"] == "backpack"
    assert wrong_target == {
        "type": "perception_result",
        "intent_id": "bridge",
        "accepted": False,
        "reason": "target_class_mismatch",
    }
    assert unrelated["reason"] == "unverified_frame"
    assert runtime.progress("bridge", 1).covered_cells == 1


def test_search_bridge_accepts_signed_typed_worker_sighting() -> None:
    bridge, runtime, preview, _, mapping = _active_bridge()
    publisher = DetectionPublisher(
        DetectionPublisherConfig(
            SESSION,
            "bridge",
            preview.search.mission.frame_mission_id,
            1,
            1,
            "camera-1",
            "camera-serial-1",
            mapping,
            mapping,
        ),
        PERCEPTION_KEY,
    )

    class Frames:
        def __init__(self) -> None:
            self.frame = DecodedFrame(np.zeros((2, 2, 3), dtype=np.uint8), 100, 100, True)

        def read_timed(self, timeout: float):
            assert timeout == 0
            frame, self.frame = self.frame, None
            return frame

    class Detector:
        target_labels = ("backpack",)
        detector_config_sha256 = _DETECTOR_CONFIG_SHA256

        def detect(self, image: np.ndarray):
            return (DetectionCandidate("backpack", 24, 0.9, (0, 0, 1, 1)),)

    worker = LiveDetectionWorker(
        Frames(),
        Detector(),
        source_id="camera-1",
        mission_id=preview.search.mission.frame_mission_id,
        worker_run_id="worker-1",
        monotonic_clock=lambda: 100,
    )
    processed, sighting = worker.poll()
    principal = Principal("perception", None, PERCEPTION_KEY)

    coverage = bridge.consume(publisher.enqueue(processed), principal, 100_000)
    candidate = bridge.consume(publisher.enqueue(sighting), principal, 100_000)

    assert coverage["accepted"] is True
    assert candidate["candidate"]["label"] == "backpack"
    assert runtime.progress("bridge", 1).covered_cells == 1


def test_search_bridge_rejects_a_capture_at_the_wrong_camera_height() -> None:
    bridge, runtime, preview, snapshot, mapping = _active_bridge()
    aircraft = snapshot.aircraft[1]
    bridge.observe_snapshot(
        replace(
            snapshot,
            now_ms=100_001,
            aircraft={
                1: replace(
                    aircraft,
                    pose=replace(aircraft.pose, z=aircraft.pose.z + 0.2),
                    control_provenance=replace(
                        aircraft.control_provenance, evaluated_at_relay_ms=100_001
                    ),
                )
            },
        )
    )
    publisher = DetectionPublisher(
        DetectionPublisherConfig(
            SESSION,
            "bridge",
            preview.search.mission.frame_mission_id,
            1,
            1,
            "camera-1",
            "camera-serial-1",
            mapping,
            mapping,
        ),
        PERCEPTION_KEY,
    )
    identity = FrameIdentity("camera-1", preview.search.mission.frame_mission_id, "wrong-height", 1)

    result = bridge.consume(
        publisher.enqueue(
            ProcessedFrameEvent(
                identity,
                100.001,
                100.001,
                100.001,
                "empty",
                0,
                ("backpack",),
                _DETECTOR_CONFIG_SHA256,
                True,
                100.001,
            )
        ),
        Principal("perception", None, PERCEPTION_KEY),
        100_001,
    )

    assert result == {
        "type": "perception_result",
        "intent_id": "bridge",
        "accepted": False,
        "reason": "camera_height_mismatch",
    }
    assert runtime.progress("bridge", 1).covered_cells == 0
