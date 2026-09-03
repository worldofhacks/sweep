"""Pure, fail-closed safety checks for intents and planned commands."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from math import isfinite
from typing import Final

from planner.models import (
    AircraftState,
    Command,
    CommandOperation,
    FleetSnapshot,
    FlightState,
    Geofence,
    HoldScope,
    LifecycleStatus,
    MembershipState,
    Plan,
    Position,
    Refusal,
    RefusalReason,
)
from planner.planner import SELECTION_TARGETED_INTENTS
from relay.intent_v1 import IntentName, IntentV1

_CONFIRMED_INTENTS: Final = frozenset(
    {IntentName.TAKEOFF, IntentName.LAND_ALL, IntentName.CAPTURE_ROOM}
)
_SAFE_WHILE_STOPPED: Final = frozenset({IntentName.ESTOP, IntentName.HOLD, IntentName.LAND_ALL})
_SAFE_OPERATIONS: Final = frozenset(
    {CommandOperation.ESTOP, CommandOperation.HOVER, CommandOperation.LAND}
)
_STOPPED_OPERATION_BY_INTENT: Final = {
    IntentName.HOLD: CommandOperation.HOVER,
    IntentName.LAND_ALL: CommandOperation.LAND,
    IntentName.ESTOP: CommandOperation.ESTOP,
}
_ARMED_INTENTS: Final = frozenset(
    {IntentName.TRANSLATE, IntentName.COME_HOME, IntentName.CAPTURE_ROOM}
)
_CAMERA_OPERATIONS: Final = frozenset(
    {
        CommandOperation.CAMERA_CAPABILITIES,
        CommandOperation.SET_GIMBAL_PITCH,
        CommandOperation.CAMERA_READY,
        CommandOperation.CAPTURE_PANORAMA,
        CommandOperation.CAPTURE_PHOTO,
        CommandOperation.RETRIEVE_MEDIA,
    }
)
_CAPTURE_OPERATIONS: Final = frozenset(
    {CommandOperation.CAPTURE_PANORAMA, CommandOperation.CAPTURE_PHOTO}
)
_POSITION_REQUIRED: Final = frozenset(
    {
        CommandOperation.TAKEOFF,
        CommandOperation.GOTO,
        CommandOperation.ROTATE_TO,
        *_CAMERA_OPERATIONS,
    }
)
_PHYSICALLY_ARMED_OPERATIONS: Final = frozenset(
    {CommandOperation.GOTO, CommandOperation.ROTATE_TO, *_CAMERA_OPERATIONS}
)
_STABLE_MOTION_STATES: Final = frozenset({FlightState.AIRBORNE, FlightState.HOVERING})


@dataclass(frozen=True, slots=True)
class SafetyConfig:
    """Safety values must be supplied from measured, staged configuration."""

    geofence: Geofence
    ceiling_m: float
    min_spacing_m: float
    battery_reserve_fraction: float
    battery_critical_fraction: float
    battery_cost_per_m: float
    min_link_quality: float
    max_link_age_ms: int
    min_position_quality: float
    max_position_age_ms: int
    operator_timeout_ms: int
    max_future_clock_skew_ms: int
    min_capture_storage_bytes: int
    max_capture_pose_drift_m: float
    max_capture_gimbal_error_deg: float
    positioning_loss_hold_ms: int
    motion_conflict_window_ms: int

    def __post_init__(self) -> None:
        if not isinstance(self.geofence, Geofence):
            raise ValueError("geofence must be a validated Geofence")
        positive = {
            "ceiling_m": self.ceiling_m,
            "min_spacing_m": self.min_spacing_m,
        }
        for name, value in positive.items():
            if (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"{name} must be a finite positive number")
        if not _finite_fraction(self.battery_critical_fraction) or not _finite_fraction(
            self.battery_reserve_fraction
        ):
            raise ValueError("battery thresholds must be finite")
        if self.battery_critical_fraction >= self.battery_reserve_fraction:
            raise ValueError("battery thresholds must satisfy critical < reserve")
        if not _finite_nonnegative(self.battery_cost_per_m):
            raise ValueError("battery_cost_per_m cannot be negative")
        if not _finite_nonnegative(self.max_capture_pose_drift_m):
            raise ValueError("max_capture_pose_drift_m cannot be negative")
        if not _finite_range(self.max_capture_gimbal_error_deg, 0, 180):
            raise ValueError("max_capture_gimbal_error_deg must be finite and in [0, 180)")
        if not _finite_fraction(self.min_link_quality):
            raise ValueError("min_link_quality must be a fraction")
        if not _finite_fraction(self.min_position_quality):
            raise ValueError("min_position_quality must be a fraction")
        times = (
            self.max_link_age_ms,
            self.max_position_age_ms,
            self.operator_timeout_ms,
            self.max_future_clock_skew_ms,
            self.positioning_loss_hold_ms,
            self.motion_conflict_window_ms,
        )
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in times
        ):
            raise ValueError("time thresholds cannot be negative")
        if (
            not isinstance(self.min_capture_storage_bytes, int)
            or isinstance(self.min_capture_storage_bytes, bool)
            or self.min_capture_storage_bytes < 0
        ):
            raise ValueError("min_capture_storage_bytes cannot be negative")


class SafetyArbiter:
    def __init__(self, config: SafetyConfig) -> None:
        self.config = config

    def check_intent(self, intent: IntentV1, snapshot: FleetSnapshot) -> Refusal | None:
        """Check intent-level state before the planner can create adapter work."""
        if (
            intent.name in SELECTION_TARGETED_INTENTS
            and tuple(intent.selection) != snapshot.selection
        ):
            return self._intent_refusal(
                intent,
                snapshot,
                RefusalReason.STALE_SELECTION,
                "intent selection differs from authoritative state",
            )

        if intent.name in _CONFIRMED_INTENTS and not intent.confirm:
            return self._intent_refusal(
                intent,
                snapshot,
                RefusalReason.CONFIRMATION_REQUIRED,
                f"{intent.name.value} requires operator confirmation",
            )

        if snapshot.estop_active and intent.name not in _SAFE_WHILE_STOPPED:
            return self._intent_refusal(
                intent,
                snapshot,
                RefusalReason.ESTOP_ACTIVE,
                "network stop is active",
            )

        if intent.name not in {IntentName.ESTOP, IntentName.HOLD}:
            operator_refusal = self._check_operator(intent.intent_id, snapshot)
            if operator_refusal is not None:
                return operator_refusal

        target_ids = self._intent_targets(intent, snapshot)
        if isinstance(target_ids, Refusal):
            return target_ids
        if not target_ids and intent.name not in {
            IntentName.SELECT,
            IntentName.ARM,
            IntentName.ESTOP,
        }:
            return self._intent_refusal(
                intent,
                snapshot,
                RefusalReason.INVALID_SELECTION,
                "intent has no eligible aircraft targets",
            )

        for drone_id in target_ids:
            aircraft = snapshot.aircraft.get(drone_id)
            if aircraft is None:
                return self._intent_refusal(
                    intent,
                    snapshot,
                    RefusalReason.AIRCRAFT_NOT_REGISTERED,
                    f"aircraft {drone_id} is absent from the registry",
                    drone_id,
                )

            membership_refusal = self._check_membership(
                intent.intent_id,
                snapshot,
                aircraft,
                safe_action=intent.name in _SAFE_WHILE_STOPPED,
            )
            if membership_refusal is not None:
                return membership_refusal

            state_refusal = self._check_intent_state(intent, snapshot, aircraft)
            if state_refusal is not None:
                return state_refusal

            if intent.name in _ARMED_INTENTS:
                armed_refusal = self._check_armed(
                    intent.intent_id,
                    snapshot,
                    aircraft,
                )
                if armed_refusal is not None:
                    return armed_refusal

            if intent.name not in {IntentName.ESTOP, IntentName.HOLD, IntentName.LAND_ALL}:
                authority_refusal = self._check_authority(intent.intent_id, snapshot, aircraft)
                if authority_refusal is not None:
                    return authority_refusal

            if intent.name not in {IntentName.ESTOP, IntentName.HOLD, IntentName.LAND_ALL}:
                telemetry_refusal = self._check_telemetry(
                    intent.intent_id,
                    snapshot,
                    aircraft,
                    require_position=True,
                )
                if telemetry_refusal is not None:
                    return telemetry_refusal
                battery_refusal = self._check_battery(
                    intent.intent_id,
                    snapshot,
                    aircraft,
                    aircraft.pose,
                )
                if battery_refusal is not None:
                    return battery_refusal

            if intent.name is IntentName.CAPTURE_ROOM:
                capture_refusal = self._check_capture_preconditions(
                    intent.intent_id, snapshot, aircraft
                )
                if capture_refusal is not None:
                    return capture_refusal

        return None

    def check_plan(self, plan: Plan, snapshot: FleetSnapshot) -> Refusal | None:
        """Preflight every command before the dispatcher performs any adapter I/O."""
        return self._check_plan(plan, snapshot, required_hold_targets=None)

    def check_targeted_hold(
        self,
        plan: Plan,
        snapshot: FleetSnapshot,
        *,
        required_targets: tuple[int, ...],
    ) -> Refusal | None:
        """Validate a scoped internal hold against caller-derived exact targets."""
        if (
            plan.intent_name is not IntentName.HOLD
            or plan.hold_scope is not HoldScope.TARGETED_SAFETY
            or not required_targets
            or any(
                not isinstance(drone_id, int) or isinstance(drone_id, bool) or drone_id <= 0
                for drone_id in required_targets
            )
            or len(required_targets) != len(set(required_targets))
        ):
            return self._invalid_plan_refusal(
                plan,
                snapshot,
                "targeted safety hold requires explicit unique caller-derived targets",
            )
        return self._check_plan(
            plan,
            snapshot,
            required_hold_targets=tuple(sorted(required_targets)),
        )

    def _check_plan(
        self,
        plan: Plan,
        snapshot: FleetSnapshot,
        *,
        required_hold_targets: tuple[int, ...] | None,
    ) -> Refusal | None:
        try:
            authorization = self.check_plan_authorization(
                plan,
                snapshot,
                required_hold_targets=required_hold_targets,
            )
            if authorization is not None:
                return authorization

            # Simulate deterministic sequential occupancy. Preloading every final
            # target would hide transient collisions with aircraft that have not
            # moved yet and could permit partial I/O before a later refusal.
            projected: dict[int, Position] = {}
            for command in plan.commands:
                refusal = self.check_command(
                    plan,
                    command,
                    snapshot,
                    projected_positions=projected,
                )
                if refusal is not None:
                    return refusal
                aircraft = snapshot.aircraft.get(command.drone_id)
                if aircraft is not None:
                    target = self.command_position(command, aircraft)
                    if target is not None:
                        projected[command.drone_id] = target
        except (KeyError, OverflowError, TypeError, ValueError):
            return Refusal(
                intent_id=plan.intent_id,
                roster_version=snapshot.roster_version,
                drone_id=None,
                connection_epoch=None,
                reason=RefusalReason.INVALID_PLAN,
                detail="plan contains malformed command parameters",
            )
        return None

    def check_plan_authorization(
        self,
        plan: Plan,
        snapshot: FleetSnapshot,
        *,
        completed_command_ids: frozenset[str] = frozenset(),
        required_hold_targets: tuple[int, ...] | None = None,
    ) -> Refusal | None:
        """Validate plan-wide authorization without replaying completed command state."""
        boundary = self._check_plan_boundary(plan, snapshot)
        if boundary is not None:
            return boundary
        for command in plan.commands:
            boundary = self._check_command_boundary(plan, command, snapshot)
            if boundary is not None:
                return boundary
        if plan.roster_version != snapshot.roster_version:
            return Refusal(
                intent_id=plan.intent_id,
                roster_version=snapshot.roster_version,
                drone_id=None,
                connection_epoch=None,
                reason=RefusalReason.STALE_ROSTER,
                detail=(
                    f"plan roster {plan.roster_version} does not match current "
                    f"roster {snapshot.roster_version}"
                ),
            )
        structural = self.check_plan_structure(
            plan,
            snapshot,
            completed_command_ids=completed_command_ids,
            required_hold_targets=required_hold_targets,
        )
        if structural is not None:
            return structural
        if (
            plan.intent_name in SELECTION_TARGETED_INTENTS
            and (
                plan.intent_name is not IntentName.HOLD
                or plan.hold_scope is HoldScope.OPERATOR_SELECTION
            )
            and plan.selection != snapshot.selection
        ):
            return Refusal(
                intent_id=plan.intent_id,
                roster_version=snapshot.roster_version,
                drone_id=None,
                connection_epoch=None,
                reason=RefusalReason.STALE_SELECTION,
                detail="plan selection differs from authoritative state",
            )
        if plan.intent_name in _CONFIRMED_INTENTS and plan.confirmed is not True:
            return Refusal(
                intent_id=plan.intent_id,
                roster_version=snapshot.roster_version,
                drone_id=None,
                connection_epoch=None,
                reason=RefusalReason.CONFIRMATION_REQUIRED,
                detail=f"{plan.intent_name.value} plan is not confirmed",
            )
        return None

    def check_command(
        self,
        plan: Plan,
        command: Command,
        snapshot: FleetSnapshot,
        *,
        projected_positions: dict[int, Position] | None = None,
    ) -> Refusal | None:
        """Revalidate one command immediately before adapter I/O."""
        boundary = self._check_command_boundary(plan, command, snapshot)
        if boundary is not None:
            return boundary
        if (
            plan.roster_version != snapshot.roster_version
            or command.roster_version != snapshot.roster_version
        ):
            return self._command_refusal(
                command,
                snapshot,
                RefusalReason.STALE_ROSTER,
                "command or plan was built against a stale roster",
            )
        if (
            plan.intent_name in SELECTION_TARGETED_INTENTS
            and (
                plan.intent_name is not IntentName.HOLD
                or plan.hold_scope is HoldScope.OPERATOR_SELECTION
            )
            and plan.selection != snapshot.selection
        ):
            return self._command_refusal(
                command,
                snapshot,
                RefusalReason.STALE_SELECTION,
                "plan selection differs from authoritative state",
            )
        aircraft = snapshot.aircraft.get(command.drone_id)
        if aircraft is None:
            return self._command_refusal(
                command,
                snapshot,
                RefusalReason.AIRCRAFT_NOT_REGISTERED,
                "command target is absent from the registry",
            )
        if command.connection_epoch != aircraft.connection_epoch:
            return self._command_refusal(
                command,
                snapshot,
                RefusalReason.STALE_CONNECTION_EPOCH,
                "command connection epoch does not match the current aircraft epoch",
                epoch=aircraft.connection_epoch,
            )
        if plan.intent_name is IntentName.CAPTURE_ROOM:
            pose_refusal = self._check_capture_pose_lock(plan, command, snapshot, aircraft)
            if pose_refusal is not None:
                return pose_refusal
        if command.intent_id != plan.intent_id:
            return self._command_refusal(
                command,
                snapshot,
                RefusalReason.INVALID_PLAN,
                "command intent_id does not match its plan",
            )
        if command.safety_action and not self._is_legitimate_safety_action(plan, command):
            return self._command_refusal(
                command,
                snapshot,
                RefusalReason.INVALID_PLAN,
                "safety bypass does not match a hold, land_all, or estop plan",
            )

        membership_refusal = self._check_membership(
            command.intent_id,
            snapshot,
            aircraft,
            safe_action=command.safety_action,
        )
        if membership_refusal is not None:
            return membership_refusal

        if snapshot.estop_active and not self._is_allowed_while_stopped(plan, command):
            return self._command_refusal(
                command,
                snapshot,
                RefusalReason.ESTOP_ACTIVE,
                "network stop is active",
            )

        if not command.safety_action:
            operator_refusal = self._check_operator(command.intent_id, snapshot, command)
            if operator_refusal is not None:
                return operator_refusal
            authority_refusal = self._check_authority(
                command.intent_id, snapshot, aircraft, command
            )
            if authority_refusal is not None:
                return authority_refusal

        state_refusal = self._check_command_state(command, snapshot, aircraft)
        if state_refusal is not None:
            return state_refusal

        if not command.safety_action and command.operation in _PHYSICALLY_ARMED_OPERATIONS:
            armed_refusal = self._check_armed(
                command.intent_id,
                snapshot,
                aircraft,
                command,
            )
            if armed_refusal is not None:
                return armed_refusal

        require_position = command.operation in _POSITION_REQUIRED
        if not command.safety_action:
            telemetry_refusal = self._check_telemetry(
                command.intent_id,
                snapshot,
                aircraft,
                require_position=require_position,
                command=command,
            )
            if telemetry_refusal is not None:
                return telemetry_refusal

        target = self.command_position(command, aircraft)
        if target is not None and not command.safety_action:
            if not self.config.geofence.contains(target):
                return self._command_refusal(
                    command,
                    snapshot,
                    RefusalReason.GEOFENCE,
                    "planned target is outside the configured geofence",
                )
            if target.z > self.config.ceiling_m:
                return self._command_refusal(
                    command,
                    snapshot,
                    RefusalReason.CEILING,
                    "planned target exceeds the configured ceiling",
                )
            spacing_refusal = self._check_spacing(
                command,
                snapshot,
                target,
                projected_positions or {},
            )
            if spacing_refusal is not None:
                return spacing_refusal

        if command.operation not in _SAFE_OPERATIONS and not command.safety_action:
            battery_refusal = self._check_battery(
                command.intent_id,
                snapshot,
                aircraft,
                target or aircraft.pose,
                command,
            )
            if battery_refusal is not None:
                return battery_refusal

        if command.operation in _CAPTURE_OPERATIONS:
            capture_refusal = self._check_capture_preconditions(
                command.intent_id, snapshot, aircraft, command
            )
            if capture_refusal is not None:
                return capture_refusal
        return None

    def check_plan_structure(
        self,
        plan: Plan,
        snapshot: FleetSnapshot,
        *,
        completed_command_ids: frozenset[str] = frozenset(),
        required_hold_targets: tuple[int, ...] | None = None,
    ) -> Refusal | None:
        """Validate immutable plan identity, operation, safety, and target structure.

        Resume validation may supply already-completed command IDs. This only affects
        `land_all`: a target proven landed by its terminal acknowledgement is combined
        with the aircraft that remain airborne in the current snapshot.
        """
        boundary = self._check_plan_boundary(plan, snapshot)
        if boundary is not None:
            return boundary
        for command in plan.commands:
            boundary = self._check_command_boundary(plan, command, snapshot)
            if boundary is not None:
                return boundary
        update_refusal = self._check_plan_state_updates(plan, snapshot)
        if update_refusal is not None:
            return update_refusal
        if any(
            not isinstance(drone_id, int) or isinstance(drone_id, bool) or drone_id <= 0
            for drone_id in plan.selection
        ) or len(plan.selection) != len(set(plan.selection)):
            return Refusal(
                intent_id=plan.intent_id,
                roster_version=snapshot.roster_version,
                drone_id=None,
                connection_epoch=None,
                reason=RefusalReason.INVALID_PLAN,
                detail="plan selection contains duplicate aircraft ids",
            )
        command_ids = [command.command_id for command in plan.commands]
        if any(
            not isinstance(command_id, str) or not command_id for command_id in command_ids
        ) or len(command_ids) != len(set(command_ids)):
            return Refusal(
                intent_id=plan.intent_id,
                roster_version=snapshot.roster_version,
                drone_id=None,
                connection_epoch=None,
                reason=RefusalReason.INVALID_PLAN,
                detail="plan contains duplicate command ids",
            )
        expected_operations = {
            IntentName.ARM: frozenset(),
            IntentName.SELECT: frozenset(),
            IntentName.TAKEOFF: frozenset({CommandOperation.TAKEOFF}),
            IntentName.TRANSLATE: frozenset({CommandOperation.GOTO}),
            IntentName.HOLD: frozenset({CommandOperation.HOVER}),
            IntentName.COME_HOME: frozenset({CommandOperation.GOTO}),
            IntentName.LAND_ALL: frozenset({CommandOperation.LAND}),
            IntentName.ESTOP: frozenset({CommandOperation.ESTOP}),
            IntentName.CAPTURE_ROOM: _CAMERA_OPERATIONS | frozenset({CommandOperation.ROTATE_TO}),
        }
        allowed = expected_operations.get(plan.intent_name)
        if allowed is None:
            return Refusal(
                intent_id=plan.intent_id,
                roster_version=snapshot.roster_version,
                drone_id=None,
                connection_epoch=None,
                reason=RefusalReason.INVALID_PLAN,
                detail="plan intent has no supported command shape",
            )
        deterministic_shape = self._check_deterministic_plan_shape(plan, snapshot)
        if deterministic_shape is not None:
            return deterministic_shape
        if (
            plan.intent_name is IntentName.HOLD
            and plan.hold_scope is HoldScope.TARGETED_SAFETY
            and required_hold_targets is None
        ):
            return self._invalid_plan_refusal(
                plan,
                snapshot,
                "targeted safety hold requires a trusted caller-derived target set",
            )
        if (
            plan.intent_name is IntentName.HOLD
            and plan.hold_scope is HoldScope.OPERATOR_SELECTION
            and not snapshot.selection
            and any(
                aircraft.membership in {MembershipState.READY, MembershipState.DEGRADED}
                and aircraft.airborne
                for aircraft in snapshot.aircraft.values()
            )
        ):
            return self._invalid_plan_refusal(
                plan,
                snapshot,
                "operator hold cannot omit eligible airborne aircraft with no selection",
            )
        safety_targets = self._expected_safety_targets(
            plan,
            snapshot,
            completed_command_ids=completed_command_ids,
            required_hold_targets=required_hold_targets,
        )
        if safety_targets is not None:
            actual_targets = tuple(sorted(command.drone_id for command in plan.commands))
            if actual_targets != safety_targets or any(
                not command.safety_action for command in plan.commands
            ):
                return Refusal(
                    intent_id=plan.intent_id,
                    roster_version=snapshot.roster_version,
                    drone_id=None,
                    connection_epoch=None,
                    reason=RefusalReason.INVALID_PLAN,
                    detail=(
                        f"{plan.intent_name.value} commands must cover its exact required "
                        "aircraft set once"
                    ),
                )
        selection = set(plan.selection)
        for command in plan.commands:
            if command.intent_id != plan.intent_id:
                return self._command_refusal(
                    command,
                    snapshot,
                    RefusalReason.INVALID_PLAN,
                    "command intent_id does not match its plan",
                )
            if command.safety_action:
                if not self._is_legitimate_safety_action(plan, command):
                    return self._command_refusal(
                        command,
                        snapshot,
                        RefusalReason.INVALID_PLAN,
                        "safety bypass does not match a hold, land_all, or estop plan",
                    )
            if command.operation not in allowed:
                return self._command_refusal(
                    command,
                    snapshot,
                    RefusalReason.INVALID_PLAN,
                    "command operation does not match the plan intent",
                )
            if not command.safety_action and command.drone_id not in selection:
                return self._command_refusal(
                    command,
                    snapshot,
                    RefusalReason.INVALID_PLAN,
                    "normal command target is outside the frozen plan selection",
                )
        return None

    @staticmethod
    def _check_plan_boundary(plan: Plan, snapshot: FleetSnapshot) -> Refusal | None:
        optional_updates_are_typed = (
            plan.selection_update is None or isinstance(plan.selection_update, tuple)
        ) and all(
            value is None or isinstance(value, bool)
            for value in (plan.armed_update, plan.estop_update)
        )
        valid = (
            isinstance(plan.plan_id, str)
            and bool(plan.plan_id)
            and isinstance(plan.intent_id, str)
            and bool(plan.intent_id)
            and isinstance(plan.intent_name, IntentName)
            and isinstance(plan.roster_version, int)
            and not isinstance(plan.roster_version, bool)
            and plan.roster_version >= 0
            and isinstance(plan.selection, tuple)
            and isinstance(plan.confirmed, bool)
            and isinstance(plan.commands, tuple)
            and all(isinstance(command, Command) for command in plan.commands)
            and (plan.hold_scope is None or isinstance(plan.hold_scope, HoldScope))
            and optional_updates_are_typed
            and plan.status is LifecycleStatus.ACCEPTED
        )
        if valid:
            return None
        return SafetyArbiter._invalid_plan_refusal(
            plan,
            snapshot,
            "plan fields do not match the strict autonomy boundary",
        )

    @staticmethod
    def _check_command_boundary(
        plan: Plan,
        command: Command,
        snapshot: FleetSnapshot,
    ) -> Refusal | None:
        valid = (
            isinstance(command.command_id, str)
            and bool(command.command_id)
            and isinstance(command.intent_id, str)
            and bool(command.intent_id)
            and isinstance(command.roster_version, int)
            and not isinstance(command.roster_version, bool)
            and command.roster_version >= 0
            and isinstance(command.drone_id, int)
            and not isinstance(command.drone_id, bool)
            and command.drone_id > 0
            and isinstance(command.connection_epoch, int)
            and not isinstance(command.connection_epoch, bool)
            and command.connection_epoch >= 0
            and isinstance(command.operation, CommandOperation)
            and isinstance(command.parameters, Mapping)
            and all(isinstance(key, str) for key in command.parameters)
            and isinstance(command.safety_action, bool)
        )
        if valid:
            return None
        return SafetyArbiter._invalid_plan_refusal(
            plan,
            snapshot,
            "command fields do not match the strict autonomy boundary",
        )

    def _check_deterministic_plan_shape(
        self,
        plan: Plan,
        snapshot: FleetSnapshot,
    ) -> Refusal | None:
        if plan.intent_name in {
            IntentName.TAKEOFF,
            IntentName.TRANSLATE,
            IntentName.COME_HOME,
        }:
            expected = tuple(sorted(plan.selection))
            actual = tuple(sorted(command.drone_id for command in plan.commands))
            if not expected or actual != expected:
                return self._invalid_plan_refusal(
                    plan,
                    snapshot,
                    f"{plan.intent_name.value} requires exactly one command per selected aircraft",
                )
            for command in plan.commands:
                if not self._valid_normal_command(plan.intent_name, command):
                    return self._invalid_plan_refusal(
                        plan,
                        snapshot,
                        f"{plan.intent_name.value} command parameters are malformed",
                    )
        if plan.intent_name is IntentName.CAPTURE_ROOM:
            return self._check_capture_plan_shape(plan, snapshot)
        return None

    @staticmethod
    def _valid_normal_command(intent_name: IntentName, command: Command) -> bool:
        if intent_name is IntentName.TAKEOFF:
            return (
                command.operation is CommandOperation.TAKEOFF
                and set(command.parameters) == {"z"}
                and _finite_positive(command.parameters.get("z"))
            )
        if intent_name in {IntentName.TRANSLATE, IntentName.COME_HOME}:
            return (
                command.operation is CommandOperation.GOTO
                and set(command.parameters) == {"speed", "x", "y", "z"}
                and _finite_positive(command.parameters.get("speed"))
                and all(_finite_number(command.parameters.get(axis)) for axis in ("x", "y", "z"))
            )
        return False

    def _check_capture_plan_shape(
        self,
        plan: Plan,
        snapshot: FleetSnapshot,
    ) -> Refusal | None:
        if len(plan.selection) != 1 or any(
            command.drone_id != plan.selection[0] for command in plan.commands
        ):
            return self._invalid_plan_refusal(
                plan,
                snapshot,
                "capture_room requires one frozen target for every command",
            )
        if len(plan.commands) < 2:
            return self._invalid_plan_refusal(
                plan,
                snapshot,
                "capture_room plan is incomplete",
            )
        capabilities, gimbal = plan.commands[:2]
        if (
            capabilities.operation is not CommandOperation.CAMERA_CAPABILITIES
            or set(capabilities.parameters) != {"capture_id", "pattern", "room_id"}
            or gimbal.operation is not CommandOperation.SET_GIMBAL_PITCH
            or set(gimbal.parameters) != {"pitch"}
            or not _nonempty_string(capabilities.parameters.get("capture_id"))
            or not _nonempty_string(capabilities.parameters.get("room_id"))
            or not _finite_number(gimbal.parameters.get("pitch"))
        ):
            return self._invalid_plan_refusal(
                plan,
                snapshot,
                "capture_room capability or gimbal step is malformed",
            )
        pattern = capabilities.parameters.get("pattern")
        if pattern == "pano_360":
            operations = tuple(command.operation for command in plan.commands)
            expected = (
                CommandOperation.CAMERA_CAPABILITIES,
                CommandOperation.SET_GIMBAL_PITCH,
                CommandOperation.CAMERA_READY,
                CommandOperation.CAPTURE_PANORAMA,
                CommandOperation.RETRIEVE_MEDIA,
            )
            if operations != expected:
                return self._invalid_plan_refusal(
                    plan,
                    snapshot,
                    "pano_360 requires the exact five-step camera sequence",
                )
            capture = plan.commands[3]
            if not self._valid_capture_step(capabilities, capture, frame_number=None):
                return self._invalid_plan_refusal(
                    plan,
                    snapshot,
                    "pano_360 capture metadata is malformed or cross-linked",
                )
            if plan.commands[2].parameters or not self._valid_retrieval_step(
                plan.commands[4], capture
            ):
                return self._invalid_plan_refusal(
                    plan,
                    snapshot,
                    "pano_360 readiness or retrieval step is malformed",
                )
            return None

        if pattern != "reconstruct_8" or len(plan.commands) != 34:
            return self._invalid_plan_refusal(
                plan,
                snapshot,
                "reconstruct_8 requires the exact 34-step camera sequence",
            )
        anchor: object | None = None
        pose_tolerance: object | None = None
        rotation_yaws: list[float] = []
        rotation_tolerances: list[float] = []
        declared_overlaps: list[float] = []
        for frame_index in range(8):
            offset = 2 + frame_index * 4
            rotation, ready, capture, retrieval = plan.commands[offset : offset + 4]
            if (
                rotation.operation is not CommandOperation.ROTATE_TO
                or set(rotation.parameters) != {"min_overlap", "speed", "tolerance", "yaw"}
                or not _finite_number(rotation.parameters.get("yaw"))
                or not _finite_positive(rotation.parameters.get("speed"))
                or not _finite_range(rotation.parameters.get("tolerance"), 0, 180)
                or not _finite_positive_range(rotation.parameters.get("min_overlap"), 180)
                or ready.operation is not CommandOperation.CAMERA_READY
                or bool(ready.parameters)
                or capture.operation is not CommandOperation.CAPTURE_PHOTO
                or not self._valid_capture_step(
                    capabilities,
                    capture,
                    frame_number=frame_index + 1,
                )
                or not self._valid_retrieval_step(retrieval, capture)
            ):
                return self._invalid_plan_refusal(
                    plan,
                    snapshot,
                    f"reconstruct_8 frame {frame_index + 1} is malformed or out of order",
                )
            current_anchor = capture.parameters.get("approved_pose")
            current_pose_tolerance = capture.parameters.get("pose_tolerance")
            if anchor is None:
                anchor = current_anchor
                pose_tolerance = current_pose_tolerance
            elif current_anchor != anchor or current_pose_tolerance != pose_tolerance:
                return self._invalid_plan_refusal(
                    plan,
                    snapshot,
                    "reconstruct_8 capture anchors and pose tolerances must be identical",
                )
            rotation_yaws.append(float(rotation.parameters["yaw"]) % 360.0)
            rotation_tolerances.append(float(rotation.parameters["tolerance"]))
            declared_overlaps.append(float(rotation.parameters["min_overlap"]))
        if (
            len(set(rotation_yaws)) != 8
            or len(set(rotation_tolerances)) != 1
            or len(set(declared_overlaps)) != 1
        ):
            return self._invalid_plan_refusal(
                plan,
                snapshot,
                "reconstruct_8 requires unique headings and consistent tolerances",
            )
        return None

    def _valid_capture_step(
        self,
        capabilities: Command,
        capture: Command,
        *,
        frame_number: int | None,
    ) -> bool:
        expected_keys = {
            "approved_pose",
            "capture_id",
            "pattern",
            "pose_tolerance",
            "room_id",
        }
        if frame_number is not None:
            expected_keys.add("frame_number")
        pose = capture.parameters.get("approved_pose")
        if set(capture.parameters) != expected_keys or not isinstance(pose, Mapping):
            return False
        try:
            Position.from_mapping(pose)
        except (TypeError, ValueError):
            return False
        return (
            capture.parameters.get("capture_id") == capabilities.parameters.get("capture_id")
            and capture.parameters.get("pattern") == capabilities.parameters.get("pattern")
            and capture.parameters.get("room_id") == capabilities.parameters.get("room_id")
            and _finite_nonnegative(capture.parameters.get("pose_tolerance"))
            and float(capture.parameters["pose_tolerance"]) <= self.config.max_capture_pose_drift_m
            and (
                frame_number is None
                or (
                    isinstance(capture.parameters.get("frame_number"), int)
                    and not isinstance(capture.parameters.get("frame_number"), bool)
                    and capture.parameters.get("frame_number") == frame_number
                )
            )
        )

    @staticmethod
    def _valid_retrieval_step(retrieval: Command, capture: Command) -> bool:
        return (
            retrieval.operation is CommandOperation.RETRIEVE_MEDIA
            and set(retrieval.parameters) == {"source_command_id"}
            and retrieval.parameters.get("source_command_id") == capture.command_id
        )

    def _check_capture_pose_lock(
        self,
        plan: Plan,
        command: Command,
        snapshot: FleetSnapshot,
        aircraft: AircraftState,
    ) -> Refusal | None:
        capture = next(
            (
                planned
                for planned in plan.commands
                if planned.operation
                in {CommandOperation.CAPTURE_PANORAMA, CommandOperation.CAPTURE_PHOTO}
            ),
            None,
        )
        if capture is None or not isinstance(capture.parameters.get("approved_pose"), Mapping):
            return self._command_refusal(
                command,
                snapshot,
                RefusalReason.INVALID_PLAN,
                "capture plan has no approved pose lock",
            )
        try:
            approved = Position.from_mapping(capture.parameters["approved_pose"])
        except (TypeError, ValueError):
            return self._command_refusal(
                command,
                snapshot,
                RefusalReason.INVALID_PLAN,
                "capture plan approved pose is malformed",
            )
        tolerance = capture.parameters.get("pose_tolerance")
        if (
            not _finite_nonnegative(tolerance)
            or float(tolerance) > self.config.max_capture_pose_drift_m
        ):
            return self._command_refusal(
                command,
                snapshot,
                RefusalReason.INVALID_PLAN,
                "capture plan pose tolerance exceeds the configured safety maximum",
            )
        if aircraft.pose.distance_to(approved) > float(tolerance):
            return self._command_refusal(
                command,
                snapshot,
                RefusalReason.INVALID_STATE,
                "aircraft moved outside the approved capture pose tolerance",
            )
        return None

    @staticmethod
    def _check_plan_state_updates(plan: Plan, snapshot: FleetSnapshot) -> Refusal | None:
        valid = True
        if plan.intent_name is IntentName.ARM:
            valid = (
                plan.armed_update is True
                and plan.selection_update is None
                and plan.estop_update is None
            )
        elif plan.intent_name is IntentName.SELECT:
            update = plan.selection_update
            valid = (
                isinstance(update, tuple)
                and bool(update)
                and all(
                    isinstance(drone_id, int) and not isinstance(drone_id, bool) and drone_id > 0
                    for drone_id in update
                )
                and len(update) == len(set(update))
                and all(
                    drone_id in snapshot.aircraft
                    and snapshot.aircraft[drone_id].membership is MembershipState.READY
                    for drone_id in update
                )
                and plan.armed_update is None
                and plan.estop_update is None
            )
        elif plan.intent_name is IntentName.ESTOP:
            valid = (
                plan.estop_update is True
                and plan.selection_update is None
                and plan.armed_update is None
            )
        else:
            valid = (
                plan.selection_update is None
                and plan.armed_update is None
                and plan.estop_update is None
            )
        valid = valid and (
            isinstance(plan.hold_scope, HoldScope)
            if plan.intent_name is IntentName.HOLD
            else plan.hold_scope is None
        )
        if valid:
            return None
        return Refusal(
            intent_id=plan.intent_id,
            roster_version=snapshot.roster_version,
            drone_id=None,
            connection_epoch=None,
            reason=RefusalReason.INVALID_PLAN,
            detail="plan state updates do not match its intent",
        )

    @staticmethod
    def _expected_safety_targets(
        plan: Plan,
        snapshot: FleetSnapshot,
        *,
        completed_command_ids: frozenset[str],
        required_hold_targets: tuple[int, ...] | None,
    ) -> tuple[int, ...] | None:
        if plan.intent_name is IntentName.HOLD:
            if plan.hold_scope is HoldScope.OPERATOR_SELECTION:
                return tuple(sorted(snapshot.selection))
            if plan.hold_scope is HoldScope.FLEET_SAFETY:
                return tuple(
                    drone_id
                    for drone_id, aircraft in sorted(snapshot.aircraft.items())
                    if aircraft.membership in {MembershipState.READY, MembershipState.DEGRADED}
                    and aircraft.airborne
                )
            if plan.hold_scope is HoldScope.TARGETED_SAFETY:
                return required_hold_targets or ()
            return ()
        if plan.intent_name is IntentName.LAND_ALL:
            still_airborne = {
                drone_id
                for drone_id, aircraft in sorted(snapshot.aircraft.items())
                if aircraft.membership in {MembershipState.READY, MembershipState.DEGRADED}
                and aircraft.airborne
            }
            proven_landed = {
                command.drone_id
                for command in plan.commands
                if command.command_id in completed_command_ids
            }
            return tuple(sorted(still_airborne | proven_landed))
        if plan.intent_name is IntentName.ESTOP:
            return tuple(
                drone_id
                for drone_id, aircraft in sorted(snapshot.aircraft.items())
                if aircraft.membership in {MembershipState.READY, MembershipState.DEGRADED}
            )
        return None

    @staticmethod
    def _is_legitimate_safety_action(plan: Plan, command: Command) -> bool:
        return _STOPPED_OPERATION_BY_INTENT.get(plan.intent_name) is command.operation

    @staticmethod
    def _is_allowed_while_stopped(plan: Plan, command: Command) -> bool:
        return _STOPPED_OPERATION_BY_INTENT.get(plan.intent_name) is command.operation and (
            plan.intent_name is not IntentName.LAND_ALL or plan.confirmed is True
        )

    def _intent_targets(
        self, intent: IntentV1, snapshot: FleetSnapshot
    ) -> tuple[int, ...] | Refusal:
        if intent.name is IntentName.SELECT:
            raw_ids = intent.args.get("ids")
            if not isinstance(raw_ids, tuple):
                return self._intent_refusal(
                    intent,
                    snapshot,
                    RefusalReason.INVALID_SELECTION,
                    "select args contain no normalized ids",
                )
            return tuple(raw_ids)
        if intent.name in {IntentName.LAND_ALL, IntentName.ESTOP}:
            return tuple(
                drone_id
                for drone_id, aircraft in sorted(snapshot.aircraft.items())
                if aircraft.membership in {MembershipState.READY, MembershipState.DEGRADED}
                and (intent.name is IntentName.ESTOP or aircraft.airborne)
            )
        return snapshot.selection

    def _check_operator(
        self,
        intent_id: str,
        snapshot: FleetSnapshot,
        command: Command | None = None,
    ) -> Refusal | None:
        if not snapshot.operator_present:
            return self._refusal_for(
                intent_id,
                snapshot,
                RefusalReason.OPERATOR_ABSENT,
                "operator presence is not asserted",
                command=command,
            )
        if (
            self.timestamp_exceeds_future_skew(snapshot, snapshot.operator_last_seen_ms)
            or snapshot.now_ms - snapshot.operator_last_seen_ms > self.config.operator_timeout_ms
        ):
            return self._refusal_for(
                intent_id,
                snapshot,
                RefusalReason.OPERATOR_ABSENT,
                "operator activity is stale",
                command=command,
            )
        return None

    def timestamp_exceeds_future_skew(
        self,
        snapshot: FleetSnapshot,
        timestamp_ms: int | None,
    ) -> bool:
        """Apply the deployment clock-skew budget to snapshot-relative evidence."""
        return (
            timestamp_ms is not None
            and timestamp_ms > snapshot.now_ms + self.config.max_future_clock_skew_ms
        )

    def _check_membership(
        self,
        intent_id: str,
        snapshot: FleetSnapshot,
        aircraft: AircraftState,
        *,
        safe_action: bool,
    ) -> Refusal | None:
        allowed = {MembershipState.READY}
        if safe_action:
            allowed.add(MembershipState.DEGRADED)
        if aircraft.membership not in allowed:
            return self._refusal_for(
                intent_id,
                snapshot,
                RefusalReason.AIRCRAFT_NOT_READY,
                f"aircraft membership is {aircraft.membership.value}",
                aircraft=aircraft,
            )
        return None

    def _check_authority(
        self,
        intent_id: str,
        snapshot: FleetSnapshot,
        aircraft: AircraftState,
        command: Command | None = None,
    ) -> Refusal | None:
        if not aircraft.control_authority:
            return self._refusal_for(
                intent_id,
                snapshot,
                RefusalReason.CONTROL_AUTHORITY,
                "network control authority is unavailable",
                aircraft=aircraft,
                command=command,
            )
        if not aircraft.rc_safety_operator_present:
            return self._refusal_for(
                intent_id,
                snapshot,
                RefusalReason.RC_SAFETY_OPERATOR_ABSENT,
                "the aircraft has no present physical RC safety operator",
                aircraft=aircraft,
                command=command,
            )
        if not aircraft.physical_rc_available:
            return self._refusal_for(
                intent_id,
                snapshot,
                RefusalReason.CONTROL_AUTHORITY,
                "physical RC takeover is unavailable",
                aircraft=aircraft,
                command=command,
            )
        return None

    def _check_armed(
        self,
        intent_id: str,
        snapshot: FleetSnapshot,
        aircraft: AircraftState,
        command: Command | None = None,
    ) -> Refusal | None:
        if not snapshot.armed:
            return self._refusal_for(
                intent_id,
                snapshot,
                RefusalReason.ARMED_REQUIRED,
                "session arm authorization is not active",
                aircraft=aircraft,
                command=command,
            )
        if not aircraft.armed:
            return self._refusal_for(
                intent_id,
                snapshot,
                RefusalReason.ARMED_REQUIRED,
                "aircraft telemetry does not prove physically armed state",
                aircraft=aircraft,
                command=command,
            )
        return None

    def _check_intent_state(
        self, intent: IntentV1, snapshot: FleetSnapshot, aircraft: AircraftState
    ) -> Refusal | None:
        if intent.name is IntentName.ARM:
            if (
                aircraft.flight_state
                not in {
                    FlightState.DISARMED,
                    FlightState.LANDED,
                }
                or aircraft.armed
            ):
                return self._intent_refusal(
                    intent,
                    snapshot,
                    RefusalReason.INVALID_STATE,
                    "arm requires a landed and disarmed aircraft",
                    aircraft.drone_id,
                )
        elif intent.name is IntentName.TAKEOFF:
            if not snapshot.armed or aircraft.flight_state not in {
                FlightState.ARMED,
                FlightState.DISARMED,
                FlightState.LANDED,
            }:
                return self._intent_refusal(
                    intent,
                    snapshot,
                    RefusalReason.INVALID_STATE,
                    "takeoff requires armed state on a landed aircraft",
                    aircraft.drone_id,
                )
        elif (
            intent.name
            in {
                IntentName.TRANSLATE,
                IntentName.COME_HOME,
            }
            and aircraft.flight_state not in _STABLE_MOTION_STATES
        ):
            return self._intent_refusal(
                intent,
                snapshot,
                RefusalReason.INVALID_STATE,
                f"{intent.name.value} requires an airborne aircraft",
                aircraft.drone_id,
            )
        elif intent.name is IntentName.HOLD and not aircraft.airborne:
            return self._intent_refusal(
                intent,
                snapshot,
                RefusalReason.INVALID_STATE,
                "hold requires an airborne aircraft",
                aircraft.drone_id,
            )
        elif intent.name is IntentName.LAND_ALL and not aircraft.airborne:
            return self._intent_refusal(
                intent,
                snapshot,
                RefusalReason.INVALID_STATE,
                "land_all targets must be airborne",
                aircraft.drone_id,
            )
        elif (
            intent.name is IntentName.CAPTURE_ROOM
            and aircraft.flight_state is not FlightState.HOVERING
        ):
            return self._intent_refusal(
                intent,
                snapshot,
                RefusalReason.INVALID_STATE,
                "capture_room requires a hovering aircraft",
                aircraft.drone_id,
            )
        return None

    def _check_command_state(
        self, command: Command, snapshot: FleetSnapshot, aircraft: AircraftState
    ) -> Refusal | None:
        operation = command.operation
        if operation is CommandOperation.TAKEOFF:
            if not snapshot.armed or aircraft.flight_state not in {
                FlightState.ARMED,
                FlightState.DISARMED,
                FlightState.LANDED,
            }:
                return self._command_refusal(
                    command,
                    snapshot,
                    RefusalReason.INVALID_STATE,
                    "takeoff command requires an armed, landed aircraft",
                )
        elif (
            operation in {CommandOperation.GOTO, CommandOperation.ROTATE_TO}
            and aircraft.flight_state not in _STABLE_MOTION_STATES
        ):
            return self._command_refusal(
                command,
                snapshot,
                RefusalReason.INVALID_STATE,
                f"{operation.value} requires an airborne aircraft",
            )
        elif operation is CommandOperation.HOVER and not aircraft.airborne:
            return self._command_refusal(
                command,
                snapshot,
                RefusalReason.INVALID_STATE,
                "hover requires an airborne aircraft",
            )
        elif operation is CommandOperation.LAND and not aircraft.airborne:
            return self._command_refusal(
                command,
                snapshot,
                RefusalReason.INVALID_STATE,
                "land requires an airborne aircraft",
            )
        elif operation in _CAMERA_OPERATIONS and aircraft.flight_state is not FlightState.HOVERING:
            return self._command_refusal(
                command,
                snapshot,
                RefusalReason.INVALID_STATE,
                "camera missions require a hovering aircraft",
            )
        return None

    def _check_telemetry(
        self,
        intent_id: str,
        snapshot: FleetSnapshot,
        aircraft: AircraftState,
        *,
        require_position: bool,
        command: Command | None = None,
    ) -> Refusal | None:
        if aircraft.link_quality < self.config.min_link_quality:
            return self._refusal_for(
                intent_id,
                snapshot,
                RefusalReason.LINK_QUALITY,
                "aircraft link quality is below the configured threshold",
                aircraft=aircraft,
                command=command,
            )
        if (
            self.timestamp_exceeds_future_skew(snapshot, aircraft.link_last_seen_ms)
            or snapshot.now_ms - aircraft.link_last_seen_ms > self.config.max_link_age_ms
        ):
            return self._refusal_for(
                intent_id,
                snapshot,
                RefusalReason.LINK_STALE,
                "aircraft link telemetry is stale",
                aircraft=aircraft,
                command=command,
            )
        if require_position and aircraft.position_quality < self.config.min_position_quality:
            return self._refusal_for(
                intent_id,
                snapshot,
                RefusalReason.POSITION_QUALITY,
                "aircraft positioning quality is below the configured threshold",
                aircraft=aircraft,
                command=command,
            )
        if self.timestamp_exceeds_future_skew(snapshot, aircraft.position_loss_since_ms):
            return self._refusal_for(
                intent_id,
                snapshot,
                RefusalReason.POSITION_STALE,
                "position-loss timestamp exceeds configured future clock skew",
                aircraft=aircraft,
                command=command,
            )
        if require_position and (
            self.timestamp_exceeds_future_skew(snapshot, aircraft.position_last_seen_ms)
            or snapshot.now_ms - aircraft.position_last_seen_ms > self.config.max_position_age_ms
        ):
            return self._refusal_for(
                intent_id,
                snapshot,
                RefusalReason.POSITION_STALE,
                "aircraft positioning telemetry is stale",
                aircraft=aircraft,
                command=command,
            )
        return None

    def _check_battery(
        self,
        intent_id: str,
        snapshot: FleetSnapshot,
        aircraft: AircraftState,
        target: Position,
        command: Command | None = None,
    ) -> Refusal | None:
        if aircraft.battery <= self.config.battery_critical_fraction:
            return self._refusal_for(
                intent_id,
                snapshot,
                RefusalReason.BATTERY_CRITICAL,
                "aircraft battery is at or below the configured critical threshold",
                aircraft=aircraft,
                command=command,
            )
        if aircraft.home is None:
            return self._refusal_for(
                intent_id,
                snapshot,
                RefusalReason.HOME_POSE_MISSING,
                "aircraft home pose is unavailable",
                aircraft=aircraft,
                command=command,
            )
        route_distance = target.distance_to(aircraft.home)
        if command is not None and command.operation in {
            CommandOperation.TAKEOFF,
            CommandOperation.GOTO,
        }:
            route_distance += aircraft.pose.distance_to(target)
        required = self.config.battery_reserve_fraction + (
            route_distance * self.config.battery_cost_per_m
        )
        if aircraft.battery < required:
            return self._refusal_for(
                intent_id,
                snapshot,
                RefusalReason.BATTERY_RESERVE,
                "battery cannot preserve the configured return reserve for this target",
                aircraft=aircraft,
                command=command,
            )
        return None

    def _check_capture_preconditions(
        self,
        intent_id: str,
        snapshot: FleetSnapshot,
        aircraft: AircraftState,
        command: Command | None = None,
    ) -> Refusal | None:
        if aircraft.active_task_id is not None:
            return self._refusal_for(
                intent_id,
                snapshot,
                RefusalReason.ACTIVE_TASK,
                "aircraft already has an active task",
                aircraft=aircraft,
                command=command,
            )
        if not aircraft.camera_ready:
            return self._refusal_for(
                intent_id,
                snapshot,
                RefusalReason.CAMERA_NOT_READY,
                "camera is not ready",
                aircraft=aircraft,
                command=command,
            )
        if aircraft.storage_remaining_bytes < self.config.min_capture_storage_bytes:
            return self._refusal_for(
                intent_id,
                snapshot,
                RefusalReason.STORAGE,
                "aircraft storage is below the configured capture minimum",
                aircraft=aircraft,
                command=command,
            )
        return None

    def _check_spacing(
        self,
        command: Command,
        snapshot: FleetSnapshot,
        target: Position,
        projected_positions: dict[int, Position],
    ) -> Refusal | None:
        for other_id, other in sorted(snapshot.aircraft.items()):
            if other_id == command.drone_id:
                continue
            if other.membership is not MembershipState.READY or (
                not other.airborne and other_id not in projected_positions
            ):
                continue
            other_target = projected_positions.get(other_id, other.pose)
            if target.distance_to(other_target) < self.config.min_spacing_m:
                return self._command_refusal(
                    command,
                    snapshot,
                    RefusalReason.SPACING,
                    f"planned target violates spacing from aircraft {other_id}",
                )
        return None

    def projected_positions(self, plan: Plan, snapshot: FleetSnapshot) -> dict[int, Position]:
        projected: dict[int, Position] = {}
        for command in plan.commands:
            aircraft = snapshot.aircraft.get(command.drone_id)
            if aircraft is None:
                continue
            target = self.command_position(command, aircraft)
            if target is not None:
                projected[command.drone_id] = target
        return projected

    @staticmethod
    def command_position(command: Command, aircraft: AircraftState) -> Position | None:
        if command.operation is CommandOperation.TAKEOFF:
            return Position(aircraft.pose.x, aircraft.pose.y, float(command.parameters["z"]))
        if command.operation is CommandOperation.GOTO:
            return Position(
                float(command.parameters["x"]),
                float(command.parameters["y"]),
                float(command.parameters["z"]),
            )
        if command.operation in {CommandOperation.ROTATE_TO, *_CAMERA_OPERATIONS}:
            return aircraft.pose
        return None

    def _intent_refusal(
        self,
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

    def _command_refusal(
        self,
        command: Command,
        snapshot: FleetSnapshot,
        reason: RefusalReason,
        detail: str,
        *,
        epoch: int | None = None,
    ) -> Refusal:
        return Refusal(
            intent_id=command.intent_id,
            roster_version=snapshot.roster_version,
            drone_id=command.drone_id,
            connection_epoch=epoch if epoch is not None else command.connection_epoch,
            reason=reason,
            detail=detail,
        )

    @staticmethod
    def _invalid_plan_refusal(
        plan: Plan,
        snapshot: FleetSnapshot,
        detail: str,
    ) -> Refusal:
        return Refusal(
            intent_id=plan.intent_id if isinstance(plan.intent_id, str) else "invalid-plan",
            roster_version=snapshot.roster_version,
            drone_id=None,
            connection_epoch=None,
            reason=RefusalReason.INVALID_PLAN,
            detail=detail,
        )

    def _refusal_for(
        self,
        intent_id: str,
        snapshot: FleetSnapshot,
        reason: RefusalReason,
        detail: str,
        *,
        aircraft: AircraftState | None = None,
        command: Command | None = None,
    ) -> Refusal:
        if command is not None:
            return self._command_refusal(command, snapshot, reason, detail)
        return Refusal(
            intent_id=intent_id,
            roster_version=snapshot.roster_version,
            drone_id=aircraft.drone_id if aircraft is not None else None,
            connection_epoch=aircraft.connection_epoch if aircraft is not None else None,
            reason=reason,
            detail=detail,
        )


def _finite_fraction(value: object) -> bool:
    return (
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and isfinite(value)
        and 0 <= value <= 1
    )


def _finite_nonnegative(value: object) -> bool:
    return (
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and isfinite(value)
        and value >= 0
    )


def _finite_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and isfinite(value)


def _finite_positive(value: object) -> bool:
    return _finite_number(value) and value > 0


def _finite_range(value: object, lower: float, upper: float) -> bool:
    return _finite_number(value) and lower <= value < upper


def _finite_positive_range(value: object, upper: float) -> bool:
    return _finite_number(value) and 0 < value < upper


def _nonempty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value)
