"""Prepares and starts confirmed visual-search previews over guarded navigation."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Literal

from adapters.dispatch import AdapterDispatcher
from perception.object_detection import LiveDetectionWorker, ProcessedFrameEvent, SightingEvent
from perception.search_events import (
    CameraPolicy,
    CoverageLedger,
    CoverageObservation,
    FramePoseEvidence,
    SearchCandidateEvent,
    SearchMissionIdentity,
    SearchTaskEvent,
)
from planner.models import (
    ExecutionResult,
    FleetSnapshot,
    LifecycleStatus,
    Plan,
    Refusal,
    RefusalReason,
)
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
from planner.navigation_runtime import NavigationRuntime
from planner.search import (
    CoverageLane,
    SearchArea,
    SearchDrone,
    SearchPlanner,
    SearchPreview,
    SearchRefusal,
    SearchRequest,
)
from relay.intent_v1 import IntentV1


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

    def __post_init__(self) -> None:
        if not isinstance(self.calibration_id, str) or not self.calibration_id.strip():
            raise ValueError("search camera calibration id must be nonempty")
        if type(self.mission_version) is not int or self.mission_version < 1:
            raise ValueError("search mission version must be positive")
        if type(self.maximum_drones) is not int or not 1 <= self.maximum_drones <= 4:
            raise ValueError("search maximum drones must be between one and four")
        areas = dict(self.areas)
        sources = dict(self.source_by_drone)
        if not areas or any(zone_id != area.zone_id for zone_id, area in areas.items()):
            raise ValueError("search areas must be keyed by their zone ids")
        if any(type(drone_id) is not int or drone_id <= 0 for drone_id in sources):
            raise ValueError("search camera drone ids must be positive integers")
        if any(not isinstance(source, str) or not source.strip() for source in sources.values()):
            raise ValueError("search camera sources must be nonempty")
        if len(set(sources.values())) != len(sources):
            raise ValueError("search camera sources must be unique")
        object.__setattr__(self, "areas", MappingProxyType(areas))
        object.__setattr__(self, "source_by_drone", MappingProxyType(sources))


@dataclass(frozen=True, slots=True)
class SearchMissionPreview:
    search: SearchPreview
    plan: Plan


@dataclass(frozen=True, slots=True)
class SearchMissionStatus:
    intent_id: str
    state: Literal["prepared", "running", "hold", "cancelled", "covered", "incomplete"]
    events: tuple[SearchTaskEvent, ...] = ()


@dataclass(slots=True)
class _Mission:
    preview: SearchMissionPreview
    ledger: CoverageLedger
    started: bool = False


class SearchRuntime:
    def __init__(self, config: SearchRuntimeConfig, navigation: NavigationRuntime) -> None:
        self.config = config
        self.navigation = navigation
        self._planner = SearchPlanner(navigation.planner)
        self._missions: dict[str, _Mission] = {}

    def prepare(self, intent: IntentV1, snapshot: FleetSnapshot) -> SearchMissionPreview | Refusal:
        try:
            if not intent.confirm or not intent.selection:
                raise ValueError("search requires confirmation and selected aircraft")
            if len(intent.selection) > self.config.maximum_drones:
                raise ValueError("search selection exceeds configured drone limit")
            zone_id = intent.args["zone_id"]
            target_class = intent.args["target_class"]
            if not isinstance(zone_id, str) or not isinstance(target_class, str):
                raise ValueError("search requires text zone_id and target_class")
            area = self.config.areas[zone_id]
            positions = self.navigation._positions(snapshot)
            selected = tuple(item for item in positions if item.drone_id in intent.selection)
            if len(selected) != len(intent.selection):
                raise ValueError("search selection needs fresh airborne position evidence")
            sources = tuple(
                SearchDrone(drone, self.config.source_by_drone[drone.drone_id])
                for drone in selected
            )
            artifact = self.navigation.artifact()
            if artifact.map_pin != self.config.map_pin:
                raise ValueError("search configuration map pin changed")
            search = self._planner.plan(
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
            plan = self.navigation.prepare_route(intent, snapshot, route)
        except (KeyError, ValueError) as error:
            return self._refusal(intent.intent_id, snapshot, str(error))
        if intent.intent_id in self._missions:
            return self._refusal(intent.intent_id, snapshot, "search intent already has a mission")
        preview = SearchMissionPreview(search, plan)
        self._missions[intent.intent_id] = _Mission(preview, search.ledger())
        return preview

    def start(self, intent_id: str) -> SearchMissionStatus:
        mission = self._mission(intent_id)
        mission.started = True
        return self.status(intent_id)

    def execute(
        self,
        intent_id: str,
        dispatcher: AdapterDispatcher,
        snapshot: FleetSnapshot,
        *,
        current_snapshot: Callable[[], FleetSnapshot] | None = None,
    ) -> ExecutionResult:
        """Dispatch only the exact route frozen into the confirmed preview."""
        mission = self._mission(intent_id)
        self.start(intent_id)
        result = dispatcher.dispatch(
            mission.preview.plan, snapshot, current_snapshot=current_snapshot
        )
        if result.status is not LifecycleStatus.COMPLETED:
            reason = result.refusal.detail if result.refusal else "route_execution_failed"
            self.hold(intent_id, reason)
            return result
        latest = current_snapshot() if current_snapshot else snapshot
        events = self._activate_arrived_tasks(mission, latest)
        if not events:
            self.hold(intent_id, "fresh_coverage_arrival_unverified")
        return result

    def observe_processed_frame(
        self, intent_id: str, event: ProcessedFrameEvent, pose: FramePoseEvidence, *, now_s: float
    ) -> CoverageObservation:
        mission = self._mission(intent_id)
        if not mission.started:
            return CoverageObservation(False, "mission_not_started", ())
        return mission.ledger.observe_processed(event, pose, now_s)

    def observe_sighting(self, intent_id: str, event: SightingEvent) -> SearchCandidateEvent | None:
        return self._mission(intent_id).ledger.observe_sighting(event)

    def detection_worker(
        self,
        intent_id: str,
        drone_id: int,
        stream: object,
        detector: object,
        pose_for_frame: Callable[[ProcessedFrameEvent], FramePoseEvidence],
        *,
        now_s: Callable[[], float],
        worker_run_id: str | None = None,
    ) -> LiveDetectionWorker:
        mission = self._mission(intent_id)
        assignment = next(
            item
            for item in mission.preview.search.assignments
            if item.drone.drone.drone_id == drone_id
        )

        def consume(event: ProcessedFrameEvent | SightingEvent) -> None:
            if isinstance(event, ProcessedFrameEvent):
                self.observe_processed_frame(intent_id, event, pose_for_frame(event), now_s=now_s())
            else:
                self.observe_sighting(intent_id, event)

        return LiveDetectionWorker(
            stream,
            detector,
            source_id=assignment.drone.source_id,
            mission_id=mission.preview.search.mission.frame_mission_id,
            worker_run_id=worker_run_id,
            on_event=consume,
            monotonic_clock=now_s,
        )

    def hold(self, intent_id: str, reason: str) -> SearchMissionStatus:
        mission = self._mission(intent_id)
        return self._status(intent_id, mission, mission.ledger.hold(reason))

    def cancel(self, intent_id: str, reason: str) -> SearchMissionStatus:
        mission = self._mission(intent_id)
        return self._status(intent_id, mission, mission.ledger.cancel(reason))

    def status(self, intent_id: str) -> SearchMissionStatus:
        mission = self._mission(intent_id)
        return self._status(intent_id, mission)

    def _transit_plan(
        self, search: SearchPreview, artifact: NavigationArtifact, positions: tuple[DronePose, ...]
    ) -> NavigationPlan:
        routes = tuple(assignment.transit for assignment in search.assignments)
        return NavigationPlan(
            artifact.map_pin,
            artifact.geometry_pin,
            artifact.navigation_pin,
            artifact.evidence,
            self.navigation.config.motion,
            self.config.permission,
            search.roster_version,
            search.plan_revision,
            search.zone.zone_id,
            tuple(route.drone for route in routes),
            positions,
            tuple(route.arrival_slot for route in routes),
            routes,
            search.execution_order,
            artifact.semantic_sha256,
        )

    def _coverage_plan(
        self, search: SearchPreview, artifact: NavigationArtifact, positions: tuple[DronePose, ...]
    ) -> NavigationPlan:
        routes: list[DroneRoute] = []
        current = {item.drone_id: item for item in positions}
        for assignment in search.assignments:
            route = assignment.transit
            waypoints = list(route.waypoints)
            segments = list(route.swept_segments)
            drone = replace(route.drone, pose=route.arrival_slot.pose)
            current[drone.drone_id] = drone
            for endpoint in self._coverage_endpoints(assignment.lanes):
                if endpoint == drone.pose:
                    continue
                leg = self._coverage_leg(drone, endpoint, search, artifact, tuple(current.values()))
                if isinstance(leg, NavigationRefusal):
                    raise ValueError(f"coverage route unreachable: {leg.detail}")
                waypoints.extend(leg.waypoints[1:])
                segments.extend(leg.swept_segments)
                drone = replace(drone, pose=endpoint)
                current[drone.drone_id] = drone
            routes.append(
                DroneRoute(
                    route.drone,
                    ArrivalSlot(
                        f"{assignment.task.task_id}:complete",
                        search.zone.zone_id,
                        drone.pose,
                        self.navigation.config.motion.swept_radius_m,
                        self.navigation.config.motion.swept_half_height_m,
                    ),
                    tuple(waypoints),
                    tuple(segments),
                )
            )
        return NavigationPlan(
            artifact.map_pin,
            artifact.geometry_pin,
            artifact.navigation_pin,
            artifact.evidence,
            self.navigation.config.motion,
            self.config.permission,
            search.roster_version,
            search.plan_revision,
            search.zone.zone_id,
            tuple(route.drone for route in routes),
            tuple(sorted(positions, key=lambda item: item.drone_id)),
            tuple(route.arrival_slot for route in routes),
            tuple(routes),
            search.execution_order,
            artifact.semantic_sha256,
        )

    def _coverage_leg(
        self,
        drone: DronePose,
        endpoint: Pose,
        search: SearchPreview,
        artifact: NavigationArtifact,
        positions: tuple[DronePose, ...],
    ) -> DroneRoute | NavigationRefusal:
        zone = next(item for item in artifact.zones if item.zone_id == search.zone.zone_id)
        slot = ArrivalSlot(
            f"{search.mission.frame_mission_id}:{drone.drone_id}:{len(positions)}",
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
                if item.zone_id == zone.zone_id
                else item
                for item in artifact.zones
            ),
        )
        planned = self.navigation.planner.plan(
            NavigationRequest(
                zone.zone_id,
                search.roster_version,
                search.plan_revision,
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

    def _activate_arrived_tasks(
        self, mission: _Mission, snapshot: FleetSnapshot
    ) -> tuple[SearchTaskEvent, ...]:
        events = []
        for assignment in mission.preview.search.assignments:
            task = assignment.task
            aircraft = snapshot.aircraft.get(assignment.drone.drone.drone_id)
            if (
                aircraft is not None
                and aircraft.connection_epoch == task.connection_epoch
                and aircraft.flight_state.value == "hovering"
                and 0
                <= snapshot.now_ms - aircraft.position_last_seen_ms
                <= self.navigation.config.position_max_age_ms
                and mission.ledger.task_state(task.task_id) == "pending"
            ):
                events.append(mission.ledger.activate(task.task_id))
        return tuple(events)

    @staticmethod
    def _status(
        intent_id: str, mission: _Mission, events: tuple[SearchTaskEvent, ...] = ()
    ) -> SearchMissionStatus:
        states = {
            mission.ledger.task_state(item.task.task_id)
            for item in mission.preview.search.assignments
        }
        if states == {"covered"}:
            state = "covered"
        elif "cancel" in states:
            state = "cancelled"
        elif "hold" in states:
            state = "hold"
        elif "incomplete" in states:
            state = "incomplete"
        else:
            state = "running" if mission.started else "prepared"
        return SearchMissionStatus(intent_id, state, events)

    def _mission(self, intent_id: str) -> _Mission:
        try:
            return self._missions[intent_id]
        except KeyError as error:
            raise ValueError("search mission is unknown") from error

    @staticmethod
    def _refusal(intent_id: str, snapshot: FleetSnapshot, detail: str) -> Refusal:
        return Refusal(
            intent_id, snapshot.roster_version, None, None, RefusalReason.INVALID_PLAN, detail
        )
