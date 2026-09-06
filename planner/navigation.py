"""Deterministic, non-dispatchable previews over pinned known-map geometry."""

from __future__ import annotations

from heapq import heappop, heappush
from math import dist

from planner.navigation_artifacts import NavigationArtifact
from planner.navigation_contracts import (
    EPS,
    ArrivalSlot,
    ArtifactPin,
    Connector,
    DronePose,
    DroneRoute,
    GridLevel,
    MotionConfig,
    NavigationEvidence,
    NavigationLiveState,
    NavigationPermission,
    NavigationPlan,
    NavigationRefusal,
    NavigationRequest,
    Pose,
    SweptSegment,
    Zone,
    finite_number,
    grid_covers_pose,
    preview_evidence,
)
from planner.navigation_geometry import (
    Reservation,
    ReservationMap,
    blocked_by_stationary,
    compress_collinear,
    first_overlap,
    join_paths,
    level_for_pose,
    line_is_free,
    path_clear,
    pose_supported,
    segment_geometry_clear,
    segment_hits_reservation,
    vertical_path_clear,
    vertical_waypoints,
)

__all__ = [
    "ArrivalSlot",
    "ArtifactPin",
    "Connector",
    "DronePose",
    "DroneRoute",
    "GridLevel",
    "MotionConfig",
    "NavigationArtifact",
    "NavigationEvidence",
    "NavigationLiveState",
    "NavigationPermission",
    "NavigationPlan",
    "NavigationPlanner",
    "NavigationRefusal",
    "NavigationRequest",
    "Pose",
    "SweptSegment",
    "Zone",
    "preview_evidence",
]


class NavigationPlanner:
    def plan(
        self,
        request: NavigationRequest,
        artifact: NavigationArtifact,
    ) -> NavigationPlan | NavigationRefusal:
        if not isinstance(request, NavigationRequest) or not isinstance(
            artifact, NavigationArtifact
        ):
            raise ValueError("planner inputs must use navigation contract types")
        zone = next(
            (item for item in artifact.zones if item.zone_id == request.destination_zone_id),
            None,
        )
        if zone is None:
            return NavigationRefusal(
                "destination_unknown",
                f"unknown destination: {request.destination_zone_id}",
            )
        if zone.zone_id not in request.permission.permitted_zone_ids:
            return NavigationRefusal(
                "arrival_not_permitted",
                f"arrival permission is missing for {zone.zone_id}",
            )
        if not zone.owner_approved:
            return NavigationRefusal(
                "destination_excluded",
                f"destination is not owner-approved: {zone.zone_id}",
            )
        if (
            request.motion.swept_radius_m > artifact.grid_clearance_m + EPS
            or request.motion.swept_half_height_m > artifact.grid_clearance_m + EPS
        ):
            return NavigationRefusal(
                "clearance_exceeds_geometry",
                "horizontal or vertical motion envelope exceeds geometry inflation",
            )
        for drone in request.all_positions:
            if not pose_supported(drone.pose, artifact, request.motion):
                return NavigationRefusal(
                    "position_unmapped",
                    f"aircraft {drone.drone_id} has no clearance-checked map and altitude band",
                )
        reservations = {
            drone.drone_id: Reservation(
                drone.drone_id,
                drone.pose,
                request.motion.swept_radius_m,
                request.motion.swept_half_height_m,
            )
            for drone in request.all_positions
        }
        overlap = first_overlap(reservations)
        if overlap is not None:
            return NavigationRefusal(
                "initial_overlap",
                f"aircraft {overlap[0]} and {overlap[1]} overlap their motion envelopes",
            )
        drones = tuple(sorted(request.selected, key=lambda drone: drone.drone_id))
        slots = tuple(sorted(zone.arrival_slots, key=lambda slot: slot.slot_id))
        if len(slots) < len(drones):
            return NavigationRefusal(
                "insufficient_arrival_slots",
                "destination has too few distinct arrival slots",
            )
        fitting_slots = tuple(
            slot
            for slot in slots
            if slot.radius_m + EPS >= request.motion.swept_radius_m
            and slot.half_height_m + EPS >= request.motion.swept_half_height_m
        )
        if len(fitting_slots) < len(drones):
            return NavigationRefusal(
                "arrival_conflict",
                "destination has too few slots for the full aircraft envelope",
            )
        routes = self._assign_routes(
            drones,
            fitting_slots,
            artifact,
            request.motion,
            reservations,
        )
        if routes is None:
            wrong_floor = any(
                drone.pose.floor_id != zone.floor_id
                and not any(
                    connector.enabled
                    and connector.from_floor_id == drone.pose.floor_id
                    and connector.to_floor_id == zone.floor_id
                    for connector in artifact.connectors
                )
                for drone in drones
            )
            return NavigationRefusal(
                "wrong_floor" if wrong_floor else "route_unreachable",
                "no deterministic assignment has a clearance-checked route to every slot",
            )
        return NavigationPlan(
            artifact.map_pin,
            artifact.geometry_pin,
            artifact.navigation_pin,
            artifact.evidence,
            request.motion,
            request.permission,
            request.roster_version,
            request.plan_revision,
            zone.zone_id,
            drones,
            tuple(sorted(request.all_positions, key=lambda drone: drone.drone_id)),
            tuple(route.arrival_slot for route in routes),
            routes,
            tuple(drone.drone_id for drone in drones),
            artifact.semantic_sha256,
        )

    def revalidate(
        self,
        plan: NavigationPlan,
        artifact: NavigationArtifact,
        live: NavigationLiveState,
        route_index: int,
        segment_index: int,
        position_tolerance_m: float,
    ) -> NavigationRefusal | None:
        """Revalidate exact frozen inputs; this preview slice never authorizes dispatch."""
        if (
            not isinstance(plan, NavigationPlan)
            or not isinstance(artifact, NavigationArtifact)
            or not isinstance(live, NavigationLiveState)
        ):
            raise ValueError("revalidation inputs must use navigation contract types")
        tolerance = finite_number(position_tolerance_m, "position_tolerance_m")
        if tolerance < 0 or tolerance > plan.config.tracking_allowance_m + EPS:
            raise ValueError("position_tolerance_m is outside the frozen tracking allowance")
        if (
            artifact.map_pin != plan.map_pin
            or artifact.geometry_pin != plan.geometry_pin
            or artifact.navigation_pin != plan.navigation_pin
            or artifact.semantic_sha256 != plan.artifact_sha256
            or artifact.evidence != plan.evidence
        ):
            return NavigationRefusal(
                "artifact_changed",
                "map, geometry, or navigation pin changed",
            )
        if live.roster_version != plan.roster_version:
            return NavigationRefusal("roster_changed", "live roster version changed")
        if live.plan_revision != plan.plan_revision:
            return NavigationRefusal(
                "plan_revision_changed",
                "live plan revision changed",
            )
        planned_selection = tuple(sorted(drone.drone_id for drone in plan.selected))
        if tuple(sorted(live.selected_ids)) != planned_selection:
            return NavigationRefusal("selection_changed", "live aircraft selection changed")
        if live.motion != plan.config:
            return NavigationRefusal(
                "motion_config_changed",
                "authoritative motion config changed",
            )
        if live.permission != plan.permission:
            return NavigationRefusal(
                "permission_changed",
                "navigation permission changed",
            )
        if type(route_index) is not int or not 0 <= route_index < len(plan.routes):
            raise ValueError("route_index is outside plan")
        route = plan.routes[route_index]
        if type(segment_index) is not int or not 0 <= segment_index < len(route.swept_segments):
            raise ValueError("segment_index is outside route")
        current = {drone.drone_id: drone for drone in live.positions}
        planned_roster = {drone.drone_id: drone for drone in plan.roster}
        if current.keys() != planned_roster.keys():
            return NavigationRefusal("roster_changed", "live roster membership changed")
        for drone_id, planned in planned_roster.items():
            if current[drone_id].connection_epoch != planned.connection_epoch:
                return NavigationRefusal(
                    "connection_changed",
                    f"aircraft {drone_id} connection epoch changed",
                )
            if not pose_supported(current[drone_id].pose, artifact, plan.config):
                return NavigationRefusal(
                    "remaining_route_obstructed",
                    f"aircraft {drone_id} left accepted map or altitude coverage",
                )
        reservations = {
            drone.drone_id: Reservation(
                drone.drone_id,
                drone.pose,
                plan.config.swept_radius_m,
                plan.config.swept_half_height_m,
            )
            for drone in current.values()
        }
        overlap = first_overlap(reservations)
        if overlap is not None:
            return NavigationRefusal(
                "remaining_route_obstructed",
                f"aircraft {overlap[0]} and {overlap[1]} overlap their motion envelopes",
            )
        active = current[route.drone.drone_id]
        segment = route.swept_segments[segment_index]
        if dist(active.pose.xyz, segment.start.xyz) > tolerance + EPS:
            return NavigationRefusal(
                "position_drift",
                "aircraft is not at the frozen segment start",
            )
        stationary = {
            drone_id: reservation
            for drone_id, reservation in reservations.items()
            if drone_id != active.drone_id
        }
        if active.pose != segment.start:
            if active.pose.floor_id != segment.start.floor_id or not segment_geometry_clear(
                active.pose, segment.end, artifact, plan.config
            ):
                return NavigationRefusal(
                    "remaining_route_obstructed",
                    "drifted approach leaves accepted geometry",
                )
            if segment_hits_reservation(
                active.pose,
                segment.end,
                stationary,
                segment.radius_m,
                segment.half_height_m,
                active.drone_id,
            ):
                return NavigationRefusal(
                    "remaining_route_obstructed",
                    "drifted approach conflicts with stationary aircraft",
                )
        for candidate in route.swept_segments[segment_index:]:
            if not segment_geometry_clear(
                candidate.start,
                candidate.end,
                artifact,
                plan.config,
            ):
                return NavigationRefusal(
                    "remaining_route_obstructed",
                    "remaining route left accepted geometry",
                )
            if segment_hits_reservation(
                candidate.start,
                candidate.end,
                stationary,
                candidate.radius_m,
                candidate.half_height_m,
                active.drone_id,
            ):
                return NavigationRefusal(
                    "remaining_route_obstructed",
                    "remaining route conflicts with stationary aircraft",
                )
        return NavigationRefusal(
            "artifact_not_dispatchable",
            "preview is blocked by: " + ", ".join(plan.evidence.blocking_gaps),
        )

    def _assign_routes(
        self,
        drones: tuple[DronePose, ...],
        slots: tuple[ArrivalSlot, ...],
        artifact: NavigationArtifact,
        motion: MotionConfig,
        reservations: dict[int, Reservation],
    ) -> tuple[DroneRoute, ...] | None:
        """Search deterministic slot assignments instead of committing greedily."""
        if not drones:
            return ()
        drone = drones[0]
        for index, slot in enumerate(slots):
            path = self._route(drone, slot.pose, artifact, motion, reservations)
            if path is None:
                continue
            if len(path) == 1:
                path = [path[0], path[0]]
            segments = tuple(
                SweptSegment(
                    start,
                    end,
                    motion.swept_radius_m,
                    motion.swept_half_height_m,
                )
                for start, end in zip(path, path[1:], strict=False)
            )
            route = DroneRoute(drone, slot, tuple(path), segments)
            next_reservations = dict(reservations)
            next_reservations[drone.drone_id] = Reservation(
                drone.drone_id,
                slot.pose,
                motion.swept_radius_m,
                motion.swept_half_height_m,
            )
            tail = self._assign_routes(
                drones[1:],
                slots[:index] + slots[index + 1 :],
                artifact,
                motion,
                next_reservations,
            )
            if tail is not None:
                return (route, *tail)
        return None

    def _route(
        self,
        drone: DronePose,
        goal: Pose,
        artifact: NavigationArtifact,
        motion: MotionConfig,
        reservations: ReservationMap,
    ) -> list[Pose] | None:
        start = drone.pose
        if start.floor_id == goal.floor_id:
            return self._route_same_floor(
                start,
                goal,
                artifact,
                motion,
                reservations,
                drone.drone_id,
            )
        for connector in sorted(artifact.connectors, key=lambda item: item.connector_id):
            if (
                not connector.enabled
                or connector.from_floor_id != start.floor_id
                or connector.to_floor_id != goal.floor_id
                or not pose_supported(connector.from_pose, artifact, motion)
                or not pose_supported(connector.to_pose, artifact, motion)
            ):
                continue
            first = self._route_same_floor(
                start,
                connector.from_pose,
                artifact,
                motion,
                reservations,
                drone.drone_id,
            )
            second = self._route_same_floor(
                connector.to_pose,
                goal,
                artifact,
                motion,
                reservations,
                drone.drone_id,
            )
            if (
                first is None
                or second is None
                or not vertical_path_clear(
                    connector.from_pose,
                    connector.to_pose,
                    artifact,
                    motion.swept_half_height_m,
                )
            ):
                continue
            if segment_hits_reservation(
                connector.from_pose,
                connector.to_pose,
                reservations,
                motion.swept_radius_m,
                motion.swept_half_height_m,
                drone.drone_id,
            ):
                continue
            path = join_paths(first, [connector.from_pose, connector.to_pose], second)
            if path_clear(path, artifact, motion, reservations, drone.drone_id):
                return path
        return None

    def _route_same_floor(
        self,
        start: Pose,
        goal: Pose,
        artifact: NavigationArtifact,
        motion: MotionConfig,
        reservations: ReservationMap,
        drone_id: int,
    ) -> list[Pose] | None:
        if (
            start.floor_id != goal.floor_id
            or not pose_supported(start, artifact, motion)
            or not pose_supported(goal, artifact, motion)
        ):
            return None
        start_level = level_for_pose(
            start,
            artifact.grids,
            artifact.grid_clearance_m,
            motion.swept_half_height_m,
        )
        goal_level = level_for_pose(
            goal,
            artifact.grids,
            artifact.grid_clearance_m,
            motion.swept_half_height_m,
        )
        if start_level is None or goal_level is None:
            return None
        if start_level == goal_level:
            return self._route_on_level(
                start,
                goal,
                start_level,
                reservations,
                motion,
                drone_id,
                artifact.grid_clearance_m,
            )
        horizontal_goal = Pose(goal.x_m, goal.y_m, start_level.z_m, start.floor_id)
        horizontal = self._route_on_level(
            start,
            horizontal_goal,
            start_level,
            reservations,
            motion,
            drone_id,
            artifact.grid_clearance_m,
        )
        if horizontal is None:
            return None
        vertical = vertical_waypoints(
            horizontal_goal,
            goal,
            artifact,
            motion,
            reservations,
            drone_id,
        )
        if vertical is None:
            return None
        path = join_paths(horizontal, vertical)
        return path if path_clear(path, artifact, motion, reservations, drone_id) else None

    def _route_on_level(
        self,
        start: Pose,
        goal: Pose,
        level: GridLevel,
        reservations: ReservationMap,
        motion: MotionConfig,
        drone_id: int,
        grid_clearance_m: float,
    ) -> list[Pose] | None:
        if not grid_covers_pose(
            level,
            start,
            grid_clearance_m,
            motion.swept_half_height_m,
        ) or not grid_covers_pose(
            level,
            goal,
            grid_clearance_m,
            motion.swept_half_height_m,
        ):
            return None
        start_cell, goal_cell = level.cell_for(start), level.cell_for(goal)
        if not level.free(start_cell) or not level.free(goal_cell):
            return None
        if blocked_by_stationary(
            start,
            reservations,
            motion.swept_radius_m,
            motion.swept_half_height_m,
            drone_id,
        ):
            return None
        cells = self._astar(level, start_cell, goal_cell, reservations, motion, drone_id)
        if cells is None:
            return None
        points = compress_collinear([start, *(level.pose_for(cell) for cell in cells[1:-1]), goal])
        if any(
            not line_is_free(first, second, level)
            for first, second in zip(points, points[1:], strict=False)
        ):
            return None
        if any(
            segment_hits_reservation(
                first,
                second,
                reservations,
                motion.swept_radius_m,
                motion.swept_half_height_m,
                drone_id,
            )
            for first, second in zip(points, points[1:], strict=False)
        ):
            return None
        return points

    @staticmethod
    def _astar(
        level: GridLevel,
        start: tuple[int, int],
        goal: tuple[int, int],
        reservations: ReservationMap,
        motion: MotionConfig,
        drone_id: int,
    ) -> list[tuple[int, int]] | None:
        frontier = [(0.0, 0.0, start)]
        parents: dict[tuple[int, int], tuple[int, int] | None] = {start: None}
        costs = {start: 0.0}
        while frontier:
            _, cost, current = heappop(frontier)
            if cost != costs.get(current):
                continue
            if current == goal:
                path = []
                cursor: tuple[int, int] | None = current
                while cursor is not None:
                    path.append(cursor)
                    cursor = parents[cursor]
                return list(reversed(path))
            current_pose = level.pose_for(current)
            for dx, dy in ((-1, 0), (0, -1), (0, 1), (1, 0)):
                candidate = (current[0] + dx, current[1] + dy)
                if not level.free(candidate):
                    continue
                pose = level.pose_for(candidate)
                if blocked_by_stationary(
                    pose,
                    reservations,
                    motion.swept_radius_m,
                    motion.swept_half_height_m,
                    drone_id,
                ) or segment_hits_reservation(
                    current_pose,
                    pose,
                    reservations,
                    motion.swept_radius_m,
                    motion.swept_half_height_m,
                    drone_id,
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
