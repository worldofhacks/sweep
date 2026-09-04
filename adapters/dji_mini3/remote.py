"""Remote flight and camera adapter that drives one bridge node over the relay wire."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from math import isfinite
from types import MappingProxyType
from typing import Protocol

from adapters.protocols import (
    AdapterAcknowledgement,
    AdapterError,
    AdapterTimeout,
    CameraCapabilities,
    CameraIntrinsics,
    CameraResultStatus,
    CameraState,
    CameraStateCode,
    CaptureResult,
    MediaFile,
    MediaReference,
    MediaResult,
    Telemetry,
)
from planner.models import (
    CommandOperation,
    FleetSnapshot,
    LifecycleStatus,
    Position,
    RefusalReason,
)
from relay.contracts import AdapterAcknowledgement as WireAcknowledgement
from relay.contracts import CapabilitiesFrame, MediaFileRecord

_TERMINAL_STATUSES = frozenset({"completed", "failed", "invalidated", "refused"})


@dataclass(frozen=True, slots=True)
class CommandRequest:
    """Domain half of a command; the link adds envelope, sequence, and signature."""

    command_id: str
    intent_id: str
    roster_version: int
    drone_id: int
    connection_epoch: int
    operation: CommandOperation
    args: Mapping[str, int | str]


class NodeLink(Protocol):
    """Transport seam to one authenticated node socket.

    The link owns the wire envelope, the per-node sequence, and the signature. It
    reports the node's live connection epoch and retains the node-authored frames the
    camera path needs after a command completes.
    """

    def connection_epoch(self, drone_id: int) -> int | None: ...

    def send(self, request: CommandRequest) -> None: ...

    def await_acknowledgement(
        self, command_id: str, *, timeout_ms: int
    ) -> WireAcknowledgement | None: ...

    def camera_capabilities(self, drone_id: int) -> CapabilitiesFrame | None: ...

    def media_files(self, drone_id: int, capture_id: str) -> tuple[MediaFileRecord, ...]: ...


@dataclass(frozen=True, slots=True)
class _IntentContext:
    intent_id: str
    roster_version: int


@dataclass(frozen=True, slots=True)
class _Reply:
    request: CommandRequest
    status: LifecycleStatus
    connection_epoch: int
    reason: str | None
    detail: str

    @property
    def completed(self) -> bool:
        return self.status is LifecycleStatus.COMPLETED

    def acknowledgement(self) -> AdapterAcknowledgement:
        return AdapterAcknowledgement(
            drone_id=self.request.drone_id,
            connection_epoch=self.connection_epoch,
            operation=self.request.operation,
            status=self.status,
            detail=self.detail,
        )


class RemoteBridgeAdapter:
    """``SwarmAdapter`` and ``CameraCapture`` over one ``NodeLink``.

    The adapter stamps the connection epoch the autonomy layer believes is current and
    refuses to send when the link reports a different live epoch. Every wire
    acknowledgement is reduced to the typed adapter acknowledgement that
    ``AdapterDispatcher.validate_acknowledgement`` checks; node failure reasons travel
    in ``detail``. A nonterminal acknowledgement followed by silence is returned as is
    so the dispatcher stops dependent work and resumes on the later terminal fact.
    """

    def __init__(
        self,
        link: NodeLink,
        *,
        epochs: Mapping[int, int],
        acknowledgement_timeout_ms: int,
        command_ids: Callable[[], str] | None = None,
    ) -> None:
        if (
            not isinstance(acknowledgement_timeout_ms, int)
            or isinstance(acknowledgement_timeout_ms, bool)
            or acknowledgement_timeout_ms <= 0
        ):
            raise ValueError("acknowledgement_timeout_ms must be a positive integer")
        self._link = link
        self._epochs = dict(sorted(epochs.items()))
        self._timeout_ms = acknowledgement_timeout_ms
        self._command_ids = command_ids or (lambda: str(uuid.uuid4()))
        self._context: _IntentContext | None = None
        self._captures: dict[tuple[int, str], str] = {}
        self._reported_files: set[tuple[int, str, str]] = set()

    @classmethod
    def from_snapshot(
        cls,
        link: NodeLink,
        snapshot: FleetSnapshot,
        *,
        acknowledgement_timeout_ms: int,
        command_ids: Callable[[], str] | None = None,
    ) -> RemoteBridgeAdapter:
        return cls(
            link,
            epochs={
                drone_id: state.connection_epoch for drone_id, state in snapshot.aircraft.items()
            },
            acknowledgement_timeout_ms=acknowledgement_timeout_ms,
            command_ids=command_ids,
        )

    def update_connection_epoch(self, drone_id: int, connection_epoch: int) -> None:
        self._require_aircraft(drone_id)
        self._epochs[drone_id] = connection_epoch

    @contextmanager
    def for_intent(self, intent_id: str, roster_version: int) -> Iterator[None]:
        """Bind the intent and roster that every command in the block belongs to."""
        if self._context is not None:
            raise AdapterError("an intent context is already bound")
        self._context = _IntentContext(intent_id, roster_version)
        try:
            yield
        finally:
            self._context = None

    def takeoff(self, ids: list[int], z: float) -> tuple[AdapterAcknowledgement, ...]:
        args = {"z_mm": _milli(z, "z")}
        return tuple(self._flight(drone_id, CommandOperation.TAKEOFF, args) for drone_id in ids)

    def goto(
        self, drone_id: int, x: float, y: float, z: float, speed: float
    ) -> AdapterAcknowledgement:
        return self._flight(
            drone_id,
            CommandOperation.GOTO,
            {
                "x_mm": _milli(x, "x"),
                "y_mm": _milli(y, "y"),
                "z_mm": _milli(z, "z"),
                "speed_mm_s": _positive_milli(speed, "speed"),
            },
        )

    def rotate_to(self, drone_id: int, yaw: float, speed: float) -> AdapterAcknowledgement:
        return self._flight(
            drone_id,
            CommandOperation.ROTATE_TO,
            {"yaw_mdeg": _milli(yaw, "yaw"), "speed_mdeg_s": _positive_milli(speed, "speed")},
        )

    def hover(self, ids: list[int]) -> tuple[AdapterAcknowledgement, ...]:
        return tuple(self._flight(drone_id, CommandOperation.HOVER, {}) for drone_id in ids)

    def land(self, ids: list[int]) -> tuple[AdapterAcknowledgement, ...]:
        return tuple(self._flight(drone_id, CommandOperation.LAND, {}) for drone_id in ids)

    def estop(self) -> tuple[AdapterAcknowledgement, ...]:
        return tuple(
            self._flight(drone_id, CommandOperation.ESTOP, {}) for drone_id in sorted(self._epochs)
        )

    def telemetry(self) -> Iterator[Telemetry]:
        """Yield nothing: node telemetry reaches the relay registry over the node socket."""
        return iter(())

    def capabilities(self, drone_id: int) -> CameraCapabilities:
        reply = self._command(drone_id, CommandOperation.CAMERA_CAPABILITIES, {})
        if not reply.completed:
            raise AdapterError(f"camera capabilities {reply.status.value}: {reply.detail}")
        frame = self._link.camera_capabilities(drone_id)
        if frame is None or frame.connection_epoch != reply.connection_epoch:
            raise AdapterError(f"node {drone_id} reported no capabilities for the current epoch")
        return CameraCapabilities(
            drone_id=frame.drone_id,
            connection_epoch=frame.connection_epoch,
            native_panorama_modes=frame.native_panorama_modes,
            photo_capture=frame.photo_capture,
            gimbal_pitch_min_deg=frame.gimbal_pitch_min_deg,
            gimbal_pitch_max_deg=frame.gimbal_pitch_max_deg,
            horizontal_fov_deg=frame.horizontal_fov_deg,
            storage_remaining_bytes=frame.storage_remaining_bytes,
            media_retrieval=frame.media_retrieval,
        )

    def set_gimbal_pitch(self, drone_id: int, pitch: float) -> AdapterAcknowledgement:
        return self._flight(
            drone_id, CommandOperation.SET_GIMBAL_PITCH, {"pitch_mdeg": _milli(pitch, "pitch")}
        )

    def ready(self, drone_id: int) -> CameraState:
        reply = self._command(drone_id, CommandOperation.CAMERA_READY, {})
        if reply.completed:
            return CameraState(drone_id, reply.connection_epoch, CameraStateCode.READY)
        if reply.reason == RefusalReason.CAMERA_UNSUPPORTED.value:
            return CameraState(
                drone_id, reply.connection_epoch, CameraStateCode.UNSUPPORTED, reply.detail
            )
        return CameraState(drone_id, reply.connection_epoch, CameraStateCode.ERROR, reply.detail)

    def capture_panorama(self, drone_id: int, capture_id: str) -> CaptureResult:
        return self._capture(drone_id, CommandOperation.CAPTURE_PANORAMA, capture_id)

    def capture_photo(self, drone_id: int, capture_id: str) -> CaptureResult:
        return self._capture(drone_id, CommandOperation.CAPTURE_PHOTO, capture_id)

    def retrieve(self, drone_id: int, file_id: str) -> MediaResult:
        epoch = self._require_aircraft(drone_id)
        capture_id = self._captures.get((drone_id, file_id))
        if capture_id is None:
            return MediaResult(
                drone_id=drone_id,
                connection_epoch=epoch,
                capture_id="unknown",
                file_id=file_id,
                status=CameraResultStatus.FAILED,
                reason=RefusalReason.DOWNLOAD_FAILURE,
                detail="file was not captured through this adapter",
            )
        reply = self._command(drone_id, CommandOperation.RETRIEVE_MEDIA, {"file_id": file_id})
        if not reply.completed:
            return MediaResult(
                drone_id=drone_id,
                connection_epoch=reply.connection_epoch,
                capture_id=capture_id,
                file_id=file_id,
                status=CameraResultStatus.FAILED,
                reason=_refusal_reason(reply.reason, RefusalReason.DOWNLOAD_FAILURE),
                detail=reply.detail,
            )
        record = next(
            (
                candidate
                for candidate in self._link.media_files(drone_id, capture_id)
                if candidate.file_id == file_id
                and candidate.connection_epoch == reply.connection_epoch
            ),
            None,
        )
        if record is None or record.retrieval_status != CameraResultStatus.COMPLETED.value:
            return MediaResult(
                drone_id=drone_id,
                connection_epoch=reply.connection_epoch,
                capture_id=capture_id,
                file_id=file_id,
                status=CameraResultStatus.FAILED,
                reason=RefusalReason.DOWNLOAD_FAILURE,
                detail="node completed retrieval without a completed media_file",
            )
        return MediaResult(
            drone_id=drone_id,
            connection_epoch=reply.connection_epoch,
            capture_id=capture_id,
            file_id=file_id,
            status=CameraResultStatus.COMPLETED,
            media_file=_media_file(record),
        )

    def _capture(
        self, drone_id: int, operation: CommandOperation, capture_id: str
    ) -> CaptureResult:
        reply = self._command(drone_id, operation, {"capture_id": capture_id})
        if not reply.completed:
            unsupported = reply.reason == RefusalReason.CAMERA_UNSUPPORTED.value
            return CaptureResult(
                drone_id=drone_id,
                connection_epoch=reply.connection_epoch,
                capture_id=capture_id,
                status=(
                    CameraResultStatus.UNSUPPORTED if unsupported else CameraResultStatus.FAILED
                ),
                reason=_refusal_reason(reply.reason, RefusalReason.CAMERA_FAILURE),
                detail=reply.detail,
            )
        new_records = [
            record
            for record in self._link.media_files(drone_id, capture_id)
            if record.connection_epoch == reply.connection_epoch
            and (drone_id, capture_id, record.file_id) not in self._reported_files
        ]
        if not new_records:
            raise AdapterError(
                f"node completed {operation.value} without a media_file for {capture_id}"
            )
        for record in new_records:
            self._reported_files.add((drone_id, capture_id, record.file_id))
            self._captures[(drone_id, record.file_id)] = capture_id
        return CaptureResult(
            drone_id=drone_id,
            connection_epoch=reply.connection_epoch,
            capture_id=capture_id,
            status=CameraResultStatus.COMPLETED,
            media=tuple(MediaReference(capture_id, record.file_id) for record in new_records),
        )

    def _flight(
        self, drone_id: int, operation: CommandOperation, args: Mapping[str, int | str]
    ) -> AdapterAcknowledgement:
        return self._command(drone_id, operation, args).acknowledgement()

    def _command(
        self, drone_id: int, operation: CommandOperation, args: Mapping[str, int | str]
    ) -> _Reply:
        context = self._context
        if context is None:
            raise AdapterError("no intent context is bound; wrap dispatch in for_intent")
        expected_epoch = self._require_aircraft(drone_id)
        request = CommandRequest(
            command_id=self._command_ids(),
            intent_id=context.intent_id,
            roster_version=context.roster_version,
            drone_id=drone_id,
            connection_epoch=expected_epoch,
            operation=operation,
            args=MappingProxyType(dict(args)),
        )
        live_epoch = self._link.connection_epoch(drone_id)
        if live_epoch != expected_epoch:
            return _Reply(
                request,
                LifecycleStatus.FAILED,
                expected_epoch if live_epoch is None else live_epoch,
                RefusalReason.STALE_CONNECTION_EPOCH.value,
                "stale_connection_epoch: command not sent because the node epoch "
                f"{live_epoch} differs from command epoch {expected_epoch}",
            )
        self._link.send(request)
        latest: WireAcknowledgement | None = None
        while True:
            acknowledgement = self._link.await_acknowledgement(
                request.command_id, timeout_ms=self._timeout_ms
            )
            if acknowledgement is None:
                if latest is None:
                    raise AdapterTimeout(drone_id, operation)
                return _reply(request, latest)
            if (
                acknowledgement.command_id != request.command_id
                or acknowledgement.drone_id != drone_id
                or acknowledgement.intent_id != request.intent_id
            ):
                raise AdapterError("acknowledgement does not correlate with the sent command")
            latest = acknowledgement
            if acknowledgement.status.value in _TERMINAL_STATUSES:
                return _reply(request, acknowledgement)

    def _require_aircraft(self, drone_id: int) -> int:
        try:
            return self._epochs[drone_id]
        except KeyError as error:
            raise ValueError(f"unknown remote aircraft {drone_id}") from error


def _reply(request: CommandRequest, acknowledgement: WireAcknowledgement) -> _Reply:
    detail = ": ".join(
        part for part in (acknowledgement.reason, acknowledgement.detail) if part is not None
    )
    return _Reply(
        request,
        LifecycleStatus(acknowledgement.status.value),
        acknowledgement.connection_epoch,
        acknowledgement.reason,
        detail,
    )


def _refusal_reason(value: str | None, default: RefusalReason) -> RefusalReason:
    if value is None:
        return default
    try:
        return RefusalReason(value)
    except ValueError:
        return default


def _media_file(record: MediaFileRecord) -> MediaFile:
    return MediaFile(
        capture_id=record.capture_id,
        file_id=record.file_id,
        timestamp_ms=record.timestamp_ms,
        drone_id=record.drone_id,
        connection_epoch=record.connection_epoch,
        pose=Position(record.pose.x, record.pose.y, record.pose.z),
        actual_yaw_deg=record.actual_yaw_deg,
        gimbal_pitch_deg=record.gimbal_pitch_deg,
        intrinsics=CameraIntrinsics(
            width_px=record.intrinsics.width_px,
            height_px=record.intrinsics.height_px,
            horizontal_fov_deg=record.intrinsics.horizontal_fov_deg,
            projection=record.intrinsics.projection,
        ),
        checksum_sha256=record.checksum_sha256,
        storage_ref=record.storage_ref,
        retrieval_status=CameraResultStatus(record.retrieval_status),
    )


def _milli(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float) or not isfinite(value):
        raise AdapterError(f"{name} must be a finite number")
    return round(float(value) * 1000)


def _positive_milli(value: object, name: str) -> int:
    result = _milli(value, name)
    if result <= 0:
        raise AdapterError(f"{name} must be positive")
    return result
