from __future__ import annotations

from perception.object_detection import FrameIdentity, ProcessedFrameEvent
from perception.search_events import CameraPolicy
from planner.navigation import NavigationPermission
from planner.search import SearchArea
from relay.control_localization import ControlPose
from relay.search_detection import (
    CameraCalibrationConfig,
    DetectionSourceConfig,
    SearchDetectionConfig,
    SearchDetectionFactory,
)
from relay.search_runtime import SearchRuntime, SearchRuntimeConfig
from relay.tests.test_platform_navigation_execution import (
    SESSION,
    _deployment,
    _projector,
)


def test_default_pose_provider_projects_control_enu_into_navigation_world(tmp_path) -> None:
    deployment = _deployment(tmp_path)
    original = deployment.config.frames[0]
    pins = original.control_pins
    assert pins is not None
    navigation = deployment.for_session(SESSION, lambda _drone_id: None, _projector(deployment))
    artifact = navigation.artifact()
    search = SearchRuntime(
        SearchRuntimeConfig(
            {
                "lobby": SearchArea(
                    "lobby",
                    next(zone for zone in artifact.zones if zone.zone_id == "lobby").floor_id,
                    next(zone for zone in artifact.zones if zone.zone_id == "lobby").polygon_xy,
                )
            },
            artifact.map_pin,
            CameraPolicy(90, 90, 1, -90, -90, 0, 0.25),
            pins.camera_calibration_id,
            {1: "search-camera"},
            NavigationPermission(frozenset({"lobby"})),
        ),
        navigation,
    )
    source = DetectionSourceConfig(
        1,
        "search-camera",
        "rtsp://test.invalid/search-camera",
        tmp_path / "unused.onnx",
        "a" * 64,
        CameraCalibrationConfig(
            ((800, 0, 640), (0, 800, 360), (0, 0, 1)),
            ((1, 0, 0, 0), (0, -1, 0, 0), (0, 0, -1, 0), (0, 0, 0, 1)),
        ),
    )
    factory = SearchDetectionFactory(SearchDetectionConfig({1: source}), search)
    control_pose = ControlPose(
        100_000,
        "nonidentity-control-pose",
        SESSION,
        1,
        1,
        pins.map_id,
        pins.geometry_id,
        pins.camera_calibration_id,
        pins.body_extrinsics_id,
        99_999,
        99_999,
        1_000,
        2_000,
        3_000,
        "map_enu",
        1,
        "ready",
    )

    class Session:
        @staticmethod
        def control_pose(drone_id: int) -> ControlPose | None:
            return control_pose if drone_id == 1 else None

        @staticmethod
        def clock() -> int:
            return 100_000

    frame = ProcessedFrameEvent(
        FrameIdentity("search-camera", "mission", "worker", 1),
        1.0,
        1.0,
        1.0,
        "empty",
        0,
        ("person",),
        "a" * 64,
    )
    evidence = factory._pose_provider(Session(), 1, 1, "level_1")(frame)

    assert evidence is not None
    assert evidence.pose.xyz == (8.0, 21.0, 33.0)
    assert evidence.pose.floor_id == "level_1"
