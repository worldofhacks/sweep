from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import stat
import uuid
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

import cv2
import numpy as np

from adapters.ohmni.return_controller import (
    ApprovedReturnRoute,
    ReturnPoint,
    ReturnSegment,
    WorldToOdom,
)
from relay.auth import sign_event, verify_event_signature
from tools.console_world_bundle import canonical_json, content_hash, validate_world_bundle
from tools.geometry_math import distance_to_segment, polygon_cell_intersects, rect_inside_polygon

MAX_ROUTE_BYTES = 32_768
MAX_ROUTE_POINTS = 128


def _text(value: object) -> str:
    if (
        not isinstance(value, str)
        or not 0 < len(value) <= 128
        or value != value.strip()
        or not value.isprintable()
    ):
        raise ValueError("ground navigation identity must be bounded normalized text")
    return value


def _integer(value: object, minimum: int = 0, maximum: int = 2**53 - 1) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("ground navigation integer is outside its bound")
    return value


def _number(value: object, maximum: float, *, positive: bool = True) -> float:
    if (
        type(value) not in (float, int)
        or not math.isfinite(value)
        or not (0 < value <= maximum if positive else abs(value) <= maximum)
    ):
        raise ValueError("ground navigation physical value is outside its bound")
    return float(value)


def _object(value: object, fields: set[str]) -> dict:
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("ground navigation fields do not match the contract")
    return value


def _json(raw: bytes) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate ground navigation field")
            result[key] = value
        return result

    return json.loads(
        raw,
        object_pairs_hook=unique,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")),
    )


@dataclass(frozen=True, slots=True)
class GroundMotionLimits:
    footprint_radius_m: float
    position_uncertainty_m: float
    stopping_distance_m: float
    speed_m_s: float
    yaw_rate_deg_s: float
    pulse_s: float
    arrival_tolerance_m: float
    pose_max_age_ms: int
    route_timeout_ms: int

    def __post_init__(self):
        for name, maximum in (
            ("footprint_radius_m", 2),
            ("position_uncertainty_m", 0.5),
            ("stopping_distance_m", 1),
            ("speed_m_s", 0.18),
            ("yaw_rate_deg_s", 45),
            ("pulse_s", 0.2),
            ("arrival_tolerance_m", 0.2),
        ):
            object.__setattr__(self, name, _number(getattr(self, name), maximum))
        _integer(self.pose_max_age_ms, 1, 500)
        _integer(self.route_timeout_ms, 1000, 600_000)
        if self.arrival_tolerance_m >= self.footprint_radius_m:
            raise ValueError("ground arrival tolerance exceeds its footprint")

    @property
    def clearance_m(self) -> float:
        return (
            self.footprint_radius_m
            + self.position_uncertainty_m
            + self.stopping_distance_m
            + self.speed_m_s * self.pulse_s
            + self.arrival_tolerance_m
        )


@dataclass(frozen=True, slots=True)
class GroundNavigationDevice:
    device_id: int
    pose_source_id: str
    world_pose_source_id: str
    identity_source_id: str
    registration_id: str
    odom_origin_id: str
    odom_frame: str
    world_to_odom: WorldToOdom
    limits: GroundMotionLimits

    def __post_init__(self):
        _integer(self.device_id, 1, 2**31 - 1)
        for value in (
            self.pose_source_id,
            self.world_pose_source_id,
            self.identity_source_id,
            self.registration_id,
            self.odom_origin_id,
            self.odom_frame,
        ):
            _text(value)
        if self.world_to_odom.registration_id != self.registration_id:
            raise ValueError("ground transform registration differs from approved source")


@dataclass(frozen=True, slots=True)
class GroundNavigationPose:
    device_id: int
    connection_epoch: int
    x_m: float
    y_m: float
    t_ms: int
    source_id: str
    registration_id: str
    odom_origin_id: str

    def __post_init__(self):
        _integer(self.device_id, 1, 2**31 - 1)
        _integer(self.connection_epoch, 1)
        _integer(self.t_ms)
        for value in (self.source_id, self.registration_id, self.odom_origin_id):
            _text(value)
        _number(self.x_m, 100_000, positive=False)
        _number(self.y_m, 100_000, positive=False)

    @property
    def point(self) -> ReturnPoint:
        return ReturnPoint(self.x_m, self.y_m)


@dataclass(frozen=True, slots=True)
class GroundNavigationRoute:
    device_id: int
    connection_epoch: int
    points: tuple[ReturnPoint, ...]


@dataclass(frozen=True, slots=True)
class GroundNavigationPlan:
    plan_id: str
    session: str
    roster_version: int
    selected: tuple[int, ...]
    zone_id: str
    routes: tuple[GroundNavigationRoute, ...]
    poses: tuple[GroundNavigationPose, ...]
    created_at: int
    expires_at: int
    configuration_sha256: str
    map_reference: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class GroundNavigationAdmission:
    route: ApprovedReturnRoute
    expires_at: int
    issued_at: int


class GroundNavigationDeployment:
    """An externally signed grid and measured per-node navigation configuration."""

    @classmethod
    def load(cls, path: Path, key: bytes) -> GroundNavigationDeployment:
        if not isinstance(key, bytes) or not 32 <= len(key) <= 4096:
            raise ValueError("ground navigation needs a bounded signing key")
        path = Path(path)
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_size > 16 * 1024 * 1024:
                raise ValueError("ground navigation deployment must be a bounded regular file")
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                encoded = stream.read(16 * 1024 * 1024 + 1)
        finally:
            os.close(descriptor)
        raw = _object(
            _json(encoded),
            {
                "v",
                "approval_id",
                "approved_by",
                "approved_map",
                "devices",
                "expires_at",
                "signature",
            },
        )
        unsigned = {name: value for name, value in raw.items() if name != "signature"}
        if raw["v"] != 1 or not verify_event_signature(unsigned, raw["signature"], key):
            raise ValueError("ground navigation deployment signature is invalid")
        result = cls()
        result.approval_id = _text(raw["approval_id"])
        result.approved_by = _text(raw["approved_by"])
        result.expires_at = _integer(raw["expires_at"], 1)
        result.configuration_sha256 = content_hash(unsigned)
        result._key = key
        result._path = path
        result._file_sha256 = hashlib.sha256(encoded).digest()
        approved = _object(raw["approved_map"], {"reference", "approval", "bundle"})
        reference = _object(approved["reference"], {"bundleId", "revision", "contentHash"})
        if (
            not isinstance(approved["approval"], dict)
            or approved["approval"].get("reference") != reference
        ):
            raise ValueError("ground map approval does not bind the saved revision")
        bundle = approved["bundle"]
        if validate_world_bundle(bundle):
            raise ValueError("ground navigation map bundle is invalid")
        result.map_reference = MappingProxyType(dict(reference))
        manifest = bundle["manifest"]
        result.map_version, result.floor_id = manifest["mapVersion"], manifest["floorId"]
        image = manifest["image"]
        if image["width"] * image["height"] > 262_144:
            raise ValueError("ground navigation grid exceeds 262144 cells")
        result._resolution = image["resolutionM"]
        result._origin = (image["originXM"], image["originYM"])
        payload = base64.b64decode(bundle["image"]["dataUrl"].split(",", 1)[1], validate=True)
        pixels = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
        if pixels is None or pixels.shape != (image["height"], image["width"]):
            raise ValueError("ground grid image cannot be decoded")
        result._blocked = pixels != 255
        result._height, result._width = pixels.shape
        geofence = [(point["x"], point["y"]) for point in bundle["geofence"]["points"]]
        obstacles = [
            [(point["x"], point["y"]) for point in item["points"]] for item in bundle["obstacles"]
        ]
        if sum(len(poly) for poly in [geofence, *obstacles]) * pixels.size > 4_000_000:
            raise ValueError("ground polygon/grid validation exceeds its work bound")
        for row, column in np.argwhere(~result._blocked):
            rect = result._cell_rect(int(column), int(row))
            if not rect_inside_polygon(rect, geofence) or any(
                polygon_cell_intersects(poly, rect) for poly in obstacles
            ):
                result._blocked[row, column] = True
        result._blocked.flags.writeable = False
        result._zones = MappingProxyType(
            {
                item["id"]: tuple((point["x"], point["y"]) for point in item["points"])
                for item in bundle["zones"]
            }
        )
        if not isinstance(raw["devices"], list) or not 1 <= len(raw["devices"]) <= 8:
            raise ValueError("ground deployment requires one through eight devices")
        devices = {}
        for item in raw["devices"]:
            item = _object(item, set(GroundNavigationDevice.__dataclass_fields__))
            device = GroundNavigationDevice(
                **{
                    **item,
                    "world_to_odom": WorldToOdom(**item["world_to_odom"]),
                    "limits": GroundMotionLimits(**item["limits"]),
                }
            )
            if device.device_id in devices:
                raise ValueError("duplicate ground navigation device")
            devices[device.device_id] = device
        result._devices = MappingProxyType(devices)
        result._plans = {}
        return result

    @property
    def device_ids(self) -> tuple[int, ...]:
        return tuple(sorted(self._devices))

    def device(self, device_id: int) -> GroundNavigationDevice:
        try:
            return self._devices[device_id]
        except KeyError as error:
            raise ValueError("ground device has no approved deployment") from error

    def prepare(
        self,
        zone_id: str,
        poses: tuple[GroundNavigationPose, ...],
        selected: tuple[int, ...],
        *,
        session: str,
        roster_version: int,
        now_ms: int,
    ) -> GroundNavigationPlan:
        self._check(now_ms)
        _text(session)
        _integer(roster_version)
        if zone_id not in self._zones:
            raise ValueError("unknown approved ground destination")
        if not selected or len(set(selected)) != len(selected) or len(selected) > 8:
            raise ValueError("ground selection must contain unique approved devices")
        current = self._poses(poses, now_ms)
        if not set(selected) <= set(current):
            raise ValueError("ground selection lacks current world poses")
        routes = []
        occupied = {device_id: source.point for device_id, source in current.items()}
        for device_id in sorted(selected):
            source = current[device_id]
            reservations = tuple(
                (point, self.device(other).limits.clearance_m)
                for other, point in occupied.items()
                if other != device_id
            )
            points = self._plan_route(
                source.point, zone_id, self.device(device_id).limits.clearance_m, reservations
            )
            routes.append(GroundNavigationRoute(device_id, source.connection_epoch, points))
            occupied[device_id] = points[-1]
        self._plans = {
            identity: plan for identity, plan in self._plans.items() if plan.expires_at > now_ms
        }
        if len(self._plans) >= 256:
            raise ValueError("ground navigation review capacity is exhausted")
        plan = GroundNavigationPlan(
            "ground-" + uuid.uuid4().hex,
            session,
            roster_version,
            tuple(selected),
            zone_id,
            tuple(routes),
            tuple(poses),
            now_ms,
            min(
                self.expires_at,
                now_ms
                + sum(self.device(device_id).limits.route_timeout_ms for device_id in selected),
            ),
            self.configuration_sha256,
            self.map_reference,
        )
        self._plans[plan.plan_id] = plan
        return plan

    def cancel(self, plan: GroundNavigationPlan) -> None:
        self._plans.pop(plan.plan_id, None)

    def revalidate(
        self,
        plan: GroundNavigationPlan,
        poses: tuple[GroundNavigationPose, ...],
        selected: tuple[int, ...],
        *,
        session: str,
        roster_version: int,
        now_ms: int,
        completed: tuple[int, ...] = (),
        active_device_id: int | None = None,
    ) -> None:
        try:
            self._check(now_ms)
            if self._plans.get(plan.plan_id) is not plan:
                raise ValueError("ground navigation review is retired")
            if (
                session,
                roster_version,
                tuple(selected),
                self.configuration_sha256,
                dict(self.map_reference),
            ) != (
                plan.session,
                plan.roster_version,
                plan.selected,
                plan.configuration_sha256,
                dict(plan.map_reference),
            ) or now_ms >= plan.expires_at:
                raise ValueError("frozen ground navigation inputs changed or expired")
            route_ids = tuple(route.device_id for route in plan.routes)
            if completed != route_ids[: len(completed)] or (
                active_device_id is not None
                and (
                    len(completed) >= len(route_ids)
                    or active_device_id != route_ids[len(completed)]
                )
            ):
                raise ValueError("ground execution order changed")
            current = self._poses(poses, now_ms)
            originals = {pose.device_id: pose for pose in plan.poses}
            if set(current) != set(originals):
                raise ValueError("ground occupancy roster changed")
            routes = {route.device_id: route for route in plan.routes}
            for device_id, observed in current.items():
                before = originals[device_id]
                limits = self.device(device_id).limits
                if observed.connection_epoch != before.connection_epoch:
                    raise ValueError("ground connection epoch changed")
                if device_id == active_device_id:
                    route = routes[device_id]
                    if (
                        min(
                            distance_to_segment(
                                (observed.x_m, observed.y_m),
                                (start.x_m, start.y_m),
                                (end.x_m, end.y_m),
                            )
                            for start, end in zip(route.points, route.points[1:], strict=False)
                        )
                        > limits.position_uncertainty_m + limits.arrival_tolerance_m
                    ):
                        raise ValueError("ground pose departed its frozen route")
                else:
                    expected = (
                        routes[device_id].points[-1] if device_id in completed else before.point
                    )
                    if (
                        math.hypot(observed.x_m - expected.x_m, observed.y_m - expected.y_m)
                        > limits.arrival_tolerance_m + limits.position_uncertainty_m
                    ):
                        raise ValueError("stationary ground position changed")
            occupied = {device_id: observed.point for device_id, observed in current.items()}
            for route in plan.routes[len(completed) :]:
                reservations = tuple(
                    (point, self.device(other).limits.clearance_m)
                    for other, point in occupied.items()
                    if other != route.device_id
                )
                for start, end in zip(route.points, route.points[1:], strict=False):
                    if not self._clear(
                        start, end, self.device(route.device_id).limits.clearance_m, reservations
                    ):
                        raise ValueError("frozen ground route is obstructed")
                occupied[route.device_id] = route.points[-1]
        except (ValueError, OSError):
            self.cancel(plan)
            raise

    def route_record(self, plan: GroundNavigationPlan, device_id: int, *, now_ms: int) -> str:
        self._check(now_ms)
        if self._plans.get(plan.plan_id) is not plan or now_ms >= plan.expires_at:
            raise ValueError("ground navigation review is retired or expired")
        route = next((route for route in plan.routes if route.device_id == device_id), None)
        if route is None:
            raise ValueError("ground route does not select this device")
        record = {
            "v": 1,
            "route_id": f"{plan.plan_id}:{device_id}",
            "plan_id": plan.plan_id,
            "session": plan.session,
            "roster_version": plan.roster_version,
            "device_id": device_id,
            "connection_epoch": route.connection_epoch,
            "zone_id": plan.zone_id,
            "map_reference": dict(self.map_reference),
            "map_version": self.map_version,
            "floor_id": self.floor_id,
            "configuration_sha256": self.configuration_sha256,
            "issued_at": now_ms,
            "expires_at": min(
                plan.expires_at, now_ms + self.device(device_id).limits.route_timeout_ms
            ),
            "points": [{"x_m": point.x_m, "y_m": point.y_m} for point in route.points],
        }
        encoded = canonical_json({**record, "signature": sign_event(record, self._key)})
        if len(encoded.encode()) > MAX_ROUTE_BYTES:
            raise ValueError("ground route exceeds its wire byte bound")
        return encoded

    def admit_route(
        self,
        encoded: str,
        *,
        route_id: str,
        session: str,
        device_id: int,
        connection_epoch: int,
        roster_version: int,
        now_ms: int,
    ) -> GroundNavigationAdmission:
        self._check(now_ms)
        if not isinstance(encoded, str) or len(encoded.encode()) > MAX_ROUTE_BYTES:
            raise ValueError("ground route exceeds its wire byte bound")
        raw = _object(
            _json(encoded.encode()),
            {
                "v",
                "route_id",
                "plan_id",
                "session",
                "roster_version",
                "device_id",
                "connection_epoch",
                "zone_id",
                "map_reference",
                "map_version",
                "floor_id",
                "configuration_sha256",
                "issued_at",
                "expires_at",
                "points",
                "signature",
            },
        )
        unsigned = {name: value for name, value in raw.items() if name != "signature"}
        if raw["v"] != 1 or not verify_event_signature(unsigned, raw["signature"], self._key):
            raise ValueError("ground route signature is invalid")
        if (
            raw["route_id"],
            raw["session"],
            raw["device_id"],
            raw["connection_epoch"],
            raw["roster_version"],
        ) != (route_id, session, device_id, connection_epoch, roster_version):
            raise ValueError("ground route session or node identity changed")
        if (
            raw["map_reference"],
            raw["map_version"],
            raw["floor_id"],
            raw["configuration_sha256"],
        ) != (dict(self.map_reference), self.map_version, self.floor_id, self.configuration_sha256):
            raise ValueError("ground route map or deployment differs from node approval")
        device = self.device(device_id)
        limits = device.limits
        issued_at, expires_at = _integer(raw["issued_at"]), _integer(raw["expires_at"])
        if (
            not issued_at
            <= now_ms
            < expires_at
            <= min(self.expires_at, issued_at + limits.route_timeout_ms)
        ):
            raise ValueError("ground route authority expired or exceeds its time bound")
        if (
            raw["zone_id"] not in self._zones
            or not isinstance(raw["points"], list)
            or not 2 <= len(raw["points"]) <= MAX_ROUTE_POINTS
        ):
            raise ValueError("ground route destination or point count is invalid")
        points = tuple(ReturnPoint(**_object(value, {"x_m", "y_m"})) for value in raw["points"])
        r = limits.clearance_m
        final = points[-1]
        if not rect_inside_polygon(
            (final.x_m - r, final.y_m - r, final.x_m + r, final.y_m + r),
            self._zones[raw["zone_id"]],
        ):
            raise ValueError("ground arrival footprint leaves the approved destination")
        segments = []
        for start, end in zip(points, points[1:], strict=False):
            if start == end or not self._clear(start, end, r):
                raise ValueError("ground route crosses blocked or unknown map cells")
            x0, x1 = min(start.x_m, end.x_m) - r, max(start.x_m, end.x_m) + r
            y0, y1 = min(start.y_m, end.y_m) - r, max(start.y_m, end.y_m) + r
            segments.append(
                ReturnSegment(
                    end,
                    (
                        ReturnPoint(x0, y0),
                        ReturnPoint(x1, y0),
                        ReturnPoint(x1, y1),
                        ReturnPoint(x0, y1),
                    ),
                )
            )
        route = ApprovedReturnRoute(
            return_id=_text(route_id),
            approval_id=self.approval_id,
            approval_signer=self.approved_by,
            session=session,
            device_id=device_id,
            connection_epoch=connection_epoch,
            odom_origin_id=device.odom_origin_id,
            source_registration_id=device.registration_id,
            pose_source_id=device.pose_source_id,
            odom_frame=device.odom_frame,
            world_to_odom=device.world_to_odom,
            start=points[0],
            segments=tuple(segments),
            footprint_radius_m=limits.footprint_radius_m + limits.position_uncertainty_m,
            arrival_tolerance_m=limits.arrival_tolerance_m,
            geometry_sha256=content_hash(raw["points"]),
            speed_m_s=limits.speed_m_s,
            yaw_rate_deg_s=limits.yaw_rate_deg_s,
            pulse_s=limits.pulse_s,
            stopping_distance_m=limits.stopping_distance_m,
            pose_max_age_ms=limits.pose_max_age_ms,
        )
        return GroundNavigationAdmission(route, expires_at, issued_at)

    def check_active(self, admission: GroundNavigationAdmission, *, now_ms: int) -> bool:
        try:
            self._check(now_ms)
            return now_ms < admission.expires_at
        except (ValueError, OSError):
            return False

    def _check(self, now_ms: int):
        _integer(now_ms)
        if now_ms >= self.expires_at:
            raise ValueError("ground navigation approval expired")
        descriptor = os.open(self._path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_size > 16 * 1024 * 1024:
                raise ValueError("ground deployment changed")
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                encoded = stream.read(16 * 1024 * 1024 + 1)
        finally:
            os.close(descriptor)
        if hashlib.sha256(encoded).digest() != self._file_sha256:
            raise ValueError("ground deployment changed; fresh approval is required")

    def _poses(self, poses, now_ms):
        if not isinstance(poses, tuple) or not 1 <= len(poses) <= 8:
            raise ValueError("ground poses require a bounded immutable fleet")
        result = {}
        for pose in poses:
            if not isinstance(pose, GroundNavigationPose) or pose.device_id in result:
                raise ValueError("ground pose identity is invalid or duplicated")
            device = self.device(pose.device_id)
            if (pose.source_id, pose.registration_id, pose.odom_origin_id) != (
                device.world_pose_source_id,
                device.registration_id,
                device.odom_origin_id,
            ):
                raise ValueError("ground pose source or registration differs from deployment")
            if not 0 <= now_ms - pose.t_ms <= device.limits.pose_max_age_ms:
                raise ValueError("ground world pose is stale")
            result[pose.device_id] = pose
        return result

    def _cell_rect(self, column, row):
        x = self._origin[0] + column * self._resolution
        y = self._origin[1] + (self._height - row - 1) * self._resolution
        return (x, y, x + self._resolution, y + self._resolution)

    def _center(self, column, row):
        rect = self._cell_rect(column, row)
        return ReturnPoint((rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2)

    def _clear(self, start, end, clearance, reservations=()):
        if any(
            distance_to_segment((point.x_m, point.y_m), (start.x_m, start.y_m), (end.x_m, end.y_m))
            <= clearance + radius
            for point, radius in reservations
        ):
            return False
        r = clearance + 1e-8
        low_x, high_x = min(start.x_m, end.x_m) - r, max(start.x_m, end.x_m) + r
        low_y, high_y = min(start.y_m, end.y_m) - r, max(start.y_m, end.y_m) + r
        c0 = math.floor((low_x - self._origin[0]) / self._resolution)
        c1 = math.floor((high_x - self._origin[0]) / self._resolution)
        r0 = self._height - 1 - math.floor((high_y - self._origin[1]) / self._resolution)
        r1 = self._height - 1 - math.floor((low_y - self._origin[1]) / self._resolution)
        return (
            0 <= c0 <= c1 < self._width
            and 0 <= r0 <= r1 < self._height
            and not np.any(self._blocked[r0 : r1 + 1, c0 : c1 + 1])
        )

    def _plan_route(self, start, zone_id, clearance, reservations=()):
        column = math.floor((start.x_m - self._origin[0]) / self._resolution)
        row = self._height - 1 - math.floor((start.y_m - self._origin[1]) / self._resolution)
        if not self._clear(start, self._center(column, row), clearance, reservations):
            raise ValueError("ground start lacks approved grid clearance")
        origin = (column, row)
        previous = {origin: None}
        queue = deque([origin])
        goal = None
        while queue:
            cell = queue.popleft()
            point = self._center(*cell)
            rect = (
                point.x_m - clearance,
                point.y_m - clearance,
                point.x_m + clearance,
                point.y_m + clearance,
            )
            if rect_inside_polygon(rect, self._zones[zone_id]):
                goal = cell
                break
            for dx, dy in ((0, -1), (1, 0), (0, 1), (-1, 0)):
                neighbor = (cell[0] + dx, cell[1] + dy)
                if neighbor not in previous and self._clear(
                    point, self._center(*neighbor), clearance, reservations
                ):
                    previous[neighbor] = cell
                    queue.append(neighbor)
        if goal is None:
            raise ValueError("approved ground destination is unreachable")
        cells = []
        while goal is not None:
            cells.append(self._center(*goal))
            goal = previous[goal]
        points = [start, *reversed(cells)]
        reduced = [points[0]]
        for index in range(1, len(points) - 1):
            before, current, after = reduced[-1], points[index], points[index + 1]
            if (before.x_m == current.x_m == after.x_m) or (before.y_m == current.y_m == after.y_m):
                continue
            if current != before:
                reduced.append(current)
        if points[-1] != reduced[-1]:
            reduced.append(points[-1])
        if not 2 <= len(reduced) <= MAX_ROUTE_POINTS:
            raise ValueError("ground route has no movement or exceeds its segment bound")
        return tuple(reduced)
