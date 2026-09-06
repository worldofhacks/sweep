"""Shared, transport-neutral autonomy domain models.

The relay owns wire envelopes and the authoritative registry.  This module owns the
immutable values consumed and emitted by the planner, arbiter, and dispatcher.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from math import dist, isfinite
from types import MappingProxyType
from typing import Literal

from relay.intent_v1 import IntentName, IntentV1

type JsonScalar = None | bool | int | float | str
type JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


@dataclass(frozen=True, slots=True)
class TranslationPolicy:
    frame: Literal["world", "aircraft_relative"]
    step_m: float

    def __post_init__(self) -> None:
        if (
            self.frame not in {"world", "aircraft_relative"}
            or not _is_finite_number(self.step_m)
            or self.step_m <= 0
        ):
            raise ValueError("translation policy requires a supported frame and positive step")


@dataclass(frozen=True, slots=True)
class TranslationGrounding:
    policy: TranslationPolicy
    headings: Mapping[int, float]

    def __post_init__(self) -> None:
        normalized: dict[int, float] = {}
        for drone_id, heading in self.headings.items():
            if (
                not isinstance(drone_id, int)
                or isinstance(drone_id, bool)
                or drone_id <= 0
                or not _is_finite_number(heading)
                or not 0 <= float(heading) < 360
            ):
                raise ValueError("translation headings must map aircraft IDs to degrees")
            normalized[drone_id] = float(heading)
        object.__setattr__(self, "headings", MappingProxyType(normalized))


@dataclass(frozen=True, slots=True)
class AltitudeGrounding:
    """Deployment scale, floor, and completion evidence policy."""

    step_m: float
    floor_z_m: float | None
    configuration_id: str
    completion_tolerance_m: float

    def __post_init__(self) -> None:
        if not _is_finite_number(self.step_m) or self.step_m <= 0:
            raise ValueError("altitude step must be finite and positive")
        if self.floor_z_m is not None and not _is_finite_number(self.floor_z_m):
            raise ValueError("altitude floor reference must be finite")
        if not isinstance(self.configuration_id, str) or not self.configuration_id.strip():
            raise ValueError("altitude requires an explicit configuration identity")
        if not _is_finite_number(self.completion_tolerance_m) or self.completion_tolerance_m <= 0:
            raise ValueError("altitude completion tolerance must be finite and positive")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "step_m": self.step_m,
            "floor_z_m": self.floor_z_m,
            "configuration_id": self.configuration_id,
            "completion_tolerance_m": self.completion_tolerance_m,
        }


class MembershipState(StrEnum):
    REGISTERED = "registered"
    READY = "ready"
    LEAVING = "leaving"
    DISCONNECTED = "disconnected"
    DEGRADED = "degraded"


class FlightState(StrEnum):
    DISARMED = "disarmed"
    LANDED = "landed"
    ARMED = "armed"
    TAKING_OFF = "taking_off"
    AIRBORNE = "airborne"
    HOVERING = "hovering"
    LANDING = "landing"
    EMERGENCY = "emergency"


class LossBehavior(StrEnum):
    HOLD = "hold"
    FAILSAFE = "failsafe"


class HoldScope(StrEnum):
    OPERATOR_SELECTION = "operator_selection"
    FLEET_SAFETY = "fleet_safety"
    TARGETED_SAFETY = "targeted_safety"


class CommandOperation(StrEnum):
    TAKEOFF = "takeoff"
    GOTO = "goto"
    ROTATE_TO = "rotate_to"
    HOVER = "hover"
    LAND = "land"
    ESTOP = "estop"
    CAMERA_CAPABILITIES = "camera_capabilities"
    SET_GIMBAL_PITCH = "set_gimbal_pitch"
    CAMERA_READY = "camera_ready"
    CAPTURE_PANORAMA = "capture_panorama"
    CAPTURE_PHOTO = "capture_photo"
    RETRIEVE_MEDIA = "retrieve_media"


class LifecycleStatus(StrEnum):
    ACCEPTED = "accepted"
    REFUSED = "refused"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"
    INVALIDATED = "invalidated"


class RefusalReason(StrEnum):
    UNSUPPORTED = "unsupported"
    INVALID_SELECTION = "invalid_selection"
    STALE_SELECTION = "stale_selection"
    STALE_ROSTER = "stale_roster"
    STALE_CONNECTION_EPOCH = "stale_connection_epoch"
    AIRCRAFT_NOT_REGISTERED = "aircraft_not_registered"
    AIRCRAFT_NOT_READY = "aircraft_not_ready"
    INVALID_STATE = "invalid_state"
    CONFIRMATION_REQUIRED = "confirmation_required"
    ESTOP_ACTIVE = "estop_active"
    GEOFENCE = "geofence"
    CEILING = "ceiling"
    SPACING = "spacing"
    BATTERY_RESERVE = "battery_reserve"
    BATTERY_CRITICAL = "battery_critical"
    LINK_QUALITY = "link_quality"
    LINK_STALE = "link_stale"
    POSITION_QUALITY = "position_quality"
    POSITION_STALE = "position_stale"
    OPERATOR_ABSENT = "operator_absent"
    CONTROL_AUTHORITY = "control_authority"
    ARMED_REQUIRED = "armed_required"
    RC_SAFETY_OPERATOR_ABSENT = "rc_safety_operator_absent"
    HOME_POSE_MISSING = "home_pose_missing"
    ACTIVE_TASK = "active_task"
    STORAGE = "storage"
    CAMERA_UNSUPPORTED = "camera_unsupported"
    CAMERA_NOT_READY = "camera_not_ready"
    CAMERA_FAILURE = "camera_failure"
    DOWNLOAD_FAILURE = "download_failure"
    ADAPTER_FAILURE = "adapter_failure"
    ADAPTER_TIMEOUT = "adapter_timeout"
    PLANNER_FAILURE = "planner_failure"
    CONFLICTING_MOTION = "conflicting_motion"
    INVALID_ROSTER_TRANSITION = "invalid_roster_transition"
    INVALID_RESUME = "invalid_resume"
    INVALID_PLAN = "invalid_plan"


@dataclass(frozen=True, slots=True)
class Position:
    x: float
    y: float
    z: float

    def __post_init__(self) -> None:
        for name, value in (("x", self.x), ("y", self.y), ("z", self.z)):
            if isinstance(value, bool) or not isinstance(value, int | float) or not isfinite(value):
                raise ValueError(f"position {name} must be a finite number")

    def distance_to(self, other: Position) -> float:
        return dist((self.x, self.y, self.z), (other.x, other.y, other.z))

    def to_dict(self) -> dict[str, float]:
        return {"x": self.x, "y": self.y, "z": self.z}

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> Position:
        return cls(
            x=_number(raw, "x"),
            y=_number(raw, "y"),
            z=_number(raw, "z"),
        )


@dataclass(frozen=True, slots=True)
class Geofence:
    min_x: float
    max_x: float
    min_y: float
    max_y: float
    min_z: float
    max_z: float

    def __post_init__(self) -> None:
        bounds = (
            ("x", self.min_x, self.max_x),
            ("y", self.min_y, self.max_y),
            ("z", self.min_z, self.max_z),
        )
        for axis, lower, upper in bounds:
            if (
                isinstance(lower, bool)
                or isinstance(upper, bool)
                or not isinstance(lower, int | float)
                or not isinstance(upper, int | float)
                or not isfinite(lower)
                or not isfinite(upper)
                or lower >= upper
            ):
                raise ValueError(f"geofence {axis} bounds must be finite and ordered")

    def contains(self, position: Position) -> bool:
        return (
            self.min_x <= position.x <= self.max_x
            and self.min_y <= position.y <= self.max_y
            and self.min_z <= position.z <= self.max_z
        )


@dataclass(frozen=True, slots=True)
class AircraftState:
    drone_id: int
    connection_epoch: int
    membership: MembershipState
    pose: Position
    home: Position | None
    flight_state: FlightState
    armed: bool
    battery: float
    link_quality: float
    link_last_seen_ms: int
    position_quality: float
    position_last_seen_ms: int
    control_authority: bool
    rc_safety_operator_present: bool
    physical_rc_available: bool
    storage_remaining_bytes: int
    camera_ready: bool
    heading_deg: float | None = None
    active_task_id: str | None = None
    position_loss_since_ms: int | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.drone_id, int)
            or isinstance(self.drone_id, bool)
            or self.drone_id <= 0
        ):
            raise ValueError("drone_id must be a positive integer")
        if (
            not isinstance(self.connection_epoch, int)
            or isinstance(self.connection_epoch, bool)
            or self.connection_epoch < 0
        ):
            raise ValueError("connection_epoch must be a non-negative integer")
        if not isinstance(self.membership, MembershipState):
            raise ValueError("membership must be a MembershipState")
        if not isinstance(self.pose, Position):
            raise ValueError("pose must be a Position")
        if self.home is not None and not isinstance(self.home, Position):
            raise ValueError("home must be a Position or null")
        if not isinstance(self.flight_state, FlightState):
            raise ValueError("flight_state must be a FlightState")
        if self.heading_deg is not None and (
            not _is_finite_number(self.heading_deg) or not 0 <= self.heading_deg < 360
        ):
            raise ValueError("heading_deg must be null or finite degrees in [0, 360)")
        for name, value in (
            ("armed", self.armed),
            ("control_authority", self.control_authority),
            ("rc_safety_operator_present", self.rc_safety_operator_present),
            ("physical_rc_available", self.physical_rc_available),
            ("camera_ready", self.camera_ready),
        ):
            if not isinstance(value, bool):
                raise ValueError(f"{name} must be a boolean")
        for name, value in (
            ("battery", self.battery),
            ("link_quality", self.link_quality),
            ("position_quality", self.position_quality),
        ):
            if not _is_finite_number(value) or not 0 <= value <= 1:
                raise ValueError(f"{name} must be a finite fraction")
        for name, value in (
            ("link_last_seen_ms", self.link_last_seen_ms),
            ("position_last_seen_ms", self.position_last_seen_ms),
            ("storage_remaining_bytes", self.storage_remaining_bytes),
        ):
            if not _is_nonnegative_int(value):
                raise ValueError(f"{name} must be a non-negative integer")
        if self.active_task_id is not None and (
            not isinstance(self.active_task_id, str) or not self.active_task_id
        ):
            raise ValueError("active_task_id must be null or a non-empty string")
        if self.position_loss_since_ms is not None and not _is_nonnegative_int(
            self.position_loss_since_ms
        ):
            raise ValueError("position_loss_since_ms must be null or non-negative")

    @property
    def airborne(self) -> bool:
        return self.flight_state in {
            FlightState.TAKING_OFF,
            FlightState.AIRBORNE,
            FlightState.HOVERING,
            FlightState.LANDING,
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> AircraftState:
        """Adapt a fully enriched autonomy mapping (not the relay wire projection)."""
        pose_raw = raw.get("pose")
        if isinstance(pose_raw, Mapping):
            pose = Position.from_mapping(pose_raw)
        else:
            pose = Position.from_mapping(raw)

        home_raw = raw.get("home")
        home = Position.from_mapping(home_raw) if isinstance(home_raw, Mapping) else None
        drone_id = _positive_int(raw, "drone_id", fallback="id")

        return cls(
            drone_id=drone_id,
            connection_epoch=_nonnegative_int(raw, "connection_epoch"),
            membership=MembershipState(_string(raw, "membership")),
            pose=pose,
            home=home,
            flight_state=FlightState(_string(raw, "flight_state", fallback="state")),
            armed=_boolean(raw, "armed"),
            battery=_fraction(raw, "battery"),
            link_quality=_fraction(raw, "link_quality", fallback="link"),
            link_last_seen_ms=_nonnegative_int(raw, "link_last_seen_ms"),
            position_quality=_fraction(raw, "position_quality", fallback="pos_quality"),
            position_last_seen_ms=_nonnegative_int(raw, "position_last_seen_ms"),
            control_authority=_boolean(raw, "control_authority"),
            rc_safety_operator_present=_boolean(raw, "rc_safety_operator_present"),
            physical_rc_available=_boolean(raw, "physical_rc_available"),
            storage_remaining_bytes=_nonnegative_int(raw, "storage_remaining_bytes"),
            camera_ready=_boolean(raw, "camera_ready"),
            heading_deg=_optional_heading(raw.get("heading_deg")),
            active_task_id=_optional_string(raw.get("active_task_id")),
            position_loss_since_ms=_optional_nonnegative_int(raw.get("position_loss_since_ms")),
        )

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "drone_id": self.drone_id,
            "connection_epoch": self.connection_epoch,
            "membership": self.membership.value,
            "pose": self.pose.to_dict(),
            "home": self.home.to_dict() if self.home is not None else None,
            "flight_state": self.flight_state.value,
            "armed": self.armed,
            "battery": self.battery,
            "link_quality": self.link_quality,
            "link_last_seen_ms": self.link_last_seen_ms,
            "position_quality": self.position_quality,
            "position_last_seen_ms": self.position_last_seen_ms,
            "control_authority": self.control_authority,
            "rc_safety_operator_present": self.rc_safety_operator_present,
            "physical_rc_available": self.physical_rc_available,
            "storage_remaining_bytes": self.storage_remaining_bytes,
            "camera_ready": self.camera_ready,
            "heading_deg": self.heading_deg,
            "active_task_id": self.active_task_id,
            "position_loss_since_ms": self.position_loss_since_ms,
        }


@dataclass(frozen=True, slots=True)
class FleetSnapshot:
    roster_version: int
    aircraft: Mapping[int, AircraftState]
    selection: tuple[int, ...]
    armed: bool
    estop_active: bool
    operator_present: bool
    operator_last_seen_ms: int
    now_ms: int
    formation: str = "none"
    spacing: float = 0.8

    def __post_init__(self) -> None:
        if not _is_nonnegative_int(self.roster_version):
            raise ValueError("roster_version must be a non-negative integer")
        if not isinstance(self.aircraft, Mapping):
            raise ValueError("aircraft must be a mapping")
        if not isinstance(self.selection, tuple):
            raise ValueError("selection must be a tuple")
        if any(
            not isinstance(drone_id, int) or isinstance(drone_id, bool) or drone_id <= 0
            for drone_id in self.selection
        ):
            raise ValueError("selection ids must be positive integers")
        if not isinstance(self.armed, bool) or not isinstance(self.estop_active, bool):
            raise ValueError("armed and estop_active must be booleans")
        if not isinstance(self.operator_present, bool):
            raise ValueError("operator_present must be a boolean")
        if not _is_nonnegative_int(self.operator_last_seen_ms) or not _is_nonnegative_int(
            self.now_ms
        ):
            raise ValueError("snapshot timestamps must be non-negative integers")
        if not isinstance(self.formation, str) or not self.formation:
            raise ValueError("formation must be a non-empty string")
        if not _is_finite_number(self.spacing) or self.spacing <= 0:
            raise ValueError("spacing must be a finite positive number")
        normalized = dict(sorted(self.aircraft.items()))
        if any(not isinstance(state, AircraftState) for state in normalized.values()):
            raise ValueError("aircraft values must be AircraftState instances")
        if any(drone_id != state.drone_id for drone_id, state in normalized.items()):
            raise ValueError("aircraft keys must match AircraftState.drone_id")
        if len(set(self.selection)) != len(self.selection):
            raise ValueError("selection contains duplicate aircraft ids")
        object.__setattr__(self, "aircraft", MappingProxyType(normalized))

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> FleetSnapshot:
        """Adapt a fully enriched autonomy mapping.

        Use :meth:`from_relay_state` for the narrower #14 wire projection.  This
        strict path intentionally requires every safety input and invents no safe
        default for missing telemetry or authority.
        """
        aircraft_raw = raw.get("aircraft", raw.get("drones"))
        states: list[AircraftState]
        if isinstance(aircraft_raw, Mapping):
            states = []
            for key, item in aircraft_raw.items():
                if not isinstance(item, Mapping):
                    raise ValueError("aircraft entries must be mappings")
                with_id = dict(item)
                with_id.setdefault("drone_id", _parse_mapping_key(key))
                states.append(AircraftState.from_mapping(with_id))
        elif isinstance(aircraft_raw, Iterable) and not isinstance(aircraft_raw, str | bytes):
            states = []
            for item in aircraft_raw:
                if not isinstance(item, Mapping):
                    raise ValueError("aircraft entries must be mappings")
                states.append(AircraftState.from_mapping(item))
        else:
            raise ValueError("state requires an aircraft or drones collection")

        selection_raw = raw.get("selection")
        if not isinstance(selection_raw, Iterable) or isinstance(selection_raw, str | bytes):
            raise ValueError("selection must be an iterable of aircraft ids")
        selection = tuple(_parse_drone_id(value) for value in selection_raw)

        return cls(
            roster_version=_nonnegative_int(raw, "roster_version"),
            aircraft={state.drone_id: state for state in states},
            selection=selection,
            armed=_boolean(raw, "armed"),
            estop_active=_boolean(raw, "estop_active", fallback="estop"),
            operator_present=_boolean(raw, "operator_present"),
            operator_last_seen_ms=_nonnegative_int(raw, "operator_last_seen_ms"),
            now_ms=_nonnegative_int(raw, "now_ms"),
            formation=_string(raw, "formation", fallback="none"),
            spacing=_number_or_default(raw, "spacing", 0.8),
        )

    @classmethod
    def from_relay_state(
        cls,
        raw: Mapping[str, object],
        *,
        enrichment: RelaySnapshotEnrichment,
    ) -> FleetSnapshot:
        """Combine #14's frozen state projection with explicit safety enrichment.

        The relay projection deliberately lacks camera readiness, storage, active
        task, physical-RC availability, per-aircraft armed proof, and operator
        activity.  Those fields are required here instead of receiving optimistic
        defaults.  Nullable relay telemetry also requires explicit last-known
        fallback values in the per-aircraft enrichment.
        """
        drones_raw = raw.get("drones")
        if not isinstance(drones_raw, Iterable) or isinstance(drones_raw, str | bytes):
            raise ValueError("relay state requires a drones collection")
        states: list[AircraftState] = []
        for item in drones_raw:
            if not isinstance(item, Mapping):
                raise ValueError("relay drone entries must be mappings")
            drone_id = _positive_int(item, "drone_id")
            safety = enrichment.aircraft.get(drone_id)
            if safety is None:
                raise ValueError(f"missing safety enrichment for aircraft {drone_id}")
            telemetry = item.get("telemetry")
            if telemetry is not None and not isinstance(telemetry, Mapping):
                raise ValueError(f"malformed telemetry for aircraft {drone_id}")
            telemetry_mapping = telemetry if isinstance(telemetry, Mapping) else None

            pose = _relay_pose(telemetry_mapping, safety)
            home_raw = item.get("home_pose")
            home = (
                Position.from_mapping(home_raw)
                if isinstance(home_raw, Mapping)
                else safety.last_known_home
            )
            if telemetry_mapping is not None:
                flight_state_raw = telemetry_mapping.get("state")
                flat_flight_state = item.get("flight_state")
                if (
                    flat_flight_state is not None
                    and flight_state_raw is not None
                    and flat_flight_state != flight_state_raw
                ):
                    raise ValueError(f"divergent flight state for aircraft {drone_id}")
            else:
                flight_state_raw = safety.last_known_flight_state
            if not isinstance(flight_state_raw, str):
                raise ValueError(f"missing flight state for aircraft {drone_id}")

            battery = _relay_fraction(
                telemetry_mapping,
                telemetry_key="battery",
                fallback=safety.last_known_battery,
                field="battery",
            )
            link_quality = _relay_fraction(
                telemetry_mapping,
                telemetry_key="link",
                fallback=safety.last_known_link_quality,
                field="link",
            )
            position_quality = _relay_fraction(
                telemetry_mapping,
                telemetry_key="pos_quality",
                fallback=safety.last_known_position_quality,
                field="pos_quality",
            )
            if telemetry_mapping is not None:
                telemetry_t = _nonnegative_int(telemetry_mapping, "t")
                link_last_seen = telemetry_t
                position_last_seen = telemetry_t
            else:
                link_last_seen = safety.last_link_seen_ms
                position_last_seen = safety.last_position_seen_ms
            if link_last_seen is None or position_last_seen is None:
                raise ValueError(f"missing telemetry freshness for aircraft {drone_id}")

            states.append(
                AircraftState(
                    drone_id=drone_id,
                    connection_epoch=_nonnegative_int(item, "connection_epoch"),
                    membership=MembershipState(_string(item, "membership")),
                    pose=pose,
                    home=home,
                    flight_state=FlightState(flight_state_raw),
                    armed=safety.armed,
                    battery=battery,
                    link_quality=link_quality,
                    link_last_seen_ms=link_last_seen,
                    position_quality=position_quality,
                    position_last_seen_ms=position_last_seen,
                    control_authority=_boolean(item, "control_authority"),
                    rc_safety_operator_present=_boolean(item, "rc_safety_operator_present"),
                    physical_rc_available=safety.physical_rc_available,
                    storage_remaining_bytes=safety.storage_remaining_bytes,
                    camera_ready=safety.camera_ready,
                    heading_deg=_optional_heading(
                        item.get("heading_deg")
                        if item.get("heading_deg") is not None
                        else (
                            None
                            if telemetry_mapping is None
                            else telemetry_mapping.get("heading_deg")
                        )
                    ),
                    active_task_id=safety.active_task_id,
                    position_loss_since_ms=safety.position_loss_since_ms,
                )
            )

        selection_raw = raw.get("selection")
        if not isinstance(selection_raw, Iterable) or isinstance(selection_raw, str | bytes):
            raise ValueError("relay selection must be an iterable of aircraft ids")
        return cls(
            roster_version=_nonnegative_int(raw, "roster_version"),
            aircraft={state.drone_id: state for state in states},
            selection=tuple(_parse_drone_id(value) for value in selection_raw),
            armed=_boolean(raw, "armed"),
            estop_active=_boolean(raw, "estop"),
            operator_present=enrichment.operator_present,
            operator_last_seen_ms=enrichment.operator_last_seen_ms,
            now_ms=_nonnegative_int(raw, "t"),
            formation=_string(raw, "formation", fallback="none"),
            spacing=_number_or_default(raw, "spacing", 0.8),
        )

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "roster_version": self.roster_version,
            "aircraft": [self.aircraft[drone_id].to_dict() for drone_id in sorted(self.aircraft)],
            "selection": list(self.selection),
            "armed": self.armed,
            "estop_active": self.estop_active,
            "operator_present": self.operator_present,
            "operator_last_seen_ms": self.operator_last_seen_ms,
            "now_ms": self.now_ms,
            "formation": self.formation,
            "spacing": self.spacing,
        }


@dataclass(frozen=True, slots=True)
class Command:
    command_id: str
    intent_id: str
    roster_version: int
    drone_id: int
    connection_epoch: int
    operation: CommandOperation
    parameters: Mapping[str, JsonValue] = field(default_factory=dict)
    safety_action: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.parameters, Mapping) or any(
            not isinstance(key, str) for key in self.parameters
        ):
            raise ValueError("command parameters must be a string-keyed mapping")
        frozen = {key: _freeze_json(value) for key, value in sorted(self.parameters.items())}
        object.__setattr__(self, "parameters", MappingProxyType(frozen))

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "command_id": self.command_id,
            "intent_id": self.intent_id,
            "roster_version": self.roster_version,
            "drone_id": self.drone_id,
            "connection_epoch": self.connection_epoch,
            "operation": self.operation.value,
            "parameters": {key: _json_native(value) for key, value in self.parameters.items()},
            "safety_action": self.safety_action,
        }


@dataclass(frozen=True, slots=True)
class Plan:
    plan_id: str
    intent_id: str
    intent_name: IntentName
    roster_version: int
    selection: tuple[int, ...]
    confirmed: bool
    commands: tuple[Command, ...]
    selection_update: tuple[int, ...] | None = None
    armed_update: bool | None = None
    estop_update: bool | None = None
    formation_update: str | None = None
    spacing_update: float | None = None
    hold_scope: HoldScope | None = None
    status: LifecycleStatus = LifecycleStatus.ACCEPTED
    altitude_grounding: AltitudeGrounding | None = None

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            **(
                {"altitude_grounding": self.altitude_grounding.to_dict()}
                if self.altitude_grounding is not None
                else {}
            ),
            "plan_id": self.plan_id,
            "intent_id": self.intent_id,
            "intent_name": self.intent_name.value,
            "roster_version": self.roster_version,
            "selection": list(self.selection),
            "confirmed": self.confirmed,
            "commands": [command.to_dict() for command in self.commands],
            "selection_update": (
                list(self.selection_update) if self.selection_update is not None else None
            ),
            "armed_update": self.armed_update,
            "estop_update": self.estop_update,
            "formation_update": self.formation_update,
            "spacing_update": self.spacing_update,
            "hold_scope": self.hold_scope.value if self.hold_scope is not None else None,
            "status": self.status.value,
        }


@dataclass(frozen=True, slots=True)
class PreparedExecution:
    intent: IntentV1
    plan: Plan
    snapshot: FleetSnapshot


@dataclass(frozen=True, slots=True)
class Refusal:
    intent_id: str
    roster_version: int
    drone_id: int | None
    connection_epoch: int | None
    reason: RefusalReason
    detail: str
    status: LifecycleStatus = LifecycleStatus.REFUSED

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "intent_id": self.intent_id,
            "roster_version": self.roster_version,
            "drone_id": self.drone_id,
            "connection_epoch": self.connection_epoch,
            "status": self.status.value,
            "reason": self.reason.value,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class CommandAcknowledgement:
    command_id: str
    intent_id: str
    roster_version: int
    drone_id: int
    connection_epoch: int
    status: LifecycleStatus
    reason: RefusalReason | None = None
    detail: str = ""

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "command_id": self.command_id,
            "intent_id": self.intent_id,
            "roster_version": self.roster_version,
            "drone_id": self.drone_id,
            "connection_epoch": self.connection_epoch,
            "status": self.status.value,
            "reason": self.reason.value if self.reason is not None else None,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    intent_id: str
    roster_version: int
    status: LifecycleStatus
    plan: Plan | None = None
    acknowledgements: tuple[CommandAcknowledgement, ...] = ()
    refusal: Refusal | None = None
    capture_bundle: object | None = None
    degraded_aircraft: tuple[int, ...] = ()

    def to_dict(self) -> dict[str, JsonValue]:
        bundle = self.capture_bundle
        if bundle is not None and hasattr(bundle, "to_dict"):
            bundle = bundle.to_dict()
        return {
            "intent_id": self.intent_id,
            "roster_version": self.roster_version,
            "status": self.status.value,
            "plan": self.plan.to_dict() if self.plan is not None else None,
            "acknowledgements": [ack.to_dict() for ack in self.acknowledgements],
            "refusal": self.refusal.to_dict() if self.refusal is not None else None,
            "capture_bundle": _json_native(bundle),
            "degraded_aircraft": list(self.degraded_aircraft),
        }


type PlanResult = Plan | Refusal


@dataclass(frozen=True, slots=True)
class RelayAircraftSafetyEnrichment:
    """Safety facts intentionally absent from #14's transport projection."""

    drone_id: int
    armed: bool
    physical_rc_available: bool
    storage_remaining_bytes: int
    camera_ready: bool
    active_task_id: str | None
    position_loss_since_ms: int | None
    last_known_pose: Position | None = None
    last_known_home: Position | None = None
    last_known_flight_state: str | None = None
    last_known_battery: float | None = None
    last_known_link_quality: float | None = None
    last_known_position_quality: float | None = None
    last_link_seen_ms: int | None = None
    last_position_seen_ms: int | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.drone_id, int)
            or isinstance(self.drone_id, bool)
            or self.drone_id <= 0
        ):
            raise ValueError("enrichment drone_id must be positive")
        for name, value in (
            ("armed", self.armed),
            ("physical_rc_available", self.physical_rc_available),
            ("camera_ready", self.camera_ready),
        ):
            if not isinstance(value, bool):
                raise ValueError(f"{name} must be a boolean")
        if not _is_nonnegative_int(self.storage_remaining_bytes):
            raise ValueError("storage_remaining_bytes cannot be negative")
        if self.active_task_id is not None and (
            not isinstance(self.active_task_id, str) or not self.active_task_id
        ):
            raise ValueError("active_task_id must be null or non-empty")
        for value in (
            self.position_loss_since_ms,
            self.last_link_seen_ms,
            self.last_position_seen_ms,
        ):
            if value is not None and not _is_nonnegative_int(value):
                raise ValueError("enrichment timestamps must be null or non-negative")
        for value in (
            self.last_known_battery,
            self.last_known_link_quality,
            self.last_known_position_quality,
        ):
            if value is not None and (not _is_finite_number(value) or not 0 <= value <= 1):
                raise ValueError("last-known quality values must be finite fractions")


@dataclass(frozen=True, slots=True)
class RelaySnapshotEnrichment:
    operator_present: bool
    operator_last_seen_ms: int
    aircraft: Mapping[int, RelayAircraftSafetyEnrichment]

    def __post_init__(self) -> None:
        if not isinstance(self.operator_present, bool):
            raise ValueError("operator_present must be a boolean")
        if not _is_nonnegative_int(self.operator_last_seen_ms):
            raise ValueError("operator_last_seen_ms cannot be negative")
        if not isinstance(self.aircraft, Mapping):
            raise ValueError("aircraft enrichment must be a mapping")
        normalized = dict(sorted(self.aircraft.items()))
        if any(drone_id != value.drone_id for drone_id, value in normalized.items()):
            raise ValueError("enrichment keys must match drone_id")
        object.__setattr__(self, "aircraft", MappingProxyType(normalized))


def _json_native(value: object) -> JsonValue:
    if isinstance(value, StrEnum):
        return value.value
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        if not isfinite(value):
            raise ValueError("JSON numbers must be finite")
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_native(item) for key, item in sorted(value.items())}
    if isinstance(value, list | tuple):
        return [_json_native(item) for item in value]
    if isinstance(value, Iterable) and not isinstance(value, str | bytes):
        raise TypeError("JSON arrays must use an ordered list or tuple")
    if hasattr(value, "to_dict"):
        return _json_native(value.to_dict())
    raise TypeError(f"value is not JSON serializable: {type(value).__name__}")


def _freeze_json(value: object) -> object:
    """Validate and recursively freeze a JSON-shaped value."""
    if isinstance(value, StrEnum):
        return value.value
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        if not isfinite(value):
            raise ValueError("JSON numbers must be finite")
        return value
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("JSON object keys must be strings")
        return MappingProxyType({key: _freeze_json(item) for key, item in sorted(value.items())})
    if isinstance(value, list | tuple):
        return tuple(_freeze_json(item) for item in value)
    if isinstance(value, Iterable) and not isinstance(value, str | bytes):
        raise TypeError("JSON arrays must use an ordered list or tuple")
    if hasattr(value, "to_dict"):
        return _freeze_json(value.to_dict())
    raise TypeError(f"value is not JSON serializable: {type(value).__name__}")


def _lookup(raw: Mapping[str, object], key: str, fallback: str | None = None) -> object:
    if key in raw:
        return raw[key]
    if fallback is not None and fallback in raw:
        return raw[fallback]
    raise ValueError(f"missing required field: {key}")


def _number(raw: Mapping[str, object], key: str, fallback: str | None = None) -> float:
    value = _lookup(raw, key, fallback)
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise ValueError(f"{key} must be numeric")
    result = float(value)
    if result != result or abs(result) == float("inf"):
        raise ValueError(f"{key} must be finite")
    return result


def _number_or_default(raw: Mapping[str, object], key: str, default: float) -> float:
    value = raw.get(key, default)
    if not _is_finite_number(value):
        raise ValueError(f"{key} must be finite")
    return float(value)


def _is_finite_number(value: object) -> bool:
    if not isinstance(value, int | float) or isinstance(value, bool):
        return False
    try:
        return isfinite(value)
    except OverflowError:
        return False


def _is_nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _fraction(raw: Mapping[str, object], key: str, fallback: str | None = None) -> float:
    value = _number(raw, key, fallback)
    if not 0.0 <= value <= 1.0:
        raise ValueError(f"{key} must be between zero and one")
    return value


def _nonnegative_int(raw: Mapping[str, object], key: str, fallback: str | None = None) -> int:
    value = _lookup(raw, key, fallback)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{key} must be a non-negative integer")
    return value


def _positive_int(raw: Mapping[str, object], key: str, fallback: str | None = None) -> int:
    value = _nonnegative_int(raw, key, fallback)
    if value == 0:
        raise ValueError(f"{key} must be positive")
    return value


def _parse_drone_id(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError("aircraft ids must be positive integers")
    return value


def _parse_mapping_key(value: object) -> int:
    if isinstance(value, str) and value.isdigit():
        value = int(value)
    return _parse_drone_id(value)


def _string(raw: Mapping[str, object], key: str, fallback: str | None = None) -> str:
    value = _lookup(raw, key, fallback)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _boolean(raw: Mapping[str, object], key: str, fallback: str | None = None) -> bool:
    value = _lookup(raw, key, fallback)
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be a boolean")
    return value


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError("optional string must be null or non-empty")
    return value


def _optional_nonnegative_int(value: object) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("optional timestamp must be null or non-negative")
    return value


def _optional_heading(value: object) -> float | None:
    if value is None:
        return None
    if not _is_finite_number(value) or not 0 <= value < 360:
        raise ValueError("heading_deg must be null or finite degrees in [0, 360)")
    return float(value)


def _relay_pose(
    telemetry: Mapping[str, object] | None,
    enrichment: RelayAircraftSafetyEnrichment,
) -> Position:
    if telemetry is not None:
        return Position.from_mapping(telemetry)
    if enrichment.last_known_pose is None:
        raise ValueError(f"missing pose for aircraft {enrichment.drone_id}")
    return enrichment.last_known_pose


def _relay_fraction(
    telemetry: Mapping[str, object] | None,
    *,
    telemetry_key: str,
    fallback: float | None,
    field: str,
) -> float:
    if telemetry is not None:
        value = telemetry.get(telemetry_key)
    else:
        value = fallback
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise ValueError(f"missing {field} for relay aircraft")
    result = float(value)
    if not 0 <= result <= 1:
        raise ValueError(f"{field} must be between zero and one")
    return result
