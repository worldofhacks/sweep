"""Deployable two-aircraft simulator composition for the M1.4 gate."""

from __future__ import annotations

import time
from collections.abc import Mapping

from fastapi import FastAPI

from adapters.dispatch import AdapterDispatcher
from adapters.protocols import WatchdogConfig
from adapters.sim.camera import SimCamera, SimCameraConfig
from adapters.sim.flight import SimFlightAdapter
from arbiter.safety import SafetyArbiter, SafetyConfig
from planner.controller import AutonomyController
from planner.models import (
    AircraftState,
    FleetSnapshot,
    FlightState,
    Geofence,
    LossBehavior,
    MembershipState,
    Position,
    RelayAircraftSafetyEnrichment,
    RelaySnapshotEnrichment,
)
from planner.planner import DeterministicPlanner, PlanningConfig
from planner.relay_bridge import AutonomyRelayBridge
from relay.app import create_app
from relay.session import Clock, EventIdFactory, RelaySession
from relay.settings import RelaySettings


class SimBridgeFactory:
    def __init__(
        self,
        *,
        initial_snapshot: FleetSnapshot,
        planning: PlanningConfig,
        safety: SafetyConfig,
        camera: SimCameraConfig,
        watchdog: WatchdogConfig,
    ) -> None:
        self.initial_snapshot = initial_snapshot
        self.planning = planning
        self.safety = safety
        self.camera = camera
        self.watchdog = watchdog
        self.bridges: dict[str, AutonomyRelayBridge] = {}
        self.flights: dict[str, SimFlightAdapter] = {}

    def __call__(self, session: RelaySession) -> AutonomyRelayBridge:
        flight = SimFlightAdapter.from_snapshot(self.initial_snapshot)
        camera = SimCamera(
            drone_epochs={
                drone_id: aircraft.connection_epoch
                for drone_id, aircraft in self.initial_snapshot.aircraft.items()
            },
            pose_provider=flight.camera_pose,
            config=self.camera,
        )
        arbiter = SafetyArbiter(self.safety)
        controller = AutonomyController(
            planner=DeterministicPlanner(self.planning),
            arbiter=arbiter,
            dispatcher=AdapterDispatcher(flight=flight, camera=camera, arbiter=arbiter),
        )

        def enrichment(state: Mapping[str, object]) -> RelaySnapshotEnrichment:
            now = int(state["t"])
            return RelaySnapshotEnrichment(
                operator_present=True,
                operator_last_seen_ms=now,
                aircraft={
                    drone_id: RelayAircraftSafetyEnrichment(
                        drone_id=drone_id,
                        armed=aircraft.armed,
                        physical_rc_available=True,
                        storage_remaining_bytes=self.camera.storage_remaining_bytes,
                        camera_ready=True,
                        active_task_id=None,
                        position_loss_since_ms=None,
                        last_known_pose=aircraft.pose,
                        last_known_home=aircraft.home,
                        last_known_flight_state=aircraft.flight_state.value,
                        last_known_battery=aircraft.battery,
                        last_known_link_quality=aircraft.link_quality,
                        last_known_position_quality=aircraft.position_quality,
                        last_link_seen_ms=now,
                        last_position_seen_ms=now,
                    )
                    for drone_id, aircraft in flight.aircraft.items()
                },
            )

        bridge = AutonomyRelayBridge(
            session=session,
            controller=controller,
            enrichment=enrichment,
            watchdog_config=self.watchdog,
        )
        self.bridges[session.session_id] = bridge
        self.flights[session.session_id] = flight
        return bridge


def create_m14_sim_app(
    settings: RelaySettings | None = None,
    *,
    clock: Clock | None = None,
    event_ids: EventIdFactory | None = None,
    initial_snapshot: FleetSnapshot | None = None,
) -> FastAPI:
    now = (clock or _epoch_ms)()
    factory = SimBridgeFactory(
        initial_snapshot=initial_snapshot or _initial_snapshot(now),
        planning=_planning_config(),
        safety=_safety_config(),
        camera=_camera_config(now),
        watchdog=WatchdogConfig(
            hold_after_ms=2_000,
            failsafe_after_ms=10_000,
            loss_behavior=LossBehavior.FAILSAFE,
        ),
    )
    application = create_app(
        settings,
        clock=clock,
        event_ids=event_ids,
        intent_sink_factory=factory,
    )
    application.state.sim_bridge_factory = factory
    return application


def _initial_snapshot(now_ms: int) -> FleetSnapshot:
    aircraft = {
        drone_id: AircraftState(
            drone_id=drone_id,
            connection_epoch=1,
            membership=MembershipState.READY,
            pose=Position(float((drone_id - 1) * 2), 0.0, 0.0),
            home=Position(float((drone_id - 1) * 2), 0.0, 0.0),
            flight_state=FlightState.DISARMED,
            armed=False,
            battery=0.9,
            link_quality=0.9,
            link_last_seen_ms=now_ms,
            position_quality=0.9,
            position_last_seen_ms=now_ms,
            control_authority=True,
            rc_safety_operator_present=True,
            physical_rc_available=True,
            storage_remaining_bytes=50_000_000,
            camera_ready=True,
        )
        for drone_id in (1, 2)
    }
    return FleetSnapshot(
        roster_version=0,
        aircraft=aircraft,
        selection=(),
        armed=False,
        estop_active=False,
        operator_present=True,
        operator_last_seen_ms=now_ms,
        now_ms=now_ms,
    )


def _planning_config() -> PlanningConfig:
    return PlanningConfig(
        takeoff_altitude_m=1.0,
        translation_step_m=0.5,
        flight_speed_m_s=0.5,
        capture_yaw_speed_deg_s=30.0,
        capture_yaw_tolerance_deg=1.0,
        capture_pose_tolerance_m=0.1,
        capture_min_overlap_deg=10.0,
        capture_gimbal_pitch_deg=0.0,
        reconstruct_headings_deg=tuple(float(value) for value in range(0, 360, 45)),
    )


def _safety_config() -> SafetyConfig:
    return SafetyConfig(
        geofence=Geofence(-10.0, 10.0, -10.0, 10.0, 0.0, 5.0),
        ceiling_m=4.0,
        min_spacing_m=0.8,
        battery_reserve_fraction=0.2,
        battery_critical_fraction=0.1,
        battery_cost_per_m=0.01,
        min_link_quality=0.4,
        max_link_age_ms=1_000,
        min_position_quality=0.5,
        max_position_age_ms=1_000,
        operator_timeout_ms=10_000,
        max_future_clock_skew_ms=1_000,
        min_capture_storage_bytes=1_000_000,
        max_capture_pose_drift_m=0.2,
        max_capture_gimbal_error_deg=1.0,
        positioning_loss_hold_ms=3_000,
        motion_conflict_window_ms=500,
    )


def _camera_config(now_ms: int) -> SimCameraConfig:
    return SimCameraConfig(
        panorama_width_px=4_096,
        panorama_height_px=2_048,
        photo_width_px=1_920,
        photo_height_px=1_080,
        horizontal_fov_deg=60.0,
        gimbal_pitch_min_deg=-90.0,
        gimbal_pitch_max_deg=30.0,
        storage_remaining_bytes=50_000_000,
        initial_timestamp_ms=now_ms,
        timestamp_step_ms=100,
    )


def _epoch_ms() -> int:
    return time.time_ns() // 1_000_000
