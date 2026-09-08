"""Executable relay wire contracts.

The autonomy layer owns plans, commands, and safety decisions.  This module owns
only the transport envelopes that carry their outcomes through the relay.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final, Literal

from planner.models import CommandOperation


class ContractError(ValueError):
    """A typed failure while decoding an untrusted relay frame."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


class Membership(StrEnum):
    REGISTERED = "registered"
    READY = "ready"
    LEAVING = "leaving"
    DISCONNECTED = "disconnected"
    DEGRADED = "degraded"


class NodeType(StrEnum):
    AIRCRAFT = "aircraft"
    GROUND = "ground"


class MembershipAction(StrEnum):
    JOIN = "join"
    READINESS = "readiness"
    GRACEFUL_LEAVE = "graceful_leave"
    GRACEFUL_LEAVE_COMPLETED = "graceful_leave_completed"
    UNEXPECTED_LOSS = "unexpected_loss"
    TELEMETRY_STALE = "telemetry_stale"
    TELEMETRY_RECOVERED = "telemetry_recovered"
    OBSERVATION_STALE = "observation_stale"


class LifecycleStatus(StrEnum):
    ACCEPTED = "accepted"
    REFUSED = "refused"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"
    INVALIDATED = "invalidated"


class NodeAcknowledgementReason(StrEnum):
    """Machine-readable reasons a node may return when it does not run a command."""

    STALE_COMMAND = "stale_command"
    OUT_OF_ORDER_COMMAND = "out_of_order_command"
    AUTHORITY_LOST = "authority_lost"
    WATCHDOG_HOLD = "watchdog_hold"
    WATCHDOG_FAILSAFE = "watchdog_failsafe"


class GuidanceMode(StrEnum):
    VISUAL_ADVISORY = "visual_advisory"
    REGISTERED_METRIC = "registered_metric"


class DeltaKind(StrEnum):
    YAW = "yaw"
    GIMBAL = "gimbal"


class WatchdogState(StrEnum):
    NOMINAL = "nominal"
    HOLD = "hold"
    FAILSAFE = "failsafe"


class VideoPublishState(StrEnum):
    STOPPED = "stopped"
    CONNECTING = "connecting"
    PUBLISHING = "publishing"
    FAILED = "failed"


class PhoneThermalState(StrEnum):
    NONE = "none"
    LIGHT = "light"
    MODERATE = "moderate"
    SEVERE = "severe"
    CRITICAL = "critical"
    EMERGENCY = "emergency"
    SHUTDOWN = "shutdown"


WIRE_MEMBERSHIP_ACTIONS = frozenset(
    {
        MembershipAction.JOIN,
        MembershipAction.READINESS,
        MembershipAction.GRACEFUL_LEAVE,
    }
)

NODE_FRAME_TYPES = frozenset(
    {"capabilities", "capture_bundle", "media_file", "capture_readiness", "node_status"}
)

# Both adapter capability claims and camera panorama-mode claims become nested
# state lists. Keep their signed/canonical representation comfortably below the
# audit projector's per-aircraft ceiling before either list can mutate state.
MAX_CAPABILITY_LIST_ITEMS = 64
MAX_CAPABILITY_ITEM_UTF8_BYTES = 512
MAX_CAPABILITY_LIST_CANONICAL_BYTES = 8 * 1024
# The current capture contract has one full-equirectangular panorama artifact or
# exactly eight component frames. The same ceiling retains partial failed
# evidence without admitting an audit-sized unbounded collection.
MAX_CAPTURE_BUNDLE_MEDIA_ITEMS = 8
MAX_COVERAGE_MISSING_ITEMS = 8
# Android reports this field as a signed ``Long``. Mirror that exact upper bound
# before the value reaches retained state or its audit projection.
MAX_STORAGE_REMAINING_BYTES = (1 << 63) - 1

# Signed command arguments carry integers only (millimetres, millimetres per second,
# millidegrees, millidegrees per second) so the canonical JSON never depends on a
# cross-language float representation, the same rule signed membership claims follow.
MAX_SUPERVISED_TAKEOFF_HEIGHT_MM: Final = 2_438
COMMAND_ARGUMENT_FIELDS: Mapping[CommandOperation, Mapping[str, str]] = MappingProxyType(
    {
        CommandOperation.TAKEOFF: MappingProxyType(
            {
                "z_mm": "integer",
                "maximum_height_mm": "optional_positive",
                "max_local_height_age_ms": "optional_positive",
            }
        ),
        CommandOperation.GOTO: MappingProxyType(
            {
                "x_mm": "integer",
                "y_mm": "integer",
                "z_mm": "integer",
                "speed_mm_s": "positive",
                "navigation_route_id": "optional_id",
            }
        ),
        CommandOperation.ROTATE_TO: MappingProxyType(
            {"yaw_mdeg": "integer", "speed_mdeg_s": "positive"}
        ),
        CommandOperation.HOVER: MappingProxyType({}),
        CommandOperation.LAND: MappingProxyType({}),
        CommandOperation.ESTOP: MappingProxyType({}),
        CommandOperation.GROUND_VELOCITY: MappingProxyType(
            {
                "linear_mm_s": "ground_linear_mm_s",
                "angular_mrad_s": "ground_angular_mrad_s",
                "duration_ms": "ground_duration_ms",
            }
        ),
        CommandOperation.GROUND_RETURN: MappingProxyType({"return_id": "id"}),
        CommandOperation.GROUND_NAVIGATE: MappingProxyType(
            {"route_id": "id", "navigation_route": "ground_route_document"}
        ),
        CommandOperation.CAMERA_CAPABILITIES: MappingProxyType({}),
        CommandOperation.SET_GIMBAL_PITCH: MappingProxyType({"pitch_mdeg": "integer"}),
        CommandOperation.CAMERA_READY: MappingProxyType({}),
        CommandOperation.CAPTURE_PANORAMA: MappingProxyType({"capture_id": "id"}),
        CommandOperation.CAPTURE_PHOTO: MappingProxyType({"capture_id": "id"}),
        CommandOperation.RETRIEVE_MEDIA: MappingProxyType({"file_id": "id"}),
    }
)
MAX_GROUND_VELOCITY_MM_S = 180
MAX_GROUND_YAW_MRAD_S = 785
MAX_GROUND_VELOCITY_DURATION_MS = 500

_CAPTURE_PATTERNS = frozenset({"pano_360", "reconstruct_8", "single_still"})
_CAPTURE_COVERAGES = frozenset(
    {"full_equirectangular", "incomplete_vertical_coverage", "single_view"}
)
_CAMERA_RESULT_STATUSES = frozenset({"completed", "unsupported", "failed"})
_MEDIA_RETRIEVAL_STATUSES = _CAMERA_RESULT_STATUSES | {"pending"}
MEDIA_PENDING_CHECKSUM = "0" * 64
_ENVELOPE_FIELDS = frozenset({"v", "t", "type", "event_id", "session"})


@dataclass(frozen=True, slots=True)
class GroundPoseIdentity:
    event_id: str
    session: str
    connection_epoch: int
    source_id: str
    frame: str

    def to_event(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "session": self.session,
            "connection_epoch": self.connection_epoch,
            "source_id": self.source_id,
            "frame": self.frame,
        }


@dataclass(frozen=True, slots=True)
class MembershipRequest:
    v: Literal[1]
    t: int
    type: Literal["membership"]
    event_id: str
    session: str
    drone_id: int
    action: MembershipAction
    signature: str
    connection_epoch: int | None = None
    adapter_id: str | None = None
    capabilities: tuple[str, ...] = ()
    node_type: NodeType | None = None
    home_pose_confirmed: bool | None = None
    control_authority: bool | None = None
    rc_safety_operator_present: bool | None = None
    drive_authority: bool | None = None
    safety_operator_present: bool | None = None
    local_stop_ready: bool | None = None
    heartbeat_ready: bool | None = None
    pose_identity: GroundPoseIdentity | None = None

    def unsigned_event(self) -> dict[str, object]:
        event: dict[str, object] = {
            "v": self.v,
            "t": self.t,
            "type": self.type,
            "event_id": self.event_id,
            "session": self.session,
            "drone_id": self.drone_id,
            "action": self.action.value,
        }
        if self.action is MembershipAction.JOIN:
            event.update(
                adapter_id=self.adapter_id,
                capabilities=list(self.capabilities),
            )
            if self.node_type is not None:
                event["node_type"] = self.node_type.value
        elif self.action is MembershipAction.READINESS:
            if self.drive_authority is None:
                event.update(
                    connection_epoch=self.connection_epoch,
                    home_pose_confirmed=self.home_pose_confirmed,
                    control_authority=self.control_authority,
                    rc_safety_operator_present=self.rc_safety_operator_present,
                )
            else:
                event.update(
                    connection_epoch=self.connection_epoch,
                    drive_authority=self.drive_authority,
                    safety_operator_present=self.safety_operator_present,
                    local_stop_ready=self.local_stop_ready,
                    heartbeat_ready=self.heartbeat_ready,
                    pose_identity=None
                    if self.pose_identity is None
                    else self.pose_identity.to_event(),
                )
        elif self.action is MembershipAction.GRACEFUL_LEAVE:
            event["connection_epoch"] = self.connection_epoch
        return event


@dataclass(frozen=True, slots=True)
class TelemetryV1:
    v: Literal[1]
    t: int
    type: Literal["telemetry"]
    event_id: str
    session: str
    drone: int
    connection_epoch: int
    x: float
    y: float
    z: float
    vx: float
    vy: float
    vz: float
    battery: float
    state: str
    link: float
    pos_quality: float

    def to_event(self) -> dict[str, object]:
        return {
            "v": self.v,
            "t": self.t,
            "type": self.type,
            "event_id": self.event_id,
            "session": self.session,
            "drone": self.drone,
            "connection_epoch": self.connection_epoch,
            "x": self.x,
            "y": self.y,
            "z": self.z,
            "vx": self.vx,
            "vy": self.vy,
            "vz": self.vz,
            "battery": self.battery,
            "state": self.state,
            "link": self.link,
            "pos_quality": self.pos_quality,
        }

    def state_payload(self) -> dict[str, object]:
        """Return Appendix B telemetry without transport-only fields."""
        return {
            "v": self.v,
            "t": self.t,
            "type": self.type,
            "drone": self.drone,
            "x": self.x,
            "y": self.y,
            "z": self.z,
            "vx": self.vx,
            "vy": self.vy,
            "vz": self.vz,
            "battery": self.battery,
            "state": self.state,
            "link": self.link,
            "pos_quality": self.pos_quality,
        }


@dataclass(frozen=True, slots=True)
class AdapterAcknowledgement:
    v: Literal[1]
    t: int
    type: Literal["acknowledgement"]
    event_id: str
    session: str
    intent_id: str
    command_id: str
    status: LifecycleStatus
    drone_id: int
    connection_epoch: int
    roster_version: int
    reason: str | None
    detail: str | None

    def to_event(self) -> dict[str, object]:
        return {
            "v": self.v,
            "t": self.t,
            "type": self.type,
            "event_id": self.event_id,
            "session": self.session,
            "intent_id": self.intent_id,
            "command_id": self.command_id,
            "status": self.status.value,
            "source": "adapter",
            "drone_id": self.drone_id,
            "connection_epoch": self.connection_epoch,
            "roster_version": self.roster_version,
            "reason": self.reason,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class CommandFrame:
    """Relay-authored command, signed with the target node's adapter key."""

    v: Literal[1]
    t: int
    type: Literal["command"]
    event_id: str
    session: str
    command_id: str
    intent_id: str
    roster_version: int
    drone_id: int
    connection_epoch: int
    seq: int
    issued_at: int
    ttl_ms: int
    operation: CommandOperation
    args: Mapping[str, int | str]
    signature: str

    def unsigned_event(self) -> dict[str, object]:
        return {
            "v": self.v,
            "t": self.t,
            "type": self.type,
            "event_id": self.event_id,
            "session": self.session,
            "command_id": self.command_id,
            "intent_id": self.intent_id,
            "roster_version": self.roster_version,
            "drone_id": self.drone_id,
            "connection_epoch": self.connection_epoch,
            "seq": self.seq,
            "issued_at": self.issued_at,
            "ttl_ms": self.ttl_ms,
            "operation": self.operation.value,
            "args": dict(self.args),
        }

    def to_event(self) -> dict[str, object]:
        return {**self.unsigned_event(), "signature": self.signature}

    def audit_event(self) -> dict[str, object]:
        """Return the loggable record; signatures are never written to the audit log."""
        return self.unsigned_event()


@dataclass(frozen=True, slots=True)
class CapabilitiesFrame:
    """Node-authored camera capabilities plus the probed hardware profile."""

    v: Literal[1]
    t: int
    type: Literal["capabilities"]
    event_id: str
    session: str
    drone_id: int
    connection_epoch: int
    native_panorama_modes: tuple[str, ...]
    photo_capture: bool
    gimbal_pitch_min_deg: float
    gimbal_pitch_max_deg: float
    horizontal_fov_deg: float
    storage_remaining_bytes: int
    media_retrieval: bool
    aircraft_model: str
    aircraft_firmware: str
    rc_firmware: str
    phone_model: str
    android_version: str
    sdk_version: str
    measured_hfov_deg: float | None

    def to_event(self) -> dict[str, object]:
        return {
            "v": self.v,
            "t": self.t,
            "type": self.type,
            "event_id": self.event_id,
            "session": self.session,
            "drone_id": self.drone_id,
            "connection_epoch": self.connection_epoch,
            **self._payload(),
        }

    def state_payload(self) -> dict[str, object]:
        """Return the per-aircraft projection without transport-only fields."""
        return {
            "v": self.v,
            "t": self.t,
            "type": self.type,
            "drone_id": self.drone_id,
            **self._payload(),
        }

    def _payload(self) -> dict[str, object]:
        return {
            "native_panorama_modes": list(self.native_panorama_modes),
            "photo_capture": self.photo_capture,
            "gimbal_pitch_min_deg": self.gimbal_pitch_min_deg,
            "gimbal_pitch_max_deg": self.gimbal_pitch_max_deg,
            "horizontal_fov_deg": self.horizontal_fov_deg,
            "storage_remaining_bytes": self.storage_remaining_bytes,
            "media_retrieval": self.media_retrieval,
            "aircraft_model": self.aircraft_model,
            "aircraft_firmware": self.aircraft_firmware,
            "rc_firmware": self.rc_firmware,
            "phone_model": self.phone_model,
            "android_version": self.android_version,
            "sdk_version": self.sdk_version,
            "measured_hfov_deg": self.measured_hfov_deg,
        }


@dataclass(frozen=True, slots=True)
class WirePose:
    x: float
    y: float
    z: float

    def to_dict(self) -> dict[str, float]:
        return {"x": self.x, "y": self.y, "z": self.z}


@dataclass(frozen=True, slots=True)
class WireIntrinsics:
    width_px: int
    height_px: int
    horizontal_fov_deg: float
    projection: str

    def to_dict(self) -> dict[str, object]:
        return {
            "width_px": self.width_px,
            "height_px": self.height_px,
            "horizontal_fov_deg": self.horizontal_fov_deg,
            "projection": self.projection,
        }


@dataclass(frozen=True, slots=True)
class MapPoseProvenance:
    navigation_pose_event_id: str
    navigation_pose_seq: int
    command_id: str
    route_id: str
    pose_time_ms: int
    fix_time_ms: int
    position_uncertainty_mm: int
    navigation_config_id: str
    navigation_config_sha256: str
    map_version: str
    map_sha256: str
    geometry_sha256: str
    camera_calibration_sha256: str
    body_extrinsics_sha256: str
    world_transform_sha256: str
    control_source_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "navigation_pose_event_id": self.navigation_pose_event_id,
            "navigation_pose_seq": self.navigation_pose_seq,
            "command_id": self.command_id,
            "route_id": self.route_id,
            "pose_time_ms": self.pose_time_ms,
            "fix_time_ms": self.fix_time_ms,
            "position_uncertainty_mm": self.position_uncertainty_mm,
            "navigation_config_id": self.navigation_config_id,
            "navigation_config_sha256": self.navigation_config_sha256,
            "map_version": self.map_version,
            "map_sha256": self.map_sha256,
            "geometry_sha256": self.geometry_sha256,
            "camera_calibration_sha256": self.camera_calibration_sha256,
            "body_extrinsics_sha256": self.body_extrinsics_sha256,
            "world_transform_sha256": self.world_transform_sha256,
            "control_source_ids": list(self.control_source_ids),
        }


@dataclass(frozen=True, slots=True)
class MediaFileRecord:
    """Wire mirror of ``adapters.protocols.MediaFile`` without the transport envelope."""

    capture_id: str
    file_id: str
    timestamp_ms: int
    drone_id: int
    connection_epoch: int
    pose: WirePose
    actual_yaw_deg: float
    gimbal_pitch_deg: float
    intrinsics: WireIntrinsics
    checksum_sha256: str
    storage_ref: str
    retrieval_status: str
    position_frame: str | None = None
    yaw_frame: str | None = None
    map_pose_provenance: MapPoseProvenance | None = None

    def to_dict(self) -> dict[str, object]:
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
            "retrieval_status": self.retrieval_status,
            **(
                {}
                if self.position_frame is None
                else {
                    "position_frame": self.position_frame,
                    "yaw_frame": self.yaw_frame,
                    "map_pose_provenance": None
                    if self.map_pose_provenance is None
                    else self.map_pose_provenance.to_dict(),
                }
            ),
        }


@dataclass(frozen=True, slots=True)
class MediaFileFrame:
    v: Literal[1]
    t: int
    type: Literal["media_file"]
    event_id: str
    session: str
    file: MediaFileRecord

    def to_event(self) -> dict[str, object]:
        return {
            "v": self.v,
            "t": self.t,
            "type": self.type,
            "event_id": self.event_id,
            "session": self.session,
            **self.file.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class CaptureBundleFrame:
    """Wire mirror of ``adapters.protocols.CaptureBundle``."""

    v: Literal[1]
    t: int
    type: Literal["capture_bundle"]
    event_id: str
    session: str
    room_id: str
    capture_id: str
    drone_id: int
    connection_epoch: int
    pattern: str
    coverage: str
    status: str
    media: tuple[MediaFileRecord, ...]
    reason: str | None
    detail: str | None

    def to_event(self) -> dict[str, object]:
        return {
            "v": self.v,
            "t": self.t,
            "type": self.type,
            "event_id": self.event_id,
            "session": self.session,
            "room_id": self.room_id,
            "capture_id": self.capture_id,
            "drone_id": self.drone_id,
            "connection_epoch": self.connection_epoch,
            "pattern": self.pattern,
            "coverage": self.coverage,
            "status": self.status,
            "media": [record.to_dict() for record in self.media],
            "reason": self.reason,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class SuggestedDelta:
    kind: DeltaKind
    degrees: float

    def to_dict(self) -> dict[str, object]:
        return {"kind": self.kind.value, "degrees": self.degrees}


@dataclass(frozen=True, slots=True)
class CaptureReadinessFrame:
    """Node-authored capture guidance state shared with the console."""

    v: Literal[1]
    t: int
    type: Literal["capture_readiness"]
    event_id: str
    session: str
    drone_id: int
    connection_epoch: int
    room_id: str | None
    capture_id: str | None
    guidance_mode: GuidanceMode
    pose_source: str
    pose_ok: bool
    clearance_ok: bool
    camera_ok: bool
    storage_ok: bool
    motion_ok: bool
    image_quality_ok: bool
    coverage_missing: tuple[float, ...]
    next_heading_deg: float | None
    suggested_delta: SuggestedDelta | None

    def to_event(self) -> dict[str, object]:
        return {
            "v": self.v,
            "t": self.t,
            "type": self.type,
            "event_id": self.event_id,
            "session": self.session,
            "drone_id": self.drone_id,
            "connection_epoch": self.connection_epoch,
            "room_id": self.room_id,
            "capture_id": self.capture_id,
            "guidance_mode": self.guidance_mode.value,
            "pose_source": self.pose_source,
            "pose_ok": self.pose_ok,
            "clearance_ok": self.clearance_ok,
            "camera_ok": self.camera_ok,
            "storage_ok": self.storage_ok,
            "motion_ok": self.motion_ok,
            "image_quality_ok": self.image_quality_ok,
            "coverage_missing": list(self.coverage_missing),
            "next_heading_deg": self.next_heading_deg,
            "suggested_delta": (
                None if self.suggested_delta is None else self.suggested_delta.to_dict()
            ),
        }


@dataclass(frozen=True, slots=True)
class NodeStatusFrame:
    """Node-authored bridge health and optional local-height evidence."""

    v: Literal[1]
    t: int
    type: Literal["node_status"]
    event_id: str
    session: str
    drone_id: int
    connection_epoch: int
    virtual_stick_enabled: bool
    control_authority: bool
    authority_change_reason: str | None
    watchdog_state: WatchdogState
    video_publish_state: VideoPublishState
    phone_battery_percent: int
    phone_thermal_state: PhoneThermalState
    local_height: LocalHeightFrame | None = None

    def to_event(self) -> dict[str, object]:
        return {
            "v": self.v,
            "t": self.t,
            "type": self.type,
            "event_id": self.event_id,
            "session": self.session,
            "drone_id": self.drone_id,
            "connection_epoch": self.connection_epoch,
            **self._payload(),
        }

    def state_payload(self, *, reported_at_ms: int | None = None) -> dict[str, object]:
        """Return the per-aircraft projection without transport-only fields."""
        return {
            "v": self.v,
            "t": self.t,
            "type": self.type,
            "drone_id": self.drone_id,
            **self._payload(
                reported_at_ms=self.t if reported_at_ms is None else reported_at_ms,
                include_local_height=True,
            ),
        }

    def _payload(
        self, *, reported_at_ms: int | None = None, include_local_height: bool = False
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "virtual_stick_enabled": self.virtual_stick_enabled,
            "control_authority": self.control_authority,
            "authority_change_reason": self.authority_change_reason,
            "watchdog_state": self.watchdog_state.value,
            "video_publish_state": self.video_publish_state.value,
            "phone_battery_percent": self.phone_battery_percent,
            "phone_thermal_state": self.phone_thermal_state.value,
        }
        if self.local_height is not None:
            payload["local_height"] = {
                **self.local_height.to_dict(),
                **({"reported_at_ms": reported_at_ms} if reported_at_ms is not None else {}),
            }
        elif include_local_height:
            payload["local_height"] = None
        return payload


@dataclass(frozen=True, slots=True)
class LocalHeightFrame:
    z_m: float
    source: Literal["flight_controller_altitude"]
    age_ms: int

    def to_dict(self) -> dict[str, object]:
        return {"z_m": self.z_m, "source": self.source, "age_ms": self.age_ms}


def parse_membership_request(raw: object) -> MembershipRequest:
    value = _mapping(raw, "invalid_membership", "membership frame must be an object")
    action_value = value.get("action")
    try:
        action = MembershipAction(action_value)
    except (TypeError, ValueError):
        raise ContractError("invalid_membership", "unknown membership action") from None
    if action not in WIRE_MEMBERSHIP_ACTIONS:
        raise ContractError("invalid_membership", "membership action is relay-internal")

    common = {
        "v",
        "t",
        "type",
        "event_id",
        "session",
        "drone_id",
        "action",
        "signature",
    }
    aircraft_readiness_fields = {
        "connection_epoch",
        "home_pose_confirmed",
        "control_authority",
        "rc_safety_operator_present",
    }
    ground_readiness_fields = {
        "connection_epoch",
        "drive_authority",
        "safety_operator_present",
        "local_stop_ready",
        "heartbeat_ready",
        "pose_identity",
    }
    action_fields = {
        MembershipAction.JOIN: {"adapter_id", "capabilities"},
        MembershipAction.READINESS: aircraft_readiness_fields,
        MembershipAction.GRACEFUL_LEAVE: {"connection_epoch"},
    }
    expected_fields = common | action_fields[action]
    if action is MembershipAction.JOIN and "node_type" in value:
        expected_fields.add("node_type")
    if action is MembershipAction.READINESS and set(value) == common | ground_readiness_fields:
        expected_fields = common | ground_readiness_fields
    _exact_fields(value, expected_fields, "invalid_membership")
    _common_envelope(value, expected_type="membership", code="invalid_membership")

    drone_id = _positive_int(value["drone_id"], "drone_id", "invalid_membership")
    signature = _nonempty_string(value["signature"], "signature", "invalid_signature")

    if action is MembershipAction.JOIN:
        adapter_id = _bounded_state_text(value["adapter_id"], "adapter_id", "invalid_membership")
        capabilities = _string_list(value["capabilities"], "capabilities", allow_empty=False)
        try:
            node_type = None if "node_type" not in value else NodeType(value["node_type"])
        except (TypeError, ValueError):
            raise ContractError(
                "invalid_membership", "node_type must be aircraft or ground"
            ) from None
        return MembershipRequest(
            1,
            value["t"],
            "membership",
            value["event_id"],
            value["session"],
            drone_id,
            action,
            signature,
            adapter_id=adapter_id,
            capabilities=capabilities,
            node_type=node_type,
        )

    connection_epoch = _positive_int(
        value["connection_epoch"], "connection_epoch", "invalid_membership"
    )
    if action is MembershipAction.READINESS:
        if set(value) == common | ground_readiness_fields:
            for field in (
                "drive_authority",
                "safety_operator_present",
                "local_stop_ready",
                "heartbeat_ready",
            ):
                if not isinstance(value[field], bool):
                    raise ContractError("invalid_membership", f"{field} must be a boolean")
            pose = _mapping(
                value["pose_identity"], "invalid_membership", "pose_identity must be an object"
            )
            _exact_fields(
                pose,
                {"event_id", "session", "connection_epoch", "source_id", "frame"},
                "invalid_membership",
            )
            pose_identity = GroundPoseIdentity(
                _nonempty_string(pose["event_id"], "pose_identity.event_id", "invalid_membership"),
                _nonempty_string(pose["session"], "pose_identity.session", "invalid_membership"),
                _positive_int(
                    pose["connection_epoch"],
                    "pose_identity.connection_epoch",
                    "invalid_membership",
                ),
                _nonempty_string(
                    pose["source_id"], "pose_identity.source_id", "invalid_membership"
                ),
                _nonempty_string(pose["frame"], "pose_identity.frame", "invalid_membership"),
            )
            return MembershipRequest(
                1,
                value["t"],
                "membership",
                value["event_id"],
                value["session"],
                drone_id,
                action,
                signature,
                connection_epoch=connection_epoch,
                drive_authority=value["drive_authority"],
                safety_operator_present=value["safety_operator_present"],
                local_stop_ready=value["local_stop_ready"],
                heartbeat_ready=value["heartbeat_ready"],
                pose_identity=pose_identity,
            )
        for field in (
            "home_pose_confirmed",
            "control_authority",
            "rc_safety_operator_present",
        ):
            if not isinstance(value[field], bool):
                raise ContractError("invalid_membership", f"{field} must be a boolean")
        return MembershipRequest(
            1,
            value["t"],
            "membership",
            value["event_id"],
            value["session"],
            drone_id,
            action,
            signature,
            connection_epoch=connection_epoch,
            home_pose_confirmed=value["home_pose_confirmed"],
            control_authority=value["control_authority"],
            rc_safety_operator_present=value["rc_safety_operator_present"],
        )

    return MembershipRequest(
        1,
        value["t"],
        "membership",
        value["event_id"],
        value["session"],
        drone_id,
        action,
        signature,
        connection_epoch=connection_epoch,
    )


def parse_telemetry(raw: object) -> TelemetryV1:
    value = _mapping(raw, "invalid_telemetry", "telemetry frame must be an object")
    fields = {
        "v",
        "t",
        "type",
        "event_id",
        "session",
        "drone",
        "connection_epoch",
        "x",
        "y",
        "z",
        "vx",
        "vy",
        "vz",
        "battery",
        "state",
        "link",
        "pos_quality",
    }
    _exact_fields(value, fields, "invalid_telemetry")
    _common_envelope(value, expected_type="telemetry", code="invalid_telemetry")
    values = {
        field: _finite_number(value[field], field, "invalid_telemetry")
        for field in (
            "x",
            "y",
            "z",
            "vx",
            "vy",
            "vz",
            "battery",
            "link",
            "pos_quality",
        )
    }
    for field in ("battery", "link", "pos_quality"):
        if not 0 <= values[field] <= 1:
            raise ContractError("invalid_telemetry", f"{field} must be between 0 and 1")
    return TelemetryV1(
        1,
        value["t"],
        "telemetry",
        value["event_id"],
        value["session"],
        _positive_int(value["drone"], "drone", "invalid_telemetry"),
        _positive_int(value["connection_epoch"], "connection_epoch", "invalid_telemetry"),
        values["x"],
        values["y"],
        values["z"],
        values["vx"],
        values["vy"],
        values["vz"],
        values["battery"],
        _bounded_state_text(value["state"], "state", "invalid_telemetry", maximum_utf8_bytes=128),
        values["link"],
        values["pos_quality"],
    )


def parse_adapter_acknowledgement(raw: object) -> AdapterAcknowledgement:
    value = _mapping(raw, "invalid_acknowledgement", "acknowledgement must be an object")
    fields = {
        "v",
        "t",
        "type",
        "event_id",
        "session",
        "intent_id",
        "command_id",
        "status",
        "drone_id",
        "connection_epoch",
        "roster_version",
        "reason",
        "detail",
    }
    _exact_fields(value, fields, "invalid_acknowledgement")
    _common_envelope(value, expected_type="acknowledgement", code="invalid_acknowledgement")
    try:
        status = LifecycleStatus(value["status"])
    except (TypeError, ValueError):
        raise ContractError("invalid_acknowledgement", "unknown lifecycle status") from None
    if status is LifecycleStatus.REFUSED:
        raise ContractError("invalid_acknowledgement", "refused outcomes use the refusal envelope")
    reason = _nullable_string(value["reason"], "reason", machine_readable=True)
    detail = _nullable_string(value["detail"], "detail")
    command_id = _nonempty_string(value["command_id"], "command_id", "invalid_acknowledgement")
    if status in {LifecycleStatus.FAILED, LifecycleStatus.INVALIDATED} and reason is None:
        raise ContractError("invalid_acknowledgement", "terminal failure requires a reason")
    return AdapterAcknowledgement(
        1,
        value["t"],
        "acknowledgement",
        value["event_id"],
        value["session"],
        _nonempty_string(value["intent_id"], "intent_id", "invalid_acknowledgement"),
        command_id,
        status,
        _positive_int(value["drone_id"], "drone_id", "invalid_acknowledgement"),
        _positive_int(value["connection_epoch"], "connection_epoch", "invalid_acknowledgement"),
        _nonnegative_int(value["roster_version"], "roster_version", "invalid_acknowledgement"),
        reason,
        detail,
    )


def parse_command(raw: object) -> CommandFrame:
    code = "invalid_command"
    value = _mapping(raw, code, "command frame must be an object")
    fields = _ENVELOPE_FIELDS | {
        "command_id",
        "intent_id",
        "roster_version",
        "drone_id",
        "connection_epoch",
        "seq",
        "issued_at",
        "ttl_ms",
        "operation",
        "args",
        "signature",
    }
    _exact_fields(value, fields, code)
    _common_envelope(value, expected_type="command", code=code)
    try:
        operation = CommandOperation(value["operation"])
    except (TypeError, ValueError):
        raise ContractError(code, "unknown command operation") from None
    return CommandFrame(
        1,
        value["t"],
        "command",
        value["event_id"],
        value["session"],
        _nonempty_string(value["command_id"], "command_id", code),
        _nonempty_string(value["intent_id"], "intent_id", code),
        _nonnegative_int(value["roster_version"], "roster_version", code),
        _positive_int(value["drone_id"], "drone_id", code),
        _positive_int(value["connection_epoch"], "connection_epoch", code),
        _positive_int(value["seq"], "seq", code),
        _nonnegative_int(value["issued_at"], "issued_at", code),
        _positive_int(value["ttl_ms"], "ttl_ms", code),
        operation,
        _command_arguments(operation, value["args"], code),
        _nonempty_string(value["signature"], "signature", "invalid_signature"),
    )


def parse_capabilities(raw: object) -> CapabilitiesFrame:
    code = "invalid_capabilities"
    value = _mapping(raw, code, "capabilities frame must be an object")
    fields = _ENVELOPE_FIELDS | {
        "drone_id",
        "connection_epoch",
        "native_panorama_modes",
        "photo_capture",
        "gimbal_pitch_min_deg",
        "gimbal_pitch_max_deg",
        "horizontal_fov_deg",
        "storage_remaining_bytes",
        "media_retrieval",
        "aircraft_model",
        "aircraft_firmware",
        "rc_firmware",
        "phone_model",
        "android_version",
        "sdk_version",
        "measured_hfov_deg",
    }
    _exact_fields(value, fields, code)
    _common_envelope(value, expected_type="capabilities", code=code)
    pitch_min = _finite_number(value["gimbal_pitch_min_deg"], "gimbal_pitch_min_deg", code)
    pitch_max = _finite_number(value["gimbal_pitch_max_deg"], "gimbal_pitch_max_deg", code)
    if pitch_min >= pitch_max:
        raise ContractError(code, "gimbal pitch range must be ordered")
    horizontal_fov = _finite_number(value["horizontal_fov_deg"], "horizontal_fov_deg", code)
    if not 0 < horizontal_fov <= 360:
        raise ContractError(code, "horizontal_fov_deg must be between 0 and 360")
    measured = value["measured_hfov_deg"]
    if measured is not None:
        measured = _finite_number(measured, "measured_hfov_deg", code)
        if not 0 < measured < 180:
            raise ContractError(code, "measured_hfov_deg must be null or between 0 and 180")
    storage_remaining = _nonnegative_int(
        value["storage_remaining_bytes"], "storage_remaining_bytes", code
    )
    if storage_remaining > MAX_STORAGE_REMAINING_BYTES:
        raise ContractError(
            code,
            f"storage_remaining_bytes must be at most {MAX_STORAGE_REMAINING_BYTES}",
        )
    return CapabilitiesFrame(
        1,
        value["t"],
        "capabilities",
        value["event_id"],
        value["session"],
        _positive_int(value["drone_id"], "drone_id", code),
        _positive_int(value["connection_epoch"], "connection_epoch", code),
        _string_list(
            value["native_panorama_modes"], "native_panorama_modes", allow_empty=True, code=code
        ),
        _boolean(value["photo_capture"], "photo_capture", code),
        pitch_min,
        pitch_max,
        horizontal_fov,
        storage_remaining,
        _boolean(value["media_retrieval"], "media_retrieval", code),
        _bounded_state_text(value["aircraft_model"], "aircraft_model", code),
        _bounded_state_text(value["aircraft_firmware"], "aircraft_firmware", code),
        _bounded_state_text(value["rc_firmware"], "rc_firmware", code),
        _bounded_state_text(value["phone_model"], "phone_model", code),
        _bounded_state_text(value["android_version"], "android_version", code),
        _bounded_state_text(value["sdk_version"], "sdk_version", code),
        measured,
    )


def parse_media_file(raw: object) -> MediaFileFrame:
    code = "invalid_media_file"
    value = _mapping(raw, code, "media_file frame must be an object")
    if set(value) not in {
        _ENVELOPE_FIELDS | _MEDIA_RECORD_LEGACY_FIELDS,
        _ENVELOPE_FIELDS | _MEDIA_RECORD_FIELDS,
    }:
        raise ContractError(code, "media frame metadata is all-or-none")
    _common_envelope(value, expected_type="media_file", code=code)
    record = {key: item for key, item in value.items() if key not in _ENVELOPE_FIELDS}
    return MediaFileFrame(
        1,
        value["t"],
        "media_file",
        value["event_id"],
        value["session"],
        _media_record(record, code),
    )


def parse_capture_bundle(raw: object) -> CaptureBundleFrame:
    code = "invalid_capture_bundle"
    value = _mapping(raw, code, "capture_bundle frame must be an object")
    fields = _ENVELOPE_FIELDS | {
        "room_id",
        "capture_id",
        "drone_id",
        "connection_epoch",
        "pattern",
        "coverage",
        "status",
        "media",
        "reason",
        "detail",
    }
    _exact_fields(value, fields, code)
    _common_envelope(value, expected_type="capture_bundle", code=code)
    capture_id = _nonempty_string(value["capture_id"], "capture_id", code)
    drone_id = _positive_int(value["drone_id"], "drone_id", code)
    connection_epoch = _positive_int(value["connection_epoch"], "connection_epoch", code)
    pattern = _choice(value["pattern"], "pattern", _CAPTURE_PATTERNS, code)
    coverage = _choice(value["coverage"], "coverage", _CAPTURE_COVERAGES, code)
    status = _choice(value["status"], "status", _CAMERA_RESULT_STATUSES, code)
    media_raw = value["media"]
    if not isinstance(media_raw, list):
        raise ContractError(code, "media must be a list")
    if len(media_raw) > MAX_CAPTURE_BUNDLE_MEDIA_ITEMS:
        raise ContractError(
            code, f"media may contain at most {MAX_CAPTURE_BUNDLE_MEDIA_ITEMS} items"
        )
    media = tuple(
        _media_record(_mapping(item, code, "media entries must be objects"), code)
        for item in media_raw
    )
    if len({record.file_id for record in media}) != len(media):
        raise ContractError(code, "media may not contain duplicate file_id values")
    for record in media:
        if (
            record.capture_id != capture_id
            or record.drone_id != drone_id
            or record.connection_epoch != connection_epoch
        ):
            raise ContractError(code, "media record does not belong to this bundle")
    reason = _nullable_string(value["reason"], "reason", machine_readable=True, code=code)
    detail = _nullable_string(value["detail"], "detail", code=code)
    if status != "completed" and reason is None:
        raise ContractError(code, "failed or unsupported bundle requires a reason")
    return CaptureBundleFrame(
        1,
        value["t"],
        "capture_bundle",
        value["event_id"],
        value["session"],
        _nonempty_string(value["room_id"], "room_id", code),
        capture_id,
        drone_id,
        connection_epoch,
        pattern,
        coverage,
        status,
        media,
        reason,
        detail,
    )


def parse_capture_readiness(raw: object) -> CaptureReadinessFrame:
    code = "invalid_capture_readiness"
    value = _mapping(raw, code, "capture_readiness frame must be an object")
    fields = _ENVELOPE_FIELDS | {
        "drone_id",
        "connection_epoch",
        "room_id",
        "capture_id",
        "guidance_mode",
        "pose_source",
        "pose_ok",
        "clearance_ok",
        "camera_ok",
        "storage_ok",
        "motion_ok",
        "image_quality_ok",
        "coverage_missing",
        "next_heading_deg",
        "suggested_delta",
    }
    _exact_fields(value, fields, code)
    _common_envelope(value, expected_type="capture_readiness", code=code)
    coverage_raw = value["coverage_missing"]
    if not isinstance(coverage_raw, list):
        raise ContractError(code, "coverage_missing must be a list")
    if len(coverage_raw) > MAX_COVERAGE_MISSING_ITEMS:
        raise ContractError(
            code,
            f"coverage_missing may contain at most {MAX_COVERAGE_MISSING_ITEMS} items",
        )
    coverage_missing = tuple(_azimuth(item, "coverage_missing", code) for item in coverage_raw)
    if len(set(coverage_missing)) != len(coverage_missing):
        raise ContractError(code, "coverage_missing may not contain duplicates")
    next_heading = value["next_heading_deg"]
    if next_heading is not None:
        next_heading = _azimuth(next_heading, "next_heading_deg", code)
    delta_raw = value["suggested_delta"]
    suggested_delta = None
    if delta_raw is not None:
        delta = _mapping(delta_raw, code, "suggested_delta must be an object or null")
        _exact_fields(delta, {"kind", "degrees"}, code)
        try:
            kind = DeltaKind(delta["kind"])
        except (TypeError, ValueError):
            raise ContractError(code, "suggested_delta kind must be yaw or gimbal") from None
        suggested_delta = SuggestedDelta(kind, _finite_number(delta["degrees"], "degrees", code))
    try:
        guidance_mode = GuidanceMode(value["guidance_mode"])
    except (TypeError, ValueError):
        raise ContractError(
            code, "guidance_mode must be visual_advisory or registered_metric"
        ) from None
    return CaptureReadinessFrame(
        1,
        value["t"],
        "capture_readiness",
        value["event_id"],
        value["session"],
        _positive_int(value["drone_id"], "drone_id", code),
        _positive_int(value["connection_epoch"], "connection_epoch", code),
        _nullable_string(value["room_id"], "room_id", code=code),
        _nullable_string(value["capture_id"], "capture_id", code=code),
        guidance_mode,
        _nonempty_string(value["pose_source"], "pose_source", code),
        _boolean(value["pose_ok"], "pose_ok", code),
        _boolean(value["clearance_ok"], "clearance_ok", code),
        _boolean(value["camera_ok"], "camera_ok", code),
        _boolean(value["storage_ok"], "storage_ok", code),
        _boolean(value["motion_ok"], "motion_ok", code),
        _boolean(value["image_quality_ok"], "image_quality_ok", code),
        coverage_missing,
        next_heading,
        suggested_delta,
    )


def _local_height(value: object, code: str) -> LocalHeightFrame:
    height = _mapping(value, code, "local_height must be an object")
    _exact_fields(height, {"z_m", "source", "age_ms"}, code)
    z_m = _finite_number(height["z_m"], "local_height.z_m", code)
    source = height["source"]
    if source != "flight_controller_altitude":
        raise ContractError(code, "local_height.source is unsupported")
    return LocalHeightFrame(
        z_m=z_m,
        source="flight_controller_altitude",
        age_ms=_nonnegative_int(height["age_ms"], "local_height.age_ms", code),
    )


def parse_node_status(raw: object) -> NodeStatusFrame:
    code = "invalid_node_status"
    value = _mapping(raw, code, "node_status frame must be an object")
    fields = _ENVELOPE_FIELDS | {
        "drone_id",
        "connection_epoch",
        "virtual_stick_enabled",
        "control_authority",
        "authority_change_reason",
        "watchdog_state",
        "video_publish_state",
        "phone_battery_percent",
        "phone_thermal_state",
    }
    if "local_height" in value:
        fields = fields | {"local_height"}
    _exact_fields(value, fields, code)
    _common_envelope(value, expected_type="node_status", code=code)
    battery = _nonnegative_int(value["phone_battery_percent"], "phone_battery_percent", code)
    if battery > 100:
        raise ContractError(code, "phone_battery_percent must be between 0 and 100")
    return NodeStatusFrame(
        1,
        value["t"],
        "node_status",
        value["event_id"],
        value["session"],
        _positive_int(value["drone_id"], "drone_id", code),
        _positive_int(value["connection_epoch"], "connection_epoch", code),
        _boolean(value["virtual_stick_enabled"], "virtual_stick_enabled", code),
        _boolean(value["control_authority"], "control_authority", code),
        _nullable_string(
            value["authority_change_reason"],
            "authority_change_reason",
            machine_readable=True,
            code=code,
        ),
        _enum(WatchdogState, value["watchdog_state"], "watchdog_state", code),
        _enum(VideoPublishState, value["video_publish_state"], "video_publish_state", code),
        battery,
        _enum(PhoneThermalState, value["phone_thermal_state"], "phone_thermal_state", code),
        None if "local_height" not in value else _local_height(value["local_height"], code),
    )


def acknowledgement_event(
    *,
    t: int,
    event_id: str,
    session: str,
    intent_id: str,
    command_id: str | None = None,
    status: LifecycleStatus,
    roster_version: int,
    source: str = "relay",
    drone_id: int | None = None,
    connection_epoch: int | None = None,
    reason: str | None = None,
    detail: str | None = None,
    result: Mapping[str, object] | None = None,
) -> dict[str, object]:
    if status is LifecycleStatus.REFUSED:
        raise ValueError("refused outcomes use refusal_event")
    if status in {LifecycleStatus.FAILED, LifecycleStatus.INVALIDATED} and reason is None:
        raise ValueError("failed or invalidated acknowledgements require a reason")
    if reason is not None and not _is_machine_code(reason):
        raise ValueError("acknowledgement reason must be snake_case")
    event: dict[str, object] = {
        "v": 1,
        "t": t,
        "type": "acknowledgement",
        "event_id": event_id,
        "session": session,
        "intent_id": intent_id,
        "command_id": command_id,
        "status": status.value,
        "source": source,
        "drone_id": drone_id,
        "connection_epoch": connection_epoch,
        "roster_version": roster_version,
        "reason": reason,
        "detail": detail,
    }
    if result is not None:
        if set(result) != {"run_id", "connection_epoch"}:
            raise ValueError("acknowledgement result must contain survey run identity")
        run_id = result["run_id"]
        epoch = result["connection_epoch"]
        if (
            not isinstance(run_id, str)
            or not run_id
            or len(run_id) > 128
            or run_id != run_id.strip()
            or not run_id.isprintable()
            or not isinstance(epoch, int)
            or isinstance(epoch, bool)
            or not 1 <= epoch <= 2_147_483_647
        ):
            raise ValueError("acknowledgement result is not a bounded survey run identity")
        event["result"] = {"run_id": run_id, "connection_epoch": epoch}
    return event


def refusal_event(
    *,
    t: int,
    event_id: str,
    session: str,
    intent_id: str | None,
    command_id: str | None = None,
    reason: str,
    detail: str,
    roster_version: int,
    source: str = "relay",
    drone_id: int | None = None,
    connection_epoch: int | None = None,
) -> dict[str, object]:
    if not _is_machine_code(reason):
        raise ValueError("refusal reason must be snake_case")
    return {
        "v": 1,
        "t": t,
        "type": "refusal",
        "event_id": event_id,
        "session": session,
        "intent_id": intent_id,
        "command_id": command_id,
        "status": LifecycleStatus.REFUSED.value,
        "source": source,
        "drone_id": drone_id,
        "connection_epoch": connection_epoch,
        "roster_version": roster_version,
        "reason": reason,
        "detail": detail,
    }


def command_event(
    *,
    t: int,
    event_id: str,
    session: str,
    command_id: str,
    intent_id: str,
    roster_version: int,
    drone_id: int,
    connection_epoch: int,
    seq: int,
    issued_at: int,
    ttl_ms: int,
    operation: CommandOperation,
    args: Mapping[str, object],
) -> dict[str, object]:
    """Build the unsigned command; sign it with ``relay.auth.sign_event`` before sending."""
    event: dict[str, object] = {
        "v": 1,
        "t": t,
        "type": "command",
        "event_id": event_id,
        "session": session,
        "command_id": command_id,
        "intent_id": intent_id,
        "roster_version": roster_version,
        "drone_id": drone_id,
        "connection_epoch": connection_epoch,
        "seq": seq,
        "issued_at": issued_at,
        "ttl_ms": ttl_ms,
        "operation": operation.value if isinstance(operation, CommandOperation) else operation,
        "args": dict(args),
    }
    try:
        parse_command({**event, "signature": "0" * 64})
    except ContractError as error:
        raise ValueError(error.detail) from None
    return event


def _mapping(raw: object, code: str, detail: str) -> Mapping[str, object]:
    if not isinstance(raw, Mapping) or not all(isinstance(key, str) for key in raw):
        raise ContractError(code, detail)
    return raw


def _exact_fields(value: Mapping[str, object], fields: set[str], code: str) -> None:
    if set(value) != fields:
        raise ContractError(code, "frame fields do not match the v1 contract")


def _common_envelope(value: Mapping[str, object], *, expected_type: str, code: str) -> None:
    if value["v"] != 1 or isinstance(value["v"], bool) or not isinstance(value["v"], int):
        raise ContractError(code, "v must be integer 1")
    _nonnegative_int(value["t"], "t", code)
    if value["type"] != expected_type:
        raise ContractError(code, f"type must be {expected_type}")
    _nonempty_string(value["event_id"], "event_id", code)
    _nonempty_string(value["session"], "session", code)


def _nonempty_string(value: object, field: str, code: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise ContractError(code, f"{field} must be a non-empty string of at most 512 chars")
    return value


def _bounded_state_text(
    value: object,
    field: str,
    code: str,
    *,
    maximum_utf8_bytes: int = MAX_CAPABILITY_ITEM_UTF8_BYTES,
) -> str:
    result = _nonempty_string(value, field, code)
    if result != result.strip() or not result.isprintable():
        raise ContractError(code, f"{field} must be canonical printable text")
    try:
        encoded = result.encode("utf-8")
    except UnicodeEncodeError:
        raise ContractError(code, f"{field} must be valid UTF-8 text") from None
    if len(encoded) > maximum_utf8_bytes:
        raise ContractError(code, f"{field} may contain at most {maximum_utf8_bytes} UTF-8 bytes")
    return result


def _nullable_string(
    value: object,
    field: str,
    *,
    machine_readable: bool = False,
    code: str = "invalid_acknowledgement",
) -> str | None:
    if value is None:
        return None
    result = _nonempty_string(value, field, code)
    if machine_readable and not _is_machine_code(result):
        raise ContractError(code, f"{field} must be snake_case")
    return result


def _nonnegative_int(value: object, field: str, code: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ContractError(code, f"{field} must be a non-negative integer")
    return value


def _positive_int(value: object, field: str, code: str) -> int:
    result = _nonnegative_int(value, field, code)
    if result == 0:
        raise ContractError(code, f"{field} must be a positive integer")
    return result


def _finite_number(value: object, field: str, code: str) -> float:
    if not isinstance(value, int | float) or isinstance(value, bool) or not math.isfinite(value):
        raise ContractError(code, f"{field} must be a finite number")
    return float(value)


def _string_list(
    value: object, field: str, *, allow_empty: bool, code: str = "invalid_membership"
) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ContractError(code, f"{field} must be a list")
    if len(value) > MAX_CAPABILITY_LIST_ITEMS:
        raise ContractError(code, f"{field} may contain at most {MAX_CAPABILITY_LIST_ITEMS} items")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item or item != item.strip() or not item.isprintable():
            raise ContractError(code, f"{field} items must be canonical printable strings")
        try:
            encoded = item.encode("utf-8")
        except UnicodeEncodeError:
            raise ContractError(code, f"{field} items must be valid UTF-8 strings") from None
        if len(encoded) > MAX_CAPABILITY_ITEM_UTF8_BYTES:
            raise ContractError(
                code,
                f"{field} items may contain at most {MAX_CAPABILITY_ITEM_UTF8_BYTES} UTF-8 bytes",
            )
        result.append(item)
    if not result and not allow_empty:
        raise ContractError(code, f"{field} may not be empty")
    if len(set(result)) != len(result):
        raise ContractError(code, f"{field} may not contain duplicates")
    canonical = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(canonical) > MAX_CAPABILITY_LIST_CANONICAL_BYTES:
        raise ContractError(
            code,
            f"{field} canonical JSON may contain at most "
            f"{MAX_CAPABILITY_LIST_CANONICAL_BYTES} UTF-8 bytes",
        )
    return tuple(result)


def _boolean(value: object, field: str, code: str) -> bool:
    if not isinstance(value, bool):
        raise ContractError(code, f"{field} must be a boolean")
    return value


def _integer(value: object, field: str, code: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ContractError(code, f"{field} must be an integer")
    return value


def _choice(value: object, field: str, choices: frozenset[str], code: str) -> str:
    if not isinstance(value, str) or value not in choices:
        raise ContractError(code, f"{field} must be one of {', '.join(sorted(choices))}")
    return value


def _enum[E: StrEnum](cls: type[E], value: object, field: str, code: str) -> E:
    try:
        return cls(value)
    except (TypeError, ValueError):
        options = ", ".join(member.value for member in cls)
        raise ContractError(code, f"{field} must be one of {options}") from None


def _azimuth(value: object, field: str, code: str) -> float:
    result = _finite_number(value, field, code)
    if not 0 <= result < 360:
        raise ContractError(code, f"{field} azimuth must be between 0 and 360")
    return result


def _command_arguments(
    operation: CommandOperation, raw: object, code: str
) -> Mapping[str, int | str]:
    spec = COMMAND_ARGUMENT_FIELDS[operation]
    value = _mapping(raw, code, "command args must be an object")
    optional = {field for field, kind in spec.items() if kind.startswith("optional_")}
    if not set(value).issuperset(set(spec) - optional) or not set(value).issubset(set(spec)):
        raise ContractError(code, f"{operation.value} arguments do not match the v1 contract")
    result: dict[str, int | str] = {}
    for field, kind in spec.items():
        if kind.startswith("optional_") and field not in value:
            continue
        if kind in {"id", "optional_id"}:
            result[field] = _nonempty_string(value[field], field, code)
        elif kind == "ground_route_document":
            document = value[field]
            if not isinstance(document, str) or not document:
                raise ContractError(code, "navigation_route must be a bounded JSON document")
            try:
                size = len(document.encode("utf-8"))
            except UnicodeError:
                raise ContractError(code, "navigation_route must be valid UTF-8") from None
            if size > 32_768:
                raise ContractError(code, "navigation_route exceeds its byte bound")
            result[field] = document
        elif kind in {"positive", "optional_positive"}:
            result[field] = _positive_int(value[field], field, code)
        elif kind == "ground_linear_mm_s":
            result[field] = _nonnegative_int(value[field], field, code)
            if result[field] > MAX_GROUND_VELOCITY_MM_S:
                raise ContractError(code, f"{field} exceeds the ground speed cap")
        elif kind == "ground_angular_mrad_s":
            result[field] = _integer(value[field], field, code)
            if abs(result[field]) > MAX_GROUND_YAW_MRAD_S:
                raise ContractError(code, f"{field} exceeds the ground yaw cap")
        elif kind == "ground_duration_ms":
            result[field] = _positive_int(value[field], field, code)
            if result[field] > MAX_GROUND_VELOCITY_DURATION_MS:
                raise ContractError(code, f"{field} exceeds the ground duration cap")
        else:
            result[field] = _integer(value[field], field, code)
    if operation is CommandOperation.TAKEOFF:
        policy = {"maximum_height_mm", "max_local_height_age_ms"}
        present = policy & set(result)
        if present and (
            present != policy
            or not 0
            < result["z_mm"]
            <= result["maximum_height_mm"]
            <= MAX_SUPERVISED_TAKEOFF_HEIGHT_MM
            or result["max_local_height_age_ms"] > 500
        ):
            raise ContractError(code, "takeoff requires paired bounded supervised height policy")
    if operation is CommandOperation.GROUND_VELOCITY and (
        (result["linear_mm_s"] and result["angular_mrad_s"])
        or (not result["linear_mm_s"] and not result["angular_mrad_s"])
    ):
        raise ContractError(
            code, "ground velocity requires exactly one linear or angular component"
        )
    return MappingProxyType(result)


_MEDIA_RECORD_LEGACY_FIELDS = frozenset(
    {
        "capture_id",
        "file_id",
        "timestamp_ms",
        "drone_id",
        "connection_epoch",
        "pose",
        "actual_yaw_deg",
        "gimbal_pitch_deg",
        "intrinsics",
        "checksum_sha256",
        "storage_ref",
        "retrieval_status",
    }
)
_MEDIA_RECORD_FIELDS = _MEDIA_RECORD_LEGACY_FIELDS | {
    "position_frame",
    "yaw_frame",
    "map_pose_provenance",
}


def _media_record(value: Mapping[str, object], code: str) -> MediaFileRecord:
    if set(value) not in {_MEDIA_RECORD_LEGACY_FIELDS, _MEDIA_RECORD_FIELDS}:
        raise ContractError(code, "media frame metadata is all-or-none")
    pose = _mapping(value["pose"], code, "pose must be an object")
    _exact_fields(pose, {"x", "y", "z"}, code)
    intrinsics = _mapping(value["intrinsics"], code, "intrinsics must be an object")
    _exact_fields(intrinsics, {"width_px", "height_px", "horizontal_fov_deg", "projection"}, code)
    horizontal_fov = _finite_number(intrinsics["horizontal_fov_deg"], "horizontal_fov_deg", code)
    if not 0 < horizontal_fov <= 360:
        raise ContractError(code, "horizontal_fov_deg must be between 0 and 360")
    checksum = value["checksum_sha256"]
    if (
        not isinstance(checksum, str)
        or len(checksum) != 64
        or any(character not in "0123456789abcdef" for character in checksum)
    ):
        raise ContractError(code, "checksum_sha256 must be 64 lowercase hex characters")
    retrieval_status = _choice(
        value["retrieval_status"], "retrieval_status", _MEDIA_RETRIEVAL_STATUSES, code
    )
    if retrieval_status == "pending" and checksum != MEDIA_PENDING_CHECKSUM:
        raise ContractError(code, "pending media requires the all-zero checksum sentinel")
    if retrieval_status == "completed" and checksum == MEDIA_PENDING_CHECKSUM:
        raise ContractError(code, "completed media requires a content checksum")
    metadata_present = set(value) == _MEDIA_RECORD_FIELDS
    position_frame = None
    yaw_frame = None
    provenance = None
    if metadata_present:
        position_frame = _choice(
            value["position_frame"], "position_frame", frozenset({"dji_local_enu", "map_enu"}), code
        )
        yaw_frame = _choice(value["yaw_frame"], "yaw_frame", frozenset({"dji_compass_deg"}), code)
        provenance = _map_pose_provenance(value["map_pose_provenance"], code)
        if (position_frame == "map_enu") != (provenance is not None):
            raise ContractError(code, "map-frame media requires map pose provenance")
    return MediaFileRecord(
        _nonempty_string(value["capture_id"], "capture_id", code),
        _nonempty_string(value["file_id"], "file_id", code),
        _nonnegative_int(value["timestamp_ms"], "timestamp_ms", code),
        _positive_int(value["drone_id"], "drone_id", code),
        _positive_int(value["connection_epoch"], "connection_epoch", code),
        WirePose(
            _finite_number(pose["x"], "x", code),
            _finite_number(pose["y"], "y", code),
            _finite_number(pose["z"], "z", code),
        ),
        _finite_number(value["actual_yaw_deg"], "actual_yaw_deg", code),
        _finite_number(value["gimbal_pitch_deg"], "gimbal_pitch_deg", code),
        WireIntrinsics(
            _positive_int(intrinsics["width_px"], "width_px", code),
            _positive_int(intrinsics["height_px"], "height_px", code),
            horizontal_fov,
            _nonempty_string(intrinsics["projection"], "projection", code),
        ),
        checksum,
        _nonempty_string(value["storage_ref"], "storage_ref", code),
        retrieval_status,
        position_frame,
        yaw_frame,
        provenance,
    )


_MAP_POSE_PROVENANCE_FIELDS = frozenset(
    {
        "navigation_pose_event_id",
        "navigation_pose_seq",
        "command_id",
        "route_id",
        "pose_time_ms",
        "fix_time_ms",
        "position_uncertainty_mm",
        "navigation_config_id",
        "navigation_config_sha256",
        "map_version",
        "map_sha256",
        "geometry_sha256",
        "camera_calibration_sha256",
        "body_extrinsics_sha256",
        "world_transform_sha256",
        "control_source_ids",
    }
)


def _map_pose_provenance(value: object, code: str) -> MapPoseProvenance | None:
    if value is None:
        return None
    raw = _mapping(value, code, "map_pose_provenance must be an object or null")
    _exact_fields(raw, set(_MAP_POSE_PROVENANCE_FIELDS), code)
    identity_fields = (
        "navigation_pose_event_id",
        "command_id",
        "route_id",
        "navigation_config_id",
        "map_version",
    )
    hashes = (
        "navigation_config_sha256",
        "map_sha256",
        "geometry_sha256",
        "camera_calibration_sha256",
        "body_extrinsics_sha256",
        "world_transform_sha256",
    )
    values = {
        field: _nonempty_string(raw[field], field, code) for field in (*identity_fields, *hashes)
    }
    if any(not _is_identifier(values[field]) for field in identity_fields):
        raise ContractError(code, "map pose provenance identities are invalid")
    if any(not _is_sha256(values[field]) for field in hashes):
        raise ContractError(code, "map pose provenance hashes are invalid")
    pose_time = _nonnegative_int(raw["pose_time_ms"], "pose_time_ms", code)
    fix_time = _nonnegative_int(raw["fix_time_ms"], "fix_time_ms", code)
    sequence = _positive_int(raw["navigation_pose_seq"], "navigation_pose_seq", code)
    uncertainty = _positive_int(raw["position_uncertainty_mm"], "position_uncertainty_mm", code)
    if pose_time < fix_time:
        raise ContractError(code, "map pose provenance timing is invalid")
    sources = _string_list(
        raw["control_source_ids"], "control_source_ids", allow_empty=False, code=code
    )
    if tuple(sorted(set(sources))) != sources or any(
        not _is_identifier(source) for source in sources
    ):
        raise ContractError(code, "map pose provenance source identities are invalid")
    return MapPoseProvenance(
        values["navigation_pose_event_id"],
        sequence,
        values["command_id"],
        values["route_id"],
        pose_time,
        fix_time,
        uncertainty,
        values["navigation_config_id"],
        values["navigation_config_sha256"],
        values["map_version"],
        values["map_sha256"],
        values["geometry_sha256"],
        values["camera_calibration_sha256"],
        values["body_extrinsics_sha256"],
        values["world_transform_sha256"],
        sources,
    )


def _is_machine_code(value: str) -> bool:
    return bool(value) and all(
        "a" <= char <= "z" or "0" <= char <= "9" or char == "_" for char in value
    )


def _is_identifier(value: str) -> bool:
    return bool(value) and len(value) <= 128 and value.isprintable() and value == value.strip()


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)
