from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from relay.auth import verify_event_signature

from .models import GroundStatus, RangeScan


@dataclass(frozen=True, slots=True)
class ReturnPoint:
    x_m: float
    y_m: float

    def __post_init__(self) -> None:
        if not all(math.isfinite(value) for value in (self.x_m, self.y_m)):
            raise ValueError("return points must be finite")


@dataclass(frozen=True, slots=True)
class WorldToOdom:
    x_m: float
    y_m: float
    yaw_deg: float
    registration_id: str

    def __post_init__(self) -> None:
        if not all(math.isfinite(value) for value in (self.x_m, self.y_m, self.yaw_deg)):
            raise ValueError("world-to-odom transform must be finite")
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
        if len(self.footprint) < 3:
            raise ValueError("return corridor footprints need at least three vertices")


@dataclass(frozen=True, slots=True)
class ApprovedReturnRoute:
    return_id: str
    approval_id: str
    approval_signer: str
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
                self.source_registration_id,
                self.pose_source_id,
                self.odom_frame,
            )
        ):
            raise ValueError("return approval identities must be non-empty bounded text")
        if self.source_registration_id != self.world_to_odom.registration_id:
            raise ValueError("return source registration must bind the measured transform")
        if not self.segments:
            raise ValueError("return route needs at least one measured segment")
        if not all(
            isinstance(value, float) and math.isfinite(value) and value > 0
            for value in (self.footprint_radius_m, self.arrival_tolerance_m)
        ):
            raise ValueError("return safety distances must be finite and positive")
        if not _sha256(self.geometry_sha256):
            raise ValueError("return geometry requires its SHA-256")

    @classmethod
    def load(cls, path: Path, approval_key: bytes) -> ApprovedReturnRoute:
        if not approval_key:
            raise ValueError("return approval key is required")
        try:
            raw = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError("return approval record is unreadable") from error
        if not isinstance(raw, dict) or not isinstance(raw.get("signature"), str):
            raise ValueError("return approval signature is required")
        unsigned = {key: value for key, value in raw.items() if key != "signature"}
        if not verify_event_signature(unsigned, raw["signature"], approval_key):
            raise ValueError("return approval signature is invalid")
        required = {
            "v",
            "return_id",
            "approval_id",
            "approval_signer",
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
        encoded = raw["geometry_bytes_b64"]
        if not isinstance(encoded, str):
            raise ValueError("return geometry bytes are required")
        try:
            geometry_bytes = base64.b64decode(encoded, validate=True)
        except ValueError as error:
            raise ValueError("return geometry bytes are malformed") from error
        digest = hashlib.sha256(geometry_bytes).hexdigest()
        if raw["geometry_sha256"] != digest:
            raise ValueError("return geometry hash does not match approved bytes")
        try:
            geometry = json.loads(geometry_bytes)
        except json.JSONDecodeError as error:
            raise ValueError("return measured geometry is not JSON") from error
        start, segments = _geometry(geometry)
        transform = _transform(raw["world_to_odom"])
        return cls(
            return_id=_text(raw["return_id"]),
            approval_id=_text(raw["approval_id"]),
            approval_signer=_text(raw["approval_signer"]),
            source_registration_id=_text(raw["source_registration_id"]),
            pose_source_id=_text(raw["pose_source_id"]),
            odom_frame=_text(raw["odom_frame"]),
            world_to_odom=transform,
            start=start,
            segments=segments,
            footprint_radius_m=_positive_float(raw["footprint_radius_m"]),
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
        pose_source_id: str,
        odom_frame: str,
        monotonic: Callable[[], float],
        sleep: Callable[[float], object] = asyncio.sleep,
    ) -> None:
        self.route = route
        self._status = status
        self._scan = scan
        self._drive_velocity = drive_velocity
        self._motion_done = motion_done
        self._stop = stop
        self._grant_active = grant_active
        self._epoch = epoch
        self._pose_source_id = pose_source_id
        self._odom_frame = odom_frame
        self._monotonic = monotonic
        self._sleep = sleep

    async def run(self) -> ReturnOutcome:
        bound_epoch = self._epoch()
        if bound_epoch is None:
            return ReturnOutcome(False, "return_pose_unavailable", "no current ground epoch")
        if (
            self.route.pose_source_id != self._pose_source_id
            or self.route.odom_frame != self._odom_frame
        ):
            return ReturnOutcome(
                False, "return_pose_binding_mismatch", "route pose source or frame differs"
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
        if _within(
            status,
            self.route.world_to_odom.point(self.route.start),
            self.route.arrival_tolerance_m,
        ):
            return 0
        for index, segment in enumerate(self.route.segments):
            footprint = tuple(self.route.world_to_odom.point(point) for point in segment.footprint)
            if _inside(status, footprint):
                return index
        return None

    async def _follow_segment(
        self, bound_epoch: int, target: ReturnPoint, footprint: tuple[ReturnPoint, ...]
    ) -> ReturnOutcome:
        while True:
            current = self._qualified_status(bound_epoch)
            if isinstance(current, ReturnOutcome):
                return current
            if not _inside(current, footprint):
                return ReturnOutcome(
                    False, "return_corridor_departure", "pose left approved footprint"
                )
            if _within(current, target, self.route.arrival_tolerance_m):
                return ReturnOutcome(True)
            heading = math.degrees(math.atan2(target.y_m - current.y, target.x_m - current.x)) % 360
            angle = _shortest_angle(heading, current.yaw_deg)
            if abs(angle) > 6:
                result = await self._pulse(bound_epoch, 0.0, 35.0 if angle > 0 else -35.0)
            else:
                result = await self._pulse(bound_epoch, 0.12, 0.0)
            if result is not None:
                return result

    async def _pulse(
        self, bound_epoch: int, linear_m_s: float, yaw_deg_s: float
    ) -> ReturnOutcome | None:
        failure = self._pulse_guard(bound_epoch)
        if failure is not None:
            return failure
        try:
            motion = self._drive_velocity(linear_m_s, yaw_deg_s, 0.2)
        except (OSError, RuntimeError, ValueError) as error:
            return ReturnOutcome(False, "return_motion_refused", str(error))
        while True:
            await self._sleep(0.02)
            failure = self._pulse_guard(bound_epoch)
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

    def _pulse_guard(self, bound_epoch: int) -> ReturnOutcome | None:
        qualified = self._qualified_status(bound_epoch)
        if isinstance(qualified, ReturnOutcome):
            return qualified
        if not self._grant_active():
            return ReturnOutcome(
                False, "return_authority_lost", "current relay/operator grant is absent"
            )
        scan = self._scan()
        if scan is None:
            return ReturnOutcome(
                False, "return_lidar_unavailable", "no current full-footprint scan"
            )
        if int(self._monotonic() * 1_000) - scan.t_ms > 500:
            return ReturnOutcome(False, "return_lidar_stale", "full-footprint scan is stale")
        if not _within_scan_pose(qualified, scan):
            return ReturnOutcome(
                False, "return_lidar_pose_mismatch", "scan does not match current pose"
            )
        if any(
            value <= 0 or value < math.ceil(self.route.footprint_radius_m * 100)
            for value in scan.ranges_cm
        ):
            return ReturnOutcome(
                False, "return_footprint_blocked", "scan cannot clear the full robot footprint"
            )
        return None

    def _qualified_status(self, bound_epoch: int) -> GroundStatus | ReturnOutcome:
        if self._epoch() != bound_epoch:
            return ReturnOutcome(False, "return_epoch_changed", "ground connection epoch changed")
        status = self._status()
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


def _geometry(raw: object) -> tuple[ReturnPoint, tuple[ReturnSegment, ...]]:
    if not isinstance(raw, Mapping) or set(raw) != {"type", "start", "segments"}:
        raise ValueError("return geometry must contain measured start and segments")
    if raw["type"] != "measured_corridor_v1" or not isinstance(raw["segments"], list):
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
        if not isinstance(raw_footprint, list):
            raise ValueError("return segment footprint is invalid")
        footprint = tuple(_point(value) for value in raw_footprint)
        if not _inside_point(previous, footprint) or not _inside_point(target, footprint):
            raise ValueError("return footprint must contain its fixed segment endpoints")
        segments.append(ReturnSegment(target, footprint))
        previous = target
    if not segments:
        raise ValueError("return geometry needs a segment")
    return start, tuple(segments)


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
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise ValueError("return geometry values must be finite")
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


def _inside(status: GroundStatus, footprint: Sequence[ReturnPoint]) -> bool:
    return _inside_point(ReturnPoint(status.x, status.y), footprint)


def _inside_point(point: ReturnPoint, polygon: Sequence[ReturnPoint]) -> bool:
    inside = False
    for first, second in zip(polygon, polygon[1:] + polygon[:1], strict=True):
        if ((first.y_m > point.y_m) != (second.y_m > point.y_m)) and point.x_m < (
            (second.x_m - first.x_m) * (point.y_m - first.y_m) / (second.y_m - first.y_m)
            + first.x_m
        ):
            inside = not inside
    return inside


def _within_scan_pose(status: GroundStatus, scan: RangeScan) -> bool:
    return (
        math.hypot(status.x - scan.pose[0], status.y - scan.pose[1]) <= 0.08
        and abs(_shortest_angle(status.yaw_deg, scan.pose[2])) <= 6
    )


def _shortest_angle(target: float, current: float) -> float:
    return (target - current + 180) % 360 - 180
