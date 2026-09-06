"""Buffered Voronoi Cell projection for simultaneous velocity setpoints."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from math import isfinite, sqrt

from planner.models import Geofence, Position

_EPSILON = 1e-9
_PROJECTION_PASSES = 96


@dataclass(frozen=True, slots=True)
class BvcConfig:
    min_spacing_m: float
    horizon_s: float
    geofence: Geofence
    ceiling_m: float

    def __post_init__(self) -> None:
        for name, value in (
            ("min_spacing_m", self.min_spacing_m),
            ("horizon_s", self.horizon_s),
            ("ceiling_m", self.ceiling_m),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"{name} must be a finite positive number")
        if self.ceiling_m > self.geofence.max_z:
            raise ValueError("ceiling_m cannot exceed the geofence maximum")


def filter_velocities(
    positions: Mapping[int, Position],
    velocities: Mapping[int, Position],
    config: BvcConfig,
) -> dict[int, Position]:
    """Project simultaneous velocity requests into buffered cells.

    Invalid, incomplete, or already unsafe state returns zero velocity for every
    identified aircraft. The result is safe over ``config.horizon_s`` when all
    aircraft apply it simultaneously.
    """
    ids = _valid_ids(positions, velocities)
    zero = {drone_id: Position(0.0, 0.0, 0.0) for drone_id in ids}
    if (
        not ids
        or set(positions) != set(velocities)
        or not _safe_input(positions, velocities, config)
    ):
        return zero

    result: dict[int, Position] = {}
    for drone_id in ids:
        position = positions[drone_id]
        velocity = velocities[drone_id]
        constraints = _constraints(drone_id, positions, position, config)
        desired = _scale(velocity, config.horizon_s)
        candidate = _add_sidestep(desired, constraints)
        projected = _project(candidate, constraints)
        if projected is None:
            return zero
        result[drone_id] = _scale(projected, 1.0 / config.horizon_s)
    return result


def _valid_ids(
    positions: Mapping[int, Position], velocities: Mapping[int, Position]
) -> tuple[int, ...]:
    ids = set()
    for mapping in (positions, velocities):
        for drone_id in mapping:
            if isinstance(drone_id, int) and not isinstance(drone_id, bool) and drone_id > 0:
                ids.add(drone_id)
    return tuple(sorted(ids))


def _safe_input(
    positions: Mapping[int, Position], velocities: Mapping[int, Position], config: BvcConfig
) -> bool:
    if any(
        not isinstance(drone_id, int)
        or isinstance(drone_id, bool)
        or drone_id <= 0
        or not isinstance(position, Position)
        or not isinstance(velocities.get(drone_id), Position)
        for drone_id, position in positions.items()
    ):
        return False
    if any(not _finite_vector(velocity) for velocity in velocities.values()):
        return False
    ceiling = min(config.ceiling_m, config.geofence.max_z)
    ordered = tuple(sorted(positions))
    for index, drone_id in enumerate(ordered):
        position = positions[drone_id]
        if not config.geofence.contains(position) or position.z > ceiling:
            return False
        if any(
            position.distance_to(positions[other_id]) < config.min_spacing_m
            for other_id in ordered[index + 1 :]
        ):
            return False
    return True


def _constraints(
    drone_id: int,
    positions: Mapping[int, Position],
    position: Position,
    config: BvcConfig,
) -> list[tuple[Position, float]]:
    constraints: list[tuple[Position, float]] = []
    buffer_radius = config.min_spacing_m / 2.0
    for other_id, other in sorted(positions.items()):
        if other_id == drone_id:
            continue
        delta = _subtract(other, position)
        distance = _norm(delta)
        normal = _scale(delta, 1.0 / distance)
        constraints.append((normal, distance / 2.0 - buffer_radius))

    upper_z = min(config.ceiling_m, config.geofence.max_z)
    bounds = (
        (Position(1.0, 0.0, 0.0), config.geofence.max_x - position.x),
        (Position(-1.0, 0.0, 0.0), position.x - config.geofence.min_x),
        (Position(0.0, 1.0, 0.0), config.geofence.max_y - position.y),
        (Position(0.0, -1.0, 0.0), position.y - config.geofence.min_y),
        (Position(0.0, 0.0, 1.0), upper_z - position.z),
        (Position(0.0, 0.0, -1.0), position.z - config.geofence.min_z),
    )
    constraints.extend(bounds)
    return constraints


def _add_sidestep(
    desired: Position, constraints: list[tuple[Position, float]]
) -> Position:
    lateral = Position(0.0, 0.0, 0.0)
    for normal, limit in constraints:
        approach = _dot(desired, normal)
        if approach <= limit + _EPSILON:
            continue
        horizontal = sqrt(desired.x * desired.x + desired.y * desired.y)
        if horizontal <= _EPSILON:
            continue
        fraction = min(1.0, max(0.0, (approach - limit) / max(approach, _EPSILON)))
        lateral = _add(
            lateral,
            Position(desired.y * fraction * fraction, -desired.x * fraction * fraction, 0.0),
        )
    return _add(desired, lateral)


def _project(value: Position, constraints: list[tuple[Position, float]]) -> Position | None:
    result = value
    for _ in range(_PROJECTION_PASSES):
        changed = False
        for normal, limit in constraints:
            excess = _dot(result, normal) - limit
            if excess > _EPSILON:
                result = _subtract(result, _scale(normal, excess / _dot(normal, normal)))
                changed = True
        if not changed:
            break
    if not _finite_vector(result) or any(
        _dot(result, normal) > limit + _EPSILON for normal, limit in constraints
    ):
        return None
    return result


def _finite_vector(value: Position) -> bool:
    return all(isfinite(component) for component in (value.x, value.y, value.z))


def _add(left: Position, right: Position) -> Position:
    return Position(left.x + right.x, left.y + right.y, left.z + right.z)


def _subtract(left: Position, right: Position) -> Position:
    return Position(left.x - right.x, left.y - right.y, left.z - right.z)


def _scale(value: Position, scalar: float) -> Position:
    return Position(value.x * scalar, value.y * scalar, value.z * scalar)


def _dot(left: Position, right: Position) -> float:
    return left.x * right.x + left.y * right.y + left.z * right.z


def _norm(value: Position) -> float:
    return sqrt(_dot(value, value))
