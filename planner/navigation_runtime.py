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
    NavigationPermission,
    NavigationPlan,
    NavigationPlanner,
    NavigationRefusal,
    NavigationRequest,
    Pose,
)
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

    def command_specs(self) -> tuple[tuple[int, CommandOperation, dict[str, float]], ...]:
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
    ) -> None:
        self.artifact = artifact
        self.config = config
        self.permission = permission
        self.planner = NavigationPlanner()
        self.control_pins: Mapping[int, object] | None = None
        self.maximum_aircraft: int | None = None

    def prepare(self, intent: IntentV1, snapshot: FleetSnapshot) -> Plan | Refusal:
        try:
            positions = self._positions(snapshot)
            selected = tuple(item for item in positions if item.drone_id in intent.selection)
            route = self.planner.plan(
                NavigationRequest(
                    destination_zone_id=intent.args["zone_id"],
                    roster_version=snapshot.roster_version,
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
                    positions,
                    route_index,
                    cursor,
                    self.config.position_tolerance_m,
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
                if (
                    pin is None
                    or provenance is None
                    or aircraft.connection_epoch != pin.connection_epoch
                    or any(
                        getattr(provenance, name) != getattr(pin, name)
                        for name in (
                            "map_id",
                            "geometry_id",
                            "camera_calibration_id",
                            "body_extrinsics_id",
                            "capture_clock_id",
                            "relay_clock_id",
                            "source_ids",
                        )
                    )
                    or provenance.position_uncertainty_m is None
                    or provenance.position_uncertainty_m > self.config.motion.pose_uncertainty_m
                ):
                    raise ValueError("navigation requires accepted control localization provenance")
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
