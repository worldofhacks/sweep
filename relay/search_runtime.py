"""Prepares and starts confirmed visual-search previews over guarded navigation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

from perception.search_events import CameraPolicy, CoverageLedger, SearchMissionIdentity
from planner.models import FleetSnapshot, Plan, Refusal, RefusalReason
from planner.navigation import (
    ArtifactPin,
    DronePose,
    NavigationArtifact,
    NavigationPermission,
    NavigationPlan,
)
from planner.navigation_runtime import NavigationRuntime
from planner.search import (
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
    state: Literal["prepared", "running"]


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
            route = self._transit_plan(search, artifact, positions)
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

    def status(self, intent_id: str) -> SearchMissionStatus:
        mission = self._mission(intent_id)
        return SearchMissionStatus(intent_id, "running" if mission.started else "prepared")

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
