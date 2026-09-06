"""Runtime state for a confirmed, bounded visual search mission."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from math import dist, isfinite
from threading import RLock
from types import MappingProxyType
from typing import Literal

from perception.object_detection import ProcessedFrameEvent, SightingEvent
from perception.search_events import (
    CameraPolicy,
    CoverageLedger,
    CoverageObservation,
    CoverageTask,
    FramePoseEvidence,
    SearchCandidateEvent,
    SearchMissionIdentity,
    SearchTaskEvent,
)
from planner.models import Command, FleetSnapshot, FlightState, Plan, Refusal, RefusalReason
from planner.navigation import (
    ArrivalSlot,
    ArtifactPin,
    DronePose,
    DroneRoute,
    NavigationArtifact,
    NavigationPermission,
    NavigationPlan,
    NavigationRefusal,
    NavigationRequest,
    Pose,
    Zone,
)
from planner.navigation_runtime import NavigationRuntime, SearchCameraPreparation
from planner.search import (
    CoverageLane,
    DroneSearchAssignment,
    SearchArea,
    SearchDrone,
    SearchPlanner,
    SearchPreview,
    SearchRefusal,
    SearchRequest,
)
from relay.intent_v1 import IntentName, IntentV1


def _identifier(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")


@dataclass(frozen=True, slots=True)
class SearchRuntimeConfig:
    areas: Mapping[str, SearchArea]
    map_pin: ArtifactPin
    camera: CameraPolicy
    calibration_id: str
    source_by_drone: Mapping[int, str]
    permission: NavigationPermission
    mission_version: int = 1
    maximum_drones: int = 4
    mission_cache_limit: int = 32
    floor_z_m: float | None = None
    height_tolerance_m: float = 0.05
    camera_offset_z_m: float | None = None

    def __post_init__(self) -> None:
        _identifier(self.calibration_id, "calibration_id")
        if type(self.mission_version) is not int or self.mission_version < 1:
            raise ValueError("mission_version must be a positive integer")
        if type(self.maximum_drones) is not int or not 1 <= self.maximum_drones <= 4:
            raise ValueError("maximum_drones must be between one and four")
        if type(self.mission_cache_limit) is not int or self.mission_cache_limit < 1:
            raise ValueError("mission_cache_limit must be a positive integer")
        for name in ("floor_z_m", "camera_offset_z_m"):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int | float) or not isfinite(value)
            ):
                raise ValueError(f"{name} must be finite when configured")
        if (
            isinstance(self.height_tolerance_m, bool)
            or not isinstance(self.height_tolerance_m, int | float)
            or not isfinite(self.height_tolerance_m)
            or self.height_tolerance_m < 0
        ):
            raise ValueError("height_tolerance_m must be a nonnegative finite number")
        areas = dict(self.areas)
        if not areas or any(zone_id != area.zone_id for zone_id, area in areas.items()):
            raise ValueError("areas must be keyed by their zone ids")
        sources = dict(self.source_by_drone)
        if any(type(drone_id) is not int or drone_id <= 0 for drone_id in sources):
            raise ValueError("camera source drone ids must be positive integers")
        if len(set(sources.values())) != len(sources):
            raise ValueError("camera sources must be assigned to one drone")
        for source_id in sources.values():
            _identifier(source_id, "camera source")
        object.__setattr__(self, "areas", MappingProxyType(areas))
        object.__setattr__(self, "source_by_drone", MappingProxyType(sources))


@dataclass(frozen=True, slots=True)
class SearchTaskRoute:
    task_id: str
    drone_id: int
    gimbal_command_id: str
    camera_ready_command_id: str
    first_coverage_command_id: str
    terminal_command_id: str


@dataclass(frozen=True, slots=True)
class SearchMissionPreview:
    search: SearchPreview
    plan: Plan
    task_routes: tuple[SearchTaskRoute, ...]


@dataclass(frozen=True, slots=True)
class SearchTaskProgress:
    task_id: str
    state: str
    covered_cells: int
    total_cells: int


@dataclass(frozen=True, slots=True)
class SearchMissionStatus:
    intent_id: str
    state: Literal["prepared", "running", "hold", "cancelled", "incomplete", "covered"]
    tasks: tuple[SearchTaskProgress, ...]
    events: tuple[SearchTaskEvent, ...] = ()


@dataclass(slots=True)
class _Mission:
    preview: SearchMissionPreview
    ledger: CoverageLedger
    prepared_cameras: set[str]
    started: bool = False


class SearchRuntime:
    """Owns confirmed search coverage state; callers retain command dispatch ownership."""

    def __init__(self, config: SearchRuntimeConfig, navigation: NavigationRuntime) -> None:
        self.config = config
        self.navigation = navigation
        self._search = SearchPlanner(navigation.planner)
        self._missions: dict[str, _Mission] = {}
        self._lock = RLock()

    def prepare(self, intent: IntentV1, snapshot: FleetSnapshot) -> SearchMissionPreview | Refusal:
        try:
            if not intent.selection:
                raise ValueError("search requires a selected aircraft")
            area = self.config.areas[intent.args["zone_id"]]
            target_class = intent.args["target_class"]
            if not isinstance(target_class, str):
                raise ValueError("target_class must be text")
            if len(intent.selection) > self.config.maximum_drones:
                raise ValueError("search selection exceeds configured drone limit")
            positions = self.navigation._positions(snapshot)
            selected = tuple(item for item in positions if item.drone_id in intent.selection)
            if len(selected) != len(intent.selection):
                raise ValueError("search selection needs fresh airborne position evidence")
            sources = tuple(
                SearchDrone(drone, self.config.source_by_drone[drone.drone_id])
                for drone in selected
            )
            for drone in selected:
                height_reason = self.camera_height_reason(drone.pose.z_m)
                if height_reason is not None:
                    raise ValueError(height_reason)
            artifact = self.navigation.artifact()
            if artifact.map_pin != self.config.map_pin:
                raise ValueError("search configuration map pin changed")
            search = self._search.plan(
                SearchRequest(
                    SearchMissionIdentity(
                        intent.intent_id, self.config.mission_version, snapshot.roster_version
                    ),
                    area,
                    target_class,
                    snapshot.roster_version,
                    intent.t,
                    sources,
                    positions,
                    self.config.map_pin,
                    self.config.calibration_id,
                    self.config.camera,
                    self.navigation.config.motion,
                    self.config.permission,
                    intent.intent_id,
                ),
                artifact,
            )
            if isinstance(search, SearchRefusal):
                raise ValueError(f"{search.code}: {search.detail}")
            route = self._coverage_plan(search, artifact, positions)
            preparations = tuple(
                SearchCameraPreparation(
                    assignment.drone.drone.drone_id,
                    assignment.task.connection_epoch,
                    self.config.camera.gimbal_pitch_deg,
                )
                for assignment in search.assignments
            )
            plan = self.navigation.prepare_route(
                intent,
                snapshot,
                route,
                intent_name=IntentName.SEARCH,
                search_camera_preparations=preparations,
            )
            task_routes = self._task_routes(search, route, plan)
            preview = SearchMissionPreview(search, plan, task_routes)
        except (KeyError, ValueError) as error:
            return self._refusal(intent.intent_id, snapshot, str(error))
        with self._lock:
            if intent.intent_id in self._missions:
                return self._refusal(
                    intent.intent_id, snapshot, "search intent already has a mission"
                )
            self._evict_retired_missions()
            if len(self._missions) >= self.config.mission_cache_limit:
                return self._refusal(intent.intent_id, snapshot, "search mission cache is full")
            self._missions[intent.intent_id] = _Mission(preview, search.ledger(), set())
        return preview

    def preview(self, intent_id: str) -> SearchMissionPreview:
        with self._lock:
            return self._mission(intent_id).preview

    def active_mission(self, drone_id: int) -> tuple[str, SearchMissionPreview] | None:
        if type(drone_id) is not int or drone_id <= 0:
            raise ValueError("drone_id must be a positive integer")
        with self._lock:
            for intent_id, mission in self._missions.items():
                if self._status(intent_id, mission).state != "running":
                    continue
                if any(
                    assignment.drone.drone.drone_id == drone_id
                    for assignment in mission.preview.search.assignments
                ):
                    return intent_id, mission.preview
        return None

    def current_task(self, intent_id: str, drone_id: int) -> CoverageTask | None:
        if type(drone_id) is not int or drone_id <= 0:
            raise ValueError("drone_id must be a positive integer")
        with self._lock:
            mission = self._mission(intent_id)
            return next(
                (
                    assignment.task
                    for assignment in mission.preview.search.assignments
                    if assignment.drone.drone.drone_id == drone_id
                ),
                None,
            )

    def progress(self, intent_id: str, drone_id: int) -> SearchTaskProgress | None:
        task = self.current_task(intent_id, drone_id)
        if task is None:
            return None
        with self._lock:
            mission = self._mission(intent_id)
            covered, total = mission.ledger.progress(task.task_id)
            return SearchTaskProgress(
                task.task_id, mission.ledger.task_state(task.task_id), covered, total
            )

    def start(self, intent_id: str, snapshot: FleetSnapshot) -> SearchMissionStatus:
        with self._lock:
            mission = self._mission(intent_id)
            if not mission.started:
                selected = {
                    assignment.drone.drone.drone_id
                    for assignment in mission.preview.search.assignments
                }
                for other_intent_id, other in self._missions.items():
                    if (
                        other_intent_id == intent_id
                        or self._status(other_intent_id, other).state != "running"
                    ):
                        continue
                    if selected.intersection(
                        assignment.drone.drone.drone_id
                        for assignment in other.preview.search.assignments
                    ):
                        raise ValueError("a selected drone already has a running search mission")
                mission.started = True
            return self._status(intent_id, mission)

    def on_command(
        self, intent_id: str, command_id: str, snapshot: FleetSnapshot
    ) -> SearchMissionStatus:
        with self._lock:
            mission = self._mission(intent_id)
            if not mission.started:
                return self._status(intent_id, mission)
            events: list[SearchTaskEvent] = []
            for route in mission.preview.task_routes:
                if route.gimbal_command_id == command_id:
                    mission.prepared_cameras.add(f"gimbal:{route.task_id}")
                if (
                    route.camera_ready_command_id == command_id
                    and f"gimbal:{route.task_id}" in mission.prepared_cameras
                ):
                    mission.prepared_cameras.add(route.task_id)
                if route.first_coverage_command_id == command_id:
                    event = self._activate_if_ready(mission, route, snapshot)
                    if event is not None:
                        events.append(event)
                if route.terminal_command_id == command_id:
                    state = mission.ledger.task_state(route.task_id)
                    covered, total = mission.ledger.progress(route.task_id)
                    if state == "active" and covered < total:
                        events.append(
                            mission.ledger.mark_incomplete(
                                route.task_id, "route_finished_without_coverage"
                            )
                        )
            return self._status(intent_id, mission, tuple(events))

    def check(
        self,
        plan: Plan,
        command: Command,
        snapshot: FleetSnapshot,
        *,
        completed: bool = False,
        issued_at_ms: int | None = None,
    ) -> Refusal | None:
        """Revalidate the frozen search route before delegating its command cursor."""
        try:
            mission = self._mission(plan.intent_id)
            if plan != mission.preview.plan:
                raise ValueError("search plan differs from its confirmed preview")
            execution = plan.navigation
            if execution is None:
                raise ValueError("search plan has no navigation route")
            route = execution.route
            artifact = self.navigation.artifact()
            positions = self.navigation._positions(snapshot)
            actual_roster = {(item.drone_id, item.connection_epoch) for item in positions}
            frozen_roster = {(item.drone_id, item.connection_epoch) for item in route.roster}
            if not route.roster or frozen_roster != actual_roster:
                raise ValueError("search obstacle roster changed")
            if (
                artifact.map_pin != route.map_pin
                or artifact.geometry_pin != route.geometry_pin
                or artifact.navigation_pin != route.navigation_pin
                or artifact.evidence != route.evidence
            ):
                raise ValueError("search navigation artifact changed")
        except (KeyError, ValueError) as error:
            return self._refusal(plan.intent_id, snapshot, str(error))
        return self.navigation.check(
            plan, command, snapshot, completed=completed, issued_at_ms=issued_at_ms
        )

    def observe_processed_frame(
        self, intent_id: str, event: ProcessedFrameEvent, pose: FramePoseEvidence, *, now_s: float
    ) -> CoverageObservation:
        with self._lock:
            mission = self._mission(intent_id)
            if not mission.started:
                return CoverageObservation(False, "mission_not_started", ())
            height_reason = self.camera_height_reason(pose.pose.z_m)
            if height_reason is not None:
                return CoverageObservation(False, height_reason, ())
            return mission.ledger.observe_processed(event, pose, now_s)

    def camera_height_reason(self, body_z_m: float) -> str | None:
        if self.config.floor_z_m is None or self.config.camera_offset_z_m is None:
            return "camera_height_unverified"
        camera_z_m = body_z_m + self.config.camera_offset_z_m
        expected_z_m = self.config.floor_z_m + self.config.camera.height_agl_m
        if abs(camera_z_m - expected_z_m) > self.config.height_tolerance_m:
            return "camera_height_mismatch"
        return None

    def observe_sighting(self, intent_id: str, event: SightingEvent) -> SearchCandidateEvent | None:
        with self._lock:
            mission = self._mission(intent_id)
            return mission.ledger.observe_sighting(event)

    def hold(self, intent_id: str, reason: str) -> SearchMissionStatus:
        _identifier(reason, "hold reason")
        with self._lock:
            mission = self._mission(intent_id)
            return self._status(intent_id, mission, mission.ledger.hold(reason))

    def cancel(self, intent_id: str, reason: str) -> SearchMissionStatus:
        _identifier(reason, "cancel reason")
        with self._lock:
            mission = self._mission(intent_id)
            return self._status(intent_id, mission, mission.ledger.cancel(reason))

    def status(self, intent_id: str) -> SearchMissionStatus:
        with self._lock:
            mission = self._mission(intent_id)
            return self._status(intent_id, mission)

    def _coverage_plan(
        self,
        preview: SearchPreview,
        artifact: NavigationArtifact,
        positions: tuple[DronePose, ...],
    ) -> NavigationPlan:
        routes: list[DroneRoute] = []
        current = {item.drone_id: item for item in positions}
        for assignment in preview.assignments:
            route = self._extend_assignment(assignment, preview, artifact, tuple(current.values()))
            routes.append(route)
            current[route.drone.drone_id] = replace(route.drone, pose=route.arrival_slot.pose)
        return NavigationPlan(
            artifact.map_pin,
            artifact.geometry_pin,
            artifact.navigation_pin,
            artifact.evidence,
            self.navigation.config.motion,
            self.config.permission,
            preview.roster_version,
            preview.plan_revision,
            preview.zone.zone_id,
            tuple(route.drone for route in routes),
            tuple(sorted(positions, key=lambda drone: drone.drone_id)),
            tuple(route.arrival_slot for route in routes),
            tuple(routes),
            preview.execution_order,
        )

    def _extend_assignment(
        self,
        assignment: DroneSearchAssignment,
        preview: SearchPreview,
        artifact: NavigationArtifact,
        positions: tuple[DronePose, ...],
    ) -> DroneRoute:
        route = assignment.transit
        waypoints = list(route.waypoints)
        segments = list(route.swept_segments)
        current = replace(route.drone, pose=route.arrival_slot.pose)
        all_positions = {item.drone_id: item for item in positions}
        all_positions[current.drone_id] = current
        for endpoint in self._coverage_endpoints(assignment.lanes):
            if endpoint == current.pose:
                continue
            leg = self._coverage_leg(
                current, endpoint, preview, artifact, tuple(all_positions.values())
            )
            if isinstance(leg, NavigationRefusal):
                raise ValueError(f"coverage route unreachable: {leg.detail}")
            waypoints.extend(leg.waypoints[1:])
            segments.extend(leg.swept_segments)
            current = replace(current, pose=endpoint)
            all_positions[current.drone_id] = current
        return DroneRoute(
            route.drone,
            ArrivalSlot(
                f"{assignment.task.task_id}:complete",
                preview.zone.zone_id,
                current.pose,
                self.navigation.config.motion.swept_radius_m,
                self.navigation.config.motion.swept_half_height_m,
            ),
            tuple(waypoints),
            tuple(segments),
        )

    def _coverage_leg(
        self,
        drone: DronePose,
        endpoint: Pose,
        preview: SearchPreview,
        artifact: NavigationArtifact,
        positions: tuple[DronePose, ...],
    ) -> DroneRoute | NavigationRefusal:
        zone = next(zone for zone in artifact.zones if zone.zone_id == preview.zone.zone_id)
        slot = ArrivalSlot(
            f"{preview.mission.frame_mission_id}:{drone.drone_id}:{len(positions)}",
            zone.zone_id,
            endpoint,
            self.navigation.config.motion.swept_radius_m,
            self.navigation.config.motion.swept_half_height_m,
        )
        overlay = replace(
            artifact,
            zones=tuple(
                Zone(
                    zone.zone_id,
                    zone.floor_id,
                    zone.owner_approved,
                    zone.polygon_xy,
                    zone.z_min_m,
                    zone.z_max_m,
                    (slot,),
                    zone.aliases,
                )
                if candidate.zone_id == zone.zone_id
                else candidate
                for candidate in artifact.zones
            ),
        )
        planned = self.navigation.planner.plan(
            NavigationRequest(
                zone.zone_id,
                preview.roster_version,
                preview.plan_revision,
                (drone,),
                positions,
                self.navigation.config.motion,
                self.config.permission,
            ),
            overlay,
        )
        return planned if isinstance(planned, NavigationRefusal) else planned.routes[0]

    @staticmethod
    def _coverage_endpoints(lanes: tuple[CoverageLane, ...]) -> tuple[Pose, ...]:
        return tuple(cell.pose for lane in lanes for cell in lane.cells)

    def _task_routes(
        self, search: SearchPreview, route: NavigationPlan, plan: Plan
    ) -> tuple[SearchTaskRoute, ...]:
        result = []
        offset = 0
        for assignment, drone_route in zip(search.assignments, route.routes, strict=True):
            first = assignment.task.cells[0].pose
            segment_index = next(
                index
                for index, segment in enumerate(drone_route.swept_segments)
                if segment.end == first
            )
            result.append(
                SearchTaskRoute(
                    assignment.task.task_id,
                    drone_route.drone.drone_id,
                    plan.commands[offset].command_id,
                    plan.commands[offset + 1].command_id,
                    plan.commands[offset + 2 + segment_index].command_id,
                    plan.commands[offset + 2 + len(drone_route.swept_segments)].command_id,
                )
            )
            offset += len(drone_route.swept_segments) + 3
        return tuple(result)

    def _activate_if_ready(
        self, mission: _Mission, route: SearchTaskRoute, snapshot: FleetSnapshot
    ) -> SearchTaskEvent | None:
        task = next(
            assignment.task
            for assignment in mission.preview.search.assignments
            if assignment.task.task_id == route.task_id
        )
        aircraft = snapshot.aircraft.get(route.drone_id)
        if (
            aircraft is None
            or aircraft.connection_epoch != task.connection_epoch
            or route.task_id not in mission.prepared_cameras
            or aircraft.flight_state is not FlightState.HOVERING
            or snapshot.now_ms - aircraft.position_last_seen_ms
            > self.navigation.config.position_max_age_ms
            or dist((aircraft.pose.x, aircraft.pose.y, aircraft.pose.z), task.cells[0].pose.xyz)
            > self.navigation.config.position_tolerance_m
        ):
            return None
        if mission.ledger.task_state(task.task_id) != "pending":
            return None
        return mission.ledger.activate(task.task_id)

    def _status(
        self,
        intent_id: str,
        mission: _Mission,
        events: tuple[SearchTaskEvent, ...] = (),
    ) -> SearchMissionStatus:
        tasks = tuple(
            SearchTaskProgress(
                assignment.task.task_id,
                mission.ledger.task_state(assignment.task.task_id),
                *mission.ledger.progress(assignment.task.task_id),
            )
            for assignment in mission.preview.search.assignments
        )
        states = {task.state for task in tasks}
        if states == {"covered"}:
            state: Literal["prepared", "running", "hold", "cancelled", "incomplete", "covered"] = (
                "covered"
            )
        elif "cancel" in states:
            state = "cancelled"
        elif "hold" in states:
            state = "hold"
        elif "incomplete" in states:
            state = "incomplete"
        elif mission.started:
            state = "running"
        else:
            state = "prepared"
        return SearchMissionStatus(intent_id, state, tasks, events)

    def _evict_retired_missions(self) -> None:
        while len(self._missions) >= self.config.mission_cache_limit:
            retired = next(
                (
                    intent_id
                    for intent_id, mission in self._missions.items()
                    if self._status(intent_id, mission).state != "running"
                ),
                None,
            )
            if retired is None:
                return
            del self._missions[retired]

    def _mission(self, intent_id: str) -> _Mission:
        try:
            return self._missions[intent_id]
        except KeyError as error:
            raise ValueError("search mission is unknown") from error

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
