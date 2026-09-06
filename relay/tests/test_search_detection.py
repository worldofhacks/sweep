from __future__ import annotations

import time
from collections import deque
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import numpy as np
from fastapi.testclient import TestClient

from perception.object_detection import DEFAULT_TARGET_LABELS
from perception.search_events import CameraPolicy
from planner.models import ExecutionResult, LifecycleStatus, Position
from planner.navigation import NavigationPermission
from planner.navigation_deployment import NavigationDeployment
from planner.search import SearchArea
from planner.test_navigation_runtime import _runtime, _snapshot
from relay.autonomy import AutonomyConfig, create_autonomy_app
from relay.intent_v1 import IntentName
from relay.search_detection import (
    CameraCalibrationConfig,
    DetectionSourceConfig,
    SearchDetectionConfig,
    SearchDetectionFactory,
)
from relay.search_runtime import SearchMissionPreview, SearchRuntime, SearchRuntimeConfig
from relay.settings import AdapterBackend, RelaySettings
from relay.tests.conftest import CONSOLE_KEY
from tests.autonomy_fixtures import make_intent, planning_config, safety_config


class _Frames:
    def __init__(self) -> None:
        self.released = False
        self.closed = False
        self.frames = deque([(np.zeros((8, 8, 3), dtype=np.uint8), 10.0)])

    def start(self) -> _Frames:
        return self

    def read(self, _timeout: float):
        return self.frames.popleft() if self.released and self.frames else None

    def close(self) -> None:
        self.closed = True


class _Detector:
    target_labels = DEFAULT_TARGET_LABELS
    detector_config_sha256 = "a" * 64

    def detect(self, _frame: np.ndarray) -> tuple[()]:
        return ()


class _Dispatcher:
    def __init__(self, stream: _Frames, arrival) -> None:
        self.calls: list[object] = []
        self.on_navigation_command_completed = None
        self._stream = stream
        self._arrival = arrival
        self.worker = None

    def dispatch(self, plan, snapshot, *, current_snapshot=None):
        self._stream.released = True
        arrived = replace(
            snapshot,
            aircraft={
                1: replace(
                    snapshot.aircraft[1],
                    pose=Position(*self._arrival.xyz),
                    position_last_seen_ms=snapshot.now_ms,
                )
            },
        )
        assert self.on_navigation_command_completed is not None
        self.on_navigation_command_completed(plan, plan.commands[-1], arrived)
        assert self.worker is not None
        self.worker.poll()
        return ExecutionResult(
            intent_id=plan.intent_id,
            roster_version=snapshot.roster_version,
            status=LifecycleStatus.COMPLETED,
            plan=plan,
        )


def _camera() -> CameraCalibrationConfig:
    return CameraCalibrationConfig(
        ((100, 0, 4), (0, 100, 4), (0, 0, 1)),
        ((1, 0, 0, 0), (0, 1, 0, 0), (0, 0, 1, 0), (0, 0, 0, 1)),
    )


def _search() -> SearchRuntime:
    navigation = _runtime()
    return SearchRuntime(
        SearchRuntimeConfig(
            {"atrium": SearchArea("atrium", "level_1", ((0, 0), (8, 0), (8, 4), (0, 4)))},
            navigation.artifact().map_pin,
            CameraPolicy(90, 90, 1, -90, -90, 0, 0.25),
            "camera-calibration-v1",
            {1: "camera-1"},
            NavigationPermission(frozenset({"atrium"})),
        ),
        navigation,
    )


def test_production_detection_factory_updates_search_without_adapter_calls() -> None:
    search = _search()
    intent = make_intent(
        IntentName.SEARCH,
        selection=(1,),
        args={"zone_id": "atrium", "target_class": "backpack"},
        confirm=True,
        intent_id="detected-search",
    )
    preview = search.prepare(intent, _snapshot())
    assert isinstance(preview, SearchMissionPreview)
    arrival = preview.search.assignments[0].transit.arrival_slot.pose
    stream = _Frames()
    source = DetectionSourceConfig(
        1,
        "camera-1",
        "rtsp://camera.example/drone1",
        Path("model.onnx"),
        "a" * 64,
        _camera(),
    )
    factory = SearchDetectionFactory(
        SearchDetectionConfig({1: source}),
        search,
        stream_factory=lambda _url: stream,
        detector_factory=lambda _source: _Detector(),
        monotonic_clock=lambda: 10.0,
    )
    artifact = search.navigation.artifact()
    session = SimpleNamespace(
        clock=lambda: 1_000,
        control_pose=lambda _drone_id: SimpleNamespace(
            connection_epoch=1,
            status="ready",
            map_id=artifact.map_pin.version,
            geometry_id=artifact.geometry_pin.version,
            camera_calibration_id="camera-calibration-v1",
            pose_time_ms=1_000,
            x_mm=round(arrival.x_m * 1_000),
            y_mm=round(arrival.y_m * 1_000),
            z_mm=round(arrival.z_m * 1_000),
        ),
    )
    dispatcher = _Dispatcher(stream, arrival)
    factory.start()
    factory.start_mission(intent.intent_id, session)
    dispatcher.worker = factory._workers[(intent.intent_id, 1)][1]
    try:
        result = search.execute(intent.intent_id, dispatcher, _snapshot())
        assert result.status is LifecycleStatus.COMPLETED
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            if search.status_payload(intent.intent_id)["tasks"][0]["covered_cells"]:
                break
            time.sleep(0.01)

        status = search.status_payload(intent.intent_id)
        assert status["tasks"][0]["covered_cells"] > 0
        assert factory.status(intent.intent_id) == [
            {"drone_id": 1, "state": "running", "failure_reason": None}
        ]
        assert dispatcher.calls == []
        factory.finish_mission(intent.intent_id)
        assert factory._workers == {}
        assert factory.status(intent.intent_id) == [
            {"drone_id": 1, "state": "idle", "failure_reason": None}
        ]
        assert stream.closed
    finally:
        factory.close()
    assert stream.closed


def test_detection_factory_stops_a_search_when_a_required_source_is_missing() -> None:
    search = _search()
    intent = make_intent(
        IntentName.SEARCH,
        selection=(1,),
        args={"zone_id": "atrium", "target_class": "backpack"},
        confirm=True,
        intent_id="missing-detection-source",
    )
    assert isinstance(search.prepare(intent, _snapshot()), SearchMissionPreview)
    source = DetectionSourceConfig(
        2,
        "camera-2",
        "rtsp://camera.example/drone2",
        Path("model.onnx"),
        "a" * 64,
        _camera(),
    )
    factory = SearchDetectionFactory(SearchDetectionConfig({2: source}), search)

    factory.start()

    assert factory.start_mission(intent.intent_id, SimpleNamespace()) is False
    assert factory._workers == {}
    assert factory.status(intent.intent_id) == [
        {"drone_id": 1, "state": "failed", "failure_reason": "source_not_configured"}
    ]


def test_detection_factory_follows_the_composed_app_lifespan(tmp_path) -> None:
    search = _search()
    source = DetectionSourceConfig(
        1,
        "camera-1",
        "rtsp://camera.example/drone1",
        Path("model.onnx"),
        "a" * 64,
        _camera(),
    )
    config = AutonomyConfig(
        planning=planning_config(),
        safety=safety_config(),
        navigation_deployment=NavigationDeployment(
            search.navigation, 1, "control-store", "synthetic", "navigation-config"
        ),
        search_runtime=search,
        search_detection=SearchDetectionConfig({1: source}),
    )
    settings = RelaySettings(
        relay_token=CONSOLE_KEY, log_dir=tmp_path, adapter_backend=AdapterBackend.REMOTE
    )
    app, composition = create_autonomy_app(
        settings,
        config,
        detection_stream_factory=lambda _url: _Frames(),
        detection_detector_factory=lambda _source: _Detector(),
    )
    factory = composition.detection_factory
    assert factory is not None

    with TestClient(app):
        assert factory._started

    assert factory._closed
