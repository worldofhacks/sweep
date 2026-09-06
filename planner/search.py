"""Deterministic previews for confirmed multi-drone visual searches."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Literal

from perception.object_detection import DEFAULT_TARGET_LABELS
from perception.search_events import (
    CameraPolicy,
    CoverageCell,
    CoverageLedger,
    CoverageTask,
    SearchMissionIdentity,
)
from planner.navigation import (
    ArrivalSlot,
    ArtifactPin,
    DronePose,
    DroneRoute,
    GridLevel,
    MotionConfig,
    NavigationArtifact,
    NavigationPermission,
    NavigationPlanner,
    NavigationRefusal,
    NavigationRequest,
    Pose,
    Zone,
)


def _identifier(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")


@dataclass(frozen=True, slots=True)
class SearchArea:
    zone_id: str
    floor_id: str
    polygon_xy_m: tuple[tuple[float, float], ...]

    def __post_init__(self) -> None:
        _identifier(self.zone_id, "zone_id")
        _identifier(self.floor_id, "floor_id")
        if len(self.polygon_xy_m) < 3 or not all(
            len(point) == 2 and all(math.isfinite(value) for value in point)
            for point in self.polygon_xy_m
        ):
            raise ValueError("search area needs three or more finite polygon points")
        if abs(_polygon_area(self.polygon_xy_m)) < 1e-9:
            raise ValueError("search area polygon has no area")

    def contains(self, x_m: float, y_m: float) -> bool:
        return _point_in_polygon(x_m, y_m, self.polygon_xy_m)


@dataclass(frozen=True, slots=True)
class SearchDrone:
    drone: DronePose
    source_id: str

    def __post_init__(self) -> None:
        _identifier(self.source_id, "source_id")


@dataclass(frozen=True, slots=True)
class SearchRequest:
    mission: SearchMissionIdentity
    zone: SearchArea
    target_class: str
    roster_version: int
    plan_revision: int
    selected: tuple[SearchDrone, ...]
    all_positions: tuple[DronePose, ...]
    map_pin: ArtifactPin
    config_id: str
    camera: CameraPolicy
    motion: MotionConfig
    permission: NavigationPermission
    confirmation_id: str

    def __post_init__(self) -> None:
        if self.target_class not in DEFAULT_TARGET_LABELS:
            raise ValueError("target_class is not enabled by the fixed COCO detector")
        if (
            isinstance(self.roster_version, bool)
            or not isinstance(self.roster_version, int)
            or self.roster_version < 0
            or isinstance(self.plan_revision, bool)
            or not isinstance(self.plan_revision, int)
            or self.plan_revision < 0
        ):
            raise ValueError("roster_version must be an integer")
        _identifier(self.config_id, "config_id")
        _identifier(self.confirmation_id, "confirmation_id")
        if not self.selected or len({item.drone.drone_id for item in self.selected}) != len(
            self.selected
        ):
            raise ValueError("selected search drones must be nonempty and distinct")
        if len({item.source_id for item in self.selected}) != len(self.selected):
            raise ValueError("selected search drone sources must be distinct")
        positions = {drone.drone_id: drone for drone in self.all_positions}
        if len(positions) != len(self.all_positions) or any(
            positions.get(item.drone.drone_id) != item.drone for item in self.selected
        ):
            raise ValueError("all_positions must include every selected drone exactly")


@dataclass(frozen=True, slots=True)
class CoverageLane:
    lane_id: str
    cells: tuple[CoverageCell, ...]


@dataclass(frozen=True, slots=True)
class DroneSearchAssignment:
    drone: SearchDrone
    task: CoverageTask
    transit: DroneRoute
    lanes: tuple[CoverageLane, ...]

    @property
    def workload_cells(self) -> int:
        return len(self.task.cells)


@dataclass(frozen=True, slots=True)
class SearchPreview:
    mission: SearchMissionIdentity
    zone: SearchArea
    target_class: str
    map_pin: ArtifactPin
    geometry_pin: ArtifactPin
    roster_version: int
    plan_revision: int
    config_id: str
    camera: CameraPolicy
    confirmation_id: str
    assignments: tuple[DroneSearchAssignment, ...]
    execution_order: tuple[int, ...]

    def ledger(self) -> CoverageLedger:
        return CoverageLedger(
            self.mission, self.camera, tuple(item.task for item in self.assignments)
        )

    def payload(self) -> dict[str, object]:
        return {
            "type": "perception.search_preview",
            **self.mission.payload(),
            "zone_id": self.zone.zone_id,
            "target_class": self.target_class,
            "map": {
                "version": self.map_pin.version,
                "content_sha256": self.map_pin.content_sha256,
                "geometry_version": self.geometry_pin.version,
                "geometry_sha256": self.geometry_pin.content_sha256,
            },
            "roster_version": self.roster_version,
            "plan_revision": self.plan_revision,
            "config_id": self.config_id,
            "confirmation_id": self.confirmation_id,
            "camera": self.camera.payload(),
            "execution_order": list(self.execution_order),
            "allocations": [
                {
                    "drone_id": assignment.drone.drone.drone_id,
                    "source_id": assignment.drone.source_id,
                    "task_id": assignment.task.task_id,
                    "workload_cells": assignment.workload_cells,
                    "lane_count": len(assignment.lanes),
                    "transit_waypoints": [
                        [waypoint.x_m, waypoint.y_m, waypoint.z_m]
                        for waypoint in assignment.transit.waypoints
                    ],
                }
                for assignment in self.assignments
            ],
        }


@dataclass(frozen=True, slots=True)
class SearchRefusal:
    code: Literal[
        "map_changed",
        "zone_unknown",
        "zone_excluded",
        "wrong_floor",
        "area_empty",
        "area_disconnected",
        "insufficient_coverage_cells",
        "allocation_disconnected",
        "transit_unreachable",
    ]
    detail: str


class SearchPlanner:
    """Produces a frozen allocation and routes. It does not execute them."""

    def __init__(self, navigation: NavigationPlanner | None = None) -> None:
        self._navigation = navigation or NavigationPlanner()

    def plan(
        self, request: SearchRequest, artifact: NavigationArtifact
    ) -> SearchPreview | SearchRefusal:
        if request.map_pin != artifact.map_pin:
            return SearchRefusal(
                "map_changed", "search request map pin does not match navigation map"
            )
        zone = next((item for item in artifact.zones if item.zone_id == request.zone.zone_id), None)
        if zone is None:
            return SearchRefusal("zone_unknown", "search zone is absent from navigation map")
        if not zone.owner_approved or zone.zone_id not in request.permission.permitted_zone_ids:
            return SearchRefusal("zone_excluded", "search zone is not permitted for navigation")
        if zone.floor_id != request.zone.floor_id:
            return SearchRefusal(
                "wrong_floor", "search zone and navigation zone have different floors"
            )
        level = self._level_for(request.zone, artifact.grids)
        if level is None or any(
            item.drone.pose.floor_id != request.zone.floor_id for item in request.selected
        ):
            return SearchRefusal(
                "wrong_floor", "search area and selected aircraft need one route grid"
            )
        cells = tuple(
            cell
            for cell in (
                (x, y)
                for y in range(level.height)
                for x in range(level.width)
                if level.free((x, y))
            )
            if request.zone.contains(level.pose_for(cell).x_m, level.pose_for(cell).y_m)
        )
        if not cells:
            return SearchRefusal("area_empty", "search area contains no known free coverage cells")
        if len(cells) < len(request.selected):
            return SearchRefusal(
                "insufficient_coverage_cells", "fewer coverage cells than selected drones"
            )
        if len(_components(set(cells))) != 1:
            return SearchRefusal(
                "area_disconnected", "search area has disconnected known free space"
            )
        regions = _balanced_regions(set(cells), len(request.selected))
        if regions is None or any(len(_components(region)) != 1 for region in regions):
            return SearchRefusal(
                "allocation_disconnected", "coverage cannot form contiguous allocations"
            )
        ordered = tuple(sorted(request.selected, key=lambda item: item.drone.drone_id))
        positions = {drone.drone_id: drone for drone in request.all_positions}
        assignments = []
        for index, (drone, region) in enumerate(zip(ordered, regions, strict=True), start=1):
            ordered_cells = tuple(sorted(region, key=lambda cell: (cell[1], cell[0])))
            coverage_cells = tuple(
                CoverageCell(
                    f"{request.mission.frame_mission_id}:{drone.drone.drone_id}:{cell[0]}:{cell[1]}",
                    level.pose_for(cell),
                )
                for cell in ordered_cells
            )
            lanes = _lanes(
                f"{request.mission.frame_mission_id}:{drone.drone.drone_id}",
                coverage_cells,
                level,
                request.camera,
            )
            transit = self._transit(
                request,
                artifact,
                zone,
                drone.drone,
                tuple(positions.values()),
                coverage_cells[0].pose,
                index,
            )
            if isinstance(transit, NavigationRefusal):
                return SearchRefusal("transit_unreachable", transit.detail)
            task = CoverageTask(
                f"{request.mission.frame_mission_id}:{drone.drone.drone_id}",
                drone.source_id,
                drone.drone.connection_epoch,
                coverage_cells,
            )
            assignments.append(DroneSearchAssignment(drone, task, transit, lanes))
            positions[drone.drone.drone_id] = DronePose(
                drone.drone.drone_id, drone.drone.connection_epoch, coverage_cells[0].pose
            )
        return SearchPreview(
            request.mission,
            request.zone,
            request.target_class,
            artifact.map_pin,
            artifact.geometry_pin,
            request.roster_version,
            request.plan_revision,
            request.config_id,
            request.camera,
            request.confirmation_id,
            tuple(assignments),
            tuple(item.drone.drone.drone_id for item in assignments),
        )

    def _transit(
        self,
        request: SearchRequest,
        artifact: NavigationArtifact,
        zone: Zone,
        drone: DronePose,
        positions: tuple[DronePose, ...],
        destination: Pose,
        index: int,
    ) -> DroneRoute | NavigationRefusal:
        slot = ArrivalSlot(
            f"search-{request.mission.mission_id}-{drone.drone_id}-{index}",
            zone.zone_id,
            destination,
            request.motion.swept_radius_m,
            request.motion.swept_half_height_m,
        )
        overlay_zone = Zone(
            zone.zone_id,
            zone.floor_id,
            zone.owner_approved,
            zone.polygon_xy,
            zone.z_min_m,
            zone.z_max_m,
            (slot,),
            zone.aliases,
        )
        overlay = replace(
            artifact,
            zones=tuple(
                overlay_zone if item.zone_id == zone.zone_id else item for item in artifact.zones
            ),
        )
        planned = self._navigation.plan(
            NavigationRequest(
                zone.zone_id,
                request.roster_version,
                request.plan_revision,
                (drone,),
                positions,
                request.motion,
                request.permission,
            ),
            overlay,
        )
        return planned if isinstance(planned, NavigationRefusal) else planned.routes[0]

    @staticmethod
    def _level_for(area: SearchArea, grids: tuple[GridLevel, ...]) -> GridLevel | None:
        return next((grid for grid in grids if grid.floor_id == area.floor_id), None)


def _polygon_area(points: tuple[tuple[float, float], ...]) -> float:
    return (
        sum(
            first[0] * second[1] - second[0] * first[1]
            for first, second in zip(points, points[1:] + points[:1], strict=True)
        )
        / 2
    )


def _point_in_polygon(x_m: float, y_m: float, polygon: tuple[tuple[float, float], ...]) -> bool:
    inside = False
    for first, second in zip(polygon, polygon[1:] + polygon[:1], strict=True):
        if (first[1] > y_m) != (second[1] > y_m):
            crossing_x = (second[0] - first[0]) * (y_m - first[1]) / (second[1] - first[1]) + first[
                0
            ]
            if x_m < crossing_x:
                inside = not inside
    return inside


def _neighbors(cell: tuple[int, int]) -> tuple[tuple[int, int], ...]:
    return tuple((cell[0] + dx, cell[1] + dy) for dx, dy in ((0, -1), (-1, 0), (1, 0), (0, 1)))


def _components(cells: set[tuple[int, int]]) -> tuple[set[tuple[int, int]], ...]:
    remaining = set(cells)
    components = []
    while remaining:
        component = {min(remaining, key=lambda cell: (cell[1], cell[0]))}
        frontier = list(component)
        remaining -= component
        while frontier:
            for neighbor in _neighbors(frontier.pop()):
                if neighbor in remaining:
                    remaining.remove(neighbor)
                    component.add(neighbor)
                    frontier.append(neighbor)
        components.append(component)
    return tuple(components)


def _balanced_regions(
    cells: set[tuple[int, int]], count: int
) -> tuple[set[tuple[int, int]], ...] | None:
    columns: dict[int, set[tuple[int, int]]] = {}
    for cell in cells:
        columns.setdefault(cell[0], set()).add(cell)
    regions = [set() for _ in range(count)]
    total = len(cells)
    region_index = 0
    for column_index, column in enumerate(sorted(columns.values(), key=lambda item: min(item))):
        target = total // count + (1 if region_index < total % count else 0)
        if (
            region_index < count - 1
            and regions[region_index]
            and len(regions[region_index]) + len(column) > target
        ):
            region_index += 1
        regions[region_index].update(column)
        if column_index == len(columns) - 1:
            break
    if any(not region for region in regions) or max(map(len, regions)) - min(map(len, regions)) > 1:
        return None
    return tuple(regions)


def _lanes(
    prefix: str,
    cells: tuple[CoverageCell, ...],
    level: GridLevel,
    camera: CameraPolicy,
) -> tuple[CoverageLane, ...]:
    grouped: dict[int, list[CoverageCell]] = {}
    for cell in cells:
        grid_cell = level.cell_for(cell.pose)
        grouped.setdefault(grid_cell[1], []).append(cell)
    lanes = []
    lane_index = 0
    row_stride = max(
        1, int(camera.footprint_depth_m * (1 - camera.overlap_fraction) // level.cell_m)
    )
    rows = sorted(grouped.items())
    selected_rows = list(range(0, len(rows), row_stride))
    if selected_rows[-1] != len(rows) - 1:
        selected_rows.append(len(rows) - 1)
    for row_index in selected_rows:
        _, row = rows[row_index]
        row.sort(key=lambda cell: level.cell_for(cell.pose)[0])
        run = [row[0]]
        for cell in row[1:]:
            if level.cell_for(cell.pose)[0] == level.cell_for(run[-1].pose)[0] + 1:
                run.append(cell)
            else:
                if lane_index % 2:
                    run.reverse()
                lanes.append(CoverageLane(f"{prefix}:lane:{lane_index}", tuple(run)))
                lane_index += 1
                run = [cell]
        if lane_index % 2:
            run.reverse()
        column_stride = max(1, int(camera.lane_spacing_m // level.cell_m))
        sampled = run[::column_stride]
        if sampled[-1] != run[-1]:
            sampled.append(run[-1])
        lanes.append(CoverageLane(f"{prefix}:lane:{lane_index}", tuple(sampled)))
        lane_index += 1
    return tuple(lanes)
