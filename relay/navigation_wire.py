"""Signed, command-scoped navigation evidence for a phone flight controller."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from math import isfinite

from adapters.dji_mini3.remote import CommandRequest
from planner.models import Command, CommandOperation, FleetSnapshot, Plan
from planner.navigation_authorization import content_digest
from planner.navigation_runtime import NavigationRuntime
from relay.auth import sign_event
from relay.control_localization import ControlPose

type SnapshotProvider = Callable[[], FleetSnapshot]
type SigningKey = Callable[[int], bytes | None]
type EventIds = Callable[[], str]
type Clock = Callable[[], int]


@dataclass(frozen=True, slots=True)
class NavigationWireConfig:
    clock_lease_id: str
    clock_lease_expires_at_ms: int
    max_clock_error_ms: int
    navigation_config_id: str
    navigation_config_sha256: str
    map_version: str
    map_sha256: str
    geometry_sha256: str
    camera_calibration_sha256: str
    body_extrinsics_sha256: str
    world_transform_sha256: str
    control_source_ids: tuple[str, ...]
    max_speed_mm_s: int
    max_acceleration_mm_s2: int
    max_deceleration_mm_s2: int
    max_position_uncertainty_mm: int
    max_cross_track_mm: int
    arrival_horizontal_tolerance_mm: int
    arrival_vertical_tolerance_mm: int
    pose_freshness_ms: int
    tracking_timeout_ms: int

    def __post_init__(self) -> None:
        for name in ("clock_lease_id", "navigation_config_id", "map_version"):
            _identity(getattr(self, name), name)
        for name in (
            "navigation_config_sha256",
            "map_sha256",
            "geometry_sha256",
            "camera_calibration_sha256",
            "body_extrinsics_sha256",
            "world_transform_sha256",
        ):
            _sha256(getattr(self, name), name)
        if not isinstance(self.control_source_ids, tuple) or not self.control_source_ids:
            raise ValueError("navigation control sources must be a nonempty tuple")
        if tuple(sorted(set(self.control_source_ids))) != self.control_source_ids:
            raise ValueError("navigation control sources must be sorted and unique")
        for source_id in self.control_source_ids:
            _identity(source_id, "control source")
        _nonnegative_int(self.clock_lease_expires_at_ms, "clock lease expiry")
        _nonnegative_int(self.max_clock_error_ms, "maximum clock error")
        for name in (
            "max_speed_mm_s",
            "max_acceleration_mm_s2",
            "max_deceleration_mm_s2",
            "max_position_uncertainty_mm",
            "max_cross_track_mm",
            "arrival_horizontal_tolerance_mm",
            "arrival_vertical_tolerance_mm",
            "pose_freshness_ms",
            "tracking_timeout_ms",
        ):
            _positive_int(getattr(self, name), name)


@dataclass(frozen=True, slots=True)
class _Scope:
    plan: Plan
    snapshot: SnapshotProvider


@dataclass(frozen=True, slots=True)
class _ActiveCommand:
    plan: Plan
    command: Command
    snapshot: SnapshotProvider
    expires_at_ms: int


class NavigationWirePublisher:
    def __init__(
        self,
        runtime: NavigationRuntime,
        wire_config: NavigationWireConfig,
        *,
        session: str,
        signing_key: SigningKey,
        event_ids: EventIds,
        clock: Clock,
    ) -> None:
        if not isinstance(runtime, NavigationRuntime):
            raise ValueError("navigation wire requires a NavigationRuntime")
        if runtime.approval.mode != "flight":
            raise ValueError("phone navigation requires a flight approval")
        if not isinstance(wire_config, NavigationWireConfig):
            raise ValueError("navigation wire requires NavigationWireConfig")
        _identity(session, "session", maximum=512)
        if not callable(signing_key) or not callable(event_ids) or not callable(clock):
            raise ValueError("navigation wire dependencies must be callable")
        expected = getattr(runtime.config, "wire_config_sha256", None)
        if expected != content_digest(asdict(wire_config)):
            raise ValueError("navigation wire configuration is not approved by the runtime")
        if any(
            frame.control_pins is None
            or tuple(sorted(frame.control_pins.source_ids)) != wire_config.control_source_ids
            for frame in runtime.config.frames
        ):
            raise ValueError("navigation wire control sources do not match retained pose pins")
        self.runtime = runtime
        self.wire_config = wire_config
        self.session = session
        self._signing_key = signing_key
        self._event_ids = event_ids
        self._clock = clock
        self._scope: ContextVar[_Scope | None] = ContextVar(
            f"navigation_wire_scope_{id(self)}", default=None
        )
        self._active: dict[str, _ActiveCommand] = {}
        self._sequences: dict[tuple[int, int], int] = {}

    @contextmanager
    def command_scope(self, plan: Plan, snapshot: SnapshotProvider) -> Iterator[None]:
        if not isinstance(plan, Plan) or plan.navigation is None or not callable(snapshot):
            raise ValueError(
                "navigation command scope requires a typed navigation plan and snapshot"
            )
        token = self._scope.set(_Scope(plan, snapshot))
        try:
            yield
        finally:
            self._scope.reset(token)

    def prepare_request(self, request: CommandRequest) -> list[dict[str, object]]:
        if not isinstance(request, CommandRequest):
            raise ValueError("navigation wire requires a typed command request")
        scope = self._scope.get()
        if scope is None:
            if "navigation_route_id" in request.args:
                raise ValueError("navigation command was sent outside its approved command scope")
            return []
        command = next(
            (item for item in scope.plan.commands if item.command_id == request.command_id), None
        )
        if command is None:
            raise ValueError("command request is outside the approved navigation plan")
        if command.operation is not request.operation or command.drone_id != request.drone_id:
            raise ValueError("command request differs from the approved command")
        if command.operation is not CommandOperation.GOTO:
            return []
        self._validate_request(request, command)
        return self.prepare(scope.plan, command, scope.snapshot())

    def prepare(
        self, plan: Plan, command: Command, snapshot: FleetSnapshot
    ) -> list[dict[str, object]]:
        refusal = self.runtime.check(plan, command, snapshot)
        if refusal is not None:
            raise ValueError(f"navigation wire refused: {refusal.detail}")
        self._validate_artifact_pins()
        segment = self._segment(plan, command)
        if segment is None:
            return []
        now_ms = self._now()
        expires_at_ms = min(
            self.wire_config.clock_lease_expires_at_ms, self.runtime.approval.expires_at_ms
        )
        if expires_at_ms <= now_ms:
            raise ValueError("navigation route authorization has expired")
        pose = self._control_pose(command, now_ms)
        route_id = plan.navigation.route_id
        route_sequence = self._next_sequence(command.drone_id, command.connection_epoch)
        key = self._key(command.drone_id)
        start = self.runtime.config.frame(command.drone_id).enu(segment.start)
        end = self.runtime.config.frame(command.drone_id).enu(segment.end)
        route = {
            "v": 1,
            "type": "navigation_route_authorization",
            "t": now_ms,
            "expires_at_ms": expires_at_ms,
            "event_id": self._event_id(),
            "session": self.session,
            "device_id": command.drone_id,
            "connection_epoch": command.connection_epoch,
            "command_id": command.command_id,
            "route_id": route_id,
            "seq": route_sequence,
            "position_frame": "map_enu",
            "clock_lease_id": self.wire_config.clock_lease_id,
            "max_clock_error_ms": self.wire_config.max_clock_error_ms,
            **self._pins(),
            "segments": [
                {
                    "start_x_mm": _millimetres(start[0], "segment start x"),
                    "start_y_mm": _millimetres(start[1], "segment start y"),
                    "start_z_mm": _millimetres(start[2], "segment start z"),
                    "end_x_mm": _millimetres(end[0], "segment end x"),
                    "end_y_mm": _millimetres(end[1], "segment end y"),
                    "end_z_mm": _millimetres(end[2], "segment end z"),
                    "tube_radius_mm": _millimetres(
                        segment.radius_m, "segment tube radius", positive=True
                    ),
                }
            ],
            **self._limits(),
            "flight_approved": True,
        }
        pose_sequence = self._next_sequence(command.drone_id, command.connection_epoch)
        frames = [
            self._sign(route, key),
            self._pose_frame(pose, command, route_id, pose_sequence, key),
        ]
        self._retire_drone(command.drone_id, command.connection_epoch)
        self._active[command.command_id] = _ActiveCommand(
            plan, command, self._scope_snapshot(plan), expires_at_ms
        )
        return frames

    def update(self, pose: ControlPose) -> list[dict[str, object]]:
        if not isinstance(pose, ControlPose):
            raise ValueError("navigation updates require a ControlPose")
        active = next(
            (
                item
                for item in self._active.values()
                if item.command.drone_id == pose.drone_id
                and item.command.connection_epoch == pose.connection_epoch
            ),
            None,
        )
        if active is None:
            return []
        now_ms = self._now()
        if now_ms >= active.expires_at_ms:
            self._active.pop(active.command.command_id, None)
            return []
        retained = self.runtime.control_pose
        if retained is None or retained(active.command.drone_id) != pose:
            raise ValueError("navigation pose update is not the retained control pose")
        if pose.status == "ready":
            refusal = self.runtime.check(active.plan, active.command, active.snapshot())
            if refusal is not None:
                self._active.pop(active.command.command_id, None)
                raise ValueError(f"navigation wire refused: {refusal.detail}")
        return [
            self._pose_frame(
                pose,
                active.command,
                active.plan.navigation.route_id,
                self._next_sequence(pose.drone_id, pose.connection_epoch),
                self._key(pose.drone_id),
            )
        ]

    def retire(self, command_id: str) -> None:
        self._active.pop(command_id, None)

    def retire_epoch(self, drone_id: int, connection_epoch: int) -> None:
        self._retire_drone(drone_id, connection_epoch)

    def retire_other_epochs(self, drone_id: int, connection_epoch: int) -> None:
        for command_id, active in tuple(self._active.items()):
            if (
                active.command.drone_id == drone_id
                and active.command.connection_epoch != connection_epoch
            ):
                self._active.pop(command_id)

    def _retire_drone(self, drone_id: int, connection_epoch: int) -> None:
        for command_id, active in tuple(self._active.items()):
            if (
                active.command.drone_id == drone_id
                and active.command.connection_epoch == connection_epoch
            ):
                self._active.pop(command_id)

    def _scope_snapshot(self, plan: Plan) -> SnapshotProvider:
        scope = self._scope.get()
        if scope is None or scope.plan is not plan:
            raise ValueError("navigation command scope changed while preparing wire evidence")
        return scope.snapshot

    def _segment(self, plan: Plan, command: Command):
        if plan.navigation is None or command.operation is not CommandOperation.GOTO:
            return None
        cursor = plan.commands.index(command)
        for route in plan.navigation.route.routes:
            if cursor < len(route.swept_segments):
                return route.swept_segments[cursor]
            cursor -= len(route.swept_segments) + 1
        raise ValueError("navigation command is outside its frozen route")

    def _validate_artifact_pins(self) -> None:
        artifact = self.runtime.artifact()
        if (
            artifact.map_pin.version != self.wire_config.map_version
            or artifact.map_pin.content_sha256 != self.wire_config.map_sha256
            or artifact.geometry_pin.content_sha256 != self.wire_config.geometry_sha256
        ):
            raise ValueError("navigation wire artifact pins do not match the runtime")

    def _validate_request(self, request: CommandRequest, command: Command) -> None:
        expected = {
            "x_mm": _millimetres(float(command.parameters["x"]), "command x"),
            "y_mm": _millimetres(float(command.parameters["y"]), "command y"),
            "z_mm": _millimetres(float(command.parameters["z"]), "command z"),
            "speed_mm_s": _millimetres(
                float(command.parameters["speed"]), "command speed", positive=True
            ),
            "navigation_route_id": plan_route_id(command),
        }
        if dict(request.args) != expected:
            raise ValueError("wire goto differs from the approved navigation command")

    def _control_pose(self, command: Command, now_ms: int) -> ControlPose:
        source = self.runtime.control_pose
        pose = source(command.drone_id) if source is not None else None
        if not isinstance(pose, ControlPose) or pose.status != "ready":
            raise ValueError("navigation route requires a ready retained control pose")
        if pose.session != self.session or pose.connection_epoch != command.connection_epoch:
            raise ValueError("navigation control pose identity changed")
        if now_ms < pose.t:
            raise ValueError("navigation control pose exceeds the relay clock")
        return pose

    def _pose_frame(
        self,
        pose: ControlPose,
        command: Command,
        route_id: str,
        sequence: int,
        key: bytes,
    ) -> dict[str, object]:
        now_ms = self._now()
        if now_ms < pose.t:
            raise ValueError("navigation control pose exceeds the relay clock")
        ready = pose.status == "ready"
        if (
            pose.session != self.session
            or pose.drone_id != command.drone_id
            or pose.connection_epoch != command.connection_epoch
        ):
            raise ValueError("navigation control pose belongs to another route")
        frame: dict[str, object] = {
            "v": 1,
            "type": "navigation_pose",
            "t": now_ms,
            "event_id": self._event_id(),
            "session": self.session,
            "device_id": command.drone_id,
            "connection_epoch": command.connection_epoch,
            "command_id": command.command_id,
            "route_id": route_id,
            "seq": sequence,
            "position_frame": "map_enu",
            "clock_lease_id": self.wire_config.clock_lease_id,
            **self._pins(),
            "pose_time_ms": pose.pose_time_ms if ready else None,
            "fix_time_ms": pose.fix_time_ms if ready else None,
            "x_mm": pose.x_mm if ready else None,
            "y_mm": pose.y_mm if ready else None,
            "z_mm": pose.z_mm if ready else None,
            "position_uncertainty_mm": pose.position_uncertainty_mm if ready else None,
            "status": pose.status,
            "flight_approved": True,
        }
        return self._sign(frame, key)

    def _pins(self) -> dict[str, object]:
        return {
            "navigation_config_id": self.wire_config.navigation_config_id,
            "navigation_config_sha256": self.wire_config.navigation_config_sha256,
            "map_version": self.wire_config.map_version,
            "map_sha256": self.wire_config.map_sha256,
            "geometry_sha256": self.wire_config.geometry_sha256,
            "camera_calibration_sha256": self.wire_config.camera_calibration_sha256,
            "body_extrinsics_sha256": self.wire_config.body_extrinsics_sha256,
            "world_transform_sha256": self.wire_config.world_transform_sha256,
            "control_source_ids": list(self.wire_config.control_source_ids),
        }

    def _limits(self) -> dict[str, int]:
        return {
            "max_speed_mm_s": self.wire_config.max_speed_mm_s,
            "max_acceleration_mm_s2": self.wire_config.max_acceleration_mm_s2,
            "max_deceleration_mm_s2": self.wire_config.max_deceleration_mm_s2,
            "max_position_uncertainty_mm": self.wire_config.max_position_uncertainty_mm,
            "max_cross_track_mm": self.wire_config.max_cross_track_mm,
            "arrival_horizontal_tolerance_mm": self.wire_config.arrival_horizontal_tolerance_mm,
            "arrival_vertical_tolerance_mm": self.wire_config.arrival_vertical_tolerance_mm,
            "pose_freshness_ms": self.wire_config.pose_freshness_ms,
            "tracking_timeout_ms": self.wire_config.tracking_timeout_ms,
        }

    def _event_id(self) -> str:
        value = self._event_ids()
        _identity(value, "event id")
        return value

    def _key(self, drone_id: int) -> bytes:
        key = self._signing_key(drone_id)
        if not isinstance(key, bytes) or len(key) < 32:
            raise ValueError("navigation signing credential is unavailable")
        return key

    def _next_sequence(self, drone_id: int, connection_epoch: int) -> int:
        identity = (drone_id, connection_epoch)
        sequence = self._sequences.get(identity, 0) + 1
        self._sequences[identity] = sequence
        return sequence

    def _now(self) -> int:
        value = self._clock()
        _nonnegative_int(value, "navigation clock")
        return value

    @staticmethod
    def _sign(event: dict[str, object], key: bytes) -> dict[str, object]:
        return {**event, "signature": sign_event(event, key)}


def plan_route_id(command: Command) -> str:
    route_id = command.parameters.get("navigation_route_id")
    _identity(route_id, "navigation route id")
    return route_id


def _millimetres(value: object, name: str, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float) or not isfinite(value):
        raise ValueError(f"{name} must be finite")
    result = round(float(value) * 1000)
    if positive and result <= 0:
        raise ValueError(f"{name} must be positive")
    return result


def _sha256(value: object, name: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _identity(value: object, name: str, *, maximum: int = 128) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or value != value.strip()
        or not value.isprintable()
    ):
        raise ValueError(f"{name} must be canonical printable text")
    return value


def _nonnegative_int(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")
    return value


def _positive_int(value: object, name: str) -> int:
    result = _nonnegative_int(value, name)
    if result == 0:
        raise ValueError(f"{name} must be positive")
    return result
