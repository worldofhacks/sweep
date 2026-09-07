from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from math import dist

import numpy as np

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
from planner.navigation_authorization import NavigationApproval, content_digest
from planner.navigation_contracts import (
    MAX_AIRCRAFT,
    finite_number,
    integer,
    normalized_text,
    sha256_digest,
)
from relay.capabilities import CapabilityProfile
from relay.control_localization import ControlLocalizationPins, ControlPose
from relay.intent_v1 import IntentName, IntentV1


@dataclass(frozen=True, slots=True)
class NavigationFrame:
    drone_id: int
    transform_id: str
    world_from_enu: tuple[tuple[float, ...], ...]
    control_pins: ControlLocalizationPins | None = None
    camera_calibration_sha256: str | None = None
    body_extrinsics_sha256: str | None = None
    world_transform_sha256: str | None = None

    def __post_init__(self) -> None:
        integer(self.drone_id, "drone_id", minimum=1)
        normalized_text(self.transform_id, "transform_id")
        for name in (
            "camera_calibration_sha256",
            "body_extrinsics_sha256",
            "world_transform_sha256",
        ):
            if getattr(self, name) is not None:
                sha256_digest(getattr(self, name), name)
        matrix = np.asarray(self.world_from_enu, dtype=float)
        if (
            not isinstance(self.world_from_enu, tuple)
            or any(not isinstance(row, tuple) for row in self.world_from_enu)
            or matrix.shape != (4, 4)
            or not np.isfinite(matrix).all()
            or not np.allclose(matrix[3], (0, 0, 0, 1), atol=1e-9, rtol=0)
            or not np.allclose(matrix[:3, :3].T @ matrix[:3, :3], np.eye(3), atol=1e-9, rtol=0)
            or not np.isclose(np.linalg.det(matrix[:3, :3]), 1, atol=1e-9, rtol=0)
        ):
            raise ValueError("navigation frame must be an immutable proper rigid transform")
        for row in self.world_from_enu:
            for value in row:
                finite_number(value, "world_from_enu coordinate")
        if self.control_pins is not None and (
            not isinstance(self.control_pins, ControlLocalizationPins)
            or self.control_pins.drone_id != self.drone_id
        ):
            raise ValueError("navigation frame control pins must match its aircraft")

    def world(self, xyz: tuple[float, float, float], floor_id: str) -> Pose:
        result = np.asarray(self.world_from_enu) @ np.asarray((*xyz, 1.0))
        return Pose(*result[:3], floor_id)

    def enu(self, pose: Pose) -> tuple[float, float, float]:
        matrix = np.asarray(self.world_from_enu)
        return tuple(
            float(value) for value in matrix[:3, :3].T @ (np.asarray(pose.xyz) - matrix[:3, 3])
        )


@dataclass(frozen=True, slots=True)
class NavigationExecutionConfig:
    floor_id: str
    motion: MotionConfig
    speed_m_s: float
    position_tolerance_m: float
    position_max_age_ms: int
    minimum_position_quality: float
    segment_timeout_ms: int
    frames: tuple[NavigationFrame, ...]
    wire_config_sha256: str | None = None
    line_zone_id: str | None = None
    max_aircraft: int = 4

    def __post_init__(self) -> None:
        normalized_text(self.floor_id, "floor_id")
        if self.wire_config_sha256 is not None:
            sha256_digest(self.wire_config_sha256, "wire_config_sha256")
        if self.line_zone_id is not None:
            normalized_text(self.line_zone_id, "line_zone_id")
        if not isinstance(self.motion, MotionConfig):
            raise ValueError("navigation motion must use MotionConfig")
        for name in ("speed_m_s", "position_tolerance_m", "minimum_position_quality"):
            finite_number(getattr(self, name), name, positive=True)
        if (
            self.minimum_position_quality > 1
            or self.position_tolerance_m > self.motion.tracking_allowance_m
        ):
            raise ValueError("navigation quality or arrival tolerance is outside its envelope")
        for name in ("position_max_age_ms", "segment_timeout_ms"):
            integer(getattr(self, name), name, minimum=1)
        integer(self.max_aircraft, "max_aircraft", minimum=1)
        if self.max_aircraft > MAX_AIRCRAFT:
            raise ValueError(f"max_aircraft exceeds the {MAX_AIRCRAFT}-aircraft resource limit")
        if (
            not isinstance(self.frames, tuple)
            or not 1 <= len(self.frames) <= self.max_aircraft
            or any(not isinstance(frame, NavigationFrame) for frame in self.frames)
            or len({frame.drone_id for frame in self.frames}) != len(self.frames)
        ):
            raise ValueError("navigation requires unique aircraft frames within max_aircraft")

    def frame(self, drone_id: int) -> NavigationFrame:
        for frame in self.frames:
            if frame.drone_id == drone_id:
                return frame
        raise ValueError("navigation has no measured frame for aircraft")


@dataclass(frozen=True, slots=True)
class NavigationExecution:
    route: NavigationPlan
    config: NavigationExecutionConfig
    prepared_at_ms: int
    intent_name: IntentName
    route_id: str
    approval_id: str
    configuration_sha256: str

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["route"]["permission"]["permitted_zone_ids"] = sorted(
            self.route.permission.permitted_zone_ids
        )
        value["intent_name"] = self.intent_name.value
        return value

    def command_specs(self) -> tuple[tuple[int, CommandOperation, dict[str, object]], ...]:
        result = []
        for route in self.route.routes:
            frame = self.config.frame(route.drone.drone_id)
            for segment in route.swept_segments:
                x, y, z = frame.enu(segment.end)
                result.append(
                    (
                        route.drone.drone_id,
                        CommandOperation.GOTO,
                        {
                            "x": x,
                            "y": y,
                            "z": z,
                            "speed": self.config.speed_m_s,
                            "navigation_route_id": self.route_id,
                        },
                    )
                )
            result.append((route.drone.drone_id, CommandOperation.HOVER, {}))
        return tuple(result)

    def matches_commands(self, plan: Plan) -> bool:
        specs = self.command_specs()
        epochs = {drone.drone_id: drone.connection_epoch for drone in self.route.selected}
        return (
            self.intent_name in {IntentName.COME_HOME, IntentName.FORMATION_SET}
            and plan.intent_name is self.intent_name
            and plan.formation_update
            == ("line" if self.intent_name is IntentName.FORMATION_SET else None)
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


def navigation_configuration_digest(
    artifact: NavigationArtifact,
    config: NavigationExecutionConfig,
    permission: NavigationPermission,
    home_zone_id: str,
) -> str:
    return content_digest(
        {
            "map": asdict(artifact.map_pin),
            "geometry": asdict(artifact.geometry_pin),
            "navigation": asdict(artifact.navigation_pin),
            "config": asdict(config),
            "permitted_zone_ids": sorted(permission.permitted_zone_ids),
            "home_zone_id": home_zone_id,
        }
    )


def navigation_capability_profile(
    base: CapabilityProfile, config: NavigationExecutionConfig
) -> CapabilityProfile:
    if config.line_zone_id is None or base.supports(IntentName.FORMATION_SET):
        return base
    return CapabilityProfile(
        f"{base.name[:50]}_mapped_line", base.enabled_intent_names | {IntentName.FORMATION_SET}
    )


class NavigationRuntime:
    def __init__(
        self,
        artifact: Callable[[], NavigationArtifact],
        config: NavigationExecutionConfig,
        permission: NavigationPermission,
        approval: NavigationApproval,
        *,
        session: str,
        home_zone_id: str,
        control_pose: Callable[[int], ControlPose | None] | None = None,
    ) -> None:
        if not isinstance(approval, NavigationApproval):
            raise ValueError("navigation requires a verified external approval")
        self.artifact = artifact
        self.config = config
        self.permission = permission
        self.approval = approval
        self.session = session
        self.home_zone_id = home_zone_id
        self.control_pose = control_pose
        self.planner = NavigationPlanner()
        if approval.mode == "flight" and (
            control_pose is None or any(frame.control_pins is None for frame in config.frames)
        ):
            raise ValueError(
                "flight navigation requires retained control poses and measured source pins"
            )

    def capability_profile(self, base: CapabilityProfile) -> CapabilityProfile:
        return navigation_capability_profile(base, self.config)

    def _approved_artifact(self) -> NavigationArtifact:
        artifact = self.artifact()
        # Arrival permission is external; the source geometry remains preview evidence.
        return replace(
            artifact,
            zones=tuple(
                replace(zone, owner_approved=zone.zone_id in self.permission.permitted_zone_ids)
                for zone in artifact.zones
            ),
        )

    def _validate(self, snapshot: FleetSnapshot) -> NavigationArtifact:
        artifact = self._approved_artifact()
        self.approval.check(
            session=self.session,
            configuration_sha256=navigation_configuration_digest(
                artifact, self.config, self.permission, self.home_zone_id
            ),
            now_ms=snapshot.now_ms,
            epochs=tuple(
                sorted(
                    (aircraft.drone_id, aircraft.connection_epoch)
                    for aircraft in snapshot.aircraft.values()
                    if aircraft.airborne or aircraft.drone_id in snapshot.selection
                )
            ),
        )
        if self.approval.mode == "flight" and artifact.evidence.evidence_kind != "measured":
            raise ValueError("flight navigation requires measured geometry")
        return artifact

    def prepare(self, intent: IntentV1, snapshot: FleetSnapshot) -> Plan | Refusal:
        try:
            artifact = self._validate(snapshot)
            destination = self.home_zone_id
            if intent.name is IntentName.FORMATION_SET and intent.args.get("name") == "line":
                destination = self.config.line_zone_id
                zone = next((zone for zone in artifact.zones if zone.zone_id == destination), None)
                if zone is None or len(intent.selection) != 2 or len(zone.arrival_slots) != 2:
                    raise ValueError("line formation requires two aircraft and two approved slots")
                first, second = (slot.pose for slot in zone.arrival_slots)
                if (
                    first.z_m != second.z_m
                    or abs(dist(first.xyz, second.xyz) - snapshot.spacing) > 1e-6
                ):
                    raise ValueError(
                        "line slot height or spacing differs from the selected formation"
                    )
            elif intent.name is not IntentName.COME_HOME:
                raise ValueError("navigation runtime has no configured route for this intent")
            positions = self._positions(snapshot)
            route = self.planner.plan(
                NavigationRequest(
                    destination,
                    snapshot.roster_version,
                    intent.t,
                    tuple(item for item in positions if item.drone_id in intent.selection),
                    positions,
                    self.config.motion,
                    self.permission,
                ),
                artifact,
            )
            if isinstance(route, NavigationRefusal):
                raise ValueError(f"{route.code}: {route.detail}")
            return self.prepare_route(intent, snapshot, route)
        except (ValueError, KeyError) as error:
            return self._refusal(intent.intent_id, snapshot, str(error))

    def prepare_route(
        self, intent: IntentV1, snapshot: FleetSnapshot, route: NavigationPlan
    ) -> Plan:
        artifact = self._validate(snapshot)
        execution = NavigationExecution(
            route,
            self.config,
            snapshot.now_ms,
            intent.name,
            intent.intent_id,
            self.approval.approval_id,
            navigation_configuration_digest(
                artifact, self.config, self.permission, self.home_zone_id
            ),
        )
        epochs = {drone.drone_id: drone.connection_epoch for drone in route.selected}
        commands = tuple(
            Command(
                f"{intent.intent_id}:navigation:{index}",
                intent.intent_id,
                snapshot.roster_version,
                drone_id,
                epochs[drone_id],
                operation,
                parameters,
            )
            for index, (drone_id, operation, parameters) in enumerate(execution.command_specs())
        )
        return Plan(
            f"plan:{intent.intent_id}",
            intent.intent_id,
            intent.name,
            snapshot.roster_version,
            tuple(sorted(intent.selection)),
            intent.confirm,
            commands,
            formation_update="line" if intent.name is IntentName.FORMATION_SET else None,
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
        _tracking_pose: ControlPose | None = None,
    ) -> Refusal | None:
        try:
            execution = plan.navigation
            if not isinstance(execution, NavigationExecution) or not execution.matches_commands(
                plan
            ):
                raise ValueError("navigation command shape changed")
            artifact = self._validate(snapshot)
            if (
                execution.config != self.config
                or execution.approval_id != self.approval.approval_id
                or execution.configuration_sha256 != self.approval.configuration_sha256
            ):
                raise ValueError("navigation configuration or approval changed")
            positions = self._positions(snapshot, _tracking_pose)
            route_plan = execution.route
            destination = self.home_zone_id
            if plan.intent_name is IntentName.FORMATION_SET:
                destination = self.config.line_zone_id
                slots = route_plan.arrival_slots
                if (
                    len(slots) != 2
                    or slots[0].pose.z_m != slots[1].pose.z_m
                    or abs(dist(slots[0].pose.xyz, slots[1].pose.xyz) - snapshot.spacing) > 1e-6
                ):
                    raise ValueError("line formation spacing or altitude changed")
            if route_plan.destination_zone_id != destination:
                raise ValueError("navigation destination differs from the configured operation")
            if route_plan.permission != self.permission or route_plan.config != self.config.motion:
                raise ValueError("navigation permission or motion envelope changed")
            if (
                tuple(sorted(snapshot.selection)) != tuple(sorted(plan.selection))
                or snapshot.roster_version != plan.roster_version
            ):
                raise ValueError("navigation roster or selection changed")
            cursor = plan.commands.index(command)
            for route_index, route in enumerate(route_plan.routes):
                count = len(route.swept_segments)
                if cursor > count:
                    cursor -= count + 1
                    continue
                active = next(item for item in positions if item.drone_id == command.drone_id)
                arrival = completed or cursor == count
                segment_index = min(cursor, count - 1)
                if _tracking_pose is not None:
                    if completed or command.operation is not CommandOperation.GOTO:
                        raise ValueError("tracking evidence requires an active goto segment")
                    segment = route.swept_segments[segment_index]
                    start, end = np.asarray(segment.start.xyz), np.asarray(segment.end.xyz)
                    vector = end - start
                    length_squared = float(vector @ vector)
                    progress = (
                        0.0
                        if length_squared == 0
                        else float(
                            np.clip(
                                (np.asarray(active.pose.xyz) - start) @ vector / length_squared,
                                0,
                                1,
                            )
                        )
                    )
                    if (
                        dist(active.pose.xyz, start + progress * vector)
                        > self.config.motion.tracking_allowance_m
                    ):
                        raise ValueError("navigation position left the frozen tracking corridor")
                    target = active.pose
                elif arrival:
                    target = route.swept_segments[segment_index].end
                    if (
                        active.pose.floor_id != target.floor_id
                        or dist(active.pose.xyz, target.xyz) > self.config.position_tolerance_m
                    ):
                        raise ValueError("fresh position has not reached the confirmed waypoint")
                    if completed:
                        seen = self._pose_time(snapshot, command.drone_id)
                        if (
                            issued_at_ms is None
                            or seen <= issued_at_ms
                            or not 0
                            <= snapshot.now_ms - issued_at_ms
                            <= self.config.segment_timeout_ms
                        ):
                            raise ValueError(
                                "arrival needs timely position evidence captured after dispatch"
                            )
                        if (
                            command.operation is CommandOperation.HOVER
                            and snapshot.aircraft[command.drone_id].flight_state
                            is not FlightState.HOVERING
                        ):
                            raise ValueError("arrival hold is not confirmed by telemetry")
                if arrival or _tracking_pose is not None:
                    segments = route.swept_segments
                    adjusted = replace(segments[segment_index], start=target)
                    adjusted_segments = (
                        *segments[:segment_index],
                        adjusted,
                        *segments[segment_index + 1 :],
                    )
                    points = (
                        target,
                        *(segment.end for segment in adjusted_segments[segment_index:]),
                    )
                    adjusted_route = replace(
                        route,
                        drone=replace(route.drone, pose=target),
                        waypoints=points,
                        swept_segments=adjusted_segments[segment_index:],
                    )
                    selected = tuple(
                        adjusted_route.drone if item.drone_id == command.drone_id else item
                        for item in route_plan.selected
                    )
                    roster = tuple(
                        adjusted_route.drone if item.drone_id == command.drone_id else item
                        for item in route_plan.roster
                    )
                    checked_plan = replace(
                        route_plan,
                        selected=selected,
                        roster=roster,
                        routes=tuple(
                            adjusted_route if index == route_index else item
                            for index, item in enumerate(route_plan.routes)
                        ),
                    )
                    segment_index = 0
                else:
                    checked_plan = route_plan
                refusal = self.planner.revalidate(
                    checked_plan,
                    artifact,
                    NavigationLiveState(
                        snapshot.roster_version,
                        route_plan.plan_revision,
                        tuple(snapshot.selection),
                        positions,
                        self.config.motion,
                        self.permission,
                    ),
                    route_index,
                    segment_index,
                    self.config.position_tolerance_m,
                )
                if refusal is not None and refusal.code != "artifact_not_dispatchable":
                    raise ValueError(f"{refusal.code}: {refusal.detail}")
                return None
            raise ValueError("command is outside confirmed route")
        except (ValueError, KeyError, StopIteration) as error:
            return self._refusal(
                plan.intent_id, snapshot, str(error) or "navigation pose is missing"
            )

    def _pose_time(self, snapshot: FleetSnapshot, drone_id: int) -> int:
        if self.approval.mode == "simulation":
            return snapshot.aircraft[drone_id].position_last_seen_ms
        pose = self.control_pose(drone_id)
        if pose is None:
            raise ValueError("navigation control pose is missing")
        return pose.pose_time_ms

    def check_tracking(
        self, plan: Plan, command: Command, snapshot: FleetSnapshot, pose: ControlPose
    ) -> Refusal | None:
        if self.control_pose is None or self.control_pose(command.drone_id) != pose:
            return self._refusal(
                plan.intent_id, snapshot, "tracking pose is not retained by this session"
            )
        return self.check(plan, command, snapshot, _tracking_pose=pose)

    def _positions(
        self, snapshot: FleetSnapshot, override: ControlPose | None = None
    ) -> tuple[DronePose, ...]:
        positions = []
        for aircraft in snapshot.aircraft.values():
            if not aircraft.airborne and aircraft.drone_id not in snapshot.selection:
                continue
            if aircraft.flight_state not in {FlightState.AIRBORNE, FlightState.HOVERING}:
                raise ValueError("navigation requires stable airborne aircraft")
            frame = self.config.frame(aircraft.drone_id)
            if (
                not 0
                <= snapshot.now_ms - aircraft.position_last_seen_ms
                <= self.config.position_max_age_ms
                or aircraft.position_quality < self.config.minimum_position_quality
            ):
                raise ValueError("navigation position evidence is stale or low quality")
            xyz = (aircraft.pose.x, aircraft.pose.y, aircraft.pose.z)
            if self.approval.mode == "flight":
                pose = (
                    override
                    if override is not None and override.drone_id == aircraft.drone_id
                    else self.control_pose(aircraft.drone_id)
                )
                pin = frame.control_pins
                if (
                    pose is None
                    or pose.session != self.session
                    or pose.connection_epoch != aircraft.connection_epoch
                    or pose.drone_id != aircraft.drone_id
                    or pose.status != "ready"
                ):
                    raise ValueError("navigation requires a ready current-epoch control pose")
                if any(
                    getattr(pose, name) != getattr(pin, name)
                    for name in (
                        "map_id",
                        "geometry_id",
                        "camera_calibration_id",
                        "body_extrinsics_id",
                    )
                ):
                    raise ValueError("navigation control pose provenance changed")
                error_ms = pin.clock_mapping.max_error_ms
                if not 0 <= snapshot.now_ms - pose.t <= self.config.position_max_age_ms or any(
                    not error_ms
                    <= snapshot.now_ms - timestamp
                    <= self.config.position_max_age_ms - error_ms
                    for timestamp in (pose.pose_time_ms, pose.fix_time_ms)
                ):
                    raise ValueError("navigation control pose or fix is stale or future")
                if pose.position_uncertainty_mm / 1000 > self.config.motion.pose_uncertainty_m:
                    raise ValueError(
                        "navigation control pose uncertainty exceeds reserved clearance"
                    )
                control_xyz = (pose.x_mm / 1000, pose.y_mm / 1000, pose.z_mm / 1000)
                if dist(xyz, control_xyz) > self.config.position_tolerance_m:
                    raise ValueError("navigation control pose disagrees with adapter ENU telemetry")
                xyz = control_xyz
            positions.append(
                DronePose(
                    aircraft.drone_id,
                    aircraft.connection_epoch,
                    frame.world(xyz, self.config.floor_id),
                )
            )
        return tuple(positions)

    @staticmethod
    def _refusal(intent_id: str, snapshot: FleetSnapshot, detail: str) -> Refusal:
        return Refusal(
            intent_id, snapshot.roster_version, None, None, RefusalReason.INVALID_PLAN, detail
        )
