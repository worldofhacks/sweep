"""Map-pinned formation previews built from clearance-checked navigation routes."""

from __future__ import annotations

from dataclasses import dataclass, replace
from itertools import permutations
from math import cos, dist, isfinite, sin
from typing import Literal

from planner.navigation import (
    ArrivalSlot,
    DronePose,
    MotionConfig,
    NavigationArtifact,
    NavigationPermission,
    NavigationPlan,
    NavigationPlanner,
    NavigationRefusal,
    NavigationRequest,
    Pose,
    Zone,
)
from planner.navigation_geometry import pose_supported
from tools.geometry_math import point_inside, polygon, segments_intersect

FormationShape = Literal["line", "column", "wedge", "diamond"]


def _number(value: float, name: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not isfinite(value):
        raise ValueError(f"{name} must be finite")
    value = float(value)
    if positive and value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


@dataclass(frozen=True, slots=True)
class FormationZone:
    zone_id: str
    floor_id: str
    polygon_xy: tuple[tuple[float, float], ...]
    z_min_m: float
    z_max_m: float
    owner_approved: bool
    formation_enabled: bool

    def __post_init__(self) -> None:
        if not self.zone_id or not self.floor_id:
            raise ValueError("formation zone identity is required")
        polygon([list(point) for point in self.polygon_xy])
        if _number(self.z_min_m, "z_min_m") >= _number(self.z_max_m, "z_max_m"):
            raise ValueError("formation zone altitude bounds must increase")


@dataclass(frozen=True, slots=True)
class FormationPermission:
    permitted_zone_ids: frozenset[str]


@dataclass(frozen=True, slots=True)
class FormationLayout:
    center: Pose
    heading_rad: float
    spacing_m: float
    altitude_offsets_m: tuple[float, ...]

    def __post_init__(self) -> None:
        _number(self.heading_rad, "heading_rad")
        _number(self.spacing_m, "spacing_m", positive=True)
        if not self.altitude_offsets_m:
            raise ValueError("altitude_offsets_m is required")
        for offset in self.altitude_offsets_m:
            _number(offset, "altitude offset")


@dataclass(frozen=True, slots=True)
class MappedFormationRequest:
    shape: FormationShape
    roster_version: int
    plan_revision: int
    selected: tuple[DronePose, ...]
    all_positions: tuple[DronePose, ...]
    airborne_drone_ids: frozenset[int]
    motion: MotionConfig
    permission: FormationPermission
    layout: FormationLayout

    def __post_init__(self) -> None:
        if self.shape not in {"line", "column", "wedge", "diamond"}:
            raise ValueError("unknown formation shape")
        if self.roster_version < 0 or self.plan_revision < 0 or len(self.selected) not in {2, 4}:
            raise ValueError("mapped formations require two or four selected aircraft")
        if len({drone.drone_id for drone in self.selected}) != len(self.selected):
            raise ValueError("selected drone ids must be unique")
        if len(self.layout.altitude_offsets_m) != len(self.selected):
            raise ValueError("altitude offsets must match selected aircraft")
        if len({drone.drone_id for drone in self.all_positions}) != len(self.all_positions):
            raise ValueError("all_positions contains duplicate drone ids")
        known = {drone.drone_id: drone for drone in self.all_positions}
        if any(known.get(drone.drone_id) != drone for drone in self.selected):
            raise ValueError("all_positions must include selected aircraft")


@dataclass(frozen=True, slots=True)
class FormationSlot:
    slot_id: str
    pose: Pose


@dataclass(frozen=True, slots=True)
class SlotAssignment:
    drone: DronePose
    slot: FormationSlot
    cost_m: float


@dataclass(frozen=True, slots=True)
class MappedFormationPlan:
    shape: FormationShape
    formation_zone_id: str
    roster_version: int
    assignments: tuple[SlotAssignment, ...]
    navigation_plan: NavigationPlan


@dataclass(frozen=True, slots=True)
class FormationRefusal:
    code: Literal[
        "formation_not_permitted",
        "formation_zone_unapproved",
        "grounded_aircraft",
        "shape_unavailable",
        "insufficient_clearance",
        "slot_outside_formation_zone",
        "slot_blocked",
        "slot_separation",
        "approach_crossing",
        "route_refused",
    ]
    detail: str


class MappedFormationPlanner:
    def __init__(self, navigation: NavigationPlanner | None = None) -> None:
        self._navigation = navigation or NavigationPlanner()

    def plan(
        self,
        request: MappedFormationRequest,
        artifact: NavigationArtifact,
        formation_zone: FormationZone,
    ) -> MappedFormationPlan | FormationRefusal:
        if formation_zone.zone_id not in request.permission.permitted_zone_ids:
            return FormationRefusal(
                "formation_not_permitted",
                f"formation permission is missing for {formation_zone.zone_id}",
            )
        if not formation_zone.owner_approved or not formation_zone.formation_enabled:
            return FormationRefusal(
                "formation_zone_unapproved",
                f"formation zone is not approved: {formation_zone.zone_id}",
            )
        if request.layout.center.floor_id != formation_zone.floor_id:
            return FormationRefusal(
                "slot_outside_formation_zone", "formation center is on the wrong floor"
            )
        if any(drone.drone_id not in request.airborne_drone_ids for drone in request.selected):
            return FormationRefusal(
                "grounded_aircraft", "formation does not take off grounded aircraft"
            )
        if not _shape_available(request.shape, len(request.selected)):
            return FormationRefusal(
                "shape_unavailable", "shape is unavailable for selected aircraft count"
            )
        if request.motion.swept_radius_m > artifact.grid_clearance_m + 1e-9:
            return FormationRefusal(
                "insufficient_clearance", "motion clearance exceeds geometry inflation"
            )
        slots = _slots(request.shape, request.layout)
        refusal = self._validate_slots(slots, request.motion, artifact, formation_zone)
        if refusal is not None:
            return refusal
        assignments = _optimal_assignments(request.selected, slots)
        navigation = self._navigation.plan(
            NavigationRequest(
                f"formation:{formation_zone.zone_id}",
                request.roster_version,
                request.plan_revision,
                tuple(assignment.drone for assignment in assignments),
                request.all_positions,
                request.motion,
                NavigationPermission(frozenset({f"formation:{formation_zone.zone_id}"})),
            ),
            _formation_artifact(artifact, formation_zone, assignments, request.motion),
        )
        if isinstance(navigation, NavigationRefusal):
            return FormationRefusal(
                "route_refused", f"formation approach refused: {navigation.code}"
            )
        if _approaches_cross(navigation):
            return FormationRefusal("approach_crossing", "sequential approaches cross")
        return MappedFormationPlan(
            request.shape,
            formation_zone.zone_id,
            request.roster_version,
            assignments,
            navigation,
        )

    def _validate_slots(
        self,
        slots: tuple[FormationSlot, ...],
        motion: MotionConfig,
        artifact: NavigationArtifact,
        zone: FormationZone,
    ) -> FormationRefusal | None:
        boundary = [list(point) for point in zone.polygon_xy]
        for slot in slots:
            if not _circle_inside(boundary, slot.pose, motion.swept_radius_m):
                return FormationRefusal(
                    "slot_outside_formation_zone",
                    f"slot is outside formation volume: {slot.slot_id}",
                )
            if not (
                zone.z_min_m <= slot.pose.z_m - motion.aircraft_height_m / 2
                and slot.pose.z_m + motion.aircraft_height_m / 2 <= zone.z_max_m
            ):
                return FormationRefusal(
                    "slot_outside_formation_zone",
                    f"slot altitude is outside formation volume: {slot.slot_id}",
                )
            if not pose_supported(slot.pose, artifact, motion):
                return FormationRefusal(
                    "slot_blocked", f"slot is outside known free space: {slot.slot_id}"
                )
        for index, first in enumerate(slots):
            for second in slots[index + 1 :]:
                if dist(first.pose.xyz, second.pose.xyz) < 2 * motion.swept_radius_m:
                    return FormationRefusal("slot_separation", "formation slots violate separation")
        return None


def _shape_available(shape: FormationShape, count: int) -> bool:
    return count == 2 and shape in {"line", "column"} or count == 4


def _slots(shape: FormationShape, layout: FormationLayout) -> tuple[FormationSlot, ...]:
    offsets = {
        "line": ((-1.5, 0.0), (-0.5, 0.0), (0.5, 0.0), (1.5, 0.0)),
        "column": ((0.0, -1.5), (0.0, -0.5), (0.0, 0.5), (0.0, 1.5)),
        "wedge": ((1.0, 0.0), (0.0, -0.75), (0.0, 0.75), (-1.0, 0.0)),
        "diamond": ((0.0, 1.0), (1.0, 0.0), (0.0, -1.0), (-1.0, 0.0)),
    }[shape]
    if len(layout.altitude_offsets_m) == 2:
        offsets = offsets[1:3] if shape in {"line", "column"} else offsets[:2]
    c, s = cos(layout.heading_rad), sin(layout.heading_rad)
    return tuple(
        FormationSlot(
            f"slot-{index:02d}",
            Pose(
                layout.center.x_m + layout.spacing_m * (x * c - y * s),
                layout.center.y_m + layout.spacing_m * (x * s + y * c),
                layout.center.z_m + layout.altitude_offsets_m[index],
                layout.center.floor_id,
            ),
        )
        for index, (x, y) in enumerate(offsets)
    )


def _optimal_assignments(
    drones: tuple[DronePose, ...], slots: tuple[FormationSlot, ...]
) -> tuple[SlotAssignment, ...]:
    ordered = tuple(sorted(drones, key=lambda drone: drone.drone_id))
    candidates = []
    for ordering in permutations(range(len(slots))):
        costs = tuple(
            dist(drone.pose.xyz, slots[index].pose.xyz)
            for drone, index in zip(ordered, ordering, strict=True)
        )
        candidates.append((sum(costs), ordering, costs))
    _, ordering, costs = min(candidates, key=lambda item: (item[0], item[1]))
    return tuple(
        SlotAssignment(drone, slots[index], cost)
        for drone, index, cost in zip(ordered, ordering, costs, strict=True)
    )


def _formation_artifact(
    artifact: NavigationArtifact,
    zone: FormationZone,
    assignments: tuple[SlotAssignment, ...],
    motion: MotionConfig,
) -> NavigationArtifact:
    route_zone_id = f"formation:{zone.zone_id}"
    route_zone = Zone(
        route_zone_id,
        zone.floor_id,
        True,
        zone.polygon_xy,
        zone.z_min_m,
        zone.z_max_m,
        tuple(
            ArrivalSlot(
                f"route-{index:02d}", route_zone_id, assignment.slot.pose, motion.swept_radius_m, motion.swept_half_height_m
            )
            for index, assignment in enumerate(assignments)
        ),
    )
    return replace(artifact, zones=(*artifact.zones, route_zone))


def _circle_inside(boundary: list[list[float]], pose: Pose, radius_m: float) -> bool:
    return all(
        point_inside(boundary, (pose.x_m + radius_m * cos(angle), pose.y_m + radius_m * sin(angle)))
        for angle in tuple(index * 2 * 3.141592653589793 / 16 for index in range(16))
    )


def _approaches_cross(plan: NavigationPlan) -> bool:
    for route_index, first in enumerate(plan.routes):
        for second in plan.routes[route_index + 1 :]:
            for a in first.swept_segments:
                for b in second.swept_segments:
                    if abs((a.start.z_m + a.end.z_m - b.start.z_m - b.end.z_m) / 2) < (
                        2 * (a.half_height_m + b.half_height_m)
                    ) / 2 and segments_intersect(
                        (a.start.x_m, a.start.y_m),
                        (a.end.x_m, a.end.y_m),
                        (b.start.x_m, b.start.y_m),
                        (b.end.x_m, b.end.y_m),
                    ):
                        return True
    return False
