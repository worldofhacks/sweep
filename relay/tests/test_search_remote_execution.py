from __future__ import annotations

import time
from collections import deque
from dataclasses import replace

import numpy as np
from fastapi.testclient import TestClient

from arbiter.safety import SafetyConfig
from perception.object_detection import (
    DEFAULT_TARGET_LABELS,
    DetectionCandidate,
    FrameIdentity,
    ProcessedFrameEvent,
    SightingEvent,
)
from perception.search_events import CameraPolicy, FramePoseEvidence
from planner.models import Geofence, Position, Refusal
from planner.navigation import NavigationPermission
from planner.search import SearchArea
from relay.autonomy import AutonomyConfig, create_autonomy_app
from relay.intent_v1 import IntentName
from relay.search_detection import (
    CameraCalibrationConfig,
    DetectionSourceConfig,
    SearchDetectionConfig,
)
from relay.search_runtime import SearchRuntimeConfig
from relay.settings import AdapterBackend, RelaySettings
from relay.tests.conftest import ADAPTER_KEY, CONSOLE_KEY, EventIds, MutableClock
from relay.tests.test_platform_navigation_execution import (
    LOCALIZATION_KEY,
    SESSION,
    _deployment,
    _prepare_session,
    _projector,
)
from tests.autonomy_fixtures import make_intent, planning_config


class _Stream:
    def __init__(self) -> None:
        self.frames = deque()

    def start(self) -> _Stream:
        return self

    def read(self, _timeout: float):
        return self.frames.popleft() if self.frames else None

    def close(self) -> None:
        self.frames.clear()


class _Detector:
    target_labels = DEFAULT_TARGET_LABELS
    detector_config_sha256 = "a" * 64

    def detect(self, _image: np.ndarray):
        return ()


def test_remote_search_preview_and_confirmed_intent_use_the_signed_control_pose(tmp_path) -> None:
    deployment = _deployment(tmp_path)
    artifact = deployment.artifact()
    zone = next(item for item in artifact.zones if item.zone_id == "lobby")
    search = SearchRuntimeConfig(
        {"lobby": SearchArea("lobby", zone.floor_id, zone.polygon_xy, zone.z_min_m)},
        artifact.map_pin,
        CameraPolicy(90, 90, 1, -90, -90, 0, 0.25),
        "test-search-camera",
        {1: "test-camera-1"},
        NavigationPermission(frozenset({"lobby"})),
        maximum_drones=1,
    )
    detection = SearchDetectionConfig(
        {
            1: DetectionSourceConfig(
                1,
                "test-camera-1",
                "rtsp://test.invalid/camera",
                tmp_path / "test.onnx",
                "a" * 64,
                CameraCalibrationConfig(
                    ((800, 0, 640), (0, 800, 360), (0, 0, 1)),
                    ((1, 0, 0, 0), (0, -1, 0, 0), (0, 0, -1, 0), (0, 0, 0, 1)),
                ),
            )
        }
    )
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
        safety=SafetyConfig(
            geofence=Geofence(-100, 100, -100, 100, -100, 100),
            ceiling_m=50,
            min_spacing_m=0.8,
            battery_reserve_fraction=0.2,
            battery_critical_fraction=0.1,
            battery_cost_per_m=0.01,
            min_link_quality=0.4,
            max_link_age_ms=1_000,
            min_position_quality=0.5,
            max_position_age_ms=1_000,
            operator_timeout_ms=10_000,
            max_future_clock_skew_ms=1_000,
            min_capture_storage_bytes=1_000_000,
            max_capture_pose_drift_m=0.2,
            max_capture_gimbal_error_deg=1,
            positioning_loss_hold_ms=3_000,
            motion_conflict_window_ms=500,
        ),
        control_localization_projector=_projector(deployment),
        navigation=deployment,
        search=search,
        search_detection=detection,
    )
    stream = _Stream()
    app, composition = create_autonomy_app(
        settings,
        config,
        clock=clock,
        event_ids=EventIds(),
        detection_stream_factory=lambda _url: stream,
        detection_detector_factory=lambda _source: _Detector(),
    )
    try:
        with TestClient(app) as client:
            session, autonomy = _prepare_session(composition, deployment)
            intent = replace(
                make_intent(
                    IntentName.SEARCH,
                    selection=(1,),
                    args={"zone_id": "lobby", "target_class": "person"},
                    confirm=True,
                    intent_id="remote-search",
                ),
                session=SESSION,
            )
            headers = {"Authorization": f"Bearer {CONSOLE_KEY.decode()}"}
            catalog = client.get(f"/session/{SESSION}/search/catalog", headers=headers)
            assert catalog.status_code == 200
            assert catalog.json() == {
                "session": SESSION,
                "target_classes": list(DEFAULT_TARGET_LABELS),
                "zones": ["lobby"],
            }
            resolved = client.post(
                f"/session/{SESSION}/search/resolve",
                headers=headers,
                json={"query": "find a person in the lobby"},
            )
            assert resolved.status_code == 200
            assert resolved.json() | {"correlation_id": "opaque"} == {
                "session": SESSION,
                "correlation_id": "opaque",
                "source": "template",
                "status": "resolved",
                "zone_id": "lobby",
                "target_class": "person",
                "detail": (
                    "The target and room are configured. Preview the route before confirming."
                ),
            }
            unsupported = client.post(
                f"/session/{SESSION}/search/resolve",
                headers=headers,
                json={"query": "find a blue backpack in the lobby"},
            )
            assert unsupported.json()["status"] == "clarify"
            assert not autonomy.search_runtime.has_mission(intent.intent_id)
            payload = {
                "v": 1,
                "t": 100_000,
                "type": "intent",
                "intent_id": intent.intent_id,
                "retry_of": None,
                "source": "console",
                "session": SESSION,
                "name": "search",
                "args": {"zone_id": "lobby", "target_class": "person"},
                "selection": [1],
                "mode": "indoor",
                "confirm": True,
            }
            preview_response = client.post(
                f"/session/{SESSION}/search/preview", headers=headers, json={"intent": payload}
            )
            assert preview_response.status_code == 200
            assert preview_response.json()["intent_id"] == intent.intent_id
            assert preview_response.json()["routes"][0]["frame"] == "map_enu"
            assert len(preview_response.json()["routes"][0]["waypoints"]) >= 2
            assert (
                client.post(
                    f"/session/{SESSION}/search/preview", headers=headers, json={"intent": payload}
                ).status_code
                == 422
            )
            expired = replace(intent, intent_id="expired-search")
            assert not isinstance(
                autonomy.preview_search(expired, session.current_state()), Refusal
            )
            assert autonomy.search_runtime is not None
            assert not autonomy.search_runtime.accepts_intent(expired, 130_001)
            autonomy.submit(intent, session.current_state())
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
                frames = [adapter.receive_json() for _ in range(8)]
                command = next((frame for frame in frames if frame.get("type") == "command"), None)
                assert command is not None, frames
                factory = autonomy.search_detection
                assert factory is not None
                worker = factory._workers[("remote-search", 1)][1]
                assert autonomy.search_runtime is not None
                task = autonomy.search_runtime.detection_task("remote-search", 1)
                arrival = task.cells[0].pose
                snapshot = autonomy.snapshot(session.current_state())
                arrived = replace(
                    snapshot,
                    aircraft={
                        1: replace(
                            snapshot.aircraft[1],
                            pose=Position(arrival.x_m, arrival.y_m, arrival.z_m),
                        )
                    },
                )
                mission = autonomy.search_runtime._mission("remote-search")
                autonomy.search_runtime._activate_arrived_tasks(mission, arrived)
                identity = FrameIdentity(
                    task.source_id,
                    mission.preview.search.mission.frame_mission_id,
                    "test-worker",
                    1,
                )
                processed = ProcessedFrameEvent(
                    identity, 1.0, 1.0, 1.0, "detections", 1, ("person",), "a" * 64
                )
                observed = autonomy.search_runtime.observe_processed_frame(
                    "remote-search",
                    processed,
                    FramePoseEvidence(identity, task.connection_epoch, arrival, 1.0, 1.0),
                    now_s=1.0,
                )
                assert observed.accepted
                sighting = SightingEvent(
                    "remote-sighting",
                    identity,
                    1.0,
                    1.0,
                    1.0,
                    1.0,
                    DetectionCandidate("person", 0, 0.9, (1.0, 1.0, 10.0, 10.0)),
                    1,
                    "a" * 64,
                )
                assert (
                    autonomy.search_runtime.observe_sighting("remote-search", sighting) is not None
                )
                finding = client.post(
                    f"/session/{SESSION}/search/remote-search/findings/remote-sighting/ack",
                    headers=headers,
                )
                assert finding.status_code == 200
                assert finding.json()["candidates"][0]["acknowledged"]
                assert (
                    client.post(
                        f"/session/{SESSION}/search/remote-search/findings/missing/ack",
                        headers=headers,
                    ).status_code
                    == 404
                )
                worker._set_failure("test_failure")
                deadline = time.monotonic() + 2
                while "remote-search" in autonomy._awaiting and time.monotonic() < deadline:
                    time.sleep(0.01)
                assert "remote-search" not in autonomy._awaiting
                deadline = time.monotonic() + 2
                while (
                    not any(
                        record["event"].get("operation") == "hover"
                        for record in composition.runtime.replay(SESSION)["events"]
                    )
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.01)
                assert any(
                    record["event"].get("operation") == "hover"
                    for record in composition.runtime.replay(SESSION)["events"]
                )
    finally:
        composition.close()
