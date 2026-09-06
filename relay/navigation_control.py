"""Signed, closed-loop authorization for mapped phone navigation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from math import ceil, dist

from planner.models import Command, CommandOperation, FleetSnapshot, Plan
from planner.navigation import Pose
from planner.navigation_runtime import NavigationRuntime
from relay.auth import sign_event
from relay.control_config import ControlRuntimeConfig
from relay.control_localization import ControlLocalizationPins

_P95_3D = 2.796


@dataclass(frozen=True, slots=True)
class NavigationControlConfig:
    navigation: NavigationRuntime
    localization: ControlRuntimeConfig
    configuration_id: str
    node_keys: Mapping[int, bytes]

    def __post_init__(self) -> None:
        if not self.configuration_id or not set(self.localization.pins).issubset(self.node_keys):
            raise ValueError("navigation control requires a configuration identity and node keys")


@dataclass(slots=True)
class _ActiveSegment:
    route_id: str
    command_id: str
    drone_id: int
    connection_epoch: int
    start: Pose
    target: Pose
    expires_at_ms: int
    sequence: int
    loss_started_ms: int | None = None


class NavigationControl:
    """Own route authorizations and emit only evidence-backed navigation poses."""

    def __init__(self, config: NavigationControlConfig) -> None:
        self.config = config
        self._active: dict[int, _ActiveSegment] = {}
        self._sequence = 0

    def authorize(
        self, plan: Plan, command: Command, snapshot: FleetSnapshot, session: str
    ) -> dict[str, object]:
        if (
            command.operation is not CommandOperation.GOTO
            or command.parameters.get("navigation_route_id") != plan.intent_id
        ):
            raise ValueError("mapped goto is missing its route identity")
        segment = self._segment(plan, command)
        aircraft = snapshot.aircraft.get(command.drone_id)
        pin = self.config.localization.pins.get(command.drone_id)
        if aircraft is None or pin is None or aircraft.connection_epoch != command.connection_epoch:
            raise ValueError("mapped goto aircraft is no longer current")
        if not self._valid_provenance(aircraft.control_provenance, pin):
            raise ValueError("mapped goto requires accepted localization provenance")
        now = snapshot.now_ms
        expires = now + self.config.navigation.config.segment_timeout_ms
        self._sequence += 1
        active = _ActiveSegment(
            plan.intent_id,
            command.command_id,
            command.drone_id,
            command.connection_epoch,
            segment.start,
            segment.end,
            expires,
            self._sequence,
        )
        self._active[command.drone_id] = active
        motion = self.config.navigation.config.motion
        max_uncertainty_mm = ceil(
            _P95_3D * self.config.localization.max_position_uncertainty_m * 1000
        )
        tube_mm = ceil(
            (motion.aircraft_radius_m + motion.tracking_allowance_m + motion.pose_uncertainty_m)
            * 1000
        )
        unsigned = {
            "v": 1,
            "type": "navigation_route_authorization",
            "t": now,
            "expires_at_ms": expires,
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
            "start_x_mm": round(segment.start.x_m * 1000),
            "start_y_mm": round(segment.start.y_m * 1000),
            "start_z_mm": round(segment.start.z_m * 1000),
            "target_x_mm": round(segment.end.x_m * 1000),
            "target_y_mm": round(segment.end.y_m * 1000),
            "target_z_mm": round(segment.end.z_m * 1000),
            "max_speed_mm_s": round(self.config.navigation.config.speed_m_s * 1000),
            "horizontal_tolerance_mm": round(
                self.config.navigation.config.position_tolerance_m * 1000
            ),
            "vertical_tolerance_mm": round(
                self.config.navigation.config.position_tolerance_m * 1000
            ),
            "max_position_uncertainty_mm": max_uncertainty_mm,
            "tube_radius_mm": tube_mm,
            "flight_approved": True,
        }
        return {
            **unsigned,
            "signature": sign_event(unsigned, self.config.node_keys[command.drone_id]),
        }

    def pose(
        self, snapshot: FleetSnapshot, session: str, *, drone_ids: frozenset[int] | None = None
    ) -> list[dict[str, object]]:
        packets = []
        for drone_id, active in tuple(self._active.items()):
            if drone_ids is not None and drone_id not in drone_ids:
                continue
            aircraft = snapshot.aircraft.get(drone_id)
            pin = self.config.localization.pins[drone_id]
            ready = (
                aircraft is not None
                and aircraft.connection_epoch == active.connection_epoch
                and snapshot.now_ms <= active.expires_at_ms
                and self._valid_provenance(aircraft.control_provenance, pin)
                and aircraft.position_quality > 0
            )
            radius_mm: int | None = None
            if ready:
                assert aircraft is not None and aircraft.control_provenance is not None
                uncertainty = aircraft.control_provenance.position_uncertainty_m
                if uncertainty is None:
                    ready = False
                else:
                    radius_mm = ceil(_P95_3D * uncertainty * 1000)
                    motion = self.config.navigation.config.motion
                    tube = (
                        motion.aircraft_radius_m
                        + motion.tracking_allowance_m
                        + motion.pose_uncertainty_m
                    )
                    ready = (
                        radius_mm
                        <= ceil(
                            _P95_3D * self.config.localization.max_position_uncertainty_m * 1000
                        )
                        and _distance_to_segment(aircraft.pose, active.start, active.target)
                        + radius_mm / 1000
                        <= tube
                    )
            if ready:
                assert aircraft is not None and radius_mm is not None
                status, values = (
                    "ready",
                    (
                        aircraft.control_provenance.evaluated_at_relay_ms,
                        aircraft.position_last_seen_ms,
                        round(aircraft.pose.x * 1000),
                        round(aircraft.pose.y * 1000),
                        round(aircraft.pose.z * 1000),
                        radius_mm,
                    ),
                )
                active.loss_started_ms = None
            else:
                active.loss_started_ms = (
                    snapshot.now_ms if active.loss_started_ms is None else active.loss_started_ms
                )
                status = (
                    "land"
                    if snapshot.now_ms - active.loss_started_ms
                    >= self.config.localization.land_after_fix_age_ms
                    else "hold"
                )
                values = (None, None, None, None, None, None)
            self._sequence += 1
            pose_time, fix_time, x, y, z, uncertainty = values
            unsigned = {
                "v": 1,
                "type": "navigation_pose",
                "t": snapshot.now_ms,
                "event_id": f"navigation-pose-{drone_id}-{self._sequence}",
                "session": session,
                "drone_id": drone_id,
                "connection_epoch": active.connection_epoch,
                "command_id": active.command_id,
                "route_id": active.route_id,
                "seq": self._sequence,
                "navigation_config_id": self.config.configuration_id,
                "map_id": pin.map_id,
                "geometry_id": pin.geometry_id,
                "camera_calibration_id": pin.camera_calibration_id,
                "body_extrinsics_id": pin.body_extrinsics_id,
                "pose_time_ms": pose_time,
                "fix_time_ms": fix_time,
                "x_mm": x,
                "y_mm": y,
                "z_mm": z,
                "position_uncertainty_mm": uncertainty,
                "status": status,
                "flight_approved": True,
            }
            packets.append(
                {**unsigned, "signature": sign_event(unsigned, self.config.node_keys[drone_id])}
            )
        return packets

    def invalidate(self, route_id: str) -> None:
        self._active = {
            key: value for key, value in self._active.items() if value.route_id != route_id
        }

    def _segment(self, plan: Plan, command: Command):
        if plan.navigation is None:
            raise ValueError("mapped goto has no frozen navigation execution")
        for route in plan.navigation.route.routes:
            if route.drone.drone_id != command.drone_id:
                continue
            gotos = [
                item
                for item in plan.commands
                if item.drone_id == command.drone_id and item.operation is CommandOperation.GOTO
            ]
            index = gotos.index(command)
            return route.swept_segments[index]
        raise ValueError("mapped goto is outside its frozen route")

    @staticmethod
    def _valid_provenance(provenance: object, pin: ControlLocalizationPins) -> bool:
        return provenance is not None and all(
            getattr(provenance, field, None) == getattr(pin, field)
            for field in (
                "map_id",
                "geometry_id",
                "camera_calibration_id",
                "body_extrinsics_id",
                "capture_clock_id",
                "relay_clock_id",
                "source_ids",
            )
        )


def _distance_to_segment(point: object, start: Pose, end: Pose) -> float:
    px, py, pz = point.x, point.y, point.z
    dx, dy, dz = end.x_m - start.x_m, end.y_m - start.y_m, end.z_m - start.z_m
    length = dx * dx + dy * dy + dz * dz
    if length == 0:
        return dist((px, py, pz), start.xyz)
    ratio = max(
        0.0,
        min(1.0, ((px - start.x_m) * dx + (py - start.y_m) * dy + (pz - start.z_m) * dz) / length),
    )
    return dist(
        (px, py, pz), (start.x_m + ratio * dx, start.y_m + ratio * dy, start.z_m + ratio * dz)
    )
