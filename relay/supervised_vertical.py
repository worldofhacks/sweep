"""The deliberately narrow GPS-denied supervised vertical flight policy."""

from __future__ import annotations

from dataclasses import dataclass
from math import floor, isfinite
from typing import Final

from planner.models import (
    AircraftState,
    Command,
    CommandOperation,
    FleetSnapshot,
    FlightState,
    HoldScope,
    MembershipState,
    Plan,
    Position,
    Refusal,
    RefusalReason,
)
from relay.capabilities import CapabilityProfile, IntentName
from relay.intent_v1 import IntentV1, Mode

LOCAL_HEIGHT_SOURCE: Final = "flight_controller_altitude"
MAX_SUPERVISED_VERTICAL_HEIGHT_M: Final = 2.4384
REVIEWED_SUPERVISED_TAKEOFF_ALTITUDES_M: Final = frozenset({1.2, 1.8})
SUPERVISED_VERTICAL_PROFILE = CapabilityProfile(
    name="supervised_vertical",
    enabled_intent_names=frozenset(
        {
            IntentName.SELECT,
            IntentName.ARM,
            IntentName.TAKEOFF,
            IntentName.HOLD,
            IntentName.LAND,
            IntentName.LAND_ALL,
            IntentName.ESTOP,
            IntentName.GROUND_VELOCITY,
            IntentName.SURVEY_AREA,
        }
    ),
    requires_home_pose=False,
)


@dataclass(frozen=True, slots=True)
class SupervisedVerticalConfig:
    takeoff_altitude_m: float
    maximum_height_m: float
    operator_declared_vertical_clearance_m: float
    min_battery_fraction: float
    min_link_quality: float
    max_link_age_ms: int
    max_local_height_age_ms: int
    operator_timeout_ms: int
    max_future_clock_skew_ms: int
    motion_conflict_window_ms: int

    def __post_init__(self) -> None:
        for name in (
            "takeoff_altitude_m",
            "maximum_height_m",
            "operator_declared_vertical_clearance_m",
        ):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"{name} must be a finite positive number")
        if self.takeoff_altitude_m not in REVIEWED_SUPERVISED_TAKEOFF_ALTITUDES_M:
            raise ValueError(
                "takeoff_altitude_m must be one of the reviewed supervised heights: "
                "1.2 or 1.8 metres"
            )
        if self.maximum_height_m > MAX_SUPERVISED_VERTICAL_HEIGHT_M:
            raise ValueError("maximum_height_m exceeds the 8 foot supervised ceiling")
        if self.takeoff_altitude_m > self.maximum_height_m:
            raise ValueError("takeoff_altitude_m must not exceed maximum_height_m")
        if self.takeoff_altitude_m > self.operator_declared_vertical_clearance_m:
            raise ValueError("takeoff_altitude_m exceeds operator_declared_vertical_clearance_m")
        for name in ("min_battery_fraction", "min_link_quality"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not isfinite(value)
                or not 0 <= value <= 1
            ):
                raise ValueError(f"{name} must be a finite fraction")
        for name in (
            "max_link_age_ms",
            "max_local_height_age_ms",
            "operator_timeout_ms",
            "max_future_clock_skew_ms",
            "motion_conflict_window_ms",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if not 1 <= self.max_local_height_age_ms <= 500:
            raise ValueError("max_local_height_age_ms must be between 1 and 500")

    def takeoff_parameters(self) -> dict[str, int | float]:
        """Signed millimetre ceiling rounds down and never exceeds declared clearance."""
        return {
            "z": self.takeoff_altitude_m,
            "maximum_height_mm": floor(
                min(self.maximum_height_m, self.operator_declared_vertical_clearance_m) * 1000
            ),
            "max_local_height_age_ms": self.max_local_height_age_ms,
        }

    def altitude_grounding(self) -> None:
        return None


class SupervisedVerticalPlanner:
    """Build only the fixed-height takeoff and stop/land commands this profile admits."""

    capability_profile = SUPERVISED_VERTICAL_PROFILE
    navigation_runtime = None

    def __init__(self, config: SupervisedVerticalConfig) -> None:
        self.config = config

    def supports(self, intent: IntentV1) -> bool:
        return self.capability_profile.supports(intent.name)

    def plan(self, intent: IntentV1, snapshot: FleetSnapshot) -> Plan | Refusal:
        if not self.supports(intent):
            return _refusal(
                intent, snapshot, RefusalReason.UNSUPPORTED, "intent is outside supervised vertical"
            )
        plan_id = f"plan:{intent.intent_id}"
        selected = tuple(sorted(snapshot.selection))
        selection_update: tuple[int, ...] | None = None
        armed_update: bool | None = None
        estop_update: bool | None = None
        commands: tuple[Command, ...] = ()
        hold_scope: HoldScope | None = None

        if intent.name is IntentName.SELECT:
            raw_ids = intent.args.get("ids")
            if not isinstance(raw_ids, tuple) or len(raw_ids) != 1 or type(raw_ids[0]) is not int:
                return _refusal(
                    intent,
                    snapshot,
                    RefusalReason.INVALID_SELECTION,
                    "select requires one aircraft",
                )
            selection_update = raw_ids
            selected = raw_ids
        elif intent.name is IntentName.ARM:
            armed_update = True
        elif intent.name is IntentName.TAKEOFF:
            commands = self._commands(
                intent,
                snapshot,
                selected,
                CommandOperation.TAKEOFF,
                self.config.takeoff_parameters(),
            )
        elif intent.name is IntentName.HOLD:
            hold_scope = HoldScope.OPERATOR_SELECTION
            commands = self._commands(
                intent, snapshot, selected, CommandOperation.HOVER, {}, safety_action=True
            )
        elif intent.name is IntentName.LAND:
            commands = self._commands(intent, snapshot, selected, CommandOperation.LAND, {})
        elif intent.name is IntentName.LAND_ALL:
            selected = tuple(
                drone_id
                for drone_id, aircraft in sorted(snapshot.aircraft.items())
                if aircraft.membership in {MembershipState.READY, MembershipState.DEGRADED}
                and aircraft.airborne
            )
            commands = self._commands(
                intent, snapshot, selected, CommandOperation.LAND, {}, safety_action=True
            )
        elif intent.name is IntentName.ESTOP:
            selected = tuple(
                drone_id
                for drone_id, aircraft in sorted(snapshot.aircraft.items())
                if aircraft.membership in {MembershipState.READY, MembershipState.DEGRADED}
            )
            estop_update = True
            commands = self._commands(
                intent, snapshot, selected, CommandOperation.ESTOP, {}, safety_action=True
            )

        return Plan(
            plan_id=plan_id,
            intent_id=intent.intent_id,
            intent_name=intent.name,
            roster_version=snapshot.roster_version,
            selection=selected,
            confirmed=intent.confirm,
            commands=commands,
            selection_update=selection_update,
            armed_update=armed_update,
            estop_update=estop_update,
            hold_scope=hold_scope,
        )

    def emergency_hold_plan(
        self, *, intent_id: str, snapshot: FleetSnapshot, drone_ids: tuple[int, ...] | None = None
    ) -> Plan:
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
        intent = _safety_intent(intent_id, IntentName.HOLD, snapshot)
        return Plan(
            plan_id=f"plan:{intent_id}:safety-hold",
            intent_id=intent_id,
            intent_name=IntentName.HOLD,
            roster_version=snapshot.roster_version,
            selection=targets,
            confirmed=True,
            commands=self._commands(
                intent,
                snapshot,
                targets,
                CommandOperation.HOVER,
                {},
                safety_action=True,
                plan_id=f"plan:{intent_id}:safety-hold",
            ),
            hold_scope=HoldScope.FLEET_SAFETY if drone_ids is None else HoldScope.TARGETED_SAFETY,
        )

    def fleet_position_loss_plan(
        self, *, intent_id: str, snapshot: FleetSnapshot, land: bool
    ) -> Plan:
        return self.emergency_hold_plan(intent_id=intent_id, snapshot=snapshot)

    @staticmethod
    def _commands(
        intent: IntentV1,
        snapshot: FleetSnapshot,
        targets: tuple[int, ...],
        operation: CommandOperation,
        parameters: dict[str, object],
        *,
        safety_action: bool = False,
        plan_id: str | None = None,
    ) -> tuple[Command, ...]:
        prefix = plan_id or f"plan:{intent.intent_id}"
        return tuple(
            Command(
                command_id=f"{prefix}:command:{index:04d}",
                intent_id=intent.intent_id,
                roster_version=snapshot.roster_version,
                drone_id=drone_id,
                connection_epoch=snapshot.aircraft[drone_id].connection_epoch,
                operation=operation,
                parameters=parameters,
                safety_action=safety_action,
            )
            for index, drone_id in enumerate(targets, 1)
        )


class SupervisedVerticalArbiter:
    """Admission for local vertical flight that never consumes world pose or GPS quality."""

    requires_world_positioning = False

    def __init__(self, config: SupervisedVerticalConfig) -> None:
        self.config = config

    def timestamp_exceeds_future_skew(
        self, snapshot: FleetSnapshot, timestamp_ms: int | None
    ) -> bool:
        return (
            timestamp_ms is not None
            and timestamp_ms > snapshot.now_ms + self.config.max_future_clock_skew_ms
        )

    def check_intent(self, intent: IntentV1, snapshot: FleetSnapshot) -> Refusal | None:
        if not SUPERVISED_VERTICAL_PROFILE.supports(intent.name):
            return _refusal(
                intent, snapshot, RefusalReason.UNSUPPORTED, "intent is outside supervised vertical"
            )
        if (
            intent.name in {IntentName.TAKEOFF, IntentName.LAND, IntentName.LAND_ALL}
            and not intent.confirm
        ):
            return _refusal(
                intent,
                snapshot,
                RefusalReason.CONFIRMATION_REQUIRED,
                f"{intent.name.value} requires operator confirmation",
            )
        if snapshot.estop_active and intent.name not in {
            IntentName.ESTOP,
            IntentName.HOLD,
            IntentName.LAND,
            IntentName.LAND_ALL,
        }:
            return _refusal(intent, snapshot, RefusalReason.ESTOP_ACTIVE, "network stop is active")
        if intent.name not in {IntentName.ESTOP, IntentName.HOLD}:
            refusal = self._operator(intent.intent_id, snapshot)
            if refusal is not None:
                return refusal
        if intent.name in {IntentName.ESTOP, IntentName.HOLD}:
            return None
        if intent.name is IntentName.SELECT:
            ids = intent.args.get("ids")
            if (
                not isinstance(ids, tuple)
                or len(ids) != 1
                or (ids[0] not in snapshot.aircraft and ids[0] not in snapshot.ground_ids)
            ):
                return _refusal(
                    intent,
                    snapshot,
                    RefusalReason.INVALID_SELECTION,
                    "select requires the registered aircraft or a ground unit",
                )
            if ids[0] in snapshot.ground_ids:
                return None
        if len(snapshot.aircraft) != 1:
            return _refusal(
                intent,
                snapshot,
                RefusalReason.INVALID_SELECTION,
                "supervised vertical requires exactly one registered aircraft",
            )
        if intent.name is IntentName.SELECT:
            return self._common(
                intent.intent_id, snapshot, snapshot.aircraft[ids[0]], require_height=False
            )
        if intent.name is IntentName.ARM:
            return self._arm_intent(intent, snapshot)
        if intent.name in {IntentName.TAKEOFF, IntentName.LAND, IntentName.HOLD}:
            if len(intent.selection) != 1 or intent.selection != snapshot.selection:
                return _refusal(
                    intent,
                    snapshot,
                    RefusalReason.STALE_SELECTION,
                    "intent selection differs from authoritative selection",
                )
            aircraft = snapshot.aircraft.get(intent.selection[0])
            if aircraft is None:
                return _refusal(
                    intent,
                    snapshot,
                    RefusalReason.AIRCRAFT_NOT_REGISTERED,
                    "selected aircraft is absent",
                )
            return self._intent_for_aircraft(intent, snapshot, aircraft)
        if intent.name is IntentName.LAND_ALL:
            for aircraft in snapshot.aircraft.values():
                if aircraft.airborne:
                    return self._land_admission(intent.intent_id, snapshot, aircraft)
        return None

    def check_plan(self, plan: Plan, snapshot: FleetSnapshot) -> Refusal | None:
        refusal = self.check_plan_authorization(plan, snapshot)
        if refusal is not None:
            return refusal
        for command in plan.commands:
            refusal = self.check_command(plan, command, snapshot)
            if refusal is not None:
                return refusal
        return None

    def check_plan_authorization(
        self, plan: Plan, snapshot: FleetSnapshot, **_: object
    ) -> Refusal | None:
        if plan.roster_version != snapshot.roster_version:
            return _plan_refusal(
                plan,
                snapshot,
                RefusalReason.STALE_ROSTER,
                "plan roster differs from authoritative roster",
            )
        if not SUPERVISED_VERTICAL_PROFILE.supports(plan.intent_name):
            return _plan_refusal(
                plan, snapshot, RefusalReason.UNSUPPORTED, "plan is outside supervised vertical"
            )
        if (
            plan.intent_name in {IntentName.TAKEOFF, IntentName.LAND, IntentName.LAND_ALL}
            and not plan.confirmed
        ):
            return _plan_refusal(
                plan,
                snapshot,
                RefusalReason.CONFIRMATION_REQUIRED,
                "plan lacks operator confirmation",
            )
        expected = self._expected_plan(plan, snapshot)
        if expected is not None:
            return expected
        return self._revalidate_plan_intent(plan, snapshot)

    def _revalidate_plan_intent(self, plan: Plan, snapshot: FleetSnapshot) -> Refusal | None:
        args: dict[str, object] = {}
        if plan.intent_name is IntentName.SELECT:
            args = {"ids": plan.selection_update}
        intent = IntentV1(
            1,
            snapshot.now_ms,
            "intent",
            plan.intent_id,
            None,
            "console",
            "revalidation",
            plan.intent_name,
            args,
            plan.selection,
            Mode.INDOOR,
            plan.confirmed,
        )
        return self.check_intent(intent, snapshot)

    def check_command(
        self,
        plan: Plan,
        command: Command,
        snapshot: FleetSnapshot,
        *,
        projected_positions: dict[int, Position] | None = None,
    ) -> Refusal | None:
        del projected_positions
        if command.intent_id != plan.intent_id:
            return _command_refusal(
                command, snapshot, RefusalReason.INVALID_PLAN, "command intent does not match plan"
            )
        if command.roster_version != snapshot.roster_version:
            return _command_refusal(
                command,
                snapshot,
                RefusalReason.STALE_ROSTER,
                "command roster differs from authoritative roster",
            )
        aircraft = snapshot.aircraft.get(command.drone_id)
        if aircraft is None:
            return _command_refusal(
                command, snapshot, RefusalReason.AIRCRAFT_NOT_REGISTERED, "command target is absent"
            )
        if command.connection_epoch != aircraft.connection_epoch:
            return _command_refusal(
                command,
                snapshot,
                RefusalReason.STALE_CONNECTION_EPOCH,
                "command epoch differs from current epoch",
            )
        if command.operation is CommandOperation.TAKEOFF:
            return self._takeoff_command(plan, command, snapshot, aircraft)
        if command.operation is CommandOperation.LAND:
            return self._land_command(plan, command, snapshot, aircraft)
        if command.operation is CommandOperation.HOVER:
            if not command.safety_action or not aircraft.airborne:
                return _command_refusal(
                    command,
                    snapshot,
                    RefusalReason.INVALID_STATE,
                    "hold requires an airborne aircraft",
                )
            return self._membership(
                command.intent_id, snapshot, aircraft, safe=True, command=command
            )
        if command.operation is CommandOperation.ESTOP:
            if not command.safety_action:
                return _command_refusal(
                    command, snapshot, RefusalReason.INVALID_PLAN, "estop must be a safety action"
                )
            return self._membership(
                command.intent_id, snapshot, aircraft, safe=True, command=command
            )
        return _command_refusal(
            command,
            snapshot,
            RefusalReason.UNSUPPORTED,
            "vertical policy does not dispatch this operation",
        )

    def check_targeted_hold(
        self, plan: Plan, snapshot: FleetSnapshot, *, required_targets: tuple[int, ...]
    ) -> Refusal | None:
        if (
            plan.intent_name is not IntentName.HOLD
            or plan.hold_scope is not HoldScope.TARGETED_SAFETY
        ):
            return _plan_refusal(
                plan,
                snapshot,
                RefusalReason.INVALID_PLAN,
                "targeted safety hold has the wrong shape",
            )
        if plan.selection != required_targets:
            return _plan_refusal(
                plan, snapshot, RefusalReason.INVALID_PLAN, "targeted safety hold targets differ"
            )
        return self.check_plan(plan, snapshot)

    def command_position(self, command: Command, aircraft: AircraftState) -> None:
        del command, aircraft
        return None

    def check_altitude_outcome(
        self, plan: Plan, command: Command, snapshot: FleetSnapshot
    ) -> Refusal | None:
        del plan, command, snapshot
        return None

    def _expected_plan(self, plan: Plan, snapshot: FleetSnapshot) -> Refusal | None:
        if (
            plan.formation_update is not None
            or plan.spacing_update is not None
            or plan.altitude_grounding is not None
            or plan.navigation is not None
        ):
            return _plan_refusal(
                plan, snapshot, RefusalReason.INVALID_PLAN, "plan includes world-policy state"
            )
        operations = tuple(command.operation for command in plan.commands)
        if plan.intent_name is IntentName.SELECT:
            if (
                plan.commands
                or plan.selection_update is None
                or len(plan.selection_update) != 1
                or plan.armed_update is not None
                or plan.estop_update is not None
                or plan.hold_scope is not None
            ):
                return _plan_refusal(
                    plan,
                    snapshot,
                    RefusalReason.INVALID_PLAN,
                    "select plan must set one registered unit",
                )
        elif plan.intent_name is IntentName.ARM:
            if (
                plan.commands
                or plan.armed_update is not True
                or plan.selection_update is not None
                or plan.estop_update is not None
                or plan.hold_scope is not None
            ):
                return _plan_refusal(
                    plan,
                    snapshot,
                    RefusalReason.INVALID_PLAN,
                    "arm plan must set session authorization",
                )
        elif plan.intent_name is IntentName.TAKEOFF:
            if (
                len(plan.selection) != 1
                or operations != (CommandOperation.TAKEOFF,)
                or plan.selection_update is not None
                or plan.armed_update is not None
                or plan.estop_update is not None
                or plan.hold_scope is not None
            ):
                return _plan_refusal(
                    plan,
                    snapshot,
                    RefusalReason.INVALID_PLAN,
                    "takeoff plan must contain one takeoff",
                )
            if plan.commands[0].parameters != self.config.takeoff_parameters():
                return _plan_refusal(
                    plan,
                    snapshot,
                    RefusalReason.INVALID_PLAN,
                    "takeoff height differs from the fixed profile",
                )
        elif plan.intent_name is IntentName.HOLD:
            if (
                plan.hold_scope
                not in {
                    HoldScope.OPERATOR_SELECTION,
                    HoldScope.FLEET_SAFETY,
                    HoldScope.TARGETED_SAFETY,
                }
                or any(operation is not CommandOperation.HOVER for operation in operations)
                or plan.selection_update is not None
                or plan.armed_update is not None
                or plan.estop_update is not None
            ):
                return _plan_refusal(
                    plan,
                    snapshot,
                    RefusalReason.INVALID_PLAN,
                    "hold plan contains an unsupported command",
                )
        elif plan.intent_name is IntentName.LAND:
            if (
                len(plan.selection) != 1
                or operations != (CommandOperation.LAND,)
                or plan.selection_update is not None
                or plan.armed_update is not None
                or plan.estop_update is not None
                or plan.hold_scope is not None
            ):
                return _plan_refusal(
                    plan,
                    snapshot,
                    RefusalReason.INVALID_PLAN,
                    "land plan contains an unsupported command",
                )
        elif plan.intent_name is IntentName.LAND_ALL:
            expected = tuple(
                drone_id
                for drone_id, aircraft in sorted(snapshot.aircraft.items())
                if aircraft.membership in {MembershipState.READY, MembershipState.DEGRADED}
                and aircraft.airborne
            )
            if (
                plan.selection != expected
                or operations != (CommandOperation.LAND,) * len(expected)
                or plan.selection_update is not None
                or plan.armed_update is not None
                or plan.estop_update is not None
                or plan.hold_scope is not None
            ):
                return _plan_refusal(
                    plan,
                    snapshot,
                    RefusalReason.INVALID_PLAN,
                    "land_all plan contains an unsupported command",
                )
        elif plan.intent_name is IntentName.ESTOP:
            expected = tuple(
                drone_id
                for drone_id, aircraft in sorted(snapshot.aircraft.items())
                if aircraft.membership in {MembershipState.READY, MembershipState.DEGRADED}
            )
            if (
                plan.selection != expected
                or plan.estop_update is not True
                or operations != (CommandOperation.ESTOP,) * len(expected)
                or plan.selection_update is not None
                or plan.armed_update is not None
                or plan.hold_scope is not None
            ):
                return _plan_refusal(
                    plan,
                    snapshot,
                    RefusalReason.INVALID_PLAN,
                    "estop plan contains an unsupported command",
                )
        return None

    def _arm_intent(self, intent: IntentV1, snapshot: FleetSnapshot) -> Refusal | None:
        if len(snapshot.selection) != 1:
            return _refusal(
                intent,
                snapshot,
                RefusalReason.INVALID_SELECTION,
                "arm requires one selected aircraft",
            )
        aircraft = snapshot.aircraft.get(snapshot.selection[0])
        if aircraft is None:
            return _refusal(
                intent,
                snapshot,
                RefusalReason.AIRCRAFT_NOT_REGISTERED,
                "selected aircraft is absent",
            )
        if (
            aircraft.flight_state not in {FlightState.LANDED, FlightState.DISARMED}
            or aircraft.armed
        ):
            return _refusal(
                intent,
                snapshot,
                RefusalReason.INVALID_STATE,
                "arm requires a landed and disarmed aircraft",
            )
        return self._common(intent.intent_id, snapshot, aircraft, require_height=False)

    def _intent_for_aircraft(
        self, intent: IntentV1, snapshot: FleetSnapshot, aircraft: AircraftState
    ) -> Refusal | None:
        if intent.name is IntentName.TAKEOFF:
            if not snapshot.armed or aircraft.flight_state not in {
                FlightState.LANDED,
                FlightState.DISARMED,
            }:
                return _refusal(
                    intent,
                    snapshot,
                    RefusalReason.INVALID_STATE,
                    "takeoff requires a session-armed landed aircraft",
                    aircraft,
                )
            return self._common(intent.intent_id, snapshot, aircraft, require_height=True)
        if intent.name is IntentName.LAND:
            if not aircraft.airborne:
                return _refusal(
                    intent,
                    snapshot,
                    RefusalReason.INVALID_STATE,
                    "land requires an airborne aircraft",
                    aircraft,
                )
            return self._land_admission(intent.intent_id, snapshot, aircraft)
        if intent.name is IntentName.HOLD:
            if not aircraft.airborne:
                return _refusal(
                    intent,
                    snapshot,
                    RefusalReason.INVALID_STATE,
                    "hold requires an airborne aircraft",
                    aircraft,
                )
            return self._membership(intent.intent_id, snapshot, aircraft, safe=True)
        return None

    def _takeoff_command(
        self, plan: Plan, command: Command, snapshot: FleetSnapshot, aircraft: AircraftState
    ) -> Refusal | None:
        if command.safety_action or command.parameters != self.config.takeoff_parameters():
            return _command_refusal(
                command,
                snapshot,
                RefusalReason.INVALID_PLAN,
                "takeoff command differs from fixed profile",
            )
        intent = _safety_intent(command.intent_id, IntentName.TAKEOFF, snapshot, confirmed=True)
        return self._intent_for_aircraft(intent, snapshot, aircraft)

    def _land_command(
        self, plan: Plan, command: Command, snapshot: FleetSnapshot, aircraft: AircraftState
    ) -> Refusal | None:
        if command.parameters:
            return _command_refusal(
                command,
                snapshot,
                RefusalReason.INVALID_PLAN,
                "land command must not include parameters",
            )
        if not aircraft.airborne:
            return _command_refusal(
                command, snapshot, RefusalReason.INVALID_STATE, "land requires an airborne aircraft"
            )
        if command.safety_action:
            return self._membership(
                command.intent_id, snapshot, aircraft, safe=True, command=command
            )
        intent = _safety_intent(command.intent_id, IntentName.LAND, snapshot, confirmed=True)
        return self._intent_for_aircraft(intent, snapshot, aircraft)

    def _land_admission(
        self, intent_id: str, snapshot: FleetSnapshot, aircraft: AircraftState
    ) -> Refusal | None:
        refusal = self._membership(intent_id, snapshot, aircraft, safe=True)
        if refusal is not None:
            return refusal
        recovery = aircraft.landing_recovery
        recovery_current = (
            recovery is not None
            and not self.timestamp_exceeds_future_skew(snapshot, recovery.observed_at_ms)
            and snapshot.now_ms - recovery.observed_at_ms <= self.config.max_link_age_ms
        )
        if (
            not aircraft.control_authority and not recovery_current
        ) or not aircraft.physical_rc_available:
            return _refusal_id(
                intent_id,
                snapshot,
                RefusalReason.CONTROL_AUTHORITY,
                "network control authority or physical RC takeover is unavailable",
                aircraft,
            )
        if not aircraft.rc_safety_operator_present:
            return _refusal_id(
                intent_id,
                snapshot,
                RefusalReason.RC_SAFETY_OPERATOR_ABSENT,
                "physical RC safety operator is absent",
                aircraft,
            )
        if aircraft.link_quality < self.config.min_link_quality:
            return _refusal_id(
                intent_id,
                snapshot,
                RefusalReason.LINK_QUALITY,
                "aircraft link quality is below the configured threshold",
                aircraft,
            )
        if (
            self.timestamp_exceeds_future_skew(snapshot, aircraft.link_last_seen_ms)
            or snapshot.now_ms - aircraft.link_last_seen_ms > self.config.max_link_age_ms
        ):
            return _refusal_id(
                intent_id,
                snapshot,
                RefusalReason.LINK_STALE,
                "aircraft link telemetry is stale",
                aircraft,
            )
        return None

    def _common(
        self,
        intent_id: str,
        snapshot: FleetSnapshot,
        aircraft: AircraftState,
        *,
        require_height: bool,
    ) -> Refusal | None:
        refusal = self._membership(intent_id, snapshot, aircraft, safe=False)
        if refusal is not None:
            return refusal
        if not aircraft.control_authority or not aircraft.physical_rc_available:
            return _refusal_id(
                intent_id,
                snapshot,
                RefusalReason.CONTROL_AUTHORITY,
                "network control authority or physical RC takeover is unavailable",
                aircraft,
            )
        if not aircraft.rc_safety_operator_present:
            return _refusal_id(
                intent_id,
                snapshot,
                RefusalReason.RC_SAFETY_OPERATOR_ABSENT,
                "physical RC safety operator is absent",
                aircraft,
            )
        if aircraft.link_quality < self.config.min_link_quality:
            return _refusal_id(
                intent_id,
                snapshot,
                RefusalReason.LINK_QUALITY,
                "aircraft link quality is below the configured threshold",
                aircraft,
            )
        if (
            self.timestamp_exceeds_future_skew(snapshot, aircraft.link_last_seen_ms)
            or snapshot.now_ms - aircraft.link_last_seen_ms > self.config.max_link_age_ms
        ):
            return _refusal_id(
                intent_id,
                snapshot,
                RefusalReason.LINK_STALE,
                "aircraft link telemetry is stale",
                aircraft,
            )
        if aircraft.battery < self.config.min_battery_fraction:
            return _refusal_id(
                intent_id,
                snapshot,
                RefusalReason.BATTERY_RESERVE,
                "aircraft battery is below the supervised vertical threshold",
                aircraft,
            )
        if require_height:
            evidence = aircraft.local_height
            if evidence is None or evidence.source != LOCAL_HEIGHT_SOURCE:
                return _refusal_id(
                    intent_id,
                    snapshot,
                    RefusalReason.LOCAL_HEIGHT_UNAVAILABLE,
                    "no current SDK local-height evidence is available",
                    aircraft,
                )
            if (
                self.timestamp_exceeds_future_skew(snapshot, evidence.observed_at_ms)
                or snapshot.now_ms - evidence.observed_at_ms > self.config.max_local_height_age_ms
            ):
                return _refusal_id(
                    intent_id,
                    snapshot,
                    RefusalReason.LOCAL_HEIGHT_UNAVAILABLE,
                    "SDK local-height evidence is stale",
                    aircraft,
                )
            if not 0 <= evidence.z_m < self.config.takeoff_altitude_m:
                return _refusal_id(
                    intent_id,
                    snapshot,
                    RefusalReason.LOCAL_HEIGHT_UNAVAILABLE,
                    "SDK local height does not show a grounded takeoff baseline",
                    aircraft,
                )
        return None

    def _membership(
        self,
        intent_id: str,
        snapshot: FleetSnapshot,
        aircraft: AircraftState,
        *,
        safe: bool,
        command: Command | None = None,
    ) -> Refusal | None:
        allowed = (
            {MembershipState.READY, MembershipState.DEGRADED} if safe else {MembershipState.READY}
        )
        if aircraft.membership not in allowed and not (
            not safe
            and aircraft.membership is MembershipState.DEGRADED
            and aircraft.readiness_reasons == ("home_pose_missing",)
        ):
            return _refusal_id(
                intent_id,
                snapshot,
                RefusalReason.AIRCRAFT_NOT_READY,
                f"aircraft membership is {aircraft.membership.value}",
                aircraft,
                command,
            )
        return None

    def _operator(self, intent_id: str, snapshot: FleetSnapshot) -> Refusal | None:
        if (
            not snapshot.operator_present
            or self.timestamp_exceeds_future_skew(snapshot, snapshot.operator_last_seen_ms)
            or snapshot.now_ms - snapshot.operator_last_seen_ms > self.config.operator_timeout_ms
        ):
            return _refusal_id(
                intent_id,
                snapshot,
                RefusalReason.OPERATOR_ABSENT,
                "operator activity is absent or stale",
            )
        return None


def _safety_intent(
    intent_id: str, name: IntentName, snapshot: FleetSnapshot, *, confirmed: bool = True
) -> IntentV1:
    return IntentV1(
        1,
        snapshot.now_ms,
        "intent",
        intent_id,
        None,
        "console",
        "safety",
        name,
        {},
        snapshot.selection,
        Mode.INDOOR,
        confirmed,
    )


def _refusal(
    intent: IntentV1,
    snapshot: FleetSnapshot,
    reason: RefusalReason,
    detail: str,
    aircraft: AircraftState | None = None,
) -> Refusal:
    return _refusal_id(intent.intent_id, snapshot, reason, detail, aircraft)


def _refusal_id(
    intent_id: str,
    snapshot: FleetSnapshot,
    reason: RefusalReason,
    detail: str,
    aircraft: AircraftState | None = None,
    command: Command | None = None,
) -> Refusal:
    return Refusal(
        intent_id,
        snapshot.roster_version,
        None if aircraft is None else aircraft.drone_id,
        None if aircraft is None else aircraft.connection_epoch,
        reason,
        detail,
    )


def _plan_refusal(
    plan: Plan, snapshot: FleetSnapshot, reason: RefusalReason, detail: str
) -> Refusal:
    return Refusal(plan.intent_id, snapshot.roster_version, None, None, reason, detail)


def _command_refusal(
    command: Command, snapshot: FleetSnapshot, reason: RefusalReason, detail: str
) -> Refusal:
    return Refusal(
        command.intent_id,
        snapshot.roster_version,
        command.drone_id,
        command.connection_epoch,
        reason,
        detail,
    )
