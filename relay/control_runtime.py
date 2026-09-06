"""Project retained localization frames into signed phone control-pose packets."""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from math import ceil

from planner.models import FleetSnapshot
from relay.auth import sign_event
from relay.control_config import ControlRuntimeConfig as ControlRuntimeConfig
from relay.control_frames import ControlLocalizationFrame
from relay.control_localization import ControlLocalizationPins, IngestResult

_GAUSSIAN_95_RADIUS_3D = 2.796


@dataclass(frozen=True, slots=True)
class _RetainedPose:
    x_mm: int
    y_mm: int
    z_mm: int
    uncertainty_mm: int
    pose_time_ms: int
    fix_time_ms: int


class ControlRuntime:
    def __init__(self, config: ControlRuntimeConfig, *, node_keys: Mapping[int, bytes]) -> None:
        if not set(config.pins).issubset(node_keys):
            raise ValueError("every controlled aircraft requires its node signing key")
        self.config = config
        self.node_keys = dict(node_keys)
        self.store = config.create_store()
        self._seen: dict[int, deque[str]] = {
            drone_id: deque(maxlen=256) for drone_id in config.pins
        }
        self._sequence = 0
        self._loss_started: dict[int, int] = {}
        self._retained: dict[int, _RetainedPose] = {}

    def ingest(
        self,
        frame: ControlLocalizationFrame,
        authenticated_drone_id: int,
        authenticated_connection_epoch: int,
        now_ms: int,
    ) -> IngestResult:
        seen = self._seen.get(authenticated_drone_id)
        if seen is None or frame.event_id in seen:
            return IngestResult(False, "duplicate_event")
        result = self.store.ingest(
            frame.to_event(), authenticated_drone_id, authenticated_connection_epoch, now_ms
        )
        if result.accepted:
            seen.append(frame.event_id)
        return result

    def apply(self, snapshot: FleetSnapshot) -> FleetSnapshot:
        return self.store.apply(snapshot)

    def control_pose(
        self, drone_id: int, snapshot: FleetSnapshot, session: str, now_ms: int
    ) -> dict[str, object] | None:
        pin = self.config.pins[drone_id]
        aircraft = snapshot.aircraft.get(drone_id)
        if aircraft is None or aircraft.connection_epoch != pin.connection_epoch:
            return self._loss_packet(drone_id, pin, session, now_ms)
        provenance = aircraft.control_provenance
        valid = provenance is not None and self._matches_pins(provenance, pin)
        fix_time = aircraft.position_last_seen_ms if valid else None
        pose_time = provenance.evaluated_at_relay_ms if valid else None
        if (
            not valid
            or fix_time is None
            or pose_time is None
            or aircraft.position_quality <= 0
            or now_ms - fix_time > self.config.max_fix_age_ms
            or now_ms < pose_time
            or pose_time < fix_time
        ):
            return self._loss_packet(drone_id, pin, session, now_ms)
        uncertainty_m = provenance.position_uncertainty_m
        if uncertainty_m is None:
            return self._loss_packet(drone_id, pin, session, now_ms)
        uncertainty_mm = ceil(1000 * uncertainty_m * _GAUSSIAN_95_RADIUS_3D)
        if uncertainty_mm / 1000 > self.config.max_position_uncertainty_m:
            return self._loss_packet(drone_id, pin, session, now_ms)
        retained = self._retained.get(drone_id)
        if retained is not None and pose_time <= retained.pose_time_ms:
            return None
        retained = _RetainedPose(
            round(aircraft.pose.x * 1000),
            round(aircraft.pose.y * 1000),
            round(aircraft.pose.z * 1000),
            uncertainty_mm,
            pose_time,
            fix_time,
        )
        self._retained[drone_id] = retained
        self._loss_started.pop(drone_id, None)
        return self._packet(drone_id, pin, session, now_ms, "ready", retained)

    def _loss_packet(
        self, drone_id: int, pin: ControlLocalizationPins, session: str, now_ms: int
    ) -> dict[str, object] | None:
        retained = self._retained.get(drone_id)
        if retained is None or now_ms < retained.pose_time_ms:
            return None
        started = self._loss_started.setdefault(drone_id, now_ms)
        status = "land" if now_ms - started >= self.config.land_after_fix_age_ms else "hold"
        return self._packet(drone_id, pin, session, now_ms, status, retained)

    def _packet(
        self,
        drone_id: int,
        pin: ControlLocalizationPins,
        session: str,
        now_ms: int,
        status: str,
        retained: _RetainedPose,
    ) -> dict[str, object]:
        self._sequence += 1
        unsigned = {
            "v": 1,
            "type": "control_pose",
            "t": now_ms,
            "event_id": f"control-pose-{drone_id}-{self._sequence}",
            "session": session,
            "drone_id": drone_id,
            "connection_epoch": pin.connection_epoch,
            "map_id": pin.map_id,
            "geometry_id": pin.geometry_id,
            "camera_calibration_id": pin.camera_calibration_id,
            "body_extrinsics_id": pin.body_extrinsics_id,
            "pose_time_ms": retained.pose_time_ms,
            "fix_time_ms": retained.fix_time_ms,
            "position_frame": "map_enu",
            "x_mm": retained.x_mm,
            "y_mm": retained.y_mm,
            "z_mm": retained.z_mm,
            "position_uncertainty_mm": retained.uncertainty_mm,
            "status": status,
            "flight_approved": False,
        }
        return {**unsigned, "signature": sign_event(unsigned, self.node_keys[drone_id])}

    @staticmethod
    def _matches_pins(provenance: object, pin: ControlLocalizationPins) -> bool:
        return all(
            getattr(provenance, field) == getattr(pin, field)
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
