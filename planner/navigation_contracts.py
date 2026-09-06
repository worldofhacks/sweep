"""Immutable value contracts for known-map navigation previews."""

from __future__ import annotations

from dataclasses import dataclass, field
from math import floor, isfinite
from typing import Literal

from tools.geometry_math import distance_to_segment, point_inside, polygon

EPS = 1e-9
MAX_GRID_CELLS = 100_000
MAX_AIRCRAFT = 4
MAX_ZONE_SLOTS = 4


def finite_number(value: object, name: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{name} must be finite")
    try:
        result = float(value)
    except OverflowError as exc:
        raise ValueError(f"{name} must be finite") from exc
    if not isfinite(result):
        raise ValueError(f"{name} must be finite")
    if positive and result <= 0:
        raise ValueError(f"{name} must be positive")
    return result


def integer(value: object, name: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer at least {minimum}")
    return value


def normalized_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{name} must be nonempty normalized text")
    return value


def sha256_digest(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


def polygon_points(value: object, name: str) -> tuple[tuple[float, float], ...]:
    if not isinstance(value, tuple) or any(
        not isinstance(point, tuple) or len(point) != 2 for point in value
    ):
        raise ValueError(f"{name} must be an immutable tuple of coordinate tuples")
    try:
        checked = polygon([list(point) for point in value])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid {name}: {exc}") from exc
    return tuple((float(point[0]), float(point[1])) for point in checked)


@dataclass(frozen=True, slots=True)
class Pose:
    x_m: float
    y_m: float
    z_m: float
    floor_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "x_m", finite_number(self.x_m, "x_m"))
        object.__setattr__(self, "y_m", finite_number(self.y_m, "y_m"))
        object.__setattr__(self, "z_m", finite_number(self.z_m, "z_m"))
        normalized_text(self.floor_id, "floor_id")

    @property
    def xyz(self) -> tuple[float, float, float]:
        return (self.x_m, self.y_m, self.z_m)


@dataclass(frozen=True, slots=True)
class ArtifactPin:
    version: str
    content_sha256: str

    def __post_init__(self) -> None:
        normalized_text(self.version, "artifact version")
        sha256_digest(self.content_sha256, "artifact content_sha256")


@dataclass(frozen=True, slots=True)
class MotionConfig:
    aircraft_radius_m: float
    aircraft_height_m: float
    map_uncertainty_m: float
    pose_uncertainty_m: float
    tracking_allowance_m: float
    stopping_allowance_m: float

    def __post_init__(self) -> None:
        positive = {"aircraft_radius_m", "aircraft_height_m"}
        for name in self.__dataclass_fields__:
            result = finite_number(getattr(self, name), name, positive=name in positive)
            if name not in positive and result < 0:
                raise ValueError(f"{name} must be nonnegative")
            object.__setattr__(self, name, result)

    @property
    def swept_radius_m(self) -> float:
        return (
            self.aircraft_radius_m
            + self.map_uncertainty_m
            + self.pose_uncertainty_m
            + self.tracking_allowance_m
            + self.stopping_allowance_m
        )

    @property
    def swept_half_height_m(self) -> float:
        return (
            self.aircraft_height_m / 2
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
        integer(self.drone_id, "drone_id")
        integer(self.connection_epoch, "connection_epoch")
        if not isinstance(self.pose, Pose):
            raise ValueError("pose must be a Pose")


@dataclass(frozen=True, slots=True)
class ArrivalSlot:
    slot_id: str
    zone_id: str
    pose: Pose
    radius_m: float
    half_height_m: float

    def __post_init__(self) -> None:
        normalized_text(self.slot_id, "slot_id")
        normalized_text(self.zone_id, "zone_id")
        if not isinstance(self.pose, Pose):
            raise ValueError("arrival slot pose must be a Pose")
        object.__setattr__(
            self, "radius_m", finite_number(self.radius_m, "radius_m", positive=True)
        )
        object.__setattr__(
            self,
            "half_height_m",
            finite_number(self.half_height_m, "half_height_m", positive=True),
        )


def volume_contains(
    pose: Pose,
    boundary: tuple[tuple[float, float], ...],
    z_min_m: float,
    z_max_m: float,
    radius_m: float,
    half_height_m: float,
) -> bool:
    point = (pose.x_m, pose.y_m)
    if (
        pose.z_m - half_height_m < z_min_m - EPS
        or pose.z_m + half_height_m > z_max_m + EPS
        or not point_inside(boundary, point)
    ):
        return False
    edge_distance = min(
        distance_to_segment(point, start, end)
        for start, end in zip(boundary, boundary[1:], strict=False)
    )
    return edge_distance + EPS >= radius_m


@dataclass(frozen=True, slots=True)
class Zone:
    zone_id: str
    floor_id: str
    owner_approved: bool
    polygon_xy: tuple[tuple[float, float], ...]
    z_min_m: float
    z_max_m: float
    arrival_slots: tuple[ArrivalSlot, ...]
    aliases: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        normalized_text(self.zone_id, "zone_id")
        normalized_text(self.floor_id, "floor_id")
        if type(self.owner_approved) is not bool:
            raise ValueError("owner_approved must be a boolean")
        if not isinstance(self.arrival_slots, tuple) or not all(
            isinstance(slot, ArrivalSlot) for slot in self.arrival_slots
        ):
            raise ValueError("zone slots must be an immutable tuple of ArrivalSlot values")
        if len(self.arrival_slots) > MAX_ZONE_SLOTS:
            raise ValueError("a preview zone supports at most four arrival slots")
        if not isinstance(self.aliases, tuple) or any(
            not isinstance(alias, str) or not alias or alias != alias.strip()
            for alias in self.aliases
        ):
            raise ValueError("zone aliases must be an immutable tuple of normalized text")
        if len(set(self.aliases)) != len(self.aliases):
            raise ValueError("zone aliases must be distinct")
        object.__setattr__(
            self, "polygon_xy", polygon_points(self.polygon_xy, f"zone {self.zone_id} polygon")
        )
        low = finite_number(self.z_min_m, "zone z_min_m")
        high = finite_number(self.z_max_m, "zone z_max_m")
        if low >= high:
            raise ValueError("zone altitude bounds must increase")
        object.__setattr__(self, "z_min_m", low)
        object.__setattr__(self, "z_max_m", high)
        if len({slot.slot_id for slot in self.arrival_slots}) != len(self.arrival_slots):
            raise ValueError("arrival slot ids must be unique within a zone")
        if any(
            slot.zone_id != self.zone_id or slot.pose.floor_id != self.floor_id
            for slot in self.arrival_slots
        ):
            raise ValueError("arrival slot must belong to its zone and floor")
        if any(
            not volume_contains(
                slot.pose,
                self.polygon_xy,
                low,
                high,
                slot.radius_m,
                slot.half_height_m,
            )
            for slot in self.arrival_slots
        ):
            raise ValueError("arrival slot volume must be contained by its zone")


@dataclass(frozen=True, slots=True)
class Connector:
    connector_id: str
    from_floor_id: str
    to_floor_id: str
    from_pose: Pose
    to_pose: Pose
    enabled: bool = True

    def __post_init__(self) -> None:
        normalized_text(self.connector_id, "connector_id")
        normalized_text(self.from_floor_id, "connector from_floor_id")
        normalized_text(self.to_floor_id, "connector to_floor_id")
        if not isinstance(self.from_pose, Pose) or not isinstance(self.to_pose, Pose):
            raise ValueError("connector endpoints must be Poses")
        if type(self.enabled) is not bool:
            raise ValueError("connector enabled must be a boolean")
        if (
            self.from_floor_id == self.to_floor_id
            or self.from_pose.floor_id != self.from_floor_id
            or self.to_pose.floor_id != self.to_floor_id
            or (self.from_pose.x_m, self.from_pose.y_m) != (self.to_pose.x_m, self.to_pose.y_m)
        ):
            raise ValueError("connector must be a vertical transition between declared floors")


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
        normalized_text(self.floor_id, "grid floor_id")
        object.__setattr__(self, "z_m", finite_number(self.z_m, "z_m"))
        if not isinstance(self.origin_xy_m, tuple) or len(self.origin_xy_m) != 2:
            raise ValueError("origin_xy_m must be an immutable coordinate pair")
        object.__setattr__(
            self,
            "origin_xy_m",
            (
                finite_number(self.origin_xy_m[0], "origin x"),
                finite_number(self.origin_xy_m[1], "origin y"),
            ),
        )
        object.__setattr__(self, "cell_m", finite_number(self.cell_m, "cell_m", positive=True))
        integer(self.width, "grid width", minimum=1)
        integer(self.height, "grid height", minimum=1)
        if self.width * self.height > MAX_GRID_CELLS:
            raise ValueError("grid exceeds the accepted cell limit")
        if not isinstance(self.blocked_cells, frozenset) or any(
            not isinstance(cell, tuple)
            or len(cell) != 2
            or type(cell[0]) is not int
            or type(cell[1]) is not int
            or not (0 <= cell[0] < self.width and 0 <= cell[1] < self.height)
            for cell in self.blocked_cells
        ):
            raise ValueError("blocked cells must be an immutable set of in-grid integer pairs")

    def cell_for(self, pose: Pose) -> tuple[int, int]:
        return (
            floor((pose.x_m - self.origin_xy_m[0]) / self.cell_m),
            floor((pose.y_m - self.origin_xy_m[1]) / self.cell_m),
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


def grid_covers_pose(
    level: GridLevel,
    pose: Pose,
    grid_clearance_m: float,
    half_height_m: float,
) -> bool:
    """Return whether one pre-inflated band covers the full vertical envelope."""
    return (
        level.floor_id == pose.floor_id
        and abs(level.z_m - pose.z_m) + half_height_m <= grid_clearance_m + EPS
    )


def free_grid_covers_pose(
    level: GridLevel,
    pose: Pose,
    grid_clearance_m: float,
    half_height_m: float,
) -> bool:
    return grid_covers_pose(level, pose, grid_clearance_m, half_height_m) and level.free(
        level.cell_for(pose)
    )


@dataclass(frozen=True, slots=True)
class NavigationPermission:
    permitted_zone_ids: frozenset[str]

    def __post_init__(self) -> None:
        if not isinstance(self.permitted_zone_ids, frozenset) or any(
            not isinstance(zone_id, str) or not zone_id or zone_id != zone_id.strip()
            for zone_id in self.permitted_zone_ids
        ):
            raise ValueError("permitted zone ids must be an immutable set of normalized text")


@dataclass(frozen=True, slots=True)
class NavigationEvidence:
    geometry_status: Literal["offline_authoring"]
    evidence_kind: Literal["synthetic", "surveyed"]
    flight_approved: bool
    camera_visibility_verified: bool
    blocking_gaps: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.geometry_status != "offline_authoring":
            raise ValueError("navigation geometry must remain offline authoring evidence")
        if self.evidence_kind not in {"synthetic", "surveyed"}:
            raise ValueError("navigation evidence kind is unsupported")
        if self.flight_approved is not False or self.camera_visibility_verified is not False:
            raise ValueError(
                "offline navigation evidence cannot claim flight or visibility approval"
            )
        expected = (
            "geometry_acceptance_missing",
            "camera_visibility_unverified",
            "runtime_dispatch_contract_missing",
        ) + (("synthetic_geometry_evidence",) if self.evidence_kind == "synthetic" else ())
        if self.blocking_gaps != expected:
            raise ValueError("navigation evidence must carry every canonical blocking gap")


def preview_evidence(evidence_kind: Literal["synthetic", "surveyed"]) -> NavigationEvidence:
    gaps = (
        "geometry_acceptance_missing",
        "camera_visibility_unverified",
        "runtime_dispatch_contract_missing",
    ) + (("synthetic_geometry_evidence",) if evidence_kind == "synthetic" else ())
    return NavigationEvidence("offline_authoring", evidence_kind, False, False, gaps)


@dataclass(frozen=True, slots=True)
class NavigationRequest:
    destination_zone_id: str
    roster_version: int
    plan_revision: int
    selected: tuple[DronePose, ...]
    all_positions: tuple[DronePose, ...]
    motion: MotionConfig
    permission: NavigationPermission

    def __post_init__(self) -> None:
        normalized_text(self.destination_zone_id, "destination_zone_id")
        integer(self.roster_version, "roster_version")
        integer(self.plan_revision, "plan_revision")
        if not isinstance(self.selected, tuple) or not isinstance(self.all_positions, tuple):
            raise ValueError("selected and all_positions must be immutable tuples")
        if not self.selected:
            raise ValueError("selected drones are required")
        if len(self.selected) > MAX_AIRCRAFT or len(self.all_positions) > MAX_AIRCRAFT:
            raise ValueError("navigation previews support at most four aircraft")
        if not all(isinstance(drone, DronePose) for drone in (*self.selected, *self.all_positions)):
            raise ValueError("selected and all_positions must contain DronePose values")
        if len({drone.drone_id for drone in self.selected}) != len(self.selected):
            raise ValueError("selected drone ids must be unique")
        positions = {drone.drone_id: drone for drone in self.all_positions}
        if len(positions) != len(self.all_positions) or any(
            positions.get(drone.drone_id) != drone for drone in self.selected
        ):
            raise ValueError("all_positions must include each selected drone exactly")
        if not isinstance(self.motion, MotionConfig) or not isinstance(
            self.permission, NavigationPermission
        ):
            raise ValueError("motion and permission must use navigation contract types")


@dataclass(frozen=True, slots=True)
class SweptSegment:
    start: Pose
    end: Pose
    radius_m: float
    half_height_m: float

    def __post_init__(self) -> None:
        if not isinstance(self.start, Pose) or not isinstance(self.end, Pose):
            raise ValueError("swept segment endpoints must be Poses")
        object.__setattr__(
            self, "radius_m", finite_number(self.radius_m, "segment radius_m", positive=True)
        )
        object.__setattr__(
            self,
            "half_height_m",
            finite_number(self.half_height_m, "segment half_height_m", positive=True),
        )


@dataclass(frozen=True, slots=True)
class DroneRoute:
    drone: DronePose
    arrival_slot: ArrivalSlot
    waypoints: tuple[Pose, ...]
    swept_segments: tuple[SweptSegment, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.drone, DronePose) or not isinstance(self.arrival_slot, ArrivalSlot):
            raise ValueError("route identity and slot must use navigation contract types")
        if not isinstance(self.waypoints, tuple) or not all(
            isinstance(point, Pose) for point in self.waypoints
        ):
            raise ValueError("waypoints must be an immutable tuple of Pose values")
        if (
            len(self.waypoints) < 2
            or not isinstance(self.swept_segments, tuple)
            or not all(isinstance(segment, SweptSegment) for segment in self.swept_segments)
        ):
            raise ValueError("route needs immutable, typed swept geometry")
        expected = tuple(zip(self.waypoints, self.waypoints[1:], strict=False))
        actual = tuple((segment.start, segment.end) for segment in self.swept_segments)
        if actual != expected or self.waypoints[-1] != self.arrival_slot.pose:
            raise ValueError("swept segments must exactly cover the route to its arrival slot")
        if self.waypoints[0] != self.drone.pose:
            raise ValueError("route must begin at the frozen aircraft pose")


@dataclass(frozen=True, slots=True)
class NavigationPlan:
    map_pin: ArtifactPin
    geometry_pin: ArtifactPin
    navigation_pin: ArtifactPin
    evidence: NavigationEvidence
    config: MotionConfig
    permission: NavigationPermission
    roster_version: int
    plan_revision: int
    destination_zone_id: str
    selected: tuple[DronePose, ...]
    roster: tuple[DronePose, ...]
    arrival_slots: tuple[ArrivalSlot, ...]
    routes: tuple[DroneRoute, ...]
    execution_order: tuple[int, ...]
    dispatch_eligible: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if not all(
            isinstance(pin, ArtifactPin)
            for pin in (self.map_pin, self.geometry_pin, self.navigation_pin)
        ):
            raise ValueError("plan pins must use ArtifactPin")
        if not isinstance(self.evidence, NavigationEvidence):
            raise ValueError("plan evidence must use NavigationEvidence")
        if not isinstance(self.config, MotionConfig) or not isinstance(
            self.permission, NavigationPermission
        ):
            raise ValueError("plan config and permission must use navigation contract types")
        integer(self.roster_version, "plan roster_version")
        integer(self.plan_revision, "plan_revision")
        normalized_text(self.destination_zone_id, "destination_zone_id")
        values = (self.selected, self.roster, self.arrival_slots, self.routes, self.execution_order)
        if any(not isinstance(value, tuple) for value in values):
            raise ValueError("navigation plan collections must be immutable tuples")
        if not all(isinstance(value, DronePose) for value in (*self.selected, *self.roster)):
            raise ValueError("plan selection and roster must contain DronePose values")
        if not all(isinstance(value, ArrivalSlot) for value in self.arrival_slots) or not all(
            isinstance(value, DroneRoute) for value in self.routes
        ):
            raise ValueError("plan slots and routes must use navigation contract types")
        if any(type(drone_id) is not int or drone_id < 0 for drone_id in self.execution_order):
            raise ValueError("execution order must contain nonnegative integer ids")
        route_ids = tuple(route.drone.drone_id for route in self.routes)
        roster = {drone.drone_id: drone for drone in self.roster}
        selected_ids = [drone.drone_id for drone in self.selected]
        if (
            not self.selected
            or len(self.selected) > MAX_AIRCRAFT
            or len(self.roster) > MAX_AIRCRAFT
            or len(roster) != len(self.roster)
            or len(set(selected_ids)) != len(selected_ids)
            or any(roster.get(drone.drone_id) != drone for drone in self.selected)
            or route_ids != self.execution_order
            or tuple(route.drone for route in self.routes) != self.selected
            or tuple(route.arrival_slot for route in self.routes) != self.arrival_slots
            or len(set(slot.slot_id for slot in self.arrival_slots)) != len(self.arrival_slots)
            or any(slot.zone_id != self.destination_zone_id for slot in self.arrival_slots)
            or any(
                segment.radius_m != self.config.swept_radius_m
                or segment.half_height_m != self.config.swept_half_height_m
                for route in self.routes
                for segment in route.swept_segments
            )
        ):
            raise ValueError("routes must exactly match the frozen selection, slots, and order")


@dataclass(frozen=True, slots=True)
class NavigationLiveState:
    roster_version: int
    plan_revision: int
    selected_ids: tuple[int, ...]
    positions: tuple[DronePose, ...]
    motion: MotionConfig
    permission: NavigationPermission

    def __post_init__(self) -> None:
        integer(self.roster_version, "live roster_version")
        integer(self.plan_revision, "live plan_revision")
        if not isinstance(self.selected_ids, tuple) or not isinstance(self.positions, tuple):
            raise ValueError("live selection and positions must be immutable tuples")
        if len(self.selected_ids) > MAX_AIRCRAFT or len(self.positions) > MAX_AIRCRAFT:
            raise ValueError("navigation previews support at most four live aircraft")
        if any(type(drone_id) is not int or drone_id < 0 for drone_id in self.selected_ids) or len(
            set(self.selected_ids)
        ) != len(self.selected_ids):
            raise ValueError("live selection must contain unique nonnegative integer ids")
        if not all(isinstance(drone, DronePose) for drone in self.positions):
            raise ValueError("live positions must contain DronePose values")
        position_ids = {drone.drone_id for drone in self.positions}
        if len(position_ids) != len(self.positions) or not set(self.selected_ids) <= position_ids:
            raise ValueError("live positions must contain each selected drone exactly")
        if not isinstance(self.motion, MotionConfig) or not isinstance(
            self.permission, NavigationPermission
        ):
            raise ValueError("live motion and permission must use navigation contract types")


RefusalCode = Literal[
    "arrival_not_permitted",
    "destination_unknown",
    "destination_excluded",
    "insufficient_arrival_slots",
    "clearance_exceeds_geometry",
    "wrong_floor",
    "position_unmapped",
    "route_unreachable",
    "arrival_conflict",
    "initial_overlap",
    "artifact_not_dispatchable",
    "artifact_changed",
    "roster_changed",
    "selection_changed",
    "plan_revision_changed",
    "motion_config_changed",
    "permission_changed",
    "connection_changed",
    "position_drift",
    "remaining_route_obstructed",
]

_REFUSAL_CODES = frozenset(RefusalCode.__args__)


@dataclass(frozen=True, slots=True)
class NavigationRefusal:
    code: RefusalCode
    detail: str

    def __post_init__(self) -> None:
        if self.code not in _REFUSAL_CODES:
            raise ValueError("unknown navigation refusal code")
        normalized_text(self.detail, "refusal detail")
