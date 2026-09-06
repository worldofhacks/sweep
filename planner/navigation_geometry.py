"""Conservative 3-D geometry and aircraft-clearance primitives."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from math import sqrt

from planner.navigation_artifacts import NavigationArtifact
from planner.navigation_contracts import (
    EPS,
    GridLevel,
    MotionConfig,
    Pose,
    free_grid_covers_pose,
    grid_covers_pose,
    volume_contains,
)
from tools.geometry_math import rect_segment_distance


@dataclass(frozen=True, slots=True)
class Reservation:
    drone_id: int
    pose: Pose
    radius_m: float
    half_height_m: float


ReservationMap = Mapping[int, Reservation]


def level_for_pose(
    pose: Pose,
    levels: tuple[GridLevel, ...],
    grid_clearance_m: float,
    half_height_m: float,
) -> GridLevel | None:
    candidates = [
        level
        for level in levels
        if free_grid_covers_pose(level, pose, grid_clearance_m, half_height_m)
    ]
    return min(candidates, key=lambda level: (abs(level.z_m - pose.z_m), level.z_m), default=None)


def pose_supported(pose: Pose, artifact: NavigationArtifact, motion: MotionConfig) -> bool:
    return (
        volume_contains(
            pose,
            artifact.geofence_polygon_xy,
            artifact.geofence_z_min_m,
            artifact.geofence_z_max_m,
            motion.swept_radius_m,
            motion.swept_half_height_m,
        )
        and level_for_pose(
            pose,
            artifact.grids,
            artifact.grid_clearance_m,
            motion.swept_half_height_m,
        )
        is not None
    )


def vertical_path_clear(
    start: Pose,
    end: Pose,
    artifact: NavigationArtifact,
    half_height_m: float,
) -> bool:
    """Require the entire vertical interval to be covered by clear inflated bands."""
    if (start.x_m, start.y_m) != (end.x_m, end.y_m):
        return False
    available = artifact.grid_clearance_m - half_height_m
    if available < -EPS:
        return False
    intervals = []
    for level in artifact.grids:
        probe = Pose(start.x_m, start.y_m, level.z_m, level.floor_id)
        if level.free(level.cell_for(probe)):
            intervals.append((level.z_m - available, level.z_m + available))
    low = min(start.z_m, end.z_m)
    high = max(start.z_m, end.z_m)
    cursor = low
    for interval_low, interval_high in sorted(intervals):
        if interval_high < cursor - EPS:
            continue
        if interval_low > cursor + EPS:
            return False
        cursor = max(cursor, interval_high)
        if cursor >= high - EPS:
            return True
    return False


def vertical_waypoints(
    start: Pose,
    end: Pose,
    artifact: NavigationArtifact,
    motion: MotionConfig,
    reservations: ReservationMap,
    drone_id: int,
) -> list[Pose] | None:
    if not vertical_path_clear(start, end, artifact, motion.swept_half_height_m):
        return None
    low, high = sorted((start.z_m, end.z_m))
    levels = sorted(
        {
            level.z_m
            for level in artifact.grids
            if level.floor_id == start.floor_id and low < level.z_m < high
        },
        reverse=end.z_m < start.z_m,
    )
    points = deduplicate(
        [
            start,
            *(Pose(start.x_m, start.y_m, z_m, start.floor_id) for z_m in levels),
            end,
        ]
    )
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


def first_overlap(reservations: ReservationMap) -> tuple[int, int] | None:
    ordered = [reservations[key] for key in sorted(reservations)]
    for index, first in enumerate(ordered):
        for second in ordered[index + 1 :]:
            if static_overlap(first, second):
                return (first.drone_id, second.drone_id)
    return None


def static_overlap(first: Reservation, second: Reservation) -> bool:
    horizontal = sqrt(
        (first.pose.x_m - second.pose.x_m) ** 2 + (first.pose.y_m - second.pose.y_m) ** 2
    )
    vertical = abs(first.pose.z_m - second.pose.z_m)
    return (
        horizontal <= first.radius_m + second.radius_m + EPS
        and vertical <= first.half_height_m + second.half_height_m + EPS
    )


def blocked_by_stationary(
    pose: Pose,
    reservations: ReservationMap,
    radius_m: float,
    half_height_m: float,
    exempt_drone_id: int,
) -> bool:
    probe = Reservation(exempt_drone_id, pose, radius_m, half_height_m)
    return any(
        drone_id != exempt_drone_id and static_overlap(probe, reservation)
        for drone_id, reservation in reservations.items()
    )


def segment_hits_reservation(
    start: Pose,
    end: Pose,
    reservations: ReservationMap,
    radius_m: float,
    half_height_m: float,
    exempt_drone_id: int,
) -> bool:
    """Analytically intersect horizontal and vertical collision-time intervals."""
    for drone_id, reservation in reservations.items():
        if drone_id == exempt_drone_id:
            continue
        horizontal = _horizontal_collision_interval(
            start,
            end,
            reservation.pose,
            radius_m + reservation.radius_m,
        )
        vertical = _vertical_collision_interval(
            start,
            end,
            reservation.pose,
            half_height_m + reservation.half_height_m,
        )
        if (
            horizontal is not None
            and vertical is not None
            and (max(horizontal[0], vertical[0], 0.0) <= min(horizontal[1], vertical[1], 1.0) + EPS)
        ):
            return True
    return False


def _horizontal_collision_interval(
    start: Pose,
    end: Pose,
    other: Pose,
    clearance_m: float,
) -> tuple[float, float] | None:
    dx, dy = end.x_m - start.x_m, end.y_m - start.y_m
    x, y = start.x_m - other.x_m, start.y_m - other.y_m
    a = dx * dx + dy * dy
    c = x * x + y * y - (clearance_m + EPS) ** 2
    if a <= EPS:
        return (-float("inf"), float("inf")) if c <= 0 else None
    b = 2 * (x * dx + y * dy)
    discriminant = b * b - 4 * a * c
    if discriminant < 0:
        return None
    root = sqrt(max(0.0, discriminant))
    return ((-b - root) / (2 * a), (-b + root) / (2 * a))


def _vertical_collision_interval(
    start: Pose,
    end: Pose,
    other: Pose,
    clearance_m: float,
) -> tuple[float, float] | None:
    delta = end.z_m - start.z_m
    offset = start.z_m - other.z_m
    if abs(delta) <= EPS:
        return (-float("inf"), float("inf")) if abs(offset) <= clearance_m + EPS else None
    first = (-clearance_m - EPS - offset) / delta
    second = (clearance_m + EPS - offset) / delta
    return (min(first, second), max(first, second))


def line_is_free(start: Pose, end: Pose, level: GridLevel) -> bool:
    """Check every grid cell touched by a segment; boundary contact counts as occupied."""
    if start.floor_id != level.floor_id or end.floor_id != level.floor_id:
        return False
    start_cell, end_cell = level.cell_for(start), level.cell_for(end)
    if not level.free(start_cell) or not level.free(end_cell):
        return False
    low_x = max(-1, min(start_cell[0], end_cell[0]) - 1)
    high_x = min(level.width, max(start_cell[0], end_cell[0]) + 1)
    low_y = max(-1, min(start_cell[1], end_cell[1]) - 1)
    high_y = min(level.height, max(start_cell[1], end_cell[1]) + 1)
    segment_start = (start.x_m, start.y_m)
    segment_end = (end.x_m, end.y_m)
    for x in range(low_x, high_x + 1):
        for y in range(low_y, high_y + 1):
            rect = (
                level.origin_xy_m[0] + x * level.cell_m,
                level.origin_xy_m[1] + y * level.cell_m,
                level.origin_xy_m[0] + (x + 1) * level.cell_m,
                level.origin_xy_m[1] + (y + 1) * level.cell_m,
            )
            if rect_segment_distance(rect, segment_start, segment_end) <= EPS and not level.free(
                (x, y)
            ):
                return False
    return True


def segment_geometry_clear(
    start: Pose,
    end: Pose,
    artifact: NavigationArtifact,
    motion: MotionConfig,
) -> bool:
    if start.floor_id == end.floor_id:
        levels = [
            level
            for level in artifact.grids
            if grid_covers_pose(
                level,
                start,
                artifact.grid_clearance_m,
                motion.swept_half_height_m,
            )
            and grid_covers_pose(
                level,
                end,
                artifact.grid_clearance_m,
                motion.swept_half_height_m,
            )
        ]
        if levels:
            level = min(levels, key=lambda item: (abs(item.z_m - start.z_m), item.z_m))
            return line_is_free(start, end, level)
        if (start.x_m, start.y_m) == (end.x_m, end.y_m):
            return vertical_path_clear(start, end, artifact, motion.swept_half_height_m)
        return False
    if (start.x_m, start.y_m) != (end.x_m, end.y_m):
        return False
    if not any(
        connector.enabled and connector.from_pose == start and connector.to_pose == end
        for connector in artifact.connectors
    ):
        return False
    return vertical_path_clear(start, end, artifact, motion.swept_half_height_m)


def path_clear(
    path: list[Pose],
    artifact: NavigationArtifact,
    motion: MotionConfig,
    reservations: ReservationMap,
    drone_id: int,
) -> bool:
    return all(
        segment_geometry_clear(first, second, artifact, motion)
        and not segment_hits_reservation(
            first,
            second,
            reservations,
            motion.swept_radius_m,
            motion.swept_half_height_m,
            drone_id,
        )
        for first, second in zip(path, path[1:], strict=False)
    )


def compress_collinear(points: list[Pose]) -> list[Pose]:
    if len(points) < 3:
        return deduplicate(points)
    result = [points[0]]
    for current, following in zip(points[1:-1], points[2:], strict=False):
        previous = result[-1]
        first = tuple(current.xyz[index] - previous.xyz[index] for index in range(3))
        second = tuple(following.xyz[index] - current.xyz[index] for index in range(3))
        cross = (
            first[1] * second[2] - first[2] * second[1],
            first[2] * second[0] - first[0] * second[2],
            first[0] * second[1] - first[1] * second[0],
        )
        same_direction = sum(first[index] * second[index] for index in range(3)) >= 0
        if (
            previous.floor_id == current.floor_id == following.floor_id
            and max(abs(value) for value in cross) <= EPS
            and same_direction
        ):
            continue
        result.append(current)
    result.append(points[-1])
    return deduplicate(result)


def deduplicate(points: list[Pose]) -> list[Pose]:
    result: list[Pose] = []
    for point in points:
        if not result or result[-1] != point:
            result.append(point)
    return result


def join_paths(*paths: list[Pose]) -> list[Pose]:
    return deduplicate([point for path in paths for point in path])
