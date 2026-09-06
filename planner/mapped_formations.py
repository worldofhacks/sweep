"""Pinned, non-dispatchable formation previews over accepted map geometry."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from math import cos, dist, sin
from typing import Literal

from planner.navigation import NavigationPlanner
from planner.navigation_artifacts import NavigationArtifact
from planner.navigation_contracts import (
    MAX_AIRCRAFT,
    ArrivalSlot,
    ArtifactPin,
    DronePose,
    MotionConfig,
    NavigationPermission,
    NavigationPlan,
    NavigationRefusal,
    NavigationRequest,
    Pose,
    Zone,
    finite_number,
    integer,
    normalized_text,
    polygon_points,
)
from planner.navigation_geometry import Reservation, pose_supported, static_overlap
from tools.geometry_math import distance_to_segment, point_inside, segments_intersect

FormationShape = Literal["line", "column", "wedge", "diamond"]
_SHAPES = frozenset(FormationShape.__args__)
MAX_FORMATION_ROUTE_SEGMENTS = 512


@dataclass(frozen=True, slots=True)
class FormationZone:
    """One separately approved formation volume bound to map and geometry pins."""

    zone_id: str
    floor_id: str
    polygon_xy: tuple[tuple[float, float], ...]
    z_min_m: float
    z_max_m: float
    max_speed_mps: float
    owner_approved: bool
    formation_enabled: bool
    map_pin: ArtifactPin
    geometry_pin: ArtifactPin

    def __post_init__(self) -> None:
        normalized_text(self.zone_id, "formation zone_id")
        normalized_text(self.floor_id, "formation floor_id")
        object.__setattr__(
            self,
            "polygon_xy",
            polygon_points(self.polygon_xy, f"formation zone {self.zone_id} polygon"),
        )
        low = finite_number(self.z_min_m, "formation z_min_m")
        high = finite_number(self.z_max_m, "formation z_max_m")
        if low >= high:
            raise ValueError("formation zone altitude bounds must increase")
        object.__setattr__(self, "z_min_m", low)
        object.__setattr__(self, "z_max_m", high)
        object.__setattr__(
            self,
            "max_speed_mps",
            finite_number(self.max_speed_mps, "formation max_speed_mps", positive=True),
        )
        if type(self.owner_approved) is not bool or type(self.formation_enabled) is not bool:
            raise ValueError("formation approval fields must be booleans")
        if not isinstance(self.map_pin, ArtifactPin) or not isinstance(
            self.geometry_pin, ArtifactPin
        ):
            raise ValueError("formation zone must carry typed map and geometry pins")


@dataclass(frozen=True, slots=True)
class FormationPermission:
    permitted_zone_ids: frozenset[str]

    def __post_init__(self) -> None:
        if not isinstance(self.permitted_zone_ids, frozenset) or any(
            not isinstance(zone_id, str) or not zone_id or zone_id != zone_id.strip()
            for zone_id in self.permitted_zone_ids
        ):
            raise ValueError("formation permission must be an immutable set of zone ids")


@dataclass(frozen=True, slots=True)
class FormationLayout:
    center: Pose
    heading_rad: float
    spacing_m: float
    altitude_offsets_m: tuple[float, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.center, Pose):
            raise ValueError("formation center must be a Pose")
        object.__setattr__(self, "heading_rad", finite_number(self.heading_rad, "heading_rad"))
        object.__setattr__(
            self,
            "spacing_m",
            finite_number(self.spacing_m, "spacing_m", positive=True),
        )
        if not isinstance(self.altitude_offsets_m, tuple) or not self.altitude_offsets_m:
            raise ValueError("altitude_offsets_m must be an immutable nonempty tuple")
        object.__setattr__(
            self,
            "altitude_offsets_m",
            tuple(finite_number(offset, "altitude offset") for offset in self.altitude_offsets_m),
        )


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
        if self.shape not in _SHAPES:
            raise ValueError("unknown formation shape")
        integer(self.roster_version, "formation roster_version")
        integer(self.plan_revision, "formation plan_revision")
        if not isinstance(self.selected, tuple) or not isinstance(self.all_positions, tuple):
            raise ValueError("formation positions must be immutable tuples")
        if len(self.selected) not in {2, 4} or len(self.all_positions) > MAX_AIRCRAFT:
            raise ValueError("mapped formations require two or four aircraft within the MVP cap")
        if not all(isinstance(item, DronePose) for item in (*self.selected, *self.all_positions)):
            raise ValueError("formation positions must contain DronePose values")
        selected_ids = {item.drone_id for item in self.selected}
        positions = {item.drone_id: item for item in self.all_positions}
        if (
            len(selected_ids) != len(self.selected)
            or len(positions) != len(self.all_positions)
            or any(positions.get(item.drone_id) != item for item in self.selected)
        ):
            raise ValueError("all_positions must include every selected aircraft exactly")
        if not isinstance(self.airborne_drone_ids, frozenset) or any(
            type(drone_id) is not int or drone_id < 0 for drone_id in self.airborne_drone_ids
        ):
            raise ValueError("airborne ids must be an immutable set of aircraft ids")
        if not self.airborne_drone_ids <= positions.keys():
            raise ValueError("airborne ids must belong to the frozen roster")
        if (
            not isinstance(self.motion, MotionConfig)
            or not isinstance(self.permission, FormationPermission)
            or not isinstance(self.layout, FormationLayout)
        ):
            raise ValueError("formation request configuration must use contract types")
        if len(self.layout.altitude_offsets_m) != len(self.selected):
            raise ValueError("altitude offsets must match selected aircraft")
        if len(self.selected) == 4 and len(set(self.layout.altitude_offsets_m)) != 4:
            raise ValueError("four-aircraft formations require a distinct altitude offset per slot")
        if len(self.selected) == 2 and self.shape not in {"line", "column"}:
            raise ValueError("two-aircraft formations support only line and column")


@dataclass(frozen=True, slots=True)
class FormationSlot:
    slot_id: str
    pose: Pose

    def __post_init__(self) -> None:
        normalized_text(self.slot_id, "formation slot_id")
        if not isinstance(self.pose, Pose):
            raise ValueError("formation slot pose must be a Pose")


@dataclass(frozen=True, slots=True)
class SlotAssignment:
    drone: DronePose
    slot: FormationSlot
    cost_m: float

    def __post_init__(self) -> None:
        if not isinstance(self.drone, DronePose) or not isinstance(self.slot, FormationSlot):
            raise ValueError("formation assignments require typed drone and slot values")
        object.__setattr__(
            self,
            "cost_m",
            finite_number(self.cost_m, "formation assignment cost"),
        )
        if self.cost_m < 0:
            raise ValueError("formation assignment cost must be nonnegative")


@dataclass(frozen=True, slots=True)
class MappedFormationPlan:
    shape: FormationShape
    formation_zone: FormationZone
    permission: FormationPermission
    layout: FormationLayout
    assignments: tuple[SlotAssignment, ...]
    navigation_plan: NavigationPlan
    dispatch_eligible: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if self.shape not in _SHAPES:
            raise ValueError("formation plan shape is invalid")
        if (
            not isinstance(self.formation_zone, FormationZone)
            or not isinstance(self.permission, FormationPermission)
            or not isinstance(self.layout, FormationLayout)
        ):
            raise ValueError("formation plan configuration must use contract types")
        if (
            not isinstance(self.assignments, tuple)
            or not all(isinstance(item, SlotAssignment) for item in self.assignments)
            or not isinstance(self.navigation_plan, NavigationPlan)
        ):
            raise ValueError("formation plan routes and assignments must use contract types")
        expected = tuple(
            (route.drone, route.arrival_slot.slot_id, route.arrival_slot.pose)
            for route in self.navigation_plan.routes
        )
        actual = tuple(
            (assignment.drone, assignment.slot.slot_id, assignment.slot.pose)
            for assignment in self.assignments
        )
        configured_slots = _slots(self.shape, self.layout, self.formation_zone.zone_id)
        configured_by_id = {slot.slot_id: slot for slot in configured_slots}
        if (
            actual != expected
            or any(
                configured_by_id.get(assignment.slot.slot_id) != assignment.slot
                for assignment in self.assignments
            )
            or self.navigation_plan.dispatch_eligible is not False
            or self.formation_zone.zone_id not in self.permission.permitted_zone_ids
            or self.navigation_plan.map_pin != self.formation_zone.map_pin
            or self.navigation_plan.geometry_pin != self.formation_zone.geometry_pin
            or self.navigation_plan.destination_zone_id
            != f"formation:{self.formation_zone.zone_id}:{self.shape}"
        ):
            raise ValueError("formation assignments must exactly match the frozen navigation plan")


FormationRefusalCode = Literal[
    "formation_not_permitted",
    "formation_artifact_changed",
    "formation_zone_unapproved",
    "grounded_aircraft",
    "insufficient_clearance",
    "slot_outside_formation_zone",
    "slot_blocked",
    "slot_separation",
    "approach_conflict",
    "route_refused",
]
_REFUSAL_CODES = frozenset(FormationRefusalCode.__args__)


@dataclass(frozen=True, slots=True)
class FormationRefusal:
    code: FormationRefusalCode
    detail: str

    def __post_init__(self) -> None:
        if self.code not in _REFUSAL_CODES:
            raise ValueError("unknown formation refusal code")
        normalized_text(self.detail, "formation refusal detail")


class MappedFormationPlanner:
    """Plan the four committed MVP shapes without exposing a dispatch path."""

    __slots__ = ("_navigation",)

    def __init__(self, navigation: NavigationPlanner | None = None) -> None:
        self._navigation = navigation or NavigationPlanner()

    def plan(
        self,
        request: MappedFormationRequest,
        artifact: NavigationArtifact,
        formation_zone: FormationZone,
    ) -> MappedFormationPlan | FormationRefusal:
        if (
            not isinstance(request, MappedFormationRequest)
            or not isinstance(artifact, NavigationArtifact)
            or not isinstance(formation_zone, FormationZone)
        ):
            raise ValueError("formation planner inputs must use contract types")
        if formation_zone.zone_id not in request.permission.permitted_zone_ids:
            return FormationRefusal(
                "formation_not_permitted",
                f"formation permission is missing for {formation_zone.zone_id}",
            )
        if (
            formation_zone.map_pin != artifact.map_pin
            or formation_zone.geometry_pin != artifact.geometry_pin
        ):
            return FormationRefusal(
                "formation_artifact_changed",
                "formation volume does not match the accepted map and geometry pins",
            )
        if not formation_zone.owner_approved or not formation_zone.formation_enabled:
            return FormationRefusal(
                "formation_zone_unapproved",
                f"formation zone is not approved: {formation_zone.zone_id}",
            )
        if request.layout.center.floor_id != formation_zone.floor_id:
            return FormationRefusal(
                "slot_outside_formation_zone",
                "formation center is on the wrong floor",
            )
        if any(item.drone_id not in request.airborne_drone_ids for item in request.selected):
            return FormationRefusal(
                "grounded_aircraft",
                "formation does not take off grounded aircraft",
            )
        if (
            request.motion.swept_radius_m > artifact.grid_clearance_m + 1e-9
            or request.motion.swept_half_height_m > artifact.grid_clearance_m + 1e-9
        ):
            return FormationRefusal(
                "insufficient_clearance",
                "formation motion envelope exceeds accepted geometry inflation",
            )
        slots = _slots(request.shape, request.layout, formation_zone.zone_id)
        refusal = _validate_slots(slots, request.motion, artifact, formation_zone)
        if refusal is not None:
            return refusal
        route_zone_id = f"formation:{formation_zone.zone_id}:{request.shape}"
        navigation = self._navigation.plan(
            NavigationRequest(
                route_zone_id,
                request.roster_version,
                request.plan_revision,
                request.selected,
                request.all_positions,
                request.motion,
                NavigationPermission(frozenset({route_zone_id})),
            ),
            _formation_artifact(artifact, formation_zone, slots, request.motion, route_zone_id),
        )
        if isinstance(navigation, NavigationRefusal):
            return FormationRefusal(
                "route_refused",
                f"formation approach refused: {navigation.code}",
            )
        if any(
            len(route.swept_segments) > MAX_FORMATION_ROUTE_SEGMENTS for route in navigation.routes
        ):
            return FormationRefusal(
                "route_refused",
                "formation approach exceeds the bounded route complexity",
            )
        if _approaches_conflict(navigation):
            return FormationRefusal(
                "approach_conflict",
                "formation approach swept volumes overlap",
            )
        assignments = tuple(
            SlotAssignment(
                route.drone,
                FormationSlot(route.arrival_slot.slot_id, route.arrival_slot.pose),
                sum(dist(segment.start.xyz, segment.end.xyz) for segment in route.swept_segments),
            )
            for route in navigation.routes
        )
        return MappedFormationPlan(
            request.shape,
            formation_zone,
            request.permission,
            request.layout,
            assignments,
            navigation,
        )


def _slots(
    shape: FormationShape,
    layout: FormationLayout,
    zone_id: str,
) -> tuple[FormationSlot, ...]:
    offsets = {
        "line": ((-1.5, 0.0), (-0.5, 0.0), (0.5, 0.0), (1.5, 0.0)),
        "column": ((0.0, -1.5), (0.0, -0.5), (0.0, 0.5), (0.0, 1.5)),
        "wedge": ((1.0, 0.0), (0.0, -0.75), (0.0, 0.75), (-1.0, 0.0)),
        "diamond": ((0.0, 1.0), (1.0, 0.0), (0.0, -1.0), (-1.0, 0.0)),
    }[shape]
    if len(layout.altitude_offsets_m) == 2:
        offsets = offsets[1:3]
    cosine, sine = cos(layout.heading_rad), sin(layout.heading_rad)
    return tuple(
        FormationSlot(
            f"formation:{zone_id}:{shape}:slot-{index:02d}",
            Pose(
                layout.center.x_m + layout.spacing_m * (x * cosine - y * sine),
                layout.center.y_m + layout.spacing_m * (x * sine + y * cosine),
                layout.center.z_m + layout.altitude_offsets_m[index],
                layout.center.floor_id,
            ),
        )
        for index, (x, y) in enumerate(offsets)
    )


def _validate_slots(
    slots: tuple[FormationSlot, ...],
    motion: MotionConfig,
    artifact: NavigationArtifact,
    zone: FormationZone,
) -> FormationRefusal | None:
    for slot in slots:
        if not _circle_inside(zone.polygon_xy, slot.pose, motion.swept_radius_m) or not (
            zone.z_min_m <= slot.pose.z_m - motion.swept_half_height_m
            and slot.pose.z_m + motion.swept_half_height_m <= zone.z_max_m
        ):
            return FormationRefusal(
                "slot_outside_formation_zone",
                f"slot is outside formation volume: {slot.slot_id}",
            )
        if not pose_supported(slot.pose, artifact, motion):
            return FormationRefusal(
                "slot_blocked",
                f"slot is outside accepted free space: {slot.slot_id}",
            )
    reservations = tuple(
        Reservation(index, slot.pose, motion.swept_radius_m, motion.swept_half_height_m)
        for index, slot in enumerate(slots)
    )
    if any(
        static_overlap(first, second)
        for index, first in enumerate(reservations)
        for second in reservations[index + 1 :]
    ):
        return FormationRefusal(
            "slot_separation",
            "formation slots violate the full horizontal and vertical separation envelope",
        )
    return None


def _formation_artifact(
    artifact: NavigationArtifact,
    zone: FormationZone,
    slots: tuple[FormationSlot, ...],
    motion: MotionConfig,
    route_zone_id: str,
) -> NavigationArtifact:
    if any(item.zone_id == route_zone_id for item in artifact.zones):
        raise ValueError("formation route zone collides with an accepted zone id")
    route_zone = Zone(
        route_zone_id,
        zone.floor_id,
        True,
        zone.polygon_xy,
        zone.z_min_m,
        zone.z_max_m,
        tuple(
            ArrivalSlot(
                slot.slot_id,
                route_zone_id,
                slot.pose,
                motion.swept_radius_m,
                motion.swept_half_height_m,
            )
            for slot in slots
        ),
    )
    return replace(artifact, zones=(*artifact.zones, route_zone))


def _circle_inside(
    boundary: tuple[tuple[float, float], ...],
    pose: Pose,
    radius_m: float,
) -> bool:
    center = (pose.x_m, pose.y_m)
    return point_inside(boundary, center) and all(
        distance_to_segment(center, first, second) >= radius_m - 1e-9
        for first, second in zip(boundary, boundary[1:], strict=False)
    )


def _segment_distance(
    first_start: Pose,
    first_end: Pose,
    second_start: Pose,
    second_end: Pose,
) -> float:
    first = ((first_start.x_m, first_start.y_m), (first_end.x_m, first_end.y_m))
    second = ((second_start.x_m, second_start.y_m), (second_end.x_m, second_end.y_m))
    if segments_intersect(first[0], first[1], second[0], second[1]):
        return 0.0
    return min(
        distance_to_segment(first[0], second[0], second[1]),
        distance_to_segment(first[1], second[0], second[1]),
        distance_to_segment(second[0], first[0], first[1]),
        distance_to_segment(second[1], first[0], first[1]),
    )


def _approaches_conflict(plan: NavigationPlan) -> bool:
    for route_index, first in enumerate(plan.routes):
        for second in plan.routes[route_index + 1 :]:
            for first_segment in first.swept_segments:
                first_low = min(first_segment.start.z_m, first_segment.end.z_m) - (
                    first_segment.half_height_m
                )
                first_high = max(first_segment.start.z_m, first_segment.end.z_m) + (
                    first_segment.half_height_m
                )
                for second_segment in second.swept_segments:
                    second_low = min(second_segment.start.z_m, second_segment.end.z_m) - (
                        second_segment.half_height_m
                    )
                    second_high = max(second_segment.start.z_m, second_segment.end.z_m) + (
                        second_segment.half_height_m
                    )
                    vertical_overlap = (
                        max(first_low, second_low) <= min(first_high, second_high) + 1e-9
                    )
                    if (
                        vertical_overlap
                        and _segment_distance(
                            first_segment.start,
                            first_segment.end,
                            second_segment.start,
                            second_segment.end,
                        )
                        <= first_segment.radius_m + second_segment.radius_m + 1e-9
                    ):
                        return True
    return False
