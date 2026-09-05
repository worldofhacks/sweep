"""Frozen flight and negotiated camera adapter contracts."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from planner.models import (
    CommandOperation,
    JsonValue,
    LifecycleStatus,
    LossBehavior,
    Position,
    RefusalReason,
)


class AdapterError(RuntimeError):
    """Base class for typed adapter boundary failures."""


class AdapterTimeout(AdapterError):
    def __init__(self, drone_id: int, operation: CommandOperation, detail: str = "") -> None:
        self.drone_id = drone_id
        self.operation = operation
        self.detail = detail or f"{operation.value} timed out for aircraft {drone_id}"
        super().__init__(self.detail)


@dataclass(frozen=True, slots=True)
class AdapterAcknowledgement:
    drone_id: int
    connection_epoch: int
    operation: CommandOperation
    status: LifecycleStatus
    detail: str = ""

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "drone_id": self.drone_id,
            "connection_epoch": self.connection_epoch,
            "operation": self.operation.value,
            "status": self.status.value,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class Telemetry:
    drone_id: int
    connection_epoch: int
    t_ms: int
    pose: Position
    velocity: Position
    yaw_deg: float
    battery: float
    flight_state: str
    link_quality: float
    position_quality: float

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "drone_id": self.drone_id,
            "connection_epoch": self.connection_epoch,
            "t_ms": self.t_ms,
            "pose": self.pose.to_dict(),
            "velocity": self.velocity.to_dict(),
            "yaw_deg": self.yaw_deg,
            "battery": self.battery,
            "flight_state": self.flight_state,
            "link_quality": self.link_quality,
            "position_quality": self.position_quality,
        }


class SwarmAdapter(Protocol):
    def takeoff(self, ids: list[int], z: float) -> tuple[AdapterAcknowledgement, ...]: ...

    def goto(
        self, drone_id: int, x: float, y: float, z: float, speed: float
    ) -> AdapterAcknowledgement: ...

    def rotate_to(self, drone_id: int, yaw: float, speed: float) -> AdapterAcknowledgement: ...

    def hover(self, ids: list[int]) -> tuple[AdapterAcknowledgement, ...]: ...

    def land(self, ids: list[int]) -> tuple[AdapterAcknowledgement, ...]: ...

    def estop(self) -> tuple[AdapterAcknowledgement, ...]:
        """Latch emergency stop atomically; reject later takeoff/goto/rotate but allow land."""

    def telemetry(self) -> Iterator[Telemetry]: ...


@dataclass(frozen=True, slots=True)
class WatchdogConfig:
    hold_after_ms: int
    failsafe_after_ms: int
    loss_behavior: LossBehavior

    def __post_init__(self) -> None:
        if (
            not isinstance(self.hold_after_ms, int)
            or isinstance(self.hold_after_ms, bool)
            or not isinstance(self.failsafe_after_ms, int)
            or isinstance(self.failsafe_after_ms, bool)
            or self.hold_after_ms < 0
            or self.failsafe_after_ms <= self.hold_after_ms
            or not isinstance(self.loss_behavior, LossBehavior)
        ):
            raise ValueError("watchdog thresholds must satisfy 0 <= hold < failsafe")


@dataclass(frozen=True, slots=True)
class NodeWatchdogState:
    """Node-local proof of the latest authenticated relay/LAN activity."""

    drone_id: int
    connection_epoch: int
    last_activity_ms: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.drone_id, int)
            or isinstance(self.drone_id, bool)
            or self.drone_id <= 0
        ):
            raise ValueError("watchdog drone_id must be a positive integer")
        if (
            not isinstance(self.connection_epoch, int)
            or isinstance(self.connection_epoch, bool)
            or self.connection_epoch < 0
        ):
            raise ValueError("watchdog connection_epoch must be non-negative")
        if (
            not isinstance(self.last_activity_ms, int)
            or isinstance(self.last_activity_ms, bool)
            or self.last_activity_ms < 0
        ):
            raise ValueError("watchdog activity timestamp must be non-negative")

    def action_at(self, now_ms: int, config: WatchdogConfig) -> LossBehavior | None:
        """Return the local action due at ``now_ms`` without relay input."""
        if not isinstance(now_ms, int) or isinstance(now_ms, bool) or now_ms < 0:
            raise ValueError("watchdog current timestamp must be non-negative")
        if now_ms < self.last_activity_ms:
            raise ValueError("watchdog current timestamp precedes last activity")
        elapsed_ms = now_ms - self.last_activity_ms
        if elapsed_ms < config.hold_after_ms:
            return None
        if elapsed_ms < config.failsafe_after_ms:
            return LossBehavior.HOLD
        return config.loss_behavior


@dataclass(frozen=True, slots=True)
class NodeSafetyAction:
    drone_id: int
    connection_epoch: int
    t_ms: int
    action: LossBehavior


class NodeWatchdog(Protocol):
    def apply_node_watchdog(
        self,
        state: NodeWatchdogState,
        *,
        now_ms: int,
        config: WatchdogConfig,
    ) -> LossBehavior | None: ...


class CameraResultStatus(StrEnum):
    COMPLETED = "completed"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"


class CameraStateCode(StrEnum):
    READY = "ready"
    BUSY = "busy"
    ERROR = "error"
    UNSUPPORTED = "unsupported"


class CapturePattern(StrEnum):
    PANO_360 = "pano_360"
    RECONSTRUCT_8 = "reconstruct_8"


class CaptureCoverage(StrEnum):
    FULL_EQUIRECTANGULAR = "full_equirectangular"
    INCOMPLETE_VERTICAL = "incomplete_vertical_coverage"


@dataclass(frozen=True, slots=True)
class CameraCapabilities:
    drone_id: int
    connection_epoch: int
    native_panorama_modes: tuple[str, ...]
    photo_capture: bool
    gimbal_pitch_min_deg: float
    gimbal_pitch_max_deg: float
    horizontal_fov_deg: float
    storage_remaining_bytes: int
    media_retrieval: bool

    def supports(self, pattern: CapturePattern) -> bool:
        if pattern is CapturePattern.PANO_360:
            return "pano_360" in self.native_panorama_modes and self.media_retrieval
        return self.photo_capture and self.media_retrieval

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "drone_id": self.drone_id,
            "connection_epoch": self.connection_epoch,
            "native_panorama_modes": list(self.native_panorama_modes),
            "photo_capture": self.photo_capture,
            "gimbal_pitch_min_deg": self.gimbal_pitch_min_deg,
            "gimbal_pitch_max_deg": self.gimbal_pitch_max_deg,
            "horizontal_fov_deg": self.horizontal_fov_deg,
            "storage_remaining_bytes": self.storage_remaining_bytes,
            "media_retrieval": self.media_retrieval,
        }


@dataclass(frozen=True, slots=True)
class CameraState:
    drone_id: int
    connection_epoch: int
    state: CameraStateCode
    detail: str = ""

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "drone_id": self.drone_id,
            "connection_epoch": self.connection_epoch,
            "state": self.state.value,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class MediaReference:
    capture_id: str
    file_id: str

    def to_dict(self) -> dict[str, JsonValue]:
        return {"capture_id": self.capture_id, "file_id": self.file_id}


@dataclass(frozen=True, slots=True)
class CaptureResult:
    drone_id: int
    connection_epoch: int
    capture_id: str
    status: CameraResultStatus
    media: tuple[MediaReference, ...] = ()
    reason: RefusalReason | None = None
    detail: str = ""

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "drone_id": self.drone_id,
            "connection_epoch": self.connection_epoch,
            "capture_id": self.capture_id,
            "status": self.status.value,
            "media": [reference.to_dict() for reference in self.media],
            "reason": self.reason.value if self.reason is not None else None,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class CameraIntrinsics:
    width_px: int
    height_px: int
    horizontal_fov_deg: float
    projection: str

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "width_px": self.width_px,
            "height_px": self.height_px,
            "horizontal_fov_deg": self.horizontal_fov_deg,
            "projection": self.projection,
        }


@dataclass(frozen=True, slots=True)
class MediaFile:
    capture_id: str
    file_id: str
    timestamp_ms: int
    drone_id: int
    connection_epoch: int
    pose: Position
    actual_yaw_deg: float
    gimbal_pitch_deg: float
    intrinsics: CameraIntrinsics
    checksum_sha256: str
    storage_ref: str
    retrieval_status: CameraResultStatus

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "capture_id": self.capture_id,
            "file_id": self.file_id,
            "timestamp_ms": self.timestamp_ms,
            "drone_id": self.drone_id,
            "connection_epoch": self.connection_epoch,
            "pose": self.pose.to_dict(),
            "actual_yaw_deg": self.actual_yaw_deg,
            "gimbal_pitch_deg": self.gimbal_pitch_deg,
            "intrinsics": self.intrinsics.to_dict(),
            "checksum_sha256": self.checksum_sha256,
            "storage_ref": self.storage_ref,
            "retrieval_status": self.retrieval_status.value,
        }


@dataclass(frozen=True, slots=True)
class MediaResult:
    drone_id: int
    connection_epoch: int
    capture_id: str
    file_id: str
    status: CameraResultStatus
    media_file: MediaFile | None = None
    reason: RefusalReason | None = None
    detail: str = ""

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "drone_id": self.drone_id,
            "connection_epoch": self.connection_epoch,
            "capture_id": self.capture_id,
            "file_id": self.file_id,
            "status": self.status.value,
            "media_file": self.media_file.to_dict() if self.media_file is not None else None,
            "reason": self.reason.value if self.reason is not None else None,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class CaptureBundle:
    room_id: str
    capture_id: str
    drone_id: int
    connection_epoch: int
    pattern: CapturePattern
    coverage: CaptureCoverage
    status: CameraResultStatus
    media: tuple[MediaFile, ...]
    reason: RefusalReason | None = None
    detail: str = ""

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "room_id": self.room_id,
            "capture_id": self.capture_id,
            "drone_id": self.drone_id,
            "connection_epoch": self.connection_epoch,
            "pattern": self.pattern.value,
            "coverage": self.coverage.value,
            "status": self.status.value,
            "media": [media_file.to_dict() for media_file in self.media],
            "reason": self.reason.value if self.reason is not None else None,
            "detail": self.detail,
        }


class CameraCapture(Protocol):
    def capabilities(self, drone_id: int) -> CameraCapabilities: ...

    def capture_panorama(self, drone_id: int, capture_id: str) -> CaptureResult: ...

    def set_gimbal_pitch(self, drone_id: int, pitch: float) -> AdapterAcknowledgement: ...

    def ready(self, drone_id: int) -> CameraState: ...

    def capture_photo(self, drone_id: int, capture_id: str) -> CaptureResult: ...

    def retrieve(self, drone_id: int, file_id: str) -> MediaResult: ...
