"""Deterministic expansion of accepted Intent v1 values into autonomy plans."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from itertools import combinations, permutations
from math import cos, dist, fsum, isfinite, radians, sin

from planner.models import (
    AltitudeGrounding,
    Command,
    CommandOperation,
    FleetSnapshot,
    FlightState,
    HoldScope,
    JsonValue,
    MembershipState,
    Plan,
    PlanResult,
    Position,
    Refusal,
    RefusalReason,
    TranslationGrounding,
    TranslationPolicy,
)
from relay.capabilities import C1_CAPABILITY_PROFILE, CapabilityProfile
from relay.intent_v1 import FORMATION_NAMES, MAX_INTENT_DRONE_IDS, IntentName, IntentV1

FORMATION_SPACING_CLEARANCE_FACTOR = 1.01

SELECTION_TARGETED_INTENTS = frozenset(
    {
        IntentName.TAKEOFF,
        IntentName.TRANSLATE,
        IntentName.ALTITUDE,
        IntentName.HOLD,
        IntentName.COME_HOME,
        IntentName.LAND,
        IntentName.CAPTURE_ROOM,
        IntentName.FORMATION_NEXT,
        IntentName.FORMATION_SET,
        IntentName.SPACING,
        IntentName.SWEEP,
    }
)


@dataclass(frozen=True, slots=True)
class PlanningConfig:
    """Explicit mission values; deployment code must supply measured configuration."""

    takeoff_altitude_m: float
    translation_step_m: float
    flight_speed_m_s: float
    capture_yaw_speed_deg_s: float
    capture_yaw_tolerance_deg: float
    capture_pose_tolerance_m: float
    capture_min_overlap_deg: float
    capture_gimbal_pitch_deg: float
    reconstruct_headings_deg: tuple[float, ...]
    translation_frame: str = "world"
    altitude_step_m: float | None = None
    altitude_floor_z_m: float | None = None
    altitude_configuration_id: str | None = None
    altitude_completion_tolerance_m: float | None = None
    spacing_step_m: float = 0.2

    def __post_init__(self) -> None:
        positive = {
            "takeoff_altitude_m": self.takeoff_altitude_m,
            "translation_step_m": self.translation_step_m,
            "flight_speed_m_s": self.flight_speed_m_s,
            "capture_yaw_speed_deg_s": self.capture_yaw_speed_deg_s,
            "spacing_step_m": self.spacing_step_m,
        }
        for name, value in positive.items():
            if (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not _is_finite_number(value)
                or value <= 0
            ):
                raise ValueError(f"{name} must be a finite positive number")
        if (
            isinstance(self.capture_gimbal_pitch_deg, bool)
            or not isinstance(self.capture_gimbal_pitch_deg, int | float)
            or not _is_finite_number(self.capture_gimbal_pitch_deg)
        ):
            raise ValueError("capture_gimbal_pitch_deg must be finite")
        if (
            isinstance(self.capture_yaw_tolerance_deg, bool)
            or not isinstance(self.capture_yaw_tolerance_deg, int | float)
            or not _is_finite_number(self.capture_yaw_tolerance_deg)
            or not 0 <= self.capture_yaw_tolerance_deg < 180
        ):
            raise ValueError("capture_yaw_tolerance_deg must be finite and in [0, 180)")
        if (
            isinstance(self.capture_pose_tolerance_m, bool)
            or not isinstance(self.capture_pose_tolerance_m, int | float)
            or not _is_finite_number(self.capture_pose_tolerance_m)
            or self.capture_pose_tolerance_m < 0
        ):
            raise ValueError("capture_pose_tolerance_m must be finite and non-negative")
        if (
            isinstance(self.capture_min_overlap_deg, bool)
            or not isinstance(self.capture_min_overlap_deg, int | float)
            or not _is_finite_number(self.capture_min_overlap_deg)
            or not 0 < self.capture_min_overlap_deg < 180
        ):
            raise ValueError("capture_min_overlap_deg must be finite and in (0, 180)")
        if not isinstance(self.reconstruct_headings_deg, tuple):
            raise ValueError("reconstruct_headings_deg must be a tuple")
        if len(self.reconstruct_headings_deg) != 8:
            raise ValueError("reconstruct_8 requires exactly eight headings")
        if any(
            isinstance(heading, bool)
            or not isinstance(heading, int | float)
            or not _is_finite_number(heading)
            or not 0 <= heading < 360
            for heading in self.reconstruct_headings_deg
        ):
            raise ValueError("reconstruct_8 headings must be finite values in [0, 360)")
        if len(set(self.reconstruct_headings_deg)) != 8:
            raise ValueError("reconstruct_8 headings must be unique")
        if self.altitude_floor_z_m is not None and (
            isinstance(self.altitude_floor_z_m, bool)
            or not isinstance(self.altitude_floor_z_m, int | float)
            or not _is_finite_number(self.altitude_floor_z_m)
        ):
            raise ValueError("altitude floor reference must be finite")
        self.altitude_grounding()
        if self.translation_frame not in {"world", "aircraft_relative"}:
            raise ValueError("translation_frame must be world or aircraft_relative")

    def altitude_grounding(self) -> AltitudeGrounding | None:
        if self.altitude_step_m is None:
            return None
        if self.altitude_completion_tolerance_m is None:
            raise ValueError("enabled altitude requires a completion tolerance")
        return AltitudeGrounding(
            self.altitude_step_m,
            self.altitude_floor_z_m,
            self.altitude_configuration_id,
            self.altitude_completion_tolerance_m,
        )

    def effective_capability_profile(
        self, requested: CapabilityProfile = C1_CAPABILITY_PROFILE
    ) -> CapabilityProfile:
        """Return the immutable deployment profile implied by configured grounding."""
        if self.altitude_grounding() is not None or not requested.supports(IntentName.ALTITUDE):
            return requested
        suffix = ".no_altitude"
        return CapabilityProfile(
            f"{requested.name[: 64 - len(suffix)]}{suffix}",
            requested.enabled_intent_names - {IntentName.ALTITUDE},
        )

    def translation_grounding(self, snapshot: FleetSnapshot) -> TranslationGrounding:
        return TranslationGrounding(
            policy=TranslationPolicy(
                frame=self.translation_frame,
                step_m=self.translation_step_m,
            ),
            headings=(
                {
                    drone_id: aircraft.heading_deg
                    for drone_id, aircraft in snapshot.aircraft.items()
                    if aircraft.heading_deg is not None
                }
                if self.translation_frame == "aircraft_relative"
                else {}
            ),
        )


class DeterministicPlanner:
    def __init__(
        self,
        config: PlanningConfig,
        capability_profile: CapabilityProfile = C1_CAPABILITY_PROFILE,
    ) -> None:
        self.config = config
        self.capability_profile = config.effective_capability_profile(capability_profile)

    def supports(self, intent: IntentV1) -> bool:
        return self.capability_profile.supports(intent.name)

    def plan(self, intent: IntentV1, snapshot: FleetSnapshot) -> PlanResult:
        if not self.supports(intent):
            return _refusal(
                intent,
                snapshot,
                RefusalReason.UNSUPPORTED,
                f"{intent.name.value} is outside capability profile {self.capability_profile.name}",
            )

        if intent.name in SELECTION_TARGETED_INTENTS and tuple(sorted(intent.selection)) != tuple(
            sorted(snapshot.selection)
        ):
            return _refusal(
                intent,
                snapshot,
                RefusalReason.STALE_SELECTION,
                "intent selection does not match the authoritative selection",
            )

        selected = tuple(sorted(snapshot.selection))
        plan_id = f"plan:{intent.intent_id}"
        builder = _CommandBuilder(intent.intent_id, snapshot, plan_id)
        selection_update: tuple[int, ...] | None = None
        armed_update: bool | None = None
        estop_update: bool | None = None
        formation_update: str | None = None
        spacing_update: float | None = None
        hold_scope: HoldScope | None = None

        if intent.name is IntentName.SELECT:
            raw_ids = intent.args.get("ids")
            ids = tuple(raw_ids) if isinstance(raw_ids, tuple) else ()
            refusal = _validate_selection(intent, snapshot, ids)
            if refusal is not None:
                return refusal
            selection_update = tuple(sorted(ids))

        elif intent.name is IntentName.ARM:
            armed_update = True

        elif intent.name is IntentName.DISARM:
            armed_update = False

        elif intent.name is IntentName.TAKEOFF:
            for drone_id in selected:
                builder.add(
                    drone_id,
                    CommandOperation.TAKEOFF,
                    {"z": self.config.takeoff_altitude_m},
                )

        elif intent.name is IntentName.TRANSLATE:
            translation_frame = self.config.translation_frame
            try:
                dx = float(intent.args["dx"]) * self.config.translation_step_m
                dy = float(intent.args["dy"]) * self.config.translation_step_m
            except OverflowError:
                return _refusal(
                    intent,
                    snapshot,
                    RefusalReason.INVALID_PLAN,
                    "translation exceeds numeric limits",
                )
            if not isfinite(dx) or not isfinite(dy):
                return _refusal(
                    intent,
                    snapshot,
                    RefusalReason.INVALID_PLAN,
                    "translation exceeds numeric limits",
                )
            displacements: dict[int, tuple[float, float]] = {}
            for drone_id in selected:
                if translation_frame == "world":
                    displacements[drone_id] = (dx, dy)
                else:
                    heading = snapshot.aircraft[drone_id].heading_deg
                    if heading is None:
                        return _refusal(
                            intent,
                            snapshot,
                            RefusalReason.INVALID_STATE,
                            f"aircraft {drone_id} has no current heading",
                            drone_id,
                        )
                    angle = radians(heading)
                    displacements[drone_id] = (
                        dx * cos(angle) - dy * sin(angle),
                        dx * sin(angle) + dy * cos(angle),
                    )
                drone_dx, drone_dy = displacements[drone_id]
                pose = snapshot.aircraft[drone_id].pose
                if not all(
                    isfinite(value)
                    for value in (drone_dx, drone_dy, pose.x + drone_dx, pose.y + drone_dy)
                ):
                    return _refusal(
                        intent,
                        snapshot,
                        RefusalReason.INVALID_PLAN,
                        "translation target exceeds numeric limits",
                        drone_id,
                    )
            ordered = selected
            if dx != 0 or dy != 0:
                ordered = tuple(
                    sorted(
                        selected,
                        key=lambda drone_id: (
                            -(
                                snapshot.aircraft[drone_id].pose.x * displacements[drone_id][0]
                                + snapshot.aircraft[drone_id].pose.y * displacements[drone_id][1]
                            ),
                            drone_id,
                        ),
                    )
                )
            for drone_id in ordered:
                pose = snapshot.aircraft[drone_id].pose
                drone_dx, drone_dy = displacements[drone_id]
                builder.add(
                    drone_id,
                    CommandOperation.GOTO,
                    {
                        "x": pose.x + drone_dx,
                        "y": pose.y + drone_dy,
                        "z": pose.z,
                        "speed": self.config.flight_speed_m_s,
                    },
                )

        elif intent.name is IntentName.ALTITUDE:
            grounding = self.config.altitude_grounding()
            if grounding is None:
                return _refusal(intent, snapshot, RefusalReason.UNSUPPORTED, "altitude is disabled")
            if set(intent.args) != {"delta"}:
                return _refusal(
                    intent, snapshot, RefusalReason.INVALID_PLAN, "invalid altitude arguments"
                )
            raw_value = intent.args["delta"]
            try:
                value = float(raw_value)
            except (OverflowError, TypeError, ValueError):
                return _refusal(
                    intent, snapshot, RefusalReason.INVALID_PLAN, "altitude must be finite"
                )
            if isinstance(raw_value, bool) or not isfinite(value):
                return _refusal(
                    intent, snapshot, RefusalReason.INVALID_PLAN, "altitude must be finite"
                )
            targets = {}
            for drone_id in selected:
                aircraft = snapshot.aircraft[drone_id]
                if aircraft.flight_state not in {FlightState.AIRBORNE, FlightState.HOVERING}:
                    return _refusal(
                        intent,
                        snapshot,
                        RefusalReason.INVALID_STATE,
                        "altitude requires an airborne aircraft",
                        drone_id,
                    )
                target_z = aircraft.pose.z + value * grounding.step_m
                if not isfinite(target_z):
                    return _refusal(
                        intent, snapshot, RefusalReason.INVALID_PLAN, "altitude target overflow"
                    )
                if target_z <= (grounding.floor_z_m if grounding.floor_z_m is not None else 0.0):
                    return _refusal(
                        intent,
                        snapshot,
                        RefusalReason.INVALID_STATE,
                        "altitude cannot descend to or below the reference floor",
                        drone_id,
                    )
                targets[drone_id] = target_z

            # Move the leading aircraft first so stacked columns retain separation.
            def vertical_order(drone_id: int) -> tuple[int, float, int]:
                start = snapshot.aircraft[drone_id].pose.z
                target = targets[drone_id]
                return (0, -start, drone_id) if target > start else (1, start, drone_id)

            for drone_id in sorted(selected, key=vertical_order):
                pose = snapshot.aircraft[drone_id].pose
                builder.add(
                    drone_id,
                    CommandOperation.GOTO,
                    {
                        "x": pose.x,
                        "y": pose.y,
                        "z": targets[drone_id],
                        "speed": self.config.flight_speed_m_s,
                    },
                )
                builder.add(drone_id, CommandOperation.HOVER)

        elif intent.name in {IntentName.FORMATION_NEXT, IntentName.FORMATION_SET}:
            name = (
                _next_formation(snapshot.formation, len(selected))
                if intent.name is IntentName.FORMATION_NEXT
                else str(intent.args["name"])
            )
            targets = _formation_targets(name, selected, snapshot, snapshot.spacing)
            if targets is None:
                return _refusal(
                    intent,
                    snapshot,
                    RefusalReason.PLANNER_FAILURE,
                    f"unknown or unavailable formation: {name}",
                )
            formation_update = name
            for drone_id, target in targets:
                builder.add(
                    drone_id,
                    CommandOperation.GOTO,
                    {
                        "x": target.x,
                        "y": target.y,
                        "z": target.z,
                        "speed": self.config.flight_speed_m_s,
                    },
                )

        elif intent.name is IntentName.SPACING:
            try:
                spacing_update = (
                    snapshot.spacing + float(intent.args["delta"]) * self.config.spacing_step_m
                )
            except (KeyError, OverflowError, TypeError, ValueError):
                return _refusal(
                    intent,
                    snapshot,
                    RefusalReason.INVALID_PLAN,
                    "spacing change exceeds numeric limits",
                )
            if not isfinite(spacing_update) or spacing_update <= 0:
                return _refusal(
                    intent,
                    snapshot,
                    RefusalReason.INVALID_PLAN,
                    "spacing must remain finite and positive",
                )

        elif intent.name is IntentName.SWEEP:
            lanes = _sweep_lanes(selected, snapshot, intent.args.get("box"))
            if lanes is None:
                return _refusal(
                    intent,
                    snapshot,
                    RefusalReason.INVALID_PLAN,
                    "sweep box cannot be expanded into finite lanes",
                )
            for drone_id, start, end in lanes:
                builder.add(
                    drone_id,
                    CommandOperation.GOTO,
                    {
                        "x": start.x,
                        "y": start.y,
                        "z": start.z,
                        "speed": self.config.flight_speed_m_s,
                    },
                )
                builder.add(
                    drone_id,
                    CommandOperation.GOTO,
                    {
                        "x": end.x,
                        "y": end.y,
                        "z": end.z,
                        "speed": self.config.flight_speed_m_s,
                    },
                )

        elif intent.name is IntentName.HOLD:
            hold_scope = HoldScope.OPERATOR_SELECTION
            for drone_id in selected:
                builder.add(drone_id, CommandOperation.HOVER, safety_action=True)

        elif intent.name is IntentName.COME_HOME:
            for drone_id in selected:
                aircraft = snapshot.aircraft[drone_id]
                if aircraft.home is None:
                    return _refusal(
                        intent,
                        snapshot,
                        RefusalReason.HOME_POSE_MISSING,
                        f"aircraft {drone_id} has no home pose",
                        drone_id,
                    )
                builder.add(
                    drone_id,
                    CommandOperation.GOTO,
                    {
                        "x": aircraft.home.x,
                        "y": aircraft.home.y,
                        "z": max(aircraft.pose.z, self.config.takeoff_altitude_m),
                        "speed": self.config.flight_speed_m_s,
                    },
                )

        elif intent.name is IntentName.LAND:
            for drone_id in selected:
                builder.add(drone_id, CommandOperation.LAND)

        elif intent.name is IntentName.LAND_ALL:
            for drone_id, aircraft in sorted(snapshot.aircraft.items()):
                if (
                    aircraft.membership in {MembershipState.READY, MembershipState.DEGRADED}
                    and aircraft.airborne
                ):
                    builder.add(drone_id, CommandOperation.LAND, safety_action=True)

        elif intent.name is IntentName.ESTOP:
            estop_update = True
            for drone_id, aircraft in sorted(snapshot.aircraft.items()):
                if aircraft.membership in {MembershipState.READY, MembershipState.DEGRADED}:
                    builder.add(drone_id, CommandOperation.ESTOP, safety_action=True)

        elif intent.name is IntentName.CAPTURE_ROOM:
            if len(selected) != 1:
                return _refusal(
                    intent,
                    snapshot,
                    RefusalReason.INVALID_SELECTION,
                    "capture_room requires exactly one selected aircraft",
                )
            self._add_capture_commands(
                builder,
                selected[0],
                intent,
                approved_pose=snapshot.aircraft[selected[0]].pose,
            )

        return Plan(
            plan_id=plan_id,
            intent_id=intent.intent_id,
            intent_name=intent.name,
            roster_version=snapshot.roster_version,
            selection=selected,
            confirmed=intent.confirm,
            commands=builder.commands,
            selection_update=selection_update,
            armed_update=armed_update,
            estop_update=estop_update,
            formation_update=formation_update,
            spacing_update=spacing_update,
            hold_scope=hold_scope,
            altitude_grounding=(
                self.config.altitude_grounding() if intent.name is IntentName.ALTITUDE else None
            ),
        )

    def emergency_hold_plan(
        self,
        *,
        intent_id: str,
        snapshot: FleetSnapshot,
        drone_ids: tuple[int, ...] | None = None,
    ) -> Plan:
        """Build a safety-only hold used after planner or adapter failures."""
        targets = (
            drone_ids
            if drone_ids is not None
            else tuple(
                drone_id
                for drone_id, aircraft in sorted(snapshot.aircraft.items())
                if aircraft.membership in {MembershipState.READY, MembershipState.DEGRADED}
                and aircraft.airborne
            )
        )
        plan_id = f"plan:{intent_id}:safety-hold"
        builder = _CommandBuilder(intent_id, snapshot, plan_id)
        for drone_id in targets:
            if drone_id in snapshot.aircraft:
                builder.add(drone_id, CommandOperation.HOVER, safety_action=True)
        return Plan(
            plan_id=plan_id,
            intent_id=intent_id,
            intent_name=IntentName.HOLD,
            roster_version=snapshot.roster_version,
            selection=tuple(targets),
            confirmed=True,
            commands=builder.commands,
            hold_scope=(HoldScope.FLEET_SAFETY if drone_ids is None else HoldScope.TARGETED_SAFETY),
        )

    def fleet_position_loss_plan(
        self,
        *,
        intent_id: str,
        snapshot: FleetSnapshot,
        land: bool,
    ) -> Plan:
        """Hold every airborne aircraft, then land all after the configured dwell."""
        plan_id = f"plan:{intent_id}"
        builder = _CommandBuilder(intent_id, snapshot, plan_id)
        targets = tuple(
            drone_id
            for drone_id, aircraft in sorted(snapshot.aircraft.items())
            if aircraft.membership in {MembershipState.READY, MembershipState.DEGRADED}
            and aircraft.airborne
        )
        operation = CommandOperation.LAND if land else CommandOperation.HOVER
        for drone_id in targets:
            builder.add(drone_id, operation, safety_action=True)
        return Plan(
            plan_id=plan_id,
            intent_id=intent_id,
            intent_name=IntentName.LAND_ALL if land else IntentName.HOLD,
            roster_version=snapshot.roster_version,
            selection=targets,
            confirmed=True,
            commands=builder.commands,
            hold_scope=HoldScope.FLEET_SAFETY if not land else None,
        )

    def _add_capture_commands(
        self,
        builder: _CommandBuilder,
        drone_id: int,
        intent: IntentV1,
        *,
        approved_pose: Position,
    ) -> None:
        capture_id = str(intent.args["capture_id"])
        room_id = str(intent.args["room_id"])
        pattern = str(intent.args["pattern"])
        builder.add(
            drone_id,
            CommandOperation.CAMERA_CAPABILITIES,
            {"capture_id": capture_id, "pattern": pattern, "room_id": room_id},
        )
        builder.add(
            drone_id,
            CommandOperation.SET_GIMBAL_PITCH,
            {"pitch": self.config.capture_gimbal_pitch_deg},
        )

        if pattern == "pano_360":
            builder.add(drone_id, CommandOperation.CAMERA_READY)
            capture_command = builder.add(
                drone_id,
                CommandOperation.CAPTURE_PANORAMA,
                {
                    "approved_pose": approved_pose.to_dict(),
                    "capture_id": capture_id,
                    "pattern": pattern,
                    "pose_tolerance": self.config.capture_pose_tolerance_m,
                    "room_id": room_id,
                },
            )
            builder.add(
                drone_id,
                CommandOperation.RETRIEVE_MEDIA,
                {"source_command_id": capture_command.command_id},
            )
            return

        for frame_number, heading in enumerate(self.config.reconstruct_headings_deg, start=1):
            builder.add(
                drone_id,
                CommandOperation.ROTATE_TO,
                {
                    "yaw": heading,
                    "speed": self.config.capture_yaw_speed_deg_s,
                    "tolerance": self.config.capture_yaw_tolerance_deg,
                    "min_overlap": self.config.capture_min_overlap_deg,
                },
            )
            builder.add(drone_id, CommandOperation.CAMERA_READY)
            capture_command = builder.add(
                drone_id,
                CommandOperation.CAPTURE_PHOTO,
                {
                    "capture_id": capture_id,
                    "frame_number": frame_number,
                    "pattern": pattern,
                    "approved_pose": approved_pose.to_dict(),
                    "pose_tolerance": self.config.capture_pose_tolerance_m,
                    "room_id": room_id,
                },
            )
            builder.add(
                drone_id,
                CommandOperation.RETRIEVE_MEDIA,
                {"source_command_id": capture_command.command_id},
            )


class _CommandBuilder:
    def __init__(self, intent_id: str, snapshot: FleetSnapshot, plan_id: str) -> None:
        self._intent_id = intent_id
        self._snapshot = snapshot
        self._plan_id = plan_id
        self._commands: list[Command] = []

    @property
    def commands(self) -> tuple[Command, ...]:
        return tuple(self._commands)

    def add(
        self,
        drone_id: int,
        operation: CommandOperation,
        parameters: dict[str, JsonValue] | None = None,
        *,
        safety_action: bool = False,
    ) -> Command:
        aircraft = self._snapshot.aircraft[drone_id]
        command = Command(
            command_id=f"{self._plan_id}:command:{len(self._commands) + 1:04d}",
            intent_id=self._intent_id,
            roster_version=self._snapshot.roster_version,
            drone_id=drone_id,
            connection_epoch=aircraft.connection_epoch,
            operation=operation,
            parameters=parameters or {},
            safety_action=safety_action,
        )
        self._commands.append(command)
        return command


def _validate_selection(
    intent: IntentV1, snapshot: FleetSnapshot, ids: tuple[object, ...]
) -> Refusal | None:
    if not ids or not all(
        isinstance(drone_id, int) and not isinstance(drone_id, bool) for drone_id in ids
    ):
        return _refusal(
            intent,
            snapshot,
            RefusalReason.INVALID_SELECTION,
            "selection must contain registered aircraft ids",
        )
    for raw_drone_id in ids:
        drone_id = int(raw_drone_id)
        aircraft = snapshot.aircraft.get(drone_id)
        if aircraft is None:
            return _refusal(
                intent,
                snapshot,
                RefusalReason.AIRCRAFT_NOT_REGISTERED,
                f"aircraft {drone_id} is not registered",
                drone_id,
            )
        if aircraft.membership is not MembershipState.READY:
            return _refusal(
                intent,
                snapshot,
                RefusalReason.AIRCRAFT_NOT_READY,
                f"aircraft {drone_id} is not ready",
                drone_id,
            )
    return None


def _next_formation(current: str, count: int) -> str:
    available = tuple(
        name for name in FORMATION_NAMES if count >= (4 if name in {"wedge", "diamond"} else 2)
    )
    if not available:
        return FORMATION_NAMES[0]
    try:
        return available[(available.index(current) + 1) % len(available)]
    except ValueError:
        return available[0]


def _formation_targets(
    name: str, selected: tuple[int, ...], snapshot: FleetSnapshot, spacing: float
) -> tuple[tuple[int, Position], ...] | None:
    count = len(selected)
    if (
        name not in FORMATION_NAMES
        or not 2 <= count <= MAX_INTENT_DRONE_IDS
        or (name in {"wedge", "diamond"} and count < 4)
    ):
        return None
    center_x = sum(snapshot.aircraft[drone_id].pose.x / count for drone_id in selected)
    center_y = sum(snapshot.aircraft[drone_id].pose.y / count for drone_id in selected)
    z = sum(snapshot.aircraft[drone_id].pose.z / count for drone_id in selected)
    if not all(isfinite(value) for value in (center_x, center_y, z, spacing)):
        return None
    offsets = _formation_offsets(name, count)
    if offsets is None:
        return None
    raw_targets = tuple((center_x + x * spacing, center_y + y * spacing, z) for x, y in offsets)
    if any(not all(isfinite(value) for value in target) for target in raw_targets):
        return None
    targets = tuple(Position(*target) for target in raw_targets)
    return _minimum_cost_formation_assignment(
        selected,
        targets,
        snapshot,
        minimum_clearance=spacing,
    )


def _formation_offsets(name: str, count: int) -> tuple[tuple[float, float], ...] | None:
    if name == "line":
        raw = tuple((index - (count - 1) / 2, 0.0) for index in range(count))
    elif name == "column":
        raw = tuple((0.0, index - (count - 1) / 2) for index in range(count))
    elif name == "wedge" and 4 <= count <= MAX_INTENT_DRONE_IDS:
        raw = _wedge_offsets(count)
    elif name == "diamond" and 4 <= count <= MAX_INTENT_DRONE_IDS:
        raw = tuple(_diamond_perimeter(4 * index / count) for index in range(count))
    else:
        return None
    return _normalize_offsets(raw)


def _wedge_offsets(count: int) -> tuple[tuple[float, float], ...]:
    offsets: list[tuple[float, float]] = []
    if count % 2:
        offsets.append((0.0, 0.0))
        first_row = 1.0
    else:
        first_row = 0.5
    for row in range(count // 2):
        distance = first_row + row
        offsets.extend(((-distance, -distance), (distance, -distance)))
    return tuple(offsets)


def _diamond_perimeter(position: float) -> tuple[float, float]:
    if position < 1:
        return (position, 1 - position)
    if position < 2:
        return (2 - position, 1 - position)
    if position < 3:
        return (2 - position, position - 3)
    return (position - 4, position - 3)


def _normalize_offsets(
    raw: tuple[tuple[float, float], ...],
) -> tuple[tuple[float, float], ...] | None:
    count = len(raw)
    center_x = sum(x / count for x, _ in raw)
    center_y = sum(y / count for _, y in raw)
    centered = tuple((x - center_x, y - center_y) for x, y in raw)
    minimum = min(dist(first, second) for first, second in combinations(centered, 2))
    if not isfinite(minimum) or minimum <= 0:
        return None
    scale = FORMATION_SPACING_CLEARANCE_FACTOR / minimum
    return tuple((x * scale, y * scale) for x, y in centered)


def _minimum_cost_formation_assignment(
    selected: tuple[int, ...],
    targets: tuple[Position, ...],
    snapshot: FleetSnapshot,
    *,
    minimum_clearance: float,
) -> tuple[tuple[int, Position], ...] | None:
    """Solve the bounded assignment exactly, then find a safe sequential order."""
    drone_ids = tuple(sorted(selected))
    ranked: list[tuple[float, tuple[int, ...], tuple[tuple[int, Position], ...]]] = []
    for target_indices in permutations(range(len(targets))):
        candidate = tuple(
            (drone_id, targets[target_index])
            for drone_id, target_index in zip(drone_ids, target_indices, strict=True)
        )
        if _formation_transitions_cross(candidate, snapshot):
            continue
        cost = fsum(
            snapshot.aircraft[drone_id].pose.distance_to(target) for drone_id, target in candidate
        )
        if not isfinite(cost):
            continue
        ranked.append((cost, target_indices, candidate))
    for _, _, candidate in sorted(ranked, key=lambda item: (item[0], item[1])):
        ordered = _sequential_formation_order(
            candidate,
            snapshot,
            minimum_clearance=minimum_clearance,
        )
        if ordered is not None:
            return ordered
    return None


def _sequential_formation_order(
    assignments: tuple[tuple[int, Position], ...],
    snapshot: FleetSnapshot,
    *,
    minimum_clearance: float,
) -> tuple[tuple[int, Position], ...] | None:
    """Pick the lexicographically first order whose arrivals clear occupancy."""
    occupied = {
        drone_id: aircraft.pose
        for drone_id, aircraft in snapshot.aircraft.items()
        if aircraft.membership is MembershipState.READY and aircraft.airborne
    }
    by_drone = dict(assignments)
    for drone_order in permutations(sorted(by_drone)):
        projected = dict(occupied)
        safe = True
        for drone_id in drone_order:
            projected.pop(drone_id, None)
            target = by_drone[drone_id]
            if any(target.distance_to(other) < minimum_clearance for other in projected.values()):
                safe = False
                break
            projected[drone_id] = target
        if safe:
            return tuple((drone_id, by_drone[drone_id]) for drone_id in drone_order)
    return None


def _formation_transitions_cross(
    assignments: tuple[tuple[int, Position], ...], snapshot: FleetSnapshot
) -> bool:
    for first, second in combinations(assignments, 2):
        first_start = snapshot.aircraft[first[0]].pose
        second_start = snapshot.aircraft[second[0]].pose
        if _segments_cross_xy(first_start, first[1], second_start, second[1]):
            return True
    return False


def _segments_cross_xy(
    first_start: Position,
    first_end: Position,
    second_start: Position,
    second_end: Position,
) -> bool:
    """Return whether two straight XY transitions have a proper crossing."""
    points = (first_start, first_end, second_start, second_end)
    coordinate_scale = max(1.0, *(abs(value) for point in points for value in (point.x, point.y)))
    epsilon = coordinate_scale * coordinate_scale * 1e-12

    orientations = (
        _orientation(first_start, first_end, second_start),
        _orientation(first_start, first_end, second_end),
        _orientation(second_start, second_end, first_start),
        _orientation(second_start, second_end, first_end),
    )
    if not all(isfinite(value) for value in orientations):
        return True
    first_a, first_b, second_a, second_b = orientations
    if (first_a > epsilon and first_b < -epsilon or first_a < -epsilon and first_b > epsilon) and (
        second_a > epsilon and second_b < -epsilon or second_a < -epsilon and second_b > epsilon
    ):
        return True
    # Collinear following paths do not cross and are deliberately allowed: the
    # dispatcher executes one GOTO at a time, with nearer moves ordered first.
    # This is what lets an existing line contract without rejecting every
    # assignment merely because two aircraft travel along the same axis.
    return False


def _orientation(first: Position, second: Position, third: Position) -> float:
    return (second.x - first.x) * (third.y - first.y) - (second.y - first.y) * (third.x - first.x)


def _sweep_lanes(
    selected: tuple[int, ...],
    snapshot: FleetSnapshot,
    raw_box: object,
) -> tuple[tuple[int, Position, Position], ...] | None:
    if not selected:
        return None
    count = len(selected)
    z = sum(snapshot.aircraft[drone_id].pose.z / count for drone_id in selected)
    if raw_box is None:
        center_x = sum(snapshot.aircraft[drone_id].pose.x / count for drone_id in selected)
        center_y = sum(snapshot.aircraft[drone_id].pose.y / count for drone_id in selected)
        width = max(snapshot.spacing, 1.0) * count
        half_length = max(snapshot.spacing, 1.0) * 2
        min_x = center_x - width / 2
        max_x = center_x + width / 2
        min_y = center_y - half_length
        max_y = center_y + half_length
    else:
        expected = {"min_x", "max_x", "min_y", "max_y"}
        if not isinstance(raw_box, Mapping) or set(raw_box) != expected:
            return None
        values = tuple(raw_box[key] for key in ("min_x", "max_x", "min_y", "max_y"))
        if any(
            isinstance(value, bool) or not isinstance(value, int | float) or not isfinite(value)
            for value in values
        ):
            return None
        min_x, max_x, min_y, max_y = (float(value) for value in values)
        if min_x >= max_x or min_y >= max_y:
            return None
    if not all(isfinite(value) for value in (min_x, max_x, min_y, max_y, z)):
        return None
    lanes = tuple(
        (
            Position(
                min_x * (1 - (index + 0.5) / count) + max_x * ((index + 0.5) / count),
                min_y,
                z,
            ),
            Position(
                min_x * (1 - (index + 0.5) / count) + max_x * ((index + 0.5) / count),
                max_y,
                z,
            ),
        )
        for index in range(count)
    )
    remaining = set(selected)
    assignments: list[tuple[int, Position, Position]] = []
    for start, end in lanes:
        drone_id = min(
            remaining,
            key=lambda candidate: (
                snapshot.aircraft[candidate].pose.distance_to(start),
                candidate,
            ),
        )
        remaining.remove(drone_id)
        assignments.append((drone_id, start, end))
    return tuple(
        sorted(
            assignments,
            key=lambda item: (
                snapshot.aircraft[item[0]].pose.distance_to(item[1]),
                item[0],
            ),
        )
    )


def _refusal(
    intent: IntentV1,
    snapshot: FleetSnapshot,
    reason: RefusalReason,
    detail: str,
    drone_id: int | None = None,
) -> Refusal:
    aircraft = snapshot.aircraft.get(drone_id) if drone_id is not None else None
    return Refusal(
        intent_id=intent.intent_id,
        roster_version=snapshot.roster_version,
        drone_id=drone_id,
        connection_epoch=aircraft.connection_epoch if aircraft is not None else None,
        reason=reason,
        detail=detail,
    )


def _is_finite_number(value: object) -> bool:
    if not isinstance(value, int | float) or isinstance(value, bool):
        return False
    try:
        return isfinite(value)
    except OverflowError:
        return False
