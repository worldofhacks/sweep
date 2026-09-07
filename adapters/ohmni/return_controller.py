from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import stat
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from relay.auth import verify_event_signature

from .models import GroundStatus, RangeScan

_MAX_APPROVAL_BYTES = 256 * 1024
_MAX_GEOMETRY_BYTES = 128 * 1024
_MAX_KEY_BYTES = 4096
_MAX_SEGMENTS = 128
_MAX_POLYGON_VERTICES = 128
_MAX_COORDINATE_M = 100_000.0
_MAX_STATUS_AGE_MS = 500
_MAX_SCAN_AGE_MS = 500
_FORWARD_M_S = 0.12
_PULSE_S = 0.2
_STOPPING_DISTANCE_M = 0.06


@dataclass(frozen=True, slots=True)
class ReturnPoint:
    x_m: float
    y_m: float

    def __post_init__(self) -> None:
        if not all(
            math.isfinite(value) and abs(value) <= _MAX_COORDINATE_M
            for value in (self.x_m, self.y_m)
        ):
            raise ValueError("return points must be bounded finite coordinates")


@dataclass(frozen=True, slots=True)
class WorldToOdom:
    x_m: float
    y_m: float
    yaw_deg: float
    registration_id: str

    def __post_init__(self) -> None:
        if not all(
            math.isfinite(value) and abs(value) <= _MAX_COORDINATE_M
            for value in (self.x_m, self.y_m)
        ) or not math.isfinite(self.yaw_deg):
            raise ValueError("world-to-odom transform must be bounded and finite")
        if not _identifier(self.registration_id):
            raise ValueError("world-to-odom transform requires a registration ID")

    def point(self, value: ReturnPoint) -> ReturnPoint:
        angle = math.radians(self.yaw_deg)
        return ReturnPoint(
            self.x_m + math.cos(angle) * value.x_m - math.sin(angle) * value.y_m,
            self.y_m + math.sin(angle) * value.x_m + math.cos(angle) * value.y_m,
        )


@dataclass(frozen=True, slots=True)
class ReturnSegment:
    target: ReturnPoint
    footprint: tuple[ReturnPoint, ...]

    def __post_init__(self) -> None:
        if not 3 <= len(self.footprint) <= _MAX_POLYGON_VERTICES:
            raise ValueError("return corridor footprints need bounded vertices")


@dataclass(frozen=True, slots=True)
class ApprovedReturnRoute:
    return_id: str
    approval_id: str
    approval_signer: str
    session: str
    device_id: int
    connection_epoch: int
    odom_origin_id: str
    source_registration_id: str
    pose_source_id: str
    odom_frame: str
    world_to_odom: WorldToOdom
    start: ReturnPoint
    segments: tuple[ReturnSegment, ...]
    footprint_radius_m: float
    arrival_tolerance_m: float
    geometry_sha256: str

    def __post_init__(self) -> None:
        if not all(
            _identifier(value)
            for value in (
                self.return_id,
                self.approval_id,
                self.approval_signer,
                self.session,
                self.odom_origin_id,
                self.source_registration_id,
                self.pose_source_id,
                self.odom_frame,
            )
        ):
            raise ValueError("return approval identities must be non-empty bounded text")
        if type(self.device_id) is not int or not 1 <= self.device_id <= 2**31 - 1:
            raise ValueError("return approval device ID is invalid")
        if type(self.connection_epoch) is not int or self.connection_epoch < 0:
            raise ValueError("return approval connection epoch is invalid")
        if self.source_registration_id != self.world_to_odom.registration_id:
            raise ValueError("return source registration must bind the measured transform")
        if not 1 <= len(self.segments) <= _MAX_SEGMENTS:
            raise ValueError("return route needs bounded measured segments")
        if not all(
            isinstance(value, float) and math.isfinite(value) and 0 < value <= _MAX_COORDINATE_M
            for value in (self.footprint_radius_m, self.arrival_tolerance_m)
        ):
            raise ValueError("return safety distances must be bounded and positive")
        if not _sha256(self.geometry_sha256):
            raise ValueError("return geometry requires its SHA-256")

    @property
    def required_clearance_m(self) -> float:
        return self.footprint_radius_m + _STOPPING_DISTANCE_M + _FORWARD_M_S * _PULSE_S

    @classmethod
    def load(cls, path: Path, approval_key: bytes) -> ApprovedReturnRoute:
        if not 32 <= len(approval_key) <= _MAX_KEY_BYTES:
            raise ValueError("return approval key has an invalid length")
        raw = _strict_json(
            _read_bounded(path, _MAX_APPROVAL_BYTES, "return approval record"),
            "return approval record",
        )
        if not isinstance(raw, dict) or not isinstance(raw.get("signature"), str):
            raise ValueError("return approval signature is required")
        required = {
            "v",
            "return_id",
            "approval_id",
            "approval_signer",
            "session",
            "device_id",
            "connection_epoch",
            "odom_origin_id",
            "source_registration_id",
            "pose_source_id",
            "odom_frame",
            "world_to_odom",
            "geometry_sha256",
            "geometry_bytes_b64",
            "footprint_radius_m",
            "arrival_tolerance_m",
            "signature",
        }
        if set(raw) != required or raw["v"] != 1:
            raise ValueError("return approval record fields do not match v1")
        unsigned = {key: value for key, value in raw.items() if key != "signature"}
        if not verify_event_signature(unsigned, raw["signature"], approval_key):
            raise ValueError("return approval signature is invalid")
        encoded = raw["geometry_bytes_b64"]
        if not isinstance(encoded, str) or len(encoded) > ((_MAX_GEOMETRY_BYTES + 2) // 3) * 4:
            raise ValueError("return geometry bytes are invalid")
        try:
            geometry_bytes = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as error:
            raise ValueError("return geometry bytes are malformed") from error
        if len(geometry_bytes) > _MAX_GEOMETRY_BYTES:
            raise ValueError("return geometry bytes exceed the safety limit")
        digest = hashlib.sha256(geometry_bytes).hexdigest()
        if raw["geometry_sha256"] != digest:
            raise ValueError("return geometry hash does not match approved bytes")
        geometry = _strict_json(geometry_bytes, "return measured geometry")
        footprint_radius_m = _positive_float(raw["footprint_radius_m"])
        start, segments = _geometry(
            geometry, footprint_radius_m + _STOPPING_DISTANCE_M + _FORWARD_M_S * _PULSE_S
        )
        transform = _transform(raw["world_to_odom"])
        return cls(
            return_id=_text(raw["return_id"]),
            approval_id=_text(raw["approval_id"]),
            approval_signer=_text(raw["approval_signer"]),
            session=_text(raw["session"]),
            device_id=_device_id(raw["device_id"]),
            connection_epoch=_epoch(raw["connection_epoch"]),
            odom_origin_id=_text(raw["odom_origin_id"]),
            source_registration_id=_text(raw["source_registration_id"]),
            pose_source_id=_text(raw["pose_source_id"]),
            odom_frame=_text(raw["odom_frame"]),
            world_to_odom=transform,
            start=start,
            segments=segments,
            footprint_radius_m=footprint_radius_m,
            arrival_tolerance_m=_positive_float(raw["arrival_tolerance_m"]),
            geometry_sha256=digest,
        )


@dataclass(frozen=True, slots=True)
class ReturnOutcome:
    completed: bool
    reason: str | None = None
    detail: str = ""


class ReturnController:
    def __init__(
        self,
        route: ApprovedReturnRoute,
        *,
        status: Callable[[], GroundStatus],
        scan: Callable[[], RangeScan | None],
        drive_velocity: Callable[[float, float, float], str],
        motion_done: Callable[[str], bool | None],
        stop: Callable[[], None],
        grant_active: Callable[[], bool],
        epoch: Callable[[], int | None],
        session: str,
        device_id: int,
        odom_origin_id: str,
        pose_source_id: str,
        odom_frame: str,
        monotonic: Callable[[], float],
        sleep: Callable[[float], object] = asyncio.sleep,
    ) -> None:
        self.route = route
        self._status, self._scan = status, scan
        self._drive_velocity, self._motion_done, self._stop = drive_velocity, motion_done, stop
        self._grant_active, self._epoch = grant_active, epoch
        self._session, self._device_id, self._odom_origin_id = session, device_id, odom_origin_id
        self._pose_source_id, self._odom_frame = pose_source_id, odom_frame
        self._monotonic, self._sleep = monotonic, sleep

    async def run(self) -> ReturnOutcome:
        bound_epoch = self._epoch()
        if bound_epoch is None:
            return ReturnOutcome(False, "return_pose_unavailable", "no current ground epoch")
        if (
            self.route.session != self._session
            or self.route.device_id != self._device_id
            or self.route.connection_epoch != bound_epoch
            or self.route.odom_origin_id != self._odom_origin_id
            or self.route.pose_source_id != self._pose_source_id
            or self.route.odom_frame != self._odom_frame
        ):
            return ReturnOutcome(
                False, "return_pose_binding_mismatch", "route identity differs from active node"
            )
        initial = self._qualified_status(bound_epoch)
        if isinstance(initial, ReturnOutcome):
            return initial
        start_index = self._resume_index(initial)
        if start_index is None:
            return ReturnOutcome(
                False, "return_resume_outside_corridor", "current pose is outside the pinned route"
            )
        for segment in self.route.segments[start_index:]:
            target = self.route.world_to_odom.point(segment.target)
            footprint = tuple(self.route.world_to_odom.point(point) for point in segment.footprint)
            outcome = await self._follow_segment(bound_epoch, target, footprint)
            if not outcome.completed:
                self._safe_stop()
                return outcome
        self._safe_stop()
        return ReturnOutcome(True)

    def _resume_index(self, status: GroundStatus) -> int | None:
        start = self.route.world_to_odom.point(self.route.start)
        if (
            _within(status, start, self.route.arrival_tolerance_m)
            and _point_clearance(start, self._transformed_footprint(0))
            >= self.route.required_clearance_m
        ):
            return 0
        for index in range(len(self.route.segments)):
            footprint = self._transformed_footprint(index)
            if (
                _point_clearance(ReturnPoint(status.x, status.y), footprint)
                >= self.route.required_clearance_m
            ):
                return index
        return None

    def _transformed_footprint(self, index: int) -> tuple[ReturnPoint, ...]:
        return tuple(
            self.route.world_to_odom.point(point) for point in self.route.segments[index].footprint
        )

    async def _follow_segment(
        self, bound_epoch: int, target: ReturnPoint, footprint: tuple[ReturnPoint, ...]
    ) -> ReturnOutcome:
        while True:
            current = self._qualified_status(bound_epoch)
            if isinstance(current, ReturnOutcome):
                return current
            if not _safe_chord(
                ReturnPoint(current.x, current.y),
                target,
                footprint,
                self.route.required_clearance_m,
            ):
                return ReturnOutcome(
                    False,
                    "return_corridor_departure",
                    "pose or remaining segment lacks approved clearance",
                )
            if _within(current, target, self.route.arrival_tolerance_m):
                return ReturnOutcome(True)
            heading = math.degrees(math.atan2(target.y_m - current.y, target.x_m - current.x)) % 360
            angle = _shortest_angle(heading, current.yaw_deg)
            if abs(angle) > 6:
                result = await self._pulse(
                    bound_epoch, target, footprint, 0.0, 35.0 if angle > 0 else -35.0
                )
            else:
                result = await self._pulse(bound_epoch, target, footprint, _FORWARD_M_S, 0.0)
            if result is not None:
                return result

    async def _pulse(
        self,
        bound_epoch: int,
        target: ReturnPoint,
        footprint: tuple[ReturnPoint, ...],
        linear_m_s: float,
        yaw_deg_s: float,
    ) -> ReturnOutcome | None:
        failure = self._pulse_guard(bound_epoch, target, footprint)
        if failure is not None:
            return failure
        try:
            motion = self._drive_velocity(linear_m_s, yaw_deg_s, _PULSE_S)
        except (OSError, RuntimeError, ValueError) as error:
            return ReturnOutcome(False, "return_motion_refused", str(error))
        while True:
            await self._sleep(0.02)
            failure = self._pulse_guard(bound_epoch, target, footprint)
            if failure is not None:
                return failure
            try:
                done = self._motion_done(motion)
            except OSError as error:
                return ReturnOutcome(False, "return_motion_failed", str(error))
            if done is False:
                continue
            if done is True:
                return None
            return ReturnOutcome(False, "return_motion_failed", "local motion stopped")

    def _pulse_guard(
        self, bound_epoch: int, target: ReturnPoint, footprint: tuple[ReturnPoint, ...]
    ) -> ReturnOutcome | None:
        qualified = self._qualified_status(bound_epoch)
        if isinstance(qualified, ReturnOutcome):
            return qualified
        if not _safe_chord(
            ReturnPoint(qualified.x, qualified.y),
            target,
            footprint,
            self.route.required_clearance_m,
        ):
            return ReturnOutcome(
                False,
                "return_corridor_departure",
                "pose or remaining segment lacks approved clearance",
            )
        if not self._grant_active():
            return ReturnOutcome(
                False, "return_authority_lost", "current relay/operator grant is absent"
            )
        scan = self._scan()
        if scan is None:
            return ReturnOutcome(
                False, "return_lidar_unavailable", "no current full-footprint scan"
            )
        age_ms = int(self._monotonic() * 1_000) - scan.t_ms
        if not 0 <= age_ms <= _MAX_SCAN_AGE_MS:
            return ReturnOutcome(False, "return_lidar_stale", "full-footprint scan is not current")
        if not _within_scan_pose(qualified, scan):
            return ReturnOutcome(
                False, "return_lidar_pose_mismatch", "scan does not match current pose"
            )
        required_cm = math.ceil(self.route.required_clearance_m * 100)
        if any(type(value) is not int or value < required_cm for value in scan.ranges_cm):
            return ReturnOutcome(
                False, "return_footprint_blocked", "scan cannot clear body and stopping distance"
            )
        return None

    def _qualified_status(self, bound_epoch: int) -> GroundStatus | ReturnOutcome:
        if self._epoch() != bound_epoch:
            return ReturnOutcome(False, "return_epoch_changed", "ground connection epoch changed")
        status = self._status()
        now_ms = int(self._monotonic() * 1_000)
        if type(status.t_ms) is not int or not 0 <= now_ms - status.t_ms <= _MAX_STATUS_AGE_MS:
            return ReturnOutcome(False, "return_pose_stale", "current odometry pose is not current")
        if status.pos_quality <= 0 or not all(
            math.isfinite(value) for value in (status.x, status.y, status.yaw_deg)
        ):
            return ReturnOutcome(
                False, "return_pose_unavailable", "current odometry pose is unqualified"
            )
        return status

    def _safe_stop(self) -> None:
        try:
            self._stop()
        except OSError:
            pass


def _geometry(raw: object, clearance_m: float) -> tuple[ReturnPoint, tuple[ReturnSegment, ...]]:
    if not isinstance(raw, Mapping) or set(raw) != {"type", "start", "segments"}:
        raise ValueError("return geometry must contain measured start and segments")
    if (
        raw["type"] != "measured_corridor_v1"
        or not isinstance(raw["segments"], list)
        or len(raw["segments"]) > _MAX_SEGMENTS
    ):
        raise ValueError("return geometry type is invalid")
    start = _point(raw["start"])
    previous = start
    segments: list[ReturnSegment] = []
    for raw_segment in raw["segments"]:
        if not isinstance(raw_segment, Mapping) or set(raw_segment) != {"target", "footprint"}:
            raise ValueError("return segment fields are invalid")
        target = _point(raw_segment["target"])
        if _distance(previous, target) <= 0:
            raise ValueError("return segments cannot repeat a point")
        raw_footprint = raw_segment["footprint"]
        if (
            not isinstance(raw_footprint, list)
            or not 3 <= len(raw_footprint) <= _MAX_POLYGON_VERTICES
        ):
            raise ValueError("return segment footprint is invalid")
        footprint = tuple(_point(value) for value in raw_footprint)
        if not _simple_polygon(footprint):
            raise ValueError("return segment footprint must be a simple polygon")
        if not _safe_chord(previous, target, footprint, clearance_m):
            raise ValueError(
                "return footprint cannot clear the fixed segment body and stopping distance"
            )
        segments.append(ReturnSegment(target, footprint))
        previous = target
    if not segments:
        raise ValueError("return geometry needs a segment")
    return start, tuple(segments)


def _strict_json(content: bytes, name: str) -> object:
    def reject_duplicates(pairs: list[tuple[object, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if type(key) is not str or key in result:
                raise ValueError("duplicate object key")
            result[key] = value
        return result

    try:
        return json.loads(
            content,
            object_pairs_hook=reject_duplicates,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("invalid JSON number")),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError(f"{name} is unreadable") from error


def read_approval_key(path: Path) -> bytes:
    key = _read_bounded(path, _MAX_KEY_BYTES, "return approval key")
    if len(key) < 32:
        raise ValueError("return approval key has an invalid length")
    return key


def _read_bounded(path: Path, maximum: int, name: str) -> bytes:
    try:
        if not stat.S_ISREG(path.stat().st_mode):
            raise ValueError(f"{name} is unreadable")
        with path.open("rb") as source:
            content = source.read(maximum + 1)
    except OSError as error:
        raise ValueError(f"{name} is unreadable") from error
    if len(content) > maximum:
        raise ValueError(f"{name} exceeds the safety limit")
    return content


def _transform(raw: object) -> WorldToOdom:
    if not isinstance(raw, Mapping) or set(raw) != {"x_m", "y_m", "yaw_deg", "registration_id"}:
        raise ValueError("return transform fields are invalid")
    return WorldToOdom(
        _finite(raw["x_m"]),
        _finite(raw["y_m"]),
        _finite(raw["yaw_deg"]),
        _text(raw["registration_id"]),
    )


def _point(raw: object) -> ReturnPoint:
    if not isinstance(raw, Mapping) or set(raw) != {"x_m", "y_m"}:
        raise ValueError("return point fields are invalid")
    return ReturnPoint(_finite(raw["x_m"]), _finite(raw["y_m"]))


def _finite(value: object) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or abs(value) > _MAX_COORDINATE_M
    ):
        raise ValueError("return geometry values must be bounded and finite")
    return float(value)


def _positive_float(value: object) -> float:
    result = _finite(value)
    if result <= 0:
        raise ValueError("return safety distances must be positive")
    return result


def _text(value: object) -> str:
    if not _identifier(value):
        raise ValueError("return identity is invalid")
    return value


def _identifier(value: object) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= 128
        and value == value.strip()
        and value.isprintable()
    )


def _device_id(value: object) -> int:
    if type(value) is not int or not 1 <= value <= 2**31 - 1:
        raise ValueError("return approval device ID is invalid")
    return value


def _epoch(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("return approval connection epoch is invalid")
    return value


def _sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def _distance(first: ReturnPoint, second: ReturnPoint) -> float:
    return math.hypot(first.x_m - second.x_m, first.y_m - second.y_m)


def _within(status: GroundStatus, target: ReturnPoint, tolerance: float) -> bool:
    return math.hypot(status.x - target.x_m, status.y - target.y_m) <= tolerance


def _simple_polygon(polygon: Sequence[ReturnPoint]) -> bool:
    if len(set(polygon)) != len(polygon) or abs(_signed_area(polygon)) < 1e-9:
        return False
    edges = list(zip(polygon, polygon[1:] + polygon[:1], strict=True))
    for index, first in enumerate(edges):
        for other_index, second in enumerate(edges[index + 1 :], index + 1):
            if other_index in {index + 1, (index - 1) % len(edges)} or (
                index == 0 and other_index == len(edges) - 1
            ):
                continue
            if _segments_intersect(*first, *second):
                return False
    return True


def _signed_area(polygon: Sequence[ReturnPoint]) -> float:
    return (
        sum(
            first.x_m * second.y_m - second.x_m * first.y_m
            for first, second in zip(polygon, polygon[1:] + polygon[:1], strict=True)
        )
        / 2
    )


def _safe_chord(
    start: ReturnPoint, target: ReturnPoint, polygon: Sequence[ReturnPoint], clearance_m: float
) -> bool:
    midpoint = ReturnPoint((start.x_m + target.x_m) / 2, (start.y_m + target.y_m) / 2)
    return (
        _inside_point(midpoint, polygon)
        and _segment_clearance(start, target, polygon) >= clearance_m
    )


def _point_clearance(point: ReturnPoint, polygon: Sequence[ReturnPoint]) -> float:
    if not _inside_point(point, polygon):
        return -1.0
    return min(
        _point_segment_distance(point, first, second)
        for first, second in zip(polygon, polygon[1:] + polygon[:1], strict=True)
    )


def _segment_clearance(
    start: ReturnPoint, target: ReturnPoint, polygon: Sequence[ReturnPoint]
) -> float:
    return min(
        _segment_distance(start, target, first, second)
        for first, second in zip(polygon, polygon[1:] + polygon[:1], strict=True)
    )


def _inside_point(point: ReturnPoint, polygon: Sequence[ReturnPoint]) -> bool:
    inside = False
    for first, second in zip(polygon, polygon[1:] + polygon[:1], strict=True):
        if ((first.y_m > point.y_m) != (second.y_m > point.y_m)) and point.x_m < (
            (second.x_m - first.x_m) * (point.y_m - first.y_m) / (second.y_m - first.y_m)
            + first.x_m
        ):
            inside = not inside
    return inside


def _orientation(first: ReturnPoint, second: ReturnPoint, third: ReturnPoint) -> float:
    return (second.x_m - first.x_m) * (third.y_m - first.y_m) - (second.y_m - first.y_m) * (
        third.x_m - first.x_m
    )


def _segments_intersect(
    first: ReturnPoint, second: ReturnPoint, third: ReturnPoint, fourth: ReturnPoint
) -> bool:
    a, b = _orientation(first, second, third), _orientation(first, second, fourth)
    c, d = _orientation(third, fourth, first), _orientation(third, fourth, second)
    if a == b == c == d == 0:
        return not (
            max(first.x_m, second.x_m) < min(third.x_m, fourth.x_m)
            or max(third.x_m, fourth.x_m) < min(first.x_m, second.x_m)
            or max(first.y_m, second.y_m) < min(third.y_m, fourth.y_m)
            or max(third.y_m, fourth.y_m) < min(first.y_m, second.y_m)
        )
    return a * b <= 0 and c * d <= 0


def _point_segment_distance(point: ReturnPoint, first: ReturnPoint, second: ReturnPoint) -> float:
    dx, dy = second.x_m - first.x_m, second.y_m - first.y_m
    length_sq = dx * dx + dy * dy
    if length_sq == 0:
        return _distance(point, first)
    fraction = max(
        0.0, min(1.0, ((point.x_m - first.x_m) * dx + (point.y_m - first.y_m) * dy) / length_sq)
    )
    return math.hypot(
        point.x_m - (first.x_m + fraction * dx), point.y_m - (first.y_m + fraction * dy)
    )


def _segment_distance(
    first: ReturnPoint, second: ReturnPoint, third: ReturnPoint, fourth: ReturnPoint
) -> float:
    if _segments_intersect(first, second, third, fourth):
        return 0.0
    return min(
        _point_segment_distance(first, third, fourth),
        _point_segment_distance(second, third, fourth),
        _point_segment_distance(third, first, second),
        _point_segment_distance(fourth, first, second),
    )


def _within_scan_pose(status: GroundStatus, scan: RangeScan) -> bool:
    return (
        math.hypot(status.x - scan.pose[0], status.y - scan.pose[1]) <= 0.08
        and abs(_shortest_angle(status.yaw_deg, scan.pose[2])) <= 6
    )


def _shortest_angle(target: float, current: float) -> float:
    return (target - current + 180) % 360 - 180
