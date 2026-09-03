"""Deterministic camera implementation with typed capture and failure fixtures."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import StrEnum
from hashlib import sha256
from math import isfinite

from adapters.protocols import (
    AdapterAcknowledgement,
    CameraCapabilities,
    CameraIntrinsics,
    CameraResultStatus,
    CameraState,
    CameraStateCode,
    CaptureResult,
    MediaFile,
    MediaReference,
    MediaResult,
)
from planner.models import (
    CommandOperation,
    LifecycleStatus,
    Position,
    RefusalReason,
)

type PoseProvider = Callable[[int], tuple[Position, float, int]]


class CameraFailureMode(StrEnum):
    NONE = "none"
    UNSUPPORTED = "unsupported"
    CAMERA = "camera"
    DOWNLOAD = "download"


@dataclass(frozen=True, slots=True)
class SimCameraConfig:
    panorama_width_px: int
    panorama_height_px: int
    photo_width_px: int
    photo_height_px: int
    horizontal_fov_deg: float
    gimbal_pitch_min_deg: float
    gimbal_pitch_max_deg: float
    storage_remaining_bytes: int
    initial_timestamp_ms: int
    timestamp_step_ms: int

    def __post_init__(self) -> None:
        integer_values = (
            self.panorama_width_px,
            self.panorama_height_px,
            self.photo_width_px,
            self.photo_height_px,
            self.storage_remaining_bytes,
            self.initial_timestamp_ms,
            self.timestamp_step_ms,
        )
        if any(not isinstance(value, int) or isinstance(value, bool) for value in integer_values):
            raise ValueError("camera sizes, storage, and timestamps must be integers")
        if self.panorama_width_px != self.panorama_height_px * 2:
            raise ValueError("panorama fixture must have a 2:1 equirectangular aspect ratio")
        if (
            min(
                self.panorama_width_px,
                self.panorama_height_px,
                self.photo_width_px,
                self.photo_height_px,
                self.timestamp_step_ms,
            )
            <= 0
        ):
            raise ValueError("camera dimensions and timestamp step must be positive")
        if not _finite_number(self.horizontal_fov_deg) or not 0 < self.horizontal_fov_deg < 180:
            raise ValueError("horizontal_fov_deg must be between zero and 180")
        if (
            not _finite_number(self.gimbal_pitch_min_deg)
            or not _finite_number(self.gimbal_pitch_max_deg)
            or self.gimbal_pitch_min_deg >= self.gimbal_pitch_max_deg
        ):
            raise ValueError("gimbal pitch range is invalid")
        if self.storage_remaining_bytes < 0 or self.initial_timestamp_ms < 0:
            raise ValueError("storage and timestamp cannot be negative")


class SimCamera:
    def __init__(
        self,
        *,
        drone_epochs: dict[int, int],
        pose_provider: PoseProvider,
        config: SimCameraConfig,
    ) -> None:
        self._epochs = dict(sorted(drone_epochs.items()))
        self._pose_provider = pose_provider
        self._config = config
        self._gimbal_pitch = {drone_id: 0.0 for drone_id in drone_epochs}
        self._failures = {drone_id: CameraFailureMode.NONE for drone_id in drone_epochs}
        self._media: dict[str, MediaFile] = {}
        self._frame_counts: dict[str, int] = {}
        self._timestamp_ms = config.initial_timestamp_ms
        self.calls: list[tuple[str, int, str | float | None]] = []

    def inject_failure(self, drone_id: int, mode: CameraFailureMode) -> None:
        self._require_drone(drone_id)
        self._failures[drone_id] = mode

    def update_connection_epoch(self, drone_id: int, connection_epoch: int) -> None:
        self._require_drone(drone_id)
        self._epochs[drone_id] = connection_epoch

    def capabilities(self, drone_id: int) -> CameraCapabilities:
        self._require_drone(drone_id)
        self.calls.append(("capabilities", drone_id, None))
        unsupported = self._failures[drone_id] is CameraFailureMode.UNSUPPORTED
        return CameraCapabilities(
            drone_id=drone_id,
            connection_epoch=self._epochs[drone_id],
            native_panorama_modes=() if unsupported else ("pano_360",),
            photo_capture=not unsupported,
            gimbal_pitch_min_deg=self._config.gimbal_pitch_min_deg,
            gimbal_pitch_max_deg=self._config.gimbal_pitch_max_deg,
            horizontal_fov_deg=self._config.horizontal_fov_deg,
            storage_remaining_bytes=self._config.storage_remaining_bytes,
            media_retrieval=not unsupported,
        )

    def set_gimbal_pitch(self, drone_id: int, pitch: float) -> AdapterAcknowledgement:
        self._require_drone(drone_id)
        self.calls.append(("set_gimbal_pitch", drone_id, float(pitch)))
        if not self._config.gimbal_pitch_min_deg <= pitch <= self._config.gimbal_pitch_max_deg:
            return AdapterAcknowledgement(
                drone_id=drone_id,
                connection_epoch=self._epochs[drone_id],
                operation=CommandOperation.SET_GIMBAL_PITCH,
                status=LifecycleStatus.FAILED,
                detail="gimbal pitch is outside simulated capability",
            )
        self._gimbal_pitch[drone_id] = float(pitch)
        return AdapterAcknowledgement(
            drone_id=drone_id,
            connection_epoch=self._epochs[drone_id],
            operation=CommandOperation.SET_GIMBAL_PITCH,
            status=LifecycleStatus.COMPLETED,
        )

    def ready(self, drone_id: int) -> CameraState:
        self._require_drone(drone_id)
        self.calls.append(("ready", drone_id, None))
        mode = self._failures[drone_id]
        if mode is CameraFailureMode.UNSUPPORTED:
            return CameraState(
                drone_id,
                self._epochs[drone_id],
                CameraStateCode.UNSUPPORTED,
                "injected unsupported camera",
            )
        return CameraState(drone_id, self._epochs[drone_id], CameraStateCode.READY)

    def capture_panorama(self, drone_id: int, capture_id: str) -> CaptureResult:
        self._require_drone(drone_id)
        self.calls.append(("capture_panorama", drone_id, capture_id))
        failure = self._capture_failure(drone_id, capture_id)
        if failure is not None:
            return failure
        file_id = f"{capture_id}-pano-360"
        media_file = self._make_media(
            drone_id,
            capture_id,
            file_id,
            width=self._config.panorama_width_px,
            height=self._config.panorama_height_px,
            projection="equirectangular",
        )
        self._media[file_id] = media_file
        return CaptureResult(
            drone_id=drone_id,
            connection_epoch=self._epochs[drone_id],
            capture_id=capture_id,
            status=CameraResultStatus.COMPLETED,
            media=(MediaReference(capture_id, file_id),),
        )

    def capture_photo(self, drone_id: int, capture_id: str) -> CaptureResult:
        self._require_drone(drone_id)
        self.calls.append(("capture_photo", drone_id, capture_id))
        failure = self._capture_failure(drone_id, capture_id)
        if failure is not None:
            return failure
        frame_number = self._frame_counts.get(capture_id, 0) + 1
        self._frame_counts[capture_id] = frame_number
        file_id = f"{capture_id}-frame-{frame_number:02d}"
        media_file = self._make_media(
            drone_id,
            capture_id,
            file_id,
            width=self._config.photo_width_px,
            height=self._config.photo_height_px,
            projection="rectilinear",
        )
        self._media[file_id] = media_file
        return CaptureResult(
            drone_id=drone_id,
            connection_epoch=self._epochs[drone_id],
            capture_id=capture_id,
            status=CameraResultStatus.COMPLETED,
            media=(MediaReference(capture_id, file_id),),
        )

    def retrieve(self, drone_id: int, file_id: str) -> MediaResult:
        self._require_drone(drone_id)
        self.calls.append(("retrieve", drone_id, file_id))
        media_file = self._media.get(file_id)
        capture_id = media_file.capture_id if media_file is not None else "unknown"
        if self._failures[drone_id] is CameraFailureMode.DOWNLOAD:
            return MediaResult(
                drone_id=drone_id,
                connection_epoch=self._epochs[drone_id],
                capture_id=capture_id,
                file_id=file_id,
                status=CameraResultStatus.FAILED,
                reason=RefusalReason.DOWNLOAD_FAILURE,
                detail="injected simulated media download failure",
            )
        if media_file is None or media_file.drone_id != drone_id:
            return MediaResult(
                drone_id=drone_id,
                connection_epoch=self._epochs[drone_id],
                capture_id=capture_id,
                file_id=file_id,
                status=CameraResultStatus.FAILED,
                reason=RefusalReason.DOWNLOAD_FAILURE,
                detail="simulated media file does not exist",
            )
        retrieved = replace(media_file, retrieval_status=CameraResultStatus.COMPLETED)
        self._media[file_id] = retrieved
        return MediaResult(
            drone_id=drone_id,
            connection_epoch=self._epochs[drone_id],
            capture_id=capture_id,
            file_id=file_id,
            status=CameraResultStatus.COMPLETED,
            media_file=retrieved,
        )

    def _capture_failure(self, drone_id: int, capture_id: str) -> CaptureResult | None:
        mode = self._failures[drone_id]
        if mode is CameraFailureMode.UNSUPPORTED:
            return CaptureResult(
                drone_id=drone_id,
                connection_epoch=self._epochs[drone_id],
                capture_id=capture_id,
                status=CameraResultStatus.UNSUPPORTED,
                reason=RefusalReason.CAMERA_UNSUPPORTED,
                detail="injected unsupported camera capability",
            )
        if mode is CameraFailureMode.CAMERA:
            return CaptureResult(
                drone_id=drone_id,
                connection_epoch=self._epochs[drone_id],
                capture_id=capture_id,
                status=CameraResultStatus.FAILED,
                reason=RefusalReason.CAMERA_FAILURE,
                detail="injected simulated camera failure",
            )
        return None

    def _make_media(
        self,
        drone_id: int,
        capture_id: str,
        file_id: str,
        *,
        width: int,
        height: int,
        projection: str,
    ) -> MediaFile:
        pose, yaw, pose_epoch = self._pose_provider(drone_id)
        if pose_epoch != self._epochs[drone_id]:
            raise ValueError("camera pose provider returned a stale connection epoch")
        self._timestamp_ms += self._config.timestamp_step_ms
        payload = (
            f"{drone_id}|{pose_epoch}|{capture_id}|{file_id}|{pose}|{yaw}|"
            f"{self._gimbal_pitch[drone_id]}|{width}|{height}|{projection}"
        ).encode()
        return MediaFile(
            capture_id=capture_id,
            file_id=file_id,
            timestamp_ms=self._timestamp_ms,
            drone_id=drone_id,
            connection_epoch=pose_epoch,
            pose=pose,
            actual_yaw_deg=yaw,
            gimbal_pitch_deg=self._gimbal_pitch[drone_id],
            intrinsics=CameraIntrinsics(
                width_px=width,
                height_px=height,
                horizontal_fov_deg=(
                    360.0 if projection == "equirectangular" else self._config.horizontal_fov_deg
                ),
                projection=projection,
            ),
            checksum_sha256=sha256(payload).hexdigest(),
            storage_ref=f"fixture://camera/{drone_id}/{file_id}",
            retrieval_status=CameraResultStatus.COMPLETED,
        )

    def _require_drone(self, drone_id: int) -> None:
        if drone_id not in self._epochs:
            raise ValueError(f"unknown simulated camera aircraft {drone_id}")


def _finite_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and isfinite(value)
