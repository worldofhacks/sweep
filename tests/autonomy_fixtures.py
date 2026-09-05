"""Deterministic, explicit safety fixtures shared by the M1.2 suite."""

from __future__ import annotations

from dataclasses import replace
from types import MappingProxyType

from adapters.dispatch import AdapterDispatcher
from adapters.sim.camera import SimCamera, SimCameraConfig
from adapters.sim.flight import SimFlightAdapter
from arbiter.safety import SafetyArbiter, SafetyConfig
from planner.controller import AutonomyController
from planner.models import (
    AircraftState,
    FleetSnapshot,
    FlightState,
    Geofence,
    MembershipState,
    Position,
)
from planner.planner import DeterministicPlanner, PlanningConfig
from relay.intent_v1 import IntentName, IntentV1, Mode

NOW_MS = 100_000


def planning_config(*, translation_frame: str = "world") -> PlanningConfig:
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
        translation_frame=translation_frame,
    )


def safety_config() -> SafetyConfig:
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


def camera_config() -> SimCameraConfig:
    return SimCameraConfig(
        panorama_width_px=4_096,
        panorama_height_px=2_048,
        photo_width_px=1_920,
        photo_height_px=1_080,
        horizontal_fov_deg=60.0,
        gimbal_pitch_min_deg=-90.0,
        gimbal_pitch_max_deg=30.0,
        storage_remaining_bytes=50_000_000,
        initial_timestamp_ms=NOW_MS,
        timestamp_step_ms=100,
    )


def make_aircraft(
    drone_id: int,
    *,
    x: float | None = None,
    flight_state: FlightState = FlightState.HOVERING,
    membership: MembershipState = MembershipState.READY,
    armed: bool = True,
    connection_epoch: int = 1,
    **changes: object,
) -> AircraftState:
    position_x = float((drone_id - 1) * 2) if x is None else x
    z = 0.0 if flight_state in {FlightState.DISARMED, FlightState.LANDED} else 1.0
    state = AircraftState(
        drone_id=drone_id,
        connection_epoch=connection_epoch,
        membership=membership,
        pose=Position(position_x, 0.0, z),
        home=Position(position_x, 0.0, 0.0),
        flight_state=flight_state,
        armed=armed,
        battery=0.9,
        link_quality=0.9,
        link_last_seen_ms=NOW_MS,
        position_quality=0.9,
        position_last_seen_ms=NOW_MS,
        control_authority=True,
        rc_safety_operator_present=True,
        physical_rc_available=True,
        storage_remaining_bytes=50_000_000,
        camera_ready=True,
        heading_deg=0.0,
    )
    return replace(state, **changes)


def make_snapshot(
    count: int = 2,
    *,
    selection: tuple[int, ...] | None = None,
    flight_state: FlightState = FlightState.HOVERING,
    roster_version: int = 7,
    armed: bool = True,
    now_ms: int = NOW_MS,
    **changes: object,
) -> FleetSnapshot:
    aircraft = {
        drone_id: make_aircraft(
            drone_id,
            flight_state=flight_state,
            armed=flight_state not in {FlightState.DISARMED, FlightState.LANDED},
        )
        for drone_id in range(1, count + 1)
    }
    snapshot = FleetSnapshot(
        roster_version=roster_version,
        aircraft=aircraft,
        selection=selection if selection is not None else tuple(range(1, count + 1)),
        armed=armed,
        estop_active=False,
        operator_present=True,
        operator_last_seen_ms=now_ms,
        now_ms=now_ms,
        formation="none",
        spacing=0.8,
    )
    return replace(snapshot, **changes)


def replace_aircraft(snapshot: FleetSnapshot, drone_id: int, **changes: object) -> FleetSnapshot:
    aircraft = dict(snapshot.aircraft)
    aircraft[drone_id] = replace(aircraft[drone_id], **changes)
    return replace(snapshot, aircraft=aircraft)


def make_intent(
    name: IntentName,
    *,
    selection: tuple[int, ...] = (1, 2),
    args: dict[str, object] | None = None,
    confirm: bool = False,
    intent_id: str | None = None,
    t: int = NOW_MS,
) -> IntentV1:
    return IntentV1(
        v=1,
        t=t,
        type="intent",
        intent_id=intent_id or f"intent-{name.value}-{t}",
        retry_of=None,
        source="console",
        session="test-session",
        name=name,
        args=MappingProxyType(args or {}),
        selection=selection,
        mode=Mode.INDOOR,
        confirm=confirm,
    )


def make_stack(
    snapshot: FleetSnapshot,
    *,
    config: PlanningConfig | None = None,
) -> tuple[
    AutonomyController,
    DeterministicPlanner,
    SafetyArbiter,
    AdapterDispatcher,
    SimFlightAdapter,
    SimCamera,
]:
    planner = DeterministicPlanner(config or planning_config())
    arbiter = SafetyArbiter(safety_config())
    flight = SimFlightAdapter.from_snapshot(snapshot)
    camera = SimCamera(
        drone_epochs={
            drone_id: aircraft.connection_epoch for drone_id, aircraft in snapshot.aircraft.items()
        },
        pose_provider=flight.camera_pose,
        config=camera_config(),
    )
    dispatcher = AdapterDispatcher(flight=flight, camera=camera, arbiter=arbiter)
    controller = AutonomyController(
        planner=planner,
        arbiter=arbiter,
        dispatcher=dispatcher,
    )
    return controller, planner, arbiter, dispatcher, flight, camera
