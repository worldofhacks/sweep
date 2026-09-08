"""Signed, command-scoped navigation evidence for a phone flight controller."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, replace
from math import isfinite
from threading import RLock
from types import MappingProxyType

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
    max_authorization_lifetime_ms: int
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
    arrival_hold_timeout_ms: int = 0

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
        _positive_int(self.max_authorization_lifetime_ms, "authorization lifetime")
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
        _nonnegative_int(self.arrival_hold_timeout_ms, "arrival hold timeout")
        if self.arrival_hold_timeout_ms > 180_000:
            raise ValueError("arrival hold timeout exceeds 180000 ms")


def wire_config_digest_candidates(
    profiles: Mapping[int, NavigationWireConfig],
) -> frozenset[str]:
    canonical = {str(drone_id): asdict(profiles[drone_id]) for drone_id in sorted(profiles)}
    canonical_digest = content_digest(canonical)
    if any(profile.arrival_hold_timeout_ms for profile in profiles.values()):
        return frozenset((canonical_digest,))
    legacy = {
        drone_id: {
            name: value for name, value in profile.items() if name != "arrival_hold_timeout_ms"
        }
        for drone_id, profile in canonical.items()
    }
    return frozenset((canonical_digest, content_digest(legacy)))


@dataclass(frozen=True, slots=True)
class _Scope:
    plan: Plan
    snapshot: SnapshotProvider


@dataclass(frozen=True, slots=True)
class _ActiveCommand:
    plan: Plan
    command: Command
    snapshot: SnapshotProvider
    profile: NavigationWireConfig
    issued_at_ms: int
    expires_at_ms: int


class NavigationTrackingError(ValueError):
    def __init__(self, active: _ActiveCommand, detail: str) -> None:
        super().__init__(f"navigation wire refused: {detail}")
        self.command_id = active.command.command_id
        self.intent_id = active.command.intent_id
        self.drone_id = active.command.drone_id
        self.connection_epoch = active.command.connection_epoch
        self.detail = detail


class NavigationWirePublisher:
    def __init__(
        self,
        runtime: NavigationRuntime,
        wire_configs: Mapping[int, NavigationWireConfig],
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
        if not isinstance(wire_configs, Mapping):
            raise ValueError("navigation wire requires per-drone NavigationWireConfig values")
        profiles = dict(wire_configs)
        frame_ids = {frame.drone_id for frame in runtime.config.frames}
        if set(profiles) != frame_ids or any(
            type(drone_id) is not int or not isinstance(profile, NavigationWireConfig)
            for drone_id, profile in profiles.items()
        ):
            raise ValueError("navigation wire profiles must match every configured aircraft")
        _identity(session, "session", maximum=512)
        if not callable(signing_key) or not callable(event_ids) or not callable(clock):
            raise ValueError("navigation wire dependencies must be callable")
        expected = getattr(runtime.config, "wire_config_sha256", None)
        if expected not in wire_config_digest_candidates(profiles):
            raise ValueError("navigation wire configuration is not approved by the runtime")
        self.runtime = runtime
        self.wire_configs = MappingProxyType(profiles)
        for frame in runtime.config.frames:
            self._validate_profile(frame, profiles[frame.drone_id])
        self.session = session
        self._signing_key = signing_key
        self._event_ids = event_ids
        self._clock = clock
        self._scope: ContextVar[_Scope | None] = ContextVar(
            f"navigation_wire_scope_{id(self)}", default=None
        )
        self._active: dict[str, _ActiveCommand] = {}
        self._retained: dict[str, _ActiveCommand] = {}
        self._pending: dict[str, _ActiveCommand] = {}
        self._sequences: dict[tuple[int, int], int] = {}
        self._lock = RLock()

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
        now_ms = self._now()
        pose = self._control_pose(command, now_ms, snapshot)
        snapshot = replace(snapshot, now_ms=now_ms)
        refusal = self.runtime.check(plan, command, snapshot, _tracking_pose=pose)
        if refusal is not None:
            raise ValueError(f"navigation wire refused: {refusal.detail}")
        self._validate_artifact_pins()
        segment = self._segment(plan, command)
        if segment is None:
            return []
        profile = self._profile(command.drone_id)
        expires_at_ms = min(
            profile.clock_lease_expires_at_ms,
            self.runtime.approval.expires_at_ms,
            now_ms + profile.max_authorization_lifetime_ms,
        )
        if expires_at_ms <= now_ms:
            raise ValueError("navigation route authorization has expired")
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
            "clock_lease_id": profile.clock_lease_id,
            "max_clock_error_ms": profile.max_clock_error_ms,
            **self._pins(profile),
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
            **self._limits(profile),
            "flight_approved": True,
        }
        pose_sequence = self._next_sequence(command.drone_id, command.connection_epoch)
        frames = [
            self._sign(route, key),
            self._pose_frame(pose, command, route_id, pose_sequence, key, profile),
        ]
        with self._lock:
            self._retire_drone(command.drone_id, command.connection_epoch)
            self._pending[command.command_id] = _ActiveCommand(
                plan, command, self._scope_snapshot(plan), profile, now_ms, expires_at_ms
            )
        return frames

    def activate(self, command_id: str) -> None:
        with self._lock:
            pending = self._pending.pop(command_id, None)
            if pending is None:
                raise ValueError("navigation route authorization is not pending activation")
            if self._now() >= pending.expires_at_ms:
                raise ValueError("navigation route authorization expired before command dispatch")
            self._active[command_id] = pending

    def update(self, pose: ControlPose) -> list[dict[str, object]]:
        if not isinstance(pose, ControlPose):
            raise ValueError("navigation updates require a ControlPose")
        with self._lock:
            active = next(
                (
                    item
                    for item in self._active.values()
                    if item.command.drone_id == pose.drone_id
                    and item.command.connection_epoch == pose.connection_epoch
                ),
                None,
            )
            retained_command = (
                None
                if active is not None
                else next(
                    (
                        item
                        for item in self._retained.values()
                        if item.command.drone_id == pose.drone_id
                        and item.command.connection_epoch == pose.connection_epoch
                    ),
                    None,
                )
            )
            active = retained_command if active is None else active
        if active is None:
            return []
        try:
            now_ms = self._now()
            if now_ms >= active.expires_at_ms:
                self.retire(active.command.command_id)
                if retained_command is not None:
                    return []
                raise NavigationTrackingError(active, "navigation route authorization expired")
            retained = self.runtime.control_pose
            if retained is None or retained(active.command.drone_id) != pose:
                self.retire(active.command.command_id)
                if retained_command is not None:
                    return []
                raise NavigationTrackingError(
                    active, "navigation pose update is not the retained control pose"
                )
            if pose.status == "ready":
                checker = getattr(self.runtime, "check_tracking", None)
                if not callable(checker):
                    self.retire(active.command.command_id)
                    if retained_command is not None:
                        return []
                    raise NavigationTrackingError(
                        active, "navigation runtime does not provide current-segment tracking"
                    )
                refusal = checker(active.plan, active.command, active.snapshot(), pose=pose)
                if refusal is not None:
                    self.retire(active.command.command_id)
                    if retained_command is not None:
                        return []
                    raise NavigationTrackingError(active, refusal.detail)
            with self._lock:
                if (
                    self._active.get(active.command.command_id) != active
                    and self._retained.get(active.command.command_id) != active
                ):
                    return []
                sequence = self._next_sequence(pose.drone_id, pose.connection_epoch)
            return [
                self._pose_frame(
                    pose,
                    active.command,
                    active.plan.navigation.route_id,
                    sequence,
                    self._key(pose.drone_id),
                    active.profile,
                )
            ]
        except NavigationTrackingError:
            raise
        except ValueError as error:
            self.retire(active.command.command_id)
            if retained_command is not None:
                return []
            raise NavigationTrackingError(active, str(error)) from error

    def retain_arrival(self, command_id: str) -> bool:
        with self._lock:
            if command_id in self._retained:
                return True
            active = self._active.get(command_id)
        if active is None or active.command.operation is not CommandOperation.GOTO:
            return False
        if self._now() >= active.expires_at_ms:
            self.retire(command_id)
            return False
        refusal = self.runtime.check(
            active.plan,
            active.command,
            active.snapshot(),
            completed=True,
            issued_at_ms=active.issued_at_ms,
        )
        if refusal is not None:
            self.retire(command_id)
            return False
        with self._lock:
            if self._active.get(command_id) != active:
                return self._retained.get(command_id) == active
            self._active.pop(command_id)
            self._retained[command_id] = active
            return True

    def retire(self, command_id: str) -> None:
        with self._lock:
            self._active.pop(command_id, None)
            self._retained.pop(command_id, None)
            self._pending.pop(command_id, None)

    def retire_intent(self, intent_id: str) -> tuple[str, ...]:
        retired: list[str] = []
        with self._lock:
            for commands in (self._active, self._retained, self._pending):
                for command_id, active in tuple(commands.items()):
                    if active.command.intent_id == intent_id:
                        commands.pop(command_id)
                        retired.append(command_id)
        return tuple(retired)

    def retire_epoch(self, drone_id: int, connection_epoch: int) -> None:
        with self._lock:
            self._retire_drone(drone_id, connection_epoch)

    def retire_other_epochs(self, drone_id: int, connection_epoch: int) -> None:
        with self._lock:
            for commands in (self._active, self._retained, self._pending):
                for command_id, active in tuple(commands.items()):
                    if (
                        active.command.drone_id == drone_id
                        and active.command.connection_epoch != connection_epoch
                    ):
                        commands.pop(command_id)

    def _retire_drone(self, drone_id: int, connection_epoch: int) -> None:
        for commands in (self._active, self._retained, self._pending):
            for command_id, active in tuple(commands.items()):
                if (
                    active.command.drone_id == drone_id
                    and active.command.connection_epoch == connection_epoch
                ):
                    commands.pop(command_id)

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
        for profile in self.wire_configs.values():
            if (
                artifact.map_pin.version != profile.map_version
                or artifact.map_pin.content_sha256 != profile.map_sha256
                or artifact.geometry_pin.content_sha256 != profile.geometry_sha256
            ):
                raise ValueError("navigation wire artifact pins do not match the runtime")

    def _validate_profile(self, frame, profile: NavigationWireConfig) -> None:
        pins = frame.control_pins
        if (
            pins is None
            or tuple(sorted(pins.source_ids)) != profile.control_source_ids
            or frame.camera_calibration_sha256 != profile.camera_calibration_sha256
            or frame.body_extrinsics_sha256 != profile.body_extrinsics_sha256
            or frame.world_transform_sha256 != profile.world_transform_sha256
        ):
            raise ValueError("navigation wire provenance does not match retained pose pins")
        config = self.runtime.config
        if (
            config.speed_m_s > profile.max_speed_mm_s / 1_000
            or profile.max_position_uncertainty_mm / 1_000 > config.motion.pose_uncertainty_m
            or profile.max_cross_track_mm / 1_000 > config.motion.tracking_allowance_m
            or profile.arrival_horizontal_tolerance_mm / 1_000 > config.position_tolerance_m
            or profile.arrival_vertical_tolerance_mm / 1_000 > config.position_tolerance_m
            or profile.pose_freshness_ms > config.position_max_age_ms
            or profile.tracking_timeout_ms > config.segment_timeout_ms
            or profile.max_authorization_lifetime_ms
            > profile.tracking_timeout_ms + profile.arrival_hold_timeout_ms
            or profile.max_clock_error_ms != pins.clock_mapping.max_error_ms
            or (config.speed_m_s**2) / (2 * (profile.max_deceleration_mm_s2 / 1_000))
            > config.motion.stopping_allowance_m
        ):
            raise ValueError("navigation wire limits exceed the approved runtime envelope")

    def _profile(self, drone_id: int) -> NavigationWireConfig:
        try:
            return self.wire_configs[drone_id]
        except KeyError as error:
            raise ValueError("navigation wire has no profile for aircraft") from error

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

    def _control_pose(self, command: Command, now_ms: int, snapshot: FleetSnapshot) -> ControlPose:
        source = self.runtime.control_pose
        pose = (
            snapshot.control_poses.get(command.drone_id)
            if snapshot.control_poses is not None
            else source(command.drone_id)
            if source is not None
            else None
        )
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
        profile: NavigationWireConfig,
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
            "clock_lease_id": profile.clock_lease_id,
            **self._pins(profile),
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

    @staticmethod
    def _pins(profile: NavigationWireConfig) -> dict[str, object]:
        return {
            "navigation_config_id": profile.navigation_config_id,
            "navigation_config_sha256": profile.navigation_config_sha256,
            "map_version": profile.map_version,
            "map_sha256": profile.map_sha256,
            "geometry_sha256": profile.geometry_sha256,
            "camera_calibration_sha256": profile.camera_calibration_sha256,
            "body_extrinsics_sha256": profile.body_extrinsics_sha256,
            "world_transform_sha256": profile.world_transform_sha256,
            "control_source_ids": list(profile.control_source_ids),
        }

    @staticmethod
    def _limits(profile: NavigationWireConfig) -> dict[str, int]:
        limits = {
            "max_speed_mm_s": profile.max_speed_mm_s,
            "max_acceleration_mm_s2": profile.max_acceleration_mm_s2,
            "max_deceleration_mm_s2": profile.max_deceleration_mm_s2,
            "max_position_uncertainty_mm": profile.max_position_uncertainty_mm,
            "max_cross_track_mm": profile.max_cross_track_mm,
            "arrival_horizontal_tolerance_mm": profile.arrival_horizontal_tolerance_mm,
            "arrival_vertical_tolerance_mm": profile.arrival_vertical_tolerance_mm,
            "pose_freshness_ms": profile.pose_freshness_ms,
            "tracking_timeout_ms": profile.tracking_timeout_ms,
        }
        if profile.arrival_hold_timeout_ms:
            limits["arrival_hold_timeout_ms"] = profile.arrival_hold_timeout_ms
        return limits

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
        with self._lock:
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
