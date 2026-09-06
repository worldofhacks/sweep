"""Synthetic camera feed and pinned configuration for the local SEARCH demo."""

from __future__ import annotations

import queue
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from threading import Lock

import numpy as np

from arbiter.safety import SafetyConfig
from perception.object_detection import (
    DEFAULT_TARGET_LABELS,
    DetectionCandidate,
    ProcessedFrameEvent,
)
from perception.search_events import CameraPolicy, CoverageTask, FramePoseEvidence
from perception.search_localization import SearchCameraModel
from planner.models import Geofence
from planner.navigation import (
    ArrivalSlot,
    ArtifactPin,
    GridLevel,
    MotionConfig,
    NavigationArtifact,
    NavigationDispatchAcceptance,
    NavigationPermission,
    Pose,
    Zone,
    preview_evidence,
)
from planner.navigation_deployment import NavigationDeployment
from planner.navigation_runtime import NavigationExecutionConfig, NavigationRuntime
from planner.planner import PlanningConfig
from planner.search import SearchArea
from relay.autonomy import AutonomyConfig
from relay.search_detection import (
    CameraCalibrationConfig,
    DetectionSourceConfig,
    SearchDetectionConfig,
)
from relay.search_runtime import SearchRuntime, SearchRuntimeConfig
from relay.session import RelaySession

_MODEL_SHA256 = "a" * 64
_DETECTOR_SHA256 = "b" * 64
_SOURCE_ID = "synthetic-search-camera-1"
_STREAM_URL = "rtsp://synthetic.local/search-camera-1"


class SyntheticFrameStream:
    """A bounded in-process frame reader for the software demo only."""

    def __init__(self) -> None:
        self._frames: queue.Queue[tuple[np.ndarray, float]] = queue.Queue(maxsize=8)
        self._closed = False
        self._lock = Lock()

    def start(self) -> SyntheticFrameStream:
        if self._closed:
            raise RuntimeError("synthetic stream is closed")
        return self

    def read(self, timeout: float) -> tuple[np.ndarray, float] | None:
        if self._closed:
            return None
        try:
            return self._frames.get(timeout=max(timeout, 0))
        except queue.Empty:
            return None

    def publish(self, image: np.ndarray, *, decoded_at_s: float | None = None) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("synthetic stream is closed")
            frame = (image, time.monotonic() if decoded_at_s is None else decoded_at_s)
            while True:
                try:
                    self._frames.put_nowait(frame)
                    return
                except queue.Full:
                    try:
                        self._frames.get_nowait()
                    except queue.Empty:
                        continue

    def close(self) -> None:
        with self._lock:
            self._closed = True
            while True:
                try:
                    self._frames.get_nowait()
                except queue.Empty:
                    return


class SyntheticDetector:
    """Emits a marked synthetic person box; it does not perform object inference."""

    target_labels = DEFAULT_TARGET_LABELS
    detector_config_sha256 = _DETECTOR_SHA256

    def detect(self, image: np.ndarray) -> tuple[DetectionCandidate, ...]:
        if image.shape != (720, 1280, 3) or image.dtype != np.uint8:
            raise ValueError("synthetic search frame must be 1280 by 720 BGR")
        return (DetectionCandidate("person", 0, 0.99, (560, 180, 720, 680)),)


@dataclass(slots=True)
class SearchDemo:
    config: AutonomyConfig
    _active_stream: SyntheticFrameStream | None = None
    _stream_lock: Lock = field(default_factory=Lock, init=False, repr=False)

    def stream_factory(self, stream_url: str) -> SyntheticFrameStream:
        if stream_url != _STREAM_URL:
            raise ValueError("unknown synthetic search stream")
        with self._stream_lock:
            self._active_stream = SyntheticFrameStream()
            return self._active_stream

    @staticmethod
    def detector_factory(_source: DetectionSourceConfig) -> SyntheticDetector:
        return SyntheticDetector()

    @staticmethod
    def pose_provider_factory(
        session: RelaySession, drone_id: int, task: CoverageTask
    ) -> Callable[[ProcessedFrameEvent], FramePoseEvidence | None]:
        def provide(event: ProcessedFrameEvent) -> FramePoseEvidence | None:
            state = session.current_state()
            drone = next(
                (
                    item
                    for item in state["drones"]
                    if isinstance(item, dict) and item.get("drone_id") == drone_id
                ),
                None,
            )
            if (
                drone is None
                or drone.get("connection_epoch") != task.connection_epoch
                or drone.get("membership") != "ready"
                or not isinstance(telemetry := drone.get("telemetry"), dict)
                or telemetry.get("state") not in {"hovering", "flying"}
            ):
                return None
            if (
                not isinstance(last_seen_at_ms := drone.get("last_seen_at"), int)
                or not 0 <= session.clock() - last_seen_at_ms <= 500
                or any(
                    isinstance(value, bool) or not isinstance(value, int | float)
                    for value in (telemetry.get("x"), telemetry.get("y"), telemetry.get("z"))
                )
            ):
                return None
            return FramePoseEvidence(
                event.identity,
                task.connection_epoch,
                Pose(
                    float(telemetry["x"]),
                    float(telemetry["y"]),
                    float(telemetry["z"]),
                    task.cells[0].pose.floor_id,
                ),
                event.frame_decoded_at_monotonic_s,
                time.monotonic(),
            )

        return provide

    @staticmethod
    def camera_provider_factory(
        session: RelaySession, source: DetectionSourceConfig, task: CoverageTask
    ) -> Callable[[ProcessedFrameEvent], tuple[int, SearchCameraModel] | None]:
        body_from_camera = np.asarray(source.camera.body_from_camera, dtype=float)
        intrinsics = source.camera.intrinsics
        image_width_px = round(2 * intrinsics[0][2])

        def provide(_event: ProcessedFrameEvent) -> tuple[int, SearchCameraModel] | None:
            state = session.current_state()
            drone = next(
                (
                    item
                    for item in state["drones"]
                    if isinstance(item, dict)
                    and item.get("drone_id") == source.drone_id
                    and item.get("connection_epoch") == task.connection_epoch
                ),
                None,
            )
            telemetry = None if drone is None else drone.get("telemetry")
            if not isinstance(telemetry, dict) or any(
                isinstance(value, bool) or not isinstance(value, int | float)
                for value in (telemetry.get("x"), telemetry.get("y"), telemetry.get("z"))
            ):
                return None
            map_from_body = np.eye(4)
            map_from_body[:3, 3] = (telemetry["x"], telemetry["y"], telemetry["z"])
            return image_width_px, SearchCameraModel(
                intrinsics, tuple(tuple(row) for row in map_from_body @ body_from_camera)
            )

        return provide

    def publish_frame(self) -> bool:
        with self._stream_lock:
            stream = self._active_stream
        if stream is None:
            return False
        try:
            stream.publish(np.zeros((720, 1280, 3), dtype=np.uint8))
        except RuntimeError:
            return False
        return True


def search_demo(base: AutonomyConfig | None = None) -> SearchDemo:
    deployment = None if base is None else base.navigation_deployment
    navigation = _navigation_runtime() if deployment is None else deployment.runtime
    artifact = navigation.artifact()
    zone = next(zone for zone in artifact.zones if zone.zone_id == "atrium")
    search = SearchRuntime(
        SearchRuntimeConfig(
            {"atrium": SearchArea(zone.zone_id, zone.floor_id, zone.polygon_xy, 0)},
            artifact.map_pin,
            CameraPolicy(90, 90, 1, -90, -90, 0, 0.25),
            "synthetic-search-camera-v1",
            {1: _SOURCE_ID},
            NavigationPermission(frozenset({"atrium"})),
            maximum_drones=1,
        ),
        navigation,
    )
    calibration = CameraCalibrationConfig(
        ((800, 0, 640), (0, 800, 360), (0, 0, 1)),
        ((1, 0, 0, 0), (0, -1, 0, 0), (0, 0, -1, 0), (0, 0, 0, 1)),
    )
    source = DetectionSourceConfig(
        1,
        _SOURCE_ID,
        _STREAM_URL,
        Path("synthetic-yolox.onnx"),
        _MODEL_SHA256,
        calibration,
    )
    config = base or AutonomyConfig(
        planning=_planning_config(),
        safety=_safety_config(),
        navigation_deployment=NavigationDeployment(
            navigation, 1, "synthetic-search-demo", "synthetic", "synthetic-search-demo-v1"
        ),
    )
    return SearchDemo(
        replace(config, search_runtime=search, search_detection=SearchDetectionConfig({1: source}))
    )


def _planning_config() -> PlanningConfig:
    return PlanningConfig(
        takeoff_altitude_m=1.0,
        translation_step_m=0.5,
        flight_speed_m_s=0.5,
        capture_yaw_speed_deg_s=30.0,
        capture_yaw_tolerance_deg=1.0,
        capture_pose_tolerance_m=0.1,
        capture_min_overlap_deg=10.0,
        capture_gimbal_pitch_deg=0.0,
        reconstruct_headings_deg=tuple(float(value) for value in range(0, 360, 45)),
        altitude_step_m=0.5,
        altitude_floor_z_m=0.0,
        altitude_configuration_id="synthetic-search-demo-floor-v1",
        altitude_completion_tolerance_m=0.05,
    )


def _safety_config() -> SafetyConfig:
    return SafetyConfig(
        geofence=Geofence(-10, 10, -10, 10, 0, 5),
        ceiling_m=4,
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
    )


def _navigation_runtime() -> NavigationRuntime:
    motion = MotionConfig(0.15, 0.2, 0.05, 0.03, 0.1, 0.05, 0.2)
    zone = Zone(
        "atrium",
        "level_1",
        True,
        ((0, 0), (8, 0), (8, 4), (0, 4), (0, 0)),
        0,
        4,
        (ArrivalSlot("atrium-1", "atrium", Pose(0.5, 1.5, 1, "level_1"), 0.5, 0.5),),
        ("atrium",),
    )
    artifact = NavigationArtifact(
        ArtifactPin("synthetic-search-map-v1", "a" * 64),
        ArtifactPin("synthetic-search-geometry-v1", "b" * 64),
        ArtifactPin("synthetic-search-preview-v1", "c" * 64),
        preview_evidence("synthetic"),
        0.75,
        ((-2, -2), (10, -2), (10, 6), (-2, 6), (-2, -2)),
        -1,
        5,
        (GridLevel("level_1", 1, (0, 0), 1, 8, 4, frozenset()),),
        (zone,),
    )

    def accepted(plan, current):
        return NavigationDispatchAcceptance(
            "synthetic-search-demo-acceptance",
            plan.map_pin,
            plan.geometry_pin,
            plan.navigation_pin,
            plan.plan_revision,
        )

    return NavigationRuntime(
        lambda: artifact,
        NavigationExecutionConfig("level_1", motion, 0.5, 0.05, 5_000, 0.5, 5_000),
        NavigationPermission(frozenset({"atrium"})),
        accepted,
    )
