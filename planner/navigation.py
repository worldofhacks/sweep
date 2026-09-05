"""Immutable, deterministic previews for known-map navigation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from heapq import heappop, heappush
from math import dist, isfinite
from pathlib import Path
from typing import Literal

import numpy as np

from tools.map_validate import validate_bundle


def _number(value: float, name: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not isfinite(value):
        raise ValueError(f"{name} must be finite")
    value = float(value)
    if positive and value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


@dataclass(frozen=True, slots=True)
class Pose:
    x_m: float
    y_m: float
    z_m: float
    floor_id: str

    def __post_init__(self) -> None:
        _number(self.x_m, "x_m")
        _number(self.y_m, "y_m")
        _number(self.z_m, "z_m")
        if not self.floor_id:
            raise ValueError("floor_id is required")

    @property
    def xyz(self) -> tuple[float, float, float]:
        return (self.x_m, self.y_m, self.z_m)


@dataclass(frozen=True, slots=True)
class ArtifactPin:
    version: str
    content_sha256: str

    def __post_init__(self) -> None:
        if not self.version or len(self.content_sha256) != 64:
            raise ValueError("artifact pin is incomplete")


@dataclass(frozen=True, slots=True)
class MotionConfig:
    aircraft_radius_m: float
    aircraft_height_m: float
    map_uncertainty_m: float
    pose_uncertainty_m: float
    tracking_allowance_m: float
    stopping_allowance_m: float

    def __post_init__(self) -> None:
        for name in (
            "aircraft_radius_m",
            "aircraft_height_m",
            "map_uncertainty_m",
            "pose_uncertainty_m",
            "tracking_allowance_m",
            "stopping_allowance_m",
        ):
            value = _number(
                getattr(self, name),
                name,
                positive=name in {"aircraft_radius_m", "aircraft_height_m"},
            )
            if name not in {"aircraft_radius_m", "aircraft_height_m"} and value < 0:
                raise ValueError(f"{name} must be nonnegative")

    @property
    def swept_radius_m(self) -> float:
        return (
            self.aircraft_radius_m
            + self.map_uncertainty_m
            + self.pose_uncertainty_m
            + self.tracking_allowance_m
            + self.stopping_allowance_m
        )


@dataclass(frozen=True, slots=True)
class DronePose:
    drone_id: int
    connection_epoch: int
    pose: Pose

    def __post_init__(self) -> None:
        if type(self.drone_id) is not int or self.drone_id < 0 or self.connection_epoch < 0:
            raise ValueError("drone identity and epochs are required")


@dataclass(frozen=True, slots=True)
class ArrivalSlot:
    slot_id: str
    zone_id: str
    pose: Pose
    radius_m: float

    def __post_init__(self) -> None:
        if not self.slot_id or not self.zone_id:
            raise ValueError("arrival slot identity is required")
        _number(self.radius_m, "radius_m", positive=True)


@dataclass(frozen=True, slots=True)
class Zone:
    zone_id: str
    floor_id: str
    navigation_allowed: bool
    arrival_slots: tuple[ArrivalSlot, ...]
    aliases: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.zone_id or not self.floor_id:
            raise ValueError("zone identity is required")
        if len({slot.slot_id for slot in self.arrival_slots}) != len(self.arrival_slots):
            raise ValueError("arrival slot ids must be unique")
        if any(
            slot.zone_id != self.zone_id or slot.pose.floor_id != self.floor_id
            for slot in self.arrival_slots
        ):
            raise ValueError("arrival slot must belong to its zone and floor")
        if any(not alias.strip() for alias in self.aliases) or len(set(self.aliases)) != len(
            self.aliases
        ):
            raise ValueError("zone aliases must be distinct nonempty text")


@dataclass(frozen=True, slots=True)
class Connector:
    connector_id: str
    from_floor_id: str
    to_floor_id: str
    from_pose: Pose
    to_pose: Pose
    enabled: bool = True

    def __post_init__(self) -> None:
        if not self.connector_id or not self.from_floor_id or not self.to_floor_id:
            raise ValueError("connector identity is required")
        if (
            self.from_pose.floor_id != self.from_floor_id
            or self.to_pose.floor_id != self.to_floor_id
        ):
            raise ValueError("connector poses must match declared floors")


@dataclass(frozen=True, slots=True)
class GridLevel:
    floor_id: str
    z_m: float
    origin_xy_m: tuple[float, float]
    cell_m: float
    width: int
    height: int
    blocked_cells: frozenset[tuple[int, int]]

    def __post_init__(self) -> None:
        _number(self.z_m, "z_m")
        if len(self.origin_xy_m) != 2:
            raise ValueError("origin_xy_m must have two coordinates")
        _number(self.origin_xy_m[0], "origin x")
        _number(self.origin_xy_m[1], "origin y")
        _number(self.cell_m, "cell_m", positive=True)
        if not self.floor_id or self.width < 1 or self.height < 1:
            raise ValueError("grid level dimensions are invalid")
        if any(not (0 <= x < self.width and 0 <= y < self.height) for x, y in self.blocked_cells):
            raise ValueError("blocked cell is outside grid")

    def cell_for(self, pose: Pose) -> tuple[int, int]:
        return (
            int((pose.x_m - self.origin_xy_m[0]) // self.cell_m),
            int((pose.y_m - self.origin_xy_m[1]) // self.cell_m),
        )

    def pose_for(self, cell: tuple[int, int]) -> Pose:
        return Pose(
            self.origin_xy_m[0] + (cell[0] + 0.5) * self.cell_m,
            self.origin_xy_m[1] + (cell[1] + 0.5) * self.cell_m,
            self.z_m,
            self.floor_id,
        )

    def free(self, cell: tuple[int, int]) -> bool:
        return (
            0 <= cell[0] < self.width
            and 0 <= cell[1] < self.height
            and cell not in self.blocked_cells
        )


@dataclass(frozen=True, slots=True)
class NavigationArtifact:
    map_pin: ArtifactPin
    geometry_pin: ArtifactPin
    grid_clearance_m: float
    grids: tuple[GridLevel, ...]
    zones: tuple[Zone, ...]
    connectors: tuple[Connector, ...] = ()

    def __post_init__(self) -> None:
        _number(self.grid_clearance_m, "grid_clearance_m", positive=True)
        if not self.grids:
            raise ValueError("navigation artifact needs grids")
        if len({(grid.floor_id, grid.z_m) for grid in self.grids}) != len(self.grids):
            raise ValueError("grid levels must be unique")
        if len({zone.zone_id for zone in self.zones}) != len(self.zones):
            raise ValueError("zone ids must be unique")

    @classmethod
    def from_geometry_directory(
        cls,
        bundle: str | Path,
        geometry_directory: str | Path,
        accepted_versions: dict[str, str],
        zones: tuple[Zone, ...],
        connectors: tuple[Connector, ...] = (),
    ) -> NavigationArtifact:
        """Load a validated map bundle and its exact generated grid artifacts."""
        validated = validate_bundle(bundle, accepted_versions)
        directory = Path(geometry_directory)
        report_path = directory / "geometry.json"
        report = json.loads(report_path.read_bytes())
        if (
            report.get("bundle_version") != validated["bundle_version"]
            or report.get("bundle_content_sha256") != validated["content_sha256"]
        ):
            raise ValueError("geometry artifact does not match accepted map")
        files = report.get("files")
        if not isinstance(files, dict):
            raise ValueError("geometry artifact has no file pins")
        grids = []
        for band in report.get("bands_above_floor_m", []):
            name = f"grid_{report['floor_id']}_{band}.npy"
            path = directory / name
            if files.get(name) != sha256(path.read_bytes()).hexdigest():
                raise ValueError(f"geometry grid hash mismatch: {name}")
            rows = np.load(path, allow_pickle=False)
            if (
                rows.dtype != np.uint8
                or rows.ndim != 2
                or tuple(rows.shape) != tuple(report["shape_yx"])
            ):
                raise ValueError(f"invalid geometry grid: {name}")
            grids.append(
                GridLevel(
                    report["floor_id"],
                    float(report["floor_elevation_m"]) + float(band),
                    tuple(float(value) for value in report["origin_xy"]),
                    float(report["cell_m"]),
                    int(rows.shape[1]),
                    int(rows.shape[0]),
                    frozenset((int(x), int(y)) for y, x in zip(*np.where(rows != 0), strict=True)),
                )
            )
        if not grids:
            raise ValueError("geometry artifact has no grids")
        return cls(
            ArtifactPin(validated["bundle_version"], validated["content_sha256"]),
            ArtifactPin(
                str(report["authoring_sha256"]),
                sha256(report_path.read_bytes()).hexdigest(),
            ),
            float(report["hazard_margin_m"]),
            tuple(grids),
            zones,
            connectors,
        )


@dataclass(frozen=True, slots=True)
class NavigationPermission:
    permitted_zone_ids: frozenset[str]


@dataclass(frozen=True, slots=True)
class FormationPermission:
    permitted_zone_ids: frozenset[str]


@dataclass(frozen=True, slots=True)
class NavigationRequest:
    destination_zone_id: str
    roster_version: int
    selected: tuple[DronePose, ...]
    all_positions: tuple[DronePose, ...]
    motion: MotionConfig
    permission: NavigationPermission

    def __post_init__(self) -> None:
        if not self.destination_zone_id or self.roster_version < 0 or not self.selected:
            raise ValueError("destination and selected drones are required")
        if len({drone.drone_id for drone in self.selected}) != len(self.selected):
            raise ValueError("selected drone ids must be unique")
        positions = {drone.drone_id: drone for drone in self.all_positions}
        if len(positions) != len(self.all_positions) or any(
            positions.get(drone.drone_id) != drone for drone in self.selected
        ):
            raise ValueError("all_positions must include each selected drone exactly")


@dataclass(frozen=True, slots=True)
class SweptSegment:
    start: Pose
    end: Pose
    radius_m: float
    height_m: float


@dataclass(frozen=True, slots=True)
class DroneRoute:
    drone: DronePose
    arrival_slot: ArrivalSlot
    waypoints: tuple[Pose, ...]
    swept_segments: tuple[SweptSegment, ...]


@dataclass(frozen=True, slots=True)
class NavigationPlan:
    map_pin: ArtifactPin
    geometry_pin: ArtifactPin
    config: MotionConfig
    roster_version: int
    destination_zone_id: str
    selected: tuple[DronePose, ...]
    arrival_slots: tuple[ArrivalSlot, ...]
    routes: tuple[DroneRoute, ...]
    execution_order: tuple[int, ...]
    obstacle_roster: tuple[DronePose, ...] = ()
    approval_digest: str = ""


@dataclass(frozen=True, slots=True)
class NavigationRefusal:
    code: Literal[
        "arrival_not_permitted",
        "destination_unknown",
        "destination_excluded",
        "insufficient_arrival_slots",
        "clearance_exceeds_geometry",
        "wrong_floor",
        "start_unreachable",
        "route_unreachable",
        "arrival_conflict",
        "artifact_changed",
        "connection_changed",
        "position_drift",
        "remaining_route_obstructed",
    ]
    detail: str


class NavigationPlanner:
    def plan(
        self, request: NavigationRequest, artifact: NavigationArtifact
    ) -> NavigationPlan | NavigationRefusal:
        zone = next(
            (item for item in artifact.zones if item.zone_id == request.destination_zone_id), None
        )
        if zone is None:
            return NavigationRefusal(
                "destination_unknown", f"unknown destination: {request.destination_zone_id}"
            )
        if zone.zone_id not in request.permission.permitted_zone_ids:
            return NavigationRefusal(
                "arrival_not_permitted", f"arrival permission is missing for {zone.zone_id}"
            )
        if not zone.navigation_allowed:
            return NavigationRefusal(
                "destination_excluded", f"destination is excluded: {zone.zone_id}"
            )
        if request.motion.swept_radius_m > artifact.grid_clearance_m + 1e-9:
            return NavigationRefusal(
                "clearance_exceeds_geometry", "motion clearance exceeds geometry inflation"
            )
        drones = tuple(sorted(request.selected, key=lambda drone: drone.drone_id))
        slots = tuple(sorted(zone.arrival_slots, key=lambda slot: slot.slot_id))
        if len(slots) < len(drones):
            return NavigationRefusal(
                "insufficient_arrival_slots", "destination has too few distinct arrival slots"
            )
        if any(slot.radius_m < request.motion.swept_radius_m for slot in slots[: len(drones)]):
            return NavigationRefusal(
                "arrival_conflict", "arrival slot cannot contain swept aircraft volume"
            )
        assigned = tuple(zip(drones, slots[: len(drones)], strict=True))
        reserved = [
            (
                drone.drone_id,
                drone.pose,
                request.motion.swept_radius_m,
                request.motion.aircraft_height_m,
            )
            for drone in request.all_positions
        ]
        routes: list[DroneRoute] = []
        for drone, slot in assigned:
            if drone.pose.floor_id != slot.pose.floor_id and not any(
                grid.floor_id == drone.pose.floor_id for grid in artifact.grids
            ):
                return NavigationRefusal(
                    "wrong_floor", f"no route grid exists for {drone.drone_id} starting floor"
                )
            if not self._pose_on_free_grid(drone.pose, artifact.grids):
                return NavigationRefusal(
                    "start_unreachable",
                    f"start position is outside known free space for {drone.drone_id}",
                )
            path = self._route(
                drone.drone_id, drone.pose, slot.pose, artifact, request.motion, reserved
            )
            if path is None:
                code = (
                    "wrong_floor"
                    if drone.pose.floor_id != slot.pose.floor_id
                    else "route_unreachable"
                )
                return NavigationRefusal(
                    code, f"no clearance-checked route for {drone.drone_id} to {slot.slot_id}"
                )
            if any(
                not _body_clear_of_static_geometry(
                    a, b, artifact.grids, request.motion.aircraft_height_m
                )
                for a, b in zip(path, path[1:], strict=False)
                if a.floor_id == b.floor_id
            ):
                return NavigationRefusal(
                    "route_unreachable", "route body intersects static geometry"
                )
            route = DroneRoute(
                drone,
                slot,
                tuple(path),
                tuple(
                    SweptSegment(
                        start, end, request.motion.swept_radius_m, request.motion.aircraft_height_m
                    )
                    for start, end in zip(path, path[1:], strict=False)
                ),
            )
            routes.append(route)
            reserved = [item for item in reserved if item[0] != drone.drone_id]
            reserved.append(
                (
                    drone.drone_id,
                    slot.pose,
                    request.motion.swept_radius_m,
                    request.motion.aircraft_height_m,
                )
            )
        return NavigationPlan(
            artifact.map_pin,
            artifact.geometry_pin,
            request.motion,
            request.roster_version,
            zone.zone_id,
            drones,
            tuple(slot for _, slot in assigned),
            tuple(routes),
            tuple(drone.drone_id for drone in drones),
            tuple(sorted(request.all_positions, key=lambda drone: drone.drone_id)),
            _approval_digest(artifact),
        )

    def revalidate(
        self,
        plan: NavigationPlan,
        artifact: NavigationArtifact,
        actual_positions: tuple[DronePose, ...],
        route_index: int,
        segment_index: int,
        position_tolerance_m: float,
    ) -> NavigationRefusal | None:
        """Check a frozen route before dispatch without calculating a replacement route."""
        _number(position_tolerance_m, "position_tolerance_m", positive=True)
        if position_tolerance_m > plan.config.tracking_allowance_m:
            raise ValueError("position_tolerance_m exceeds frozen tracking_allowance_m")
        if artifact.map_pin != plan.map_pin or artifact.geometry_pin != plan.geometry_pin:
            return NavigationRefusal("artifact_changed", "map or geometry pin changed")
        if not plan.obstacle_roster or plan.approval_digest != _approval_digest(artifact):
            return NavigationRefusal(
                "artifact_changed", "approval, slots, connectors, or obstacle roster changed"
            )
        if not 0 <= route_index < len(plan.routes):
            raise ValueError("route_index is outside plan")
        route = plan.routes[route_index]
        if not 0 <= segment_index < len(route.swept_segments):
            raise ValueError("segment_index is outside route")
        current = {drone.drone_id: drone for drone in actual_positions}
        if len(current) != len(actual_positions):
            raise ValueError("actual_positions contains duplicate drone ids")
        frozen = {drone.drone_id: drone.connection_epoch for drone in plan.obstacle_roster}
        current_epochs = {drone.drone_id: drone.connection_epoch for drone in actual_positions}
        if any(current_epochs.get(drone_id) != epoch for drone_id, epoch in frozen.items()):
            return NavigationRefusal(
                "connection_changed", "current obstacle roster or epoch changed"
            )
        for selected in plan.selected:
            actual = current.get(selected.drone_id)
            if actual is None or actual.connection_epoch != selected.connection_epoch:
                return NavigationRefusal(
                    "connection_changed", "selected aircraft connection epoch changed"
                )
        active = current[route.drone.drone_id]
        segment = route.swept_segments[segment_index]
        if dist(active.pose.xyz, segment.start.xyz) > position_tolerance_m:
            return NavigationRefusal(
                "position_drift", "aircraft is not at the frozen segment start"
            )
        stationary = [
            (drone_id, drone.pose, plan.config.swept_radius_m, plan.config.aircraft_height_m)
            for drone_id, drone in current.items()
            if drone_id != route.drone.drone_id
        ]
        if active.pose != segment.start:
            if active.pose.floor_id != segment.end.floor_id:
                return NavigationRefusal(
                    "remaining_route_obstructed", "drifted position cannot enter a frozen connector"
                )
            level = self._level_for(active.pose, artifact.grids)
            if level is None or not _line_is_free(active.pose, segment.end, level):
                return NavigationRefusal(
                    "remaining_route_obstructed", "drifted approach enters blocked space"
                )
            if self._segment_hits_reservation(
                active.pose,
                segment.end,
                stationary,
                segment.radius_m,
                active.pose,
                segment.height_m,
            ):
                return NavigationRefusal(
                    "remaining_route_obstructed",
                    "drifted approach conflicts with stationary aircraft",
                )
        remaining = route.swept_segments[segment_index:]
        for candidate in remaining:
            if candidate.start.floor_id != candidate.end.floor_id or (
                candidate.start.xyz[:2] == candidate.end.xyz[:2]
                and candidate.start.z_m != candidate.end.z_m
            ):
                if not self._valid_vertical_segment(candidate, artifact):
                    return NavigationRefusal(
                        "remaining_route_obstructed", "vertical connector changed"
                    )
            else:
                level = self._level_for(candidate.start, artifact.grids)
                if level is None or not _line_is_free(candidate.start, candidate.end, level):
                    return NavigationRefusal(
                        "remaining_route_obstructed", "remaining route enters blocked space"
                    )
            if self._segment_hits_reservation(
                candidate.start,
                candidate.end,
                stationary,
                candidate.radius_m,
                active.pose,
                candidate.height_m,
            ):
                return NavigationRefusal(
                    "remaining_route_obstructed",
                    "remaining route conflicts with stationary aircraft",
                )
        return None

    @classmethod
    def _valid_vertical_segment(cls, segment: SweptSegment, artifact: NavigationArtifact) -> bool:
        if segment.start.floor_id == segment.end.floor_id:
            return (
                cls._vertical_levels(segment.start, segment.end, artifact.grids, segment.height_m)
                is not None
            )
        return any(
            connector.enabled
            and connector.from_pose == segment.start
            and connector.to_pose == segment.end
            for connector in artifact.connectors
        )

    def _route(
        self,
        exempt_id: int,
        start: Pose,
        goal: Pose,
        artifact: NavigationArtifact,
        motion: MotionConfig,
        reserved: list[tuple[int, Pose, float, float]],
    ) -> list[Pose] | None:
        start_level = self._level_for(start, artifact.grids)
        goal_level = self._level_for(goal, artifact.grids)
        if start_level is None or goal_level is None:
            return None
        if start.floor_id != goal.floor_id:
            for connector in artifact.connectors:
                if (
                    not connector.enabled
                    or connector.from_floor_id != start.floor_id
                    or connector.to_floor_id != goal.floor_id
                ):
                    continue
                if (connector.from_pose.x_m, connector.from_pose.y_m) != (
                    connector.to_pose.x_m,
                    connector.to_pose.y_m,
                ):
                    continue
                first = self._route_on_level(
                    start,
                    connector.from_pose,
                    start_level,
                    reserved,
                    motion.swept_radius_m,
                    motion.aircraft_height_m,
                    exempt_id,
                )
                if first is None:
                    continue
                if self._segment_hits_reservation(
                    connector.from_pose,
                    connector.to_pose,
                    reserved,
                    motion.swept_radius_m,
                    exempt_id,
                    motion.aircraft_height_m,
                ):
                    continue
                second = self._route_on_level(
                    connector.to_pose,
                    goal,
                    goal_level,
                    reserved,
                    motion.swept_radius_m,
                    motion.aircraft_height_m,
                    exempt_id,
                )
                if second is not None:
                    return [*first, *second]
            return None
        if start_level.z_m != goal_level.z_m:
            intermediate = self._vertical_levels(
                start, goal, artifact.grids, motion.aircraft_height_m
            )
            if intermediate is None:
                return None
            first = self._route_on_level(
                start,
                Pose(goal.x_m, goal.y_m, start_level.z_m, start.floor_id),
                start_level,
                reserved,
                motion.swept_radius_m,
                motion.aircraft_height_m,
                exempt_id,
            )
            if first is None:
                return None
            points = [*first, *intermediate, goal]
            if any(
                self._segment_hits_reservation(
                    a, b, reserved, motion.swept_radius_m, exempt_id, motion.aircraft_height_m
                )
                for a, b in zip(points, points[1:], strict=False)
            ):
                return None
            return points
        if self._blocked_by_stationary(
            start, reserved, motion.swept_radius_m, exempt_id, motion.aircraft_height_m
        ):
            return None
        return self._route_on_level(
            start,
            goal,
            start_level,
            reserved,
            motion.swept_radius_m,
            motion.aircraft_height_m,
            exempt_id,
        )

    def _route_on_level(
        self,
        start: Pose,
        goal: Pose,
        level: GridLevel,
        reserved: list[tuple[int, Pose, float, float]],
        radius: float,
        height: float,
        exempt_id: int,
    ) -> list[Pose] | None:
        if self._blocked_by_stationary(start, reserved, radius, exempt_id, height):
            return None
        start_cell, goal_cell = level.cell_for(start), level.cell_for(goal)
        if not level.free(start_cell) or not level.free(goal_cell):
            return None
        cells = self._astar(level, start_cell, goal_cell, reserved, radius, height, exempt_id)
        if cells is None:
            return None
        points = [start, *(level.pose_for(cell) for cell in cells[1:-1]), goal]
        if any(
            self._segment_hits_reservation(a, b, reserved, radius, exempt_id, height)
            for a, b in zip(points, points[1:], strict=False)
        ):
            return None
        return _simplify(points, level, reserved, radius, height, exempt_id)

    @staticmethod
    def _level_for(pose: Pose, levels: tuple[GridLevel, ...]) -> GridLevel | None:
        candidates = [level for level in levels if level.floor_id == pose.floor_id]
        return min(candidates, key=lambda level: abs(level.z_m - pose.z_m), default=None)

    @classmethod
    def _pose_on_free_grid(cls, pose: Pose, levels: tuple[GridLevel, ...]) -> bool:
        level = cls._level_for(pose, levels)
        return level is not None and level.free(level.cell_for(pose))

    @staticmethod
    def _vertical_levels(
        start: Pose, goal: Pose, levels: tuple[GridLevel, ...], height_m: float = 0.0
    ) -> list[Pose] | None:
        floor_levels = sorted(
            (level for level in levels if level.floor_id == start.floor_id),
            key=lambda level: level.z_m,
        )
        low, high = sorted((start.z_m, goal.z_m))
        relevant = [
            level
            for level in floor_levels
            if low - height_m / 2 <= level.z_m <= high + height_m / 2
        ]
        if not relevant or any(
            not level.free(level.cell_for(Pose(goal.x_m, goal.y_m, level.z_m, goal.floor_id)))
            for level in relevant
        ):
            return None
        return [
            Pose(goal.x_m, goal.y_m, level.z_m, goal.floor_id)
            for level in relevant
            if level.z_m != start.z_m and level.z_m != goal.z_m
        ]

    def _astar(
        self,
        level: GridLevel,
        start: tuple[int, int],
        goal: tuple[int, int],
        reserved: list[tuple[int, Pose, float, float]],
        radius: float,
        height: float,
        exempt_id: int,
    ) -> list[tuple[int, int]] | None:
        frontier = [(0.0, 0.0, start)]
        parents: dict[tuple[int, int], tuple[int, int] | None] = {start: None}
        costs = {start: 0.0}
        while frontier:
            _, cost, current = heappop(frontier)
            if current == goal:
                path = []
                while current is not None:
                    path.append(current)
                    current = parents[current]
                return list(reversed(path))
            for dx, dy in ((-1, 0), (0, -1), (0, 1), (1, 0)):
                candidate = (current[0] + dx, current[1] + dy)
                if not level.free(candidate):
                    continue
                pose = level.pose_for(candidate)
                if self._blocked_by_stationary(pose, reserved, radius, exempt_id, height):
                    continue
                if self._segment_hits_reservation(
                    level.pose_for(current), pose, reserved, radius, exempt_id, height
                ):
                    continue
                candidate_cost = cost + 1.0
                if candidate_cost >= costs.get(candidate, float("inf")):
                    continue
                costs[candidate] = candidate_cost
                parents[candidate] = current
                heuristic = abs(candidate[0] - goal[0]) + abs(candidate[1] - goal[1])
                heappush(frontier, (candidate_cost + heuristic, candidate_cost, candidate))
        return None

    @staticmethod
    def _blocked_by_stationary(
        pose: Pose,
        reserved: list[tuple[int, Pose, float, float]],
        radius: float,
        exempt_id: int | None = None,
        height: float = 0.0,
    ) -> bool:
        return any(
            other_id != exempt_id
            and _horizontal_distance(other, pose) < other_radius + radius - 1e-9
            and _vertical_overlap(other.z_m, other_height, pose.z_m, height)
            for other_id, other, other_radius, other_height in reserved
        )

    @staticmethod
    def _segment_hits_reservation(
        start: Pose,
        end: Pose,
        reserved: list[tuple[int, Pose, float, float]],
        radius: float,
        exempt_id: int,
        height: float = 0.0,
    ) -> bool:
        for other_id, other, other_radius, other_height in reserved:
            if other_id == exempt_id:
                continue
            if _segment_horizontal_distance(start, end, other) >= radius + other_radius - 1e-9:
                continue
            if _segment_vertical_overlap(start, end, height, other.z_m, other_height):
                return True
        return False


def _simplify(
    points: list[Pose],
    level: GridLevel,
    reserved: list[tuple[int, Pose, float, float]],
    radius: float,
    height: float,
    exempt_id: int,
) -> list[Pose]:
    result = [points[0]]
    index = 0
    while index < len(points) - 1:
        next_index = len(points) - 1
        while next_index > index + 1:
            if _line_is_free(
                points[index], points[next_index], level
            ) and not NavigationPlanner._segment_hits_reservation(
                points[index], points[next_index], reserved, radius, exempt_id, height
            ):
                break
            next_index -= 1
        result.append(points[next_index])
        index = next_index
    return result


def _line_is_free(start: Pose, end: Pose, level: GridLevel) -> bool:
    return all(level.free(cell) for cell in _supercover(start, end, level))


def _body_clear_of_static_geometry(
    start: Pose, end: Pose, grids: tuple[GridLevel, ...], height_m: float
) -> bool:
    return all(
        all(level.free(cell) for cell in _supercover(start, end, level))
        for level in grids
        if level.floor_id == start.floor_id and abs(level.z_m - start.z_m) <= height_m / 2 + 1e-9
    )


def _supercover(start: Pose, end: Pose, level: GridLevel) -> set[tuple[int, int]]:
    """Exact closed-segment/cell intersection, so boundary and corner contacts are included."""
    low_x, high_x = sorted((level.cell_for(start)[0], level.cell_for(end)[0]))
    low_y, high_y = sorted((level.cell_for(start)[1], level.cell_for(end)[1]))
    return {
        (x, y)
        for x in range(low_x - 1, high_x + 2)
        for y in range(low_y - 1, high_y + 2)
        if _segment_intersects_cell(start, end, x, y, level)
    }


def _segment_intersects_cell(start: Pose, end: Pose, x: int, y: int, level: GridLevel) -> bool:
    left = level.origin_xy_m[0] + x * level.cell_m
    bottom = level.origin_xy_m[1] + y * level.cell_m
    dx, dy = end.x_m - start.x_m, end.y_m - start.y_m
    enter, leave = 0.0, 1.0
    for origin, delta, low, high in (
        (start.x_m, dx, left, left + level.cell_m),
        (start.y_m, dy, bottom, bottom + level.cell_m),
    ):
        if delta == 0:
            if origin < low or origin > high:
                return False
            continue
        first, second = sorted(((low - origin) / delta, (high - origin) / delta))
        enter, leave = max(enter, first), min(leave, second)
        if enter > leave:
            return False
    return leave >= 0 and enter <= 1


def _approval_digest(artifact: NavigationArtifact) -> str:
    payload = repr((artifact.zones, artifact.connectors)).encode()
    return sha256(payload).hexdigest()


def _horizontal_distance(first: Pose, second: Pose) -> float:
    return dist((first.x_m, first.y_m), (second.x_m, second.y_m))


def _vertical_overlap(
    first_z: float, first_height: float, second_z: float, second_height: float
) -> bool:
    return abs(first_z - second_z) < (first_height + second_height) / 2 - 1e-9


def _segment_horizontal_distance(start: Pose, end: Pose, point: Pose) -> float:
    dx, dy = end.x_m - start.x_m, end.y_m - start.y_m
    length_sq = dx * dx + dy * dy
    t = (
        0.0
        if length_sq == 0
        else max(
            0.0, min(1.0, ((point.x_m - start.x_m) * dx + (point.y_m - start.y_m) * dy) / length_sq)
        )
    )
    return dist((start.x_m + dx * t, start.y_m + dy * t), (point.x_m, point.y_m))


def _segment_vertical_overlap(
    start: Pose, end: Pose, height: float, z: float, other_height: float
) -> bool:
    low = min(start.z_m, end.z_m) - height / 2
    high = max(start.z_m, end.z_m) + height / 2
    return low < z + other_height / 2 - 1e-9 and high > z - other_height / 2 + 1e-9
