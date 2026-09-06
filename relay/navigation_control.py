"""Explicit host approval for flight navigation derived from diagnostic poses."""

from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import dataclass, replace
from math import ceil
from typing import TYPE_CHECKING

from planner.control_provenance import ControlProvenance
from planner.models import Command, CommandOperation, FleetSnapshot, Plan, Position
from planner.navigation import Pose
from planner.navigation_runtime import NavigationRuntime
from relay.auth import sign_event
from relay.control_config import ControlRuntimeConfig
from relay.control_localization import ControlLocalizationPins, ControlPose

if TYPE_CHECKING:
    from relay.session import RelaySession

_P95_TO_SIGMA = 2.7954834829151074


@dataclass(frozen=True, slots=True)
class NavigationControlConfig:
    navigation: NavigationRuntime
    localization: ControlRuntimeConfig
    configuration_id: str
    node_keys: Mapping[int, bytes]

    def __post_init__(self) -> None:
        if not self.configuration_id or not set(self.localization.pins).issubset(self.node_keys):
            raise ValueError("navigation control requires a configuration identity and node keys")


@dataclass(frozen=True, slots=True)
class _ActiveRoute:
    route_id: str
    command_id: str
    drone_id: int
    connection_epoch: int
    start: Pose
    target: Pose
    expires_at_ms: int
    sequence: int


class NavigationControl:
    """Host-owned approval boundary between diagnostic localization and navigation."""

    def __init__(self, config: NavigationControlConfig) -> None:
        self.config = config
        self._active: dict[int, _ActiveRoute] = {}
        self._sequence = 0
        self._lock = threading.Lock()

    def approved_snapshot(self, snapshot: FleetSnapshot, session: RelaySession) -> FleetSnapshot:
        aircraft = dict(snapshot.aircraft)
        for drone_id, current in snapshot.aircraft.items():
            pose = session.control_pose(drone_id)
            pin = self.config.localization.pins.get(drone_id)
            if pose is None or pin is None or not self._usable_pose(pose, pin, snapshot.now_ms):
                continue
            if current.connection_epoch != pose.connection_epoch:
                continue
            aircraft[drone_id] = replace(
                current,
                pose=Position(pose.x_mm / 1_000, pose.y_mm / 1_000, pose.z_mm / 1_000),
                position_quality=1.0,
                position_last_seen_ms=pose.fix_time_ms,
                control_provenance=ControlProvenance(
                    map_id=pin.map_id,
                    geometry_id=pin.geometry_id,
                    camera_calibration_id=pin.camera_calibration_id,
                    body_extrinsics_id=pin.body_extrinsics_id,
                    capture_clock_id=pin.clock_mapping.capture_clock_id,
                    relay_clock_id=pin.clock_mapping.relay_clock_id,
                    source_ids=pin.source_ids,
                    capture_time_s=None,
                    conversion_error_ms=pin.clock_mapping.max_error_ms,
                    reason="host_approved_diagnostic_pose",
                    evaluated_at_relay_ms=pose.pose_time_ms,
                    position_uncertainty_m=pose.position_uncertainty_mm / 1_000 / _P95_TO_SIGMA,
                ),
            )
        return replace(snapshot, aircraft=aircraft)

    def authorize(
        self, plan: Plan, command: Command, snapshot: FleetSnapshot, session: str
    ) -> dict[str, object]:
        if command.operation is not CommandOperation.GOTO:
            raise ValueError("only mapped goto commands may be authorized")
        segment = self._segment(plan, command)
        aircraft = snapshot.aircraft.get(command.drone_id)
        pin = self.config.localization.pins.get(command.drone_id)
        if aircraft is None or pin is None or aircraft.connection_epoch != command.connection_epoch:
            raise ValueError("mapped goto aircraft is no longer current")
        if not self._matches_provenance(aircraft.control_provenance, pin):
            raise ValueError("mapped goto requires host-approved localization")
        with self._lock:
            self._sequence += 1
            active = _ActiveRoute(
                route_id=plan.intent_id,
                command_id=command.command_id,
                drone_id=command.drone_id,
                connection_epoch=command.connection_epoch,
                start=segment.start,
                target=segment.end,
                expires_at_ms=snapshot.now_ms + self.config.navigation.config.segment_timeout_ms,
                sequence=self._sequence,
            )
            self._active[command.drone_id] = active
        motion = self.config.navigation.config.motion
        unsigned = {
            "v": 1,
            "type": "navigation_route_authorization",
            "t": snapshot.now_ms,
            "expires_at_ms": active.expires_at_ms,
            "event_id": f"navigation-route-{command.drone_id}-{active.sequence}",
            "session": session,
            "drone_id": command.drone_id,
            "connection_epoch": command.connection_epoch,
            "command_id": command.command_id,
            "route_id": plan.intent_id,
            "seq": active.sequence,
            "navigation_config_id": self.config.configuration_id,
            "map_id": pin.map_id,
            "geometry_id": pin.geometry_id,
            "camera_calibration_id": pin.camera_calibration_id,
            "body_extrinsics_id": pin.body_extrinsics_id,
            "start_x_mm": round(segment.start.x_m * 1_000),
            "start_y_mm": round(segment.start.y_m * 1_000),
            "start_z_mm": round(segment.start.z_m * 1_000),
            "target_x_mm": round(segment.end.x_m * 1_000),
            "target_y_mm": round(segment.end.y_m * 1_000),
            "target_z_mm": round(segment.end.z_m * 1_000),
            "max_speed_mm_s": round(self.config.navigation.config.speed_m_s * 1_000),
            "horizontal_tolerance_mm": round(
                self.config.navigation.config.position_tolerance_m * 1_000
            ),
            "vertical_tolerance_mm": round(
                self.config.navigation.config.position_tolerance_m * 1_000
            ),
            "max_position_uncertainty_mm": ceil(
                min(
                    self.config.localization.max_position_uncertainty_p95_m,
                    motion.pose_uncertainty_m,
                )
                * 1_000
            ),
            "tube_radius_mm": ceil(
                (motion.tracking_allowance_m + motion.pose_uncertainty_m) * 1_000
            ),
            "flight_approved": True,
        }
        return {
            **unsigned,
            "signature": sign_event(unsigned, self.config.node_keys[command.drone_id]),
        }

    def initial_pose(self, drone_id: int, session: RelaySession, now_ms: int) -> dict[str, object]:
        pose = session.control_pose(drone_id)
        with self._lock:
            active = self._active.get(drone_id)
            if active is None:
                raise ValueError("navigation route is not active")
            pin = self.config.localization.pins[drone_id]
            if pose is None or pose.connection_epoch != active.connection_epoch:
                raise ValueError("navigation pose is unavailable")
            if now_ms > active.expires_at_ms:
                raise ValueError("navigation route has expired")
            if not self._usable_pose(pose, pin, now_ms):
                raise ValueError("navigation pose is not ready")
            return self._pose_packet(active, pin, session.session_id, now_ms, pose)

    def periodic_poses(self, session: RelaySession, now_ms: int) -> list[dict[str, object]]:
        with self._lock:
            active_routes = tuple(self._active.items())
        observations = [
            (drone_id, active, session.control_pose(drone_id))
            for drone_id, active in active_routes
        ]
        registry = getattr(session, "registry", None)
        with self._lock:
            packets = []
            for drone_id, active, pose in observations:
                if self._active.get(drone_id) != active:
                    continue
                active_identity = (
                    None
                    if registry is None
                    else registry.active_connection_identity(drone_id)
                )
                if registry is not None and (
                    active_identity is None or active_identity[0] != active.connection_epoch
                ):
                    del self._active[drone_id]
                    continue
                pin = self.config.localization.pins[drone_id]
                if (
                    pose is None
                    or pose.connection_epoch != active.connection_epoch
                    or now_ms > active.expires_at_ms
                    or not self._usable_pose(pose, pin, now_ms)
                ):
                    packets.append(self._pose_packet(active, pin, session.session_id, now_ms, None))
                    del self._active[drone_id]
                else:
                    packets.append(self._pose_packet(active, pin, session.session_id, now_ms, pose))
            return packets

    def _pose_packet(
        self,
        active: _ActiveRoute,
        pin: ControlLocalizationPins,
        session_id: str,
        now_ms: int,
        pose: ControlPose | None,
    ) -> dict[str, object]:
        self._sequence += 1
        sequence = self._sequence
        unsigned = {
            "v": 1,
            "type": "navigation_pose",
            "t": now_ms,
            "event_id": f"navigation-pose-{active.drone_id}-{sequence}",
            "session": session_id,
            "drone_id": active.drone_id,
            "connection_epoch": active.connection_epoch,
            "command_id": active.command_id,
            "route_id": active.route_id,
            "seq": sequence,
            "navigation_config_id": self.config.configuration_id,
            "map_id": pin.map_id,
            "geometry_id": pin.geometry_id,
            "camera_calibration_id": pin.camera_calibration_id,
            "body_extrinsics_id": pin.body_extrinsics_id,
            "pose_time_ms": None if pose is None else pose.pose_time_ms,
            "fix_time_ms": None if pose is None else pose.fix_time_ms,
            "x_mm": None if pose is None else pose.x_mm,
            "y_mm": None if pose is None else pose.y_mm,
            "z_mm": None if pose is None else pose.z_mm,
            "position_uncertainty_mm": None if pose is None else pose.position_uncertainty_mm,
            "status": "hold" if pose is None else "ready",
            "flight_approved": True,
        }
        return {
            **unsigned,
            "signature": sign_event(unsigned, self.config.node_keys[active.drone_id]),
        }

    def invalidate(self, route_id: str) -> None:
        with self._lock:
            self._active = {
                drone_id: route
                for drone_id, route in self._active.items()
                if route.route_id != route_id
            }

    def _segment(self, plan: Plan, command: Command):
        if plan.navigation is None:
            raise ValueError("mapped goto has no frozen navigation execution")
        for route in plan.navigation.route.routes:
            if route.drone.drone_id == command.drone_id:
                gotos = [
                    item
                    for item in plan.commands
                    if item.drone_id == command.drone_id and item.operation is CommandOperation.GOTO
                ]
                return route.swept_segments[gotos.index(command)]
        raise ValueError("mapped goto is outside its frozen route")

    def _usable_pose(self, pose: ControlPose, pin: ControlLocalizationPins, now_ms: int) -> bool:
        return (
            pose.status == "ready"
            and pose.flight_approved is False
            and pose.map_id == pin.map_id
            and pose.geometry_id == pin.geometry_id
            and pose.camera_calibration_id == pin.camera_calibration_id
            and pose.body_extrinsics_id == pin.body_extrinsics_id
            and now_ms >= pose.pose_time_ms >= pose.fix_time_ms
            and now_ms - pose.fix_time_ms
            <= min(
                self.config.localization.max_fix_age_ms,
                self.config.navigation.config.position_max_age_ms,
            )
            and pose.position_uncertainty_mm / 1_000
            <= min(
                self.config.localization.max_position_uncertainty_p95_m,
                self.config.navigation.config.motion.pose_uncertainty_m,
            )
        )

    @staticmethod
    def _matches_provenance(provenance: object, pin: ControlLocalizationPins) -> bool:
        return provenance is not None and all(
            getattr(provenance, name, None) == value
            for name, value in (
                ("map_id", pin.map_id),
                ("geometry_id", pin.geometry_id),
                ("camera_calibration_id", pin.camera_calibration_id),
                ("body_extrinsics_id", pin.body_extrinsics_id),
                ("capture_clock_id", pin.clock_mapping.capture_clock_id),
                ("relay_clock_id", pin.clock_mapping.relay_clock_id),
                ("source_ids", pin.source_ids),
            )
        )
