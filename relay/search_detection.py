"""Lifecycle-owned live detection workers for configured search sources."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from types import MappingProxyType
from typing import Protocol

import numpy as np

from perception.object_detection import LiveDetectionWorker, ProcessedFrameEvent, YoloXOnnxDetector
from perception.search_events import CoverageTask, FramePoseEvidence
from perception.search_localization import SearchCameraModel
from perception.webcam_stream import WebcamStream
from planner.navigation import Pose
from relay.search_runtime import SearchRuntime
from relay.session import RelaySession


class _Stream(Protocol):
    def start(self) -> object: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class CameraCalibrationConfig:
    intrinsics: tuple[tuple[float, ...], ...]
    body_from_camera: tuple[tuple[float, ...], ...]

    def __post_init__(self) -> None:
        try:
            intrinsics = np.asarray(self.intrinsics, dtype=float)
            transform = np.asarray(self.body_from_camera, dtype=float)
        except (TypeError, ValueError) as error:
            raise ValueError("detection camera calibration is invalid") from error
        if (
            intrinsics.shape != (3, 3)
            or transform.shape != (4, 4)
            or not np.isfinite(intrinsics).all()
            or not np.isfinite(transform).all()
            or intrinsics[0, 0] <= 0
            or intrinsics[1, 1] <= 0
            or not np.allclose(transform[3], (0, 0, 0, 1), atol=1e-9)
        ):
            raise ValueError("detection camera calibration is invalid")
        rotation = transform[:3, :3]
        if not np.allclose(rotation @ rotation.T, np.eye(3), atol=1e-6) or not np.isclose(
            np.linalg.det(rotation), 1, atol=1e-6
        ):
            raise ValueError("detection body_from_camera must be rigid")
        object.__setattr__(
            self, "intrinsics", tuple(tuple(float(value) for value in row) for row in intrinsics)
        )
        object.__setattr__(
            self,
            "body_from_camera",
            tuple(tuple(float(value) for value in row) for row in transform),
        )


@dataclass(frozen=True, slots=True)
class DetectionSourceConfig:
    drone_id: int
    source_id: str
    stream_url: str
    model_path: Path
    model_sha256: str
    camera: CameraCalibrationConfig

    def __post_init__(self) -> None:
        if type(self.drone_id) is not int or self.drone_id <= 0:
            raise ValueError("detection drone id must be positive")
        for name in ("source_id", "stream_url"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"detection {name} must be nonempty")
        if not self.stream_url.startswith(("rtsp://", "rtsps://")):
            raise ValueError("detection stream_url must be an RTSP URL")
        if not isinstance(self.camera, CameraCalibrationConfig):
            raise ValueError("detection camera must be calibrated")
        if (
            not isinstance(self.model_sha256, str)
            or len(self.model_sha256) != 64
            or any(character not in "0123456789abcdef" for character in self.model_sha256)
        ):
            raise ValueError("detection model_sha256 must be a lowercase SHA-256 digest")


@dataclass(frozen=True, slots=True)
class SearchDetectionConfig:
    sources_by_drone: Mapping[int, DetectionSourceConfig]

    def __post_init__(self) -> None:
        sources = dict(self.sources_by_drone)
        if not sources or any(drone_id != source.drone_id for drone_id, source in sources.items()):
            raise ValueError("detection sources must be keyed by their drone ids")
        if len({source.source_id for source in sources.values()}) != len(sources):
            raise ValueError("detection source ids must be unique")
        object.__setattr__(self, "sources_by_drone", MappingProxyType(sources))


StreamFactory = Callable[[str], _Stream]
DetectorFactory = Callable[[DetectionSourceConfig], object]
FramePoseProvider = Callable[[ProcessedFrameEvent], FramePoseEvidence | None]
PoseProviderFactory = Callable[[RelaySession, int, CoverageTask], FramePoseProvider]
CameraForFrame = Callable[[ProcessedFrameEvent], tuple[int, SearchCameraModel] | None]
CameraProviderFactory = Callable[
    [RelaySession, DetectionSourceConfig, CoverageTask], CameraForFrame
]


class SearchDetectionFactory:
    """Own configured stream and detector workers for the application's lifetime."""

    def __init__(
        self,
        config: SearchDetectionConfig,
        search: SearchRuntime,
        *,
        stream_factory: StreamFactory = WebcamStream,
        detector_factory: DetectorFactory | None = None,
        pose_provider_factory: PoseProviderFactory | None = None,
        camera_provider_factory: CameraProviderFactory | None = None,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not callable(stream_factory) or not callable(monotonic_clock):
            raise ValueError("detection factories and clock must be callable")
        self.config = config
        self.search = search
        self._stream_factory = stream_factory
        self._detector_factory = (
            _default_detector_factory if detector_factory is None else detector_factory
        )
        self._pose_provider_factory = pose_provider_factory
        self._camera_provider_factory = camera_provider_factory
        self._monotonic_clock = monotonic_clock
        self._lock = RLock()
        self._started = False
        self._closed = False
        self._workers: dict[tuple[str, int], tuple[_Stream, LiveDetectionWorker]] = {}
        self._failures: dict[tuple[str, int], str] = {}

    def start(self) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("search detection factory is closed")
            self._started = True

    def start_mission(self, intent_id: str, session: RelaySession) -> bool:
        with self._lock:
            if not self._started or self._closed:
                return False
        drone_ids = self.search.detection_drone_ids(intent_id)
        with self._lock:
            already_failed = any((intent_id, drone_id) in self._failures for drone_id in drone_ids)
        if already_failed:
            self.finish_mission(intent_id)
            return False
        started: list[tuple[str, int]] = []
        for drone_id in drone_ids:
            key = (intent_id, drone_id)
            with self._lock:
                if key in self._workers:
                    started.append(key)
                    continue
            source = self.config.sources_by_drone.get(drone_id)
            if source is None:
                self._record_failure(intent_id, drone_id, "source_not_configured")
                self.finish_mission(intent_id)
                return False
            stream: _Stream | None = None
            worker: LiveDetectionWorker | None = None
            try:
                stream = self._stream_factory(source.stream_url)
                detector = self._detector_factory(source)
                task = self.search.detection_task(intent_id, drone_id)
                pose_provider = (
                    self._pose_provider(
                        session, drone_id, task.connection_epoch, task.cells[0].pose.floor_id
                    )
                    if self._pose_provider_factory is None
                    else self._pose_provider_factory(session, drone_id, task)
                )
                camera_for_frame = (
                    None
                    if self._camera_provider_factory is None
                    else self._camera_provider_factory(session, source, task)
                )
                worker = self.search.detection_worker(
                    intent_id,
                    drone_id,
                    stream,
                    detector,
                    pose_provider,
                    now_s=self._monotonic_clock,
                    camera_for_frame=camera_for_frame,
                )
                stream.start()
                worker.start()
            except Exception:
                try:
                    if worker is not None:
                        worker.close()
                    if stream is not None:
                        stream.close()
                except Exception:
                    pass
                self._record_failure(intent_id, drone_id, "start_failed")
                self.finish_mission(intent_id)
                return False
            with self._lock:
                closed = self._closed
                if not closed:
                    self._workers[key] = (stream, worker)
                    started.append(key)
            if closed:
                worker.close()
                stream.close()
                self._record_failure(intent_id, drone_id, "factory_closed")
                self.finish_mission(intent_id)
                return False
        return len(started) == len(drone_ids)

    def status(self, intent_id: str) -> list[dict[str, object]]:
        with self._lock:
            workers = dict(self._workers)
            failures = dict(self._failures)
        status = []
        for drone_id in self.search.detection_drone_ids(intent_id):
            key = (intent_id, drone_id)
            failure = failures.get(key)
            if failure is None and (item := workers.get(key)) is not None:
                failure = item[1].failure_reason
                if failure is not None:
                    self._record_failure(intent_id, drone_id, failure)
            status.append(
                {
                    "drone_id": drone_id,
                    "state": (
                        "failed" if failure is not None else "running" if key in workers else "idle"
                    ),
                    "failure_reason": failure,
                }
            )
        return status

    def finish_mission(self, intent_id: str) -> None:
        with self._lock:
            workers = [
                (key, worker) for key, worker in self._workers.items() if key[0] == intent_id
            ]
            for key, _ in workers:
                self._workers.pop(key, None)
        for key, (stream, worker) in workers:
            worker.close()
            stream.close()
            with self._lock:
                if worker.failure_reason is not None:
                    self._failures.setdefault(key, worker.failure_reason)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            workers = tuple(self._workers.values())
            self._workers.clear()
        for stream, worker in workers:
            worker.close()
            stream.close()

    def _pose_provider(
        self, session: RelaySession, drone_id: int, connection_epoch: int, floor_id: str
    ) -> Callable[[ProcessedFrameEvent], FramePoseEvidence | None]:
        artifact = self.search.navigation.artifact()
        map_id = artifact.map_pin.version
        geometry_id = artifact.geometry_pin.version
        calibration_id = self.search.config.calibration_id

        def provide(event: ProcessedFrameEvent) -> FramePoseEvidence | None:
            pose = session.control_pose(drone_id)
            if pose is None or pose.connection_epoch != connection_epoch:
                return None
            now_ms = session.clock()
            if (
                pose.status != "ready"
                or pose.map_id != map_id
                or pose.geometry_id != geometry_id
                or pose.camera_calibration_id != calibration_id
                or not 0 <= now_ms - pose.pose_time_ms <= 500
            ):
                return None
            observed_at_s = self._monotonic_clock()
            return FramePoseEvidence(
                event.identity,
                connection_epoch,
                Pose(pose.x_mm / 1000, pose.y_mm / 1000, pose.z_mm / 1000, floor_id),
                observed_at_s,
                observed_at_s,
            )

        return provide

    def _record_failure(self, intent_id: str, drone_id: int, reason: str) -> None:
        with self._lock:
            self._failures.setdefault((intent_id, drone_id), reason)


def _default_detector_factory(source: DetectionSourceConfig) -> YoloXOnnxDetector:
    return YoloXOnnxDetector(source.model_path, expected_model_sha256=source.model_sha256)
