"""Synthetic camera feed and pinned configuration for the local SEARCH demo."""

from __future__ import annotations

import queue
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from arbiter.safety import SafetyConfig
from perception.object_detection import (
    DEFAULT_TARGET_LABELS,
    DetectionCandidate,
    ProcessedFrameEvent,
)
from perception.search_events import CameraPolicy, CoverageTask, FramePoseEvidence
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
        if self._closed:
            raise RuntimeError("synthetic stream is closed")
        frame = (image, time.monotonic() if decoded_at_s is None else decoded_at_s)
        try:
            self._frames.put_nowait(frame)
        except queue.Full:
            self._frames.get_nowait()
            self._frames.put_nowait(frame)

    def close(self) -> None:
        self._closed = True
        while not self._frames.empty():
            self._frames.get_nowait()


class SyntheticDetector:
    """Emits a marked synthetic person box; it does not perform object inference."""

    target_labels = DEFAULT_TARGET_LABELS
    detector_config_sha256 = _DETECTOR_SHA256

    def detect(self, image: np.ndarray) -> tuple[DetectionCandidate, ...]:
        if image.shape != (720, 1280, 3) or image.dtype != np.uint8:
            raise ValueError("synthetic search frame must be 1280 by 720 BGR")
        return (DetectionCandidate("person", 0, 0.99, (560, 180, 720, 680)),)


@dataclass(frozen=True, slots=True)
class SearchDemo:
    config: AutonomyConfig
    stream: SyntheticFrameStream

    def stream_factory(self, stream_url: str) -> SyntheticFrameStream:
        if stream_url != _STREAM_URL:
            raise ValueError("unknown synthetic search stream")
        return self.stream

    @staticmethod
    def detector_factory(_source: DetectionSourceConfig) -> SyntheticDetector:
        return SyntheticDetector()

    @staticmethod
    def pose_provider_factory(
        session: RelaySession, drone_id: int, task: CoverageTask
    ) -> Callable[[ProcessedFrameEvent], FramePoseEvidence | None]:
        def provide(event: ProcessedFrameEvent) -> FramePoseEvidence | None:
            pose = session.control_pose(drone_id)
            if pose is None or pose.connection_epoch != task.connection_epoch:
                return None
            if (
                pose.status != "ready"
                or pose.map_id != "synthetic-search-map-v1"
                or pose.geometry_id != "synthetic-search-geometry-v1"
                or pose.camera_calibration_id != "synthetic-search-camera-v1"
                or not 0 <= session.clock() - pose.pose_time_ms <= 500
            ):
                return None
            observed_at_s = time.monotonic()
            return FramePoseEvidence(
                event.identity,
                task.connection_epoch,
                Pose(
                    pose.x_mm / 1000,
                    pose.y_mm / 1000,
                    pose.z_mm / 1000,
                    task.cells[0].pose.floor_id,
                ),
                observed_at_s,
                observed_at_s,
            )

        return provide

    def publish_frame(self) -> None:
        self.stream.publish(np.zeros((720, 1280, 3), dtype=np.uint8))


def search_demo() -> SearchDemo:
    navigation = _navigation_runtime()
    artifact = navigation.artifact()
    search = SearchRuntime(
        SearchRuntimeConfig(
            {
                "atrium": SearchArea(
                    "atrium", "level_1", ((0, 0), (8, 0), (8, 4), (0, 4)), 0
                )
            },
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
    return SearchDemo(
        AutonomyConfig(
            planning=_planning_config(),
            safety=_safety_config(),
            navigation_deployment=NavigationDeployment(
                navigation, 1, "synthetic-search-demo", "synthetic", "synthetic-search-demo-v1"
            ),
            search_runtime=search,
            search_detection=SearchDetectionConfig({1: source}),
        ),
        SyntheticFrameStream(),
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
    motion = MotionConfig(0.15, 0.2, 0.05, 0.03, 0.1, 0.05)
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
