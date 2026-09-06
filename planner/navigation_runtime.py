from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from math import isfinite

from planner.models import (
    Command,
    CommandOperation,
    FleetSnapshot,
    FlightState,
    Plan,
    Refusal,
    RefusalReason,
)
from planner.navigation import (
    DronePose,
    MotionConfig,
    NavigationArtifact,
    NavigationLiveState,
    NavigationPermission,
    NavigationPlan,
    NavigationPlanner,
    NavigationRefusal,
    NavigationRequest,
    Pose,
)
from planner.navigation_acceptance import NavigationDispatchAcceptance
from relay.intent_v1 import IntentName, IntentV1


@dataclass(frozen=True, slots=True)
class NavigationExecutionConfig:
    floor_id: str
    motion: MotionConfig
    speed_m_s: float
    position_tolerance_m: float
    position_max_age_ms: int
    minimum_position_quality: float
    segment_timeout_ms: int

    def __post_init__(self) -> None:
        if not isinstance(self.floor_id, str) or not self.floor_id:
            raise ValueError("navigation requires a floor identity")
        for value in (self.speed_m_s, self.position_tolerance_m, self.minimum_position_quality):
            if type(value) not in (float, int) or not isfinite(value) or value <= 0:
                raise ValueError("navigation limits must be positive finite numbers")
        if self.minimum_position_quality > 1:
            raise ValueError("position quality must be a fraction")
        if self.position_tolerance_m > self.motion.tracking_allowance_m:
            raise ValueError("arrival tolerance must fit the reserved tracking allowance")
        for value in (self.position_max_age_ms, self.segment_timeout_ms):
            if type(value) is not int or value <= 0:
                raise ValueError("navigation time limits must be positive integer milliseconds")


@dataclass(frozen=True, slots=True)
class SearchCameraPreparation:
    drone_id: int
    connection_epoch: int
    pitch_deg: float

    def __post_init__(self) -> None:
        if (
            type(self.drone_id) is not int
            or self.drone_id <= 0
            or type(self.connection_epoch) is not int
            or self.connection_epoch < 0
            or not isfinite(self.pitch_deg)
            or self.pitch_deg != -90
        ):
            raise ValueError("search camera preparation requires a nadir pitch and aircraft epoch")


@dataclass(frozen=True, slots=True)
class NavigationExecution:
    route: NavigationPlan
    config: NavigationExecutionConfig
    prepared_at_ms: int
    intent_name: IntentName = IntentName.NAVIGATE
    route_id: str = ""
    phone_authorization: bool = False
    search_camera_preparations: tuple[SearchCameraPreparation, ...] = ()

    def __post_init__(self) -> None:
        if not self.search_camera_preparations:
            return
        selected = {drone.drone_id: drone.connection_epoch for drone in self.route.selected}
        if (
            len(self.search_camera_preparations) != len(self.route.routes)
            or {item.drone_id for item in self.search_camera_preparations} != set(selected)
            or any(
                selected[item.drone_id] != item.connection_epoch
                for item in self.search_camera_preparations
            )
        ):
            raise ValueError("search camera preparations must cover exactly the route aircraft")

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    def command_specs(self) -> tuple[tuple[int, CommandOperation, dict[str, float | str]], ...]:
        result = []
        preparations = {item.drone_id: item for item in self.search_camera_preparations}
        for route in self.route.routes:
            preparation = preparations.get(route.drone.drone_id)
            if preparation is not None:
                result.extend(
                    (
                        (
                            route.drone.drone_id,
                            CommandOperation.SET_GIMBAL_PITCH,
                            {"pitch": preparation.pitch_deg},
                        ),
                        (route.drone.drone_id, CommandOperation.CAMERA_READY, {}),
                    )
                )
            for segment in route.swept_segments:
                result.append(
                    (
                        route.drone.drone_id,
                        CommandOperation.GOTO,
                        {
                            "x": segment.end.x_m,
                            "y": segment.end.y_m,
                            "z": segment.end.z_m,
                            "speed": self.config.speed_m_s,
                            **(
                                {"navigation_route_id": self.route_id}
                                if self.phone_authorization
                                else {}
                            ),
                        },
                    )
                )
            result.append((route.drone.drone_id, CommandOperation.HOVER, {}))
        return tuple(result)

    def matches_commands(self, plan: Plan) -> bool:
        specs = self.command_specs()
        epochs = {drone.drone_id: drone.connection_epoch for drone in self.route.selected}
        return (
            plan.intent_name is self.intent_name
            and plan.roster_version == self.route.roster_version
            and set(plan.selection) == set(epochs)
            and len(specs) == len(plan.commands)
            and all(
                command.drone_id == drone_id
                and command.connection_epoch == epochs[drone_id]
                and command.operation is operation
                and dict(command.parameters) == parameters
                and not command.safety_action
                for command, (drone_id, operation, parameters) in zip(
                    plan.commands, specs, strict=True
                )
            )
        )


class NavigationRuntime:
    def __init__(
        self,
        artifact: Callable[[], NavigationArtifact],
        config: NavigationExecutionConfig,
        permission: NavigationPermission,
        dispatch_acceptance: Callable[
            [NavigationPlan, NavigationArtifact], NavigationDispatchAcceptance | None
        ]
        | None = None,
    ) -> None:
        self.artifact = artifact
        self.config = config
        self.permission = permission
        self.dispatch_acceptance = dispatch_acceptance
        self.planner = NavigationPlanner()
        self.control_pins: Mapping[int, object] | None = None
        self.control_max_fix_age_ms: int | None = None
        self.control_max_position_uncertainty_p95_m: float | None = None
        self.maximum_aircraft: int | None = None
        self.require_phone_authorization = False

    def configure_control_localization(
        self,
        pins: Mapping[int, object],
        *,
        max_fix_age_ms: int,
        max_position_uncertainty_p95_m: float,
    ) -> None:
        if not isinstance(pins, Mapping) or not pins:
            raise ValueError("control localization pins must be a nonempty mapping")
        if type(max_fix_age_ms) is not int or max_fix_age_ms <= 0:
            raise ValueError("control localization max fix age must be positive milliseconds")
        if (
            isinstance(max_position_uncertainty_p95_m, bool)
            or not isinstance(max_position_uncertainty_p95_m, int | float)
            or not isfinite(max_position_uncertainty_p95_m)
            or max_position_uncertainty_p95_m <= 0
        ):
            raise ValueError("control localization P95 uncertainty must be positive and finite")
        self.control_pins = dict(pins)
        self.control_max_fix_age_ms = max_fix_age_ms
        self.control_max_position_uncertainty_p95_m = float(max_position_uncertainty_p95_m)

    def prepare(self, intent: IntentV1, snapshot: FleetSnapshot) -> Plan | Refusal:
        try:
            positions = self._positions(snapshot)
            selected = tuple(item for item in positions if item.drone_id in intent.selection)
            route = self.planner.plan(
                NavigationRequest(
                    destination_zone_id=intent.args["zone_id"],
                    roster_version=snapshot.roster_version,
                    plan_revision=intent.t,
                    selected=selected,
                    all_positions=positions,
                    motion=self.config.motion,
                    permission=self.permission,
                ),
                self.artifact(),
            )
        except (ValueError, KeyError) as error:
            return self._refusal(intent.intent_id, snapshot, str(error))
        if isinstance(route, NavigationRefusal):
            return self._refusal(intent.intent_id, snapshot, f"{route.code}: {route.detail}")
        return self.prepare_route(intent, snapshot, route)

    def prepare_route(
        self,
        intent: IntentV1,
        snapshot: FleetSnapshot,
        route: NavigationPlan,
        *,
        intent_name: IntentName | None = None,
        search_camera_preparations: tuple[SearchCameraPreparation, ...] = (),
    ) -> Plan:
        execution = NavigationExecution(
            route,
            self.config,
            snapshot.now_ms,
            intent.name if intent_name is None else intent_name,
            intent.intent_id,
            self.require_phone_authorization,
            search_camera_preparations,
        )
        epochs = {drone.drone_id: drone.connection_epoch for drone in route.selected}
        commands = tuple(
            Command(
                command_id=f"{intent.intent_id}:navigation:{index}",
                intent_id=intent.intent_id,
                roster_version=snapshot.roster_version,
                drone_id=drone_id,
                connection_epoch=epochs[drone_id],
                operation=operation,
                parameters=parameters,
            )
            for index, (drone_id, operation, parameters) in enumerate(execution.command_specs())
        )
        return Plan(
            plan_id=f"plan:{intent.intent_id}",
            intent_id=intent.intent_id,
            intent_name=execution.intent_name,
            roster_version=snapshot.roster_version,
            selection=tuple(sorted(intent.selection)),
            confirmed=intent.confirm,
            commands=commands,
            navigation=execution,
        )

    def check(
        self,
        plan: Plan,
        command: Command,
        snapshot: FleetSnapshot,
        *,
        completed: bool = False,
        issued_at_ms: int | None = None,
    ) -> Refusal | None:
        execution = plan.navigation
        if execution is None or not execution.matches_commands(plan):
            return self._refusal(plan.intent_id, snapshot, "navigation command shape changed")
        if execution.config != self.config:
            return self._refusal(plan.intent_id, snapshot, "navigation configuration changed")
        route_plan = execution.route
        if route_plan.destination_zone_id not in self.permission.permitted_zone_ids:
            return self._refusal(plan.intent_id, snapshot, "destination permission changed")
        try:
            artifact = self.artifact()
            acceptance = (
                self.dispatch_acceptance(route_plan, artifact) if self.dispatch_acceptance else None
            )
            positions = self._positions(snapshot)
            if (artifact.map_pin, artifact.geometry_pin) != (
                route_plan.map_pin,
                route_plan.geometry_pin,
            ):
                raise ValueError("map or geometry changed; create a new preview")
            if snapshot.roster_version != plan.roster_version or set(snapshot.selection) != set(
                plan.selection
            ):
                raise ValueError("roster or selection changed")
            for drone in route_plan.selected:
                if snapshot.aircraft[drone.drone_id].connection_epoch != drone.connection_epoch:
                    raise ValueError("aircraft connection changed")
            cursor = plan.commands.index(command)
            preparations = {item.drone_id for item in execution.search_camera_preparations}
            for route_index, route in enumerate(route_plan.routes):
                prelude_count = 2 if route.drone.drone_id in preparations else 0
                if cursor < prelude_count:
                    return None
                cursor -= prelude_count
                count = len(route.swept_segments)
                if cursor > count:
                    cursor -= count + 1
                    continue
                aircraft = snapshot.aircraft[command.drone_id]
                if completed or cursor == count:
                    target = (
                        route.arrival_slot.pose
                        if cursor == count
                        else route.swept_segments[cursor].end
                    )
                    if (
                        Pose(
                            aircraft.pose.x, aircraft.pose.y, aircraft.pose.z, self.config.floor_id
                        ).floor_id
                        != target.floor_id
                    ):
                        raise ValueError("arrival floor differs from preview")
                    from math import dist

                    if dist((aircraft.pose.x, aircraft.pose.y, aircraft.pose.z), target.xyz) > (
                        self.config.position_tolerance_m
                    ):
                        raise ValueError("fresh position has not reached the confirmed waypoint")
                    if completed and aircraft.position_last_seen_ms <= (
                        execution.prepared_at_ms if issued_at_ms is None else issued_at_ms
                    ):
                        raise ValueError("arrival needs position evidence captured after dispatch")
                    if (
                        completed
                        and command.operation is CommandOperation.HOVER
                        and (aircraft.flight_state is not FlightState.HOVERING)
                    ):
                        raise ValueError("arrival hold is not confirmed by telemetry")
                    return None
                refusal = self.planner.revalidate(
                    route_plan,
                    artifact,
                    NavigationLiveState(
                        route_plan.roster_version,
                        route_plan.plan_revision,
                        tuple(drone.drone_id for drone in route_plan.selected),
                        positions,
                        route_plan.config,
                        route_plan.permission,
                    ),
                    route_index,
                    cursor,
                    self.config.position_tolerance_m,
                    acceptance=acceptance,
                )
                if refusal is not None:
                    raise ValueError(f"{refusal.code}: {refusal.detail}")
                return None
            raise ValueError("command is outside confirmed route")
        except (ValueError, KeyError) as error:
            return self._refusal(plan.intent_id, snapshot, str(error))

    def _positions(self, snapshot: FleetSnapshot) -> tuple[DronePose, ...]:
        positions = []
        if self.maximum_aircraft is not None and len(snapshot.selection) > self.maximum_aircraft:
            raise ValueError("navigation selection exceeds the accepted aircraft limit")
        for aircraft in snapshot.aircraft.values():
            if not aircraft.airborne and aircraft.drone_id not in snapshot.selection:
                continue
            if aircraft.flight_state not in {FlightState.AIRBORNE, FlightState.HOVERING}:
                raise ValueError("navigation requires stable airborne aircraft")
            age = snapshot.now_ms - aircraft.position_last_seen_ms
            if not 0 <= age <= self.config.position_max_age_ms:
                raise ValueError("navigation position evidence is stale")
            if aircraft.position_quality < self.config.minimum_position_quality:
                raise ValueError("navigation position quality is insufficient")
            if self.control_pins is not None:
                pin = self.control_pins.get(aircraft.drone_id)
                provenance = aircraft.control_provenance
                if pin is None or provenance is None:
                    raise ValueError("navigation requires accepted control localization provenance")
                if (
                    any(
                        getattr(provenance, name) != getattr(pin, name)
                        for name in (
                            "map_id",
                            "geometry_id",
                            "camera_calibration_id",
                            "body_extrinsics_id",
                            "source_ids",
                        )
                    )
                    or provenance.capture_clock_id != pin.clock_mapping.capture_clock_id
                    or provenance.relay_clock_id != pin.clock_mapping.relay_clock_id
                ):
                    raise ValueError("navigation requires accepted control localization provenance")
                if self.control_max_fix_age_ms is not None and (
                    provenance.evaluated_at_relay_ms is None
                    or not 0
                    <= snapshot.now_ms - provenance.evaluated_at_relay_ms
                    <= self.control_max_fix_age_ms
                ):
                    raise ValueError("navigation control localization fix is stale")
                uncertainty_p95_m = getattr(
                    provenance,
                    "position_uncertainty_p95_m",
                    provenance.position_uncertainty_m,
                )
                maximum_uncertainty_p95_m = (
                    self.config.motion.pose_uncertainty_m
                    if self.control_max_position_uncertainty_p95_m is None
                    else self.control_max_position_uncertainty_p95_m
                )
                if uncertainty_p95_m is None or uncertainty_p95_m > maximum_uncertainty_p95_m:
                    raise ValueError("navigation control localization uncertainty is insufficient")
            positions.append(
                DronePose(
                    aircraft.drone_id,
                    aircraft.connection_epoch,
                    Pose(aircraft.pose.x, aircraft.pose.y, aircraft.pose.z, self.config.floor_id),
                )
            )
        return tuple(positions)

    @staticmethod
    def _refusal(intent_id: str, snapshot: FleetSnapshot, detail: str) -> Refusal:
        return Refusal(
            intent_id=intent_id,
            roster_version=snapshot.roster_version,
            drone_id=None,
            connection_epoch=None,
            reason=RefusalReason.INVALID_PLAN,
            detail=detail,
        )
