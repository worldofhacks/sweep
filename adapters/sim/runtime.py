"""Deployable two-aircraft simulator composition for the M1.4 gate."""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass
from threading import Event, Lock, Thread

from fastapi import FastAPI

from adapters.dispatch import AdapterDispatcher
from adapters.protocols import NodeSafetyAction, NodeWatchdogState, WatchdogConfig
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
from relay.auth import Principal, sign_event
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
        adapter_keys: Mapping[int, bytes],
        auto_start_nodes: bool,
    ) -> None:
        self.initial_snapshot = initial_snapshot
        self.planning = planning
        self.safety = safety
        self.camera = camera
        self.watchdog = watchdog
        self.adapter_keys = adapter_keys
        self.auto_start_nodes = auto_start_nodes
        self.bridges: dict[str, AutonomyRelayBridge] = {}
        self.flights: dict[str, SimFlightAdapter] = {}
        self.nodes: dict[str, _SimNodeIngress] = {}

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

        def synchronize_connection_epoch(drone_id: int, connection_epoch: int) -> None:
            flight.update_connection_epoch(drone_id, connection_epoch)
            camera.update_connection_epoch(drone_id, connection_epoch)

        nodes = _SimNodeIngress(
            session=session,
            flight=flight,
            adapter_keys=self.adapter_keys,
            watchdog_config=self.watchdog,
        )
        bridge = AutonomyRelayBridge(
            session=session,
            controller=controller,
            enrichment=enrichment,
            watchdog_config=self.watchdog,
            node_activity=nodes.watchdog_activity,
            node_safety_events=nodes.drain_safety_actions,
            synchronize_connection_epoch=synchronize_connection_epoch,
            ingress=nodes.periodic_events if self.auto_start_nodes else None,
            post_execution_ingress=nodes.periodic_events if self.auto_start_nodes else None,
        )
        nodes.bridge = bridge
        nodes.start_watchdog()
        self.bridges[session.session_id] = bridge
        self.flights[session.session_id] = flight
        self.nodes[session.session_id] = nodes
        if self.auto_start_nodes:
            nodes.start()
        return bridge

    def close(self) -> None:
        for node in self.nodes.values():
            node.close()

    def silence_node(self, session_id: str, drone_id: int) -> None:
        self.nodes[session_id].silence(drone_id)

    def disconnect_node(self, session_id: str, drone_id: int) -> list[dict[str, object]]:
        return self.nodes[session_id].disconnect(drone_id)

    def rejoin_node(self, session_id: str, drone_id: int) -> list[dict[str, object]]:
        return self.nodes[session_id].rejoin(drone_id)


class _SimNodeIngress:
    def __init__(
        self,
        *,
        session: RelaySession,
        flight: SimFlightAdapter,
        adapter_keys: Mapping[int, bytes],
        watchdog_config: WatchdogConfig,
    ) -> None:
        self.session = session
        self.flight = flight
        self.adapter_keys = adapter_keys
        self.watchdog_config = watchdog_config
        self.bridge: AutonomyRelayBridge | None = None
        self._active: set[int] = set()
        self._silent: set[int] = set()
        self._sequence = 0
        self._watchdogs: dict[int, _LocalWatchdog] = {}
        self._safety_actions: list[NodeSafetyAction] = []
        self._watchdog_lock = Lock()
        self._watchdog_stop = Event()
        self._watchdog_thread: Thread | None = None

    def start_watchdog(self) -> None:
        now_ms = self.session.clock()
        with self._watchdog_lock:
            for drone_id, aircraft in self.flight.aircraft.items():
                self._watchdogs[drone_id] = _LocalWatchdog(
                    state=NodeWatchdogState(drone_id, aircraft.connection_epoch, now_ms)
                )
        self._watchdog_thread = Thread(
            target=self._watchdog_loop,
            name=f"sim-watchdog-{self.session.session_id}",
            daemon=True,
        )
        self._watchdog_thread.start()

    def close(self) -> None:
        self._watchdog_stop.set()
        if self._watchdog_thread is not None:
            self._watchdog_thread.join(timeout=1)

    def watchdog_activity(
        self, drone_id: int, connection_epoch: int, last_activity_ms: int
    ) -> None:
        with self._watchdog_lock:
            self._watchdogs[drone_id] = _LocalWatchdog(
                state=NodeWatchdogState(drone_id, connection_epoch, last_activity_ms)
            )

    def drain_safety_actions(self) -> list[NodeSafetyAction]:
        with self._watchdog_lock:
            actions = self._safety_actions
            self._safety_actions = []
        return actions

    def _watchdog_loop(self) -> None:
        while not self._watchdog_stop.wait(0.01):
            now_ms = self.session.clock()
            with self._watchdog_lock:
                for progress in tuple(self._watchdogs.values()):
                    try:
                        action = self.flight.apply_node_watchdog(
                            progress.state,
                            now_ms=now_ms,
                            config=self.watchdog_config,
                        )
                    except ValueError:
                        continue
                    if action is None or progress.action is action:
                        continue
                    progress.action = action
                    self._safety_actions.append(
                        NodeSafetyAction(
                            drone_id=progress.state.drone_id,
                            connection_epoch=progress.state.connection_epoch,
                            t_ms=now_ms,
                            action=action,
                        )
                    )

    def start(self) -> None:
        missing = sorted(set(self.flight.aircraft) - set(self.adapter_keys))
        if missing:
            joined = ", ".join(str(drone_id) for drone_id in missing)
            raise ValueError(
                f"simulator requires configured adapter credentials for drones {joined}"
            )
        for drone_id in self.flight.aircraft:
            self._active.add(drone_id)
            self._activate(drone_id)

    def periodic_events(self) -> list[dict[str, object]]:
        events: list[dict[str, object]] = []
        for drone_id in sorted(self._active - self._silent):
            events.extend(self._process(self._telemetry(drone_id), drone_id))
        return events

    def silence(self, drone_id: int) -> None:
        self._require_active(drone_id)
        self._silent.add(drone_id)

    def disconnect(self, drone_id: int) -> list[dict[str, object]]:
        self._require_active(drone_id)
        self._active.remove(drone_id)
        self._silent.discard(drone_id)
        bridge = self._bridge()
        epoch = self.session.registry.connection_epoch(drone_id)
        events = self.session.handle_adapter_disconnect(
            drone_id=drone_id,
            connection_epoch=epoch,
        )
        events.extend(
            bridge.adapter_disconnected(
                drone_id=drone_id,
                connection_epoch=epoch,
                relay_state=self.session.current_state(),
            )
        )
        return events

    def rejoin(self, drone_id: int) -> list[dict[str, object]]:
        if drone_id not in self.flight.aircraft:
            raise ValueError(f"unknown simulated aircraft {drone_id}")
        self._active.add(drone_id)
        self._silent.discard(drone_id)
        return self._activate(drone_id)

    def _activate(self, drone_id: int) -> list[dict[str, object]]:
        events = self._process(self._membership(drone_id, "join"), drone_id)
        events.extend(self._process(self._telemetry(drone_id), drone_id))
        events.extend(self._process(self._membership(drone_id, "readiness"), drone_id))
        return events

    def _process(self, frame: dict[str, object], drone_id: int) -> list[dict[str, object]]:
        key = self.adapter_keys[drone_id]
        events = self.session.process_frame(
            frame,
            Principal(source="adapter", drone_id=drone_id, signing_key=key),
        )
        if not any(event.get("type") == "refusal" for event in events):
            events.extend(
                self._bridge().adapter_activity(
                    drone_id=drone_id,
                    relay_state=self.session.current_state(),
                )
            )
        return events

    def _membership(self, drone_id: int, action: str) -> dict[str, object]:
        event: dict[str, object] = {
            "v": 1,
            "t": self.session.clock(),
            "type": "membership",
            "event_id": self._event_id(drone_id, action),
            "session": self.session.session_id,
            "drone_id": drone_id,
            "action": action,
        }
        if action == "join":
            event.update(adapter_id=f"sim-{drone_id}", capabilities=["flight"])
        else:
            event.update(
                connection_epoch=self.flight.aircraft[drone_id].connection_epoch,
                home_pose_confirmed=True,
                control_authority=True,
                rc_safety_operator_present=True,
            )
        event["signature"] = sign_event(event, self.adapter_keys[drone_id])
        return event

    def _telemetry(self, drone_id: int) -> dict[str, object]:
        aircraft = self.flight.aircraft[drone_id]
        return {
            "v": 1,
            "t": self.session.clock(),
            "type": "telemetry",
            "event_id": self._event_id(drone_id, "telemetry"),
            "session": self.session.session_id,
            "drone": drone_id,
            "connection_epoch": aircraft.connection_epoch,
            "x": aircraft.pose.x,
            "y": aircraft.pose.y,
            "z": aircraft.pose.z,
            "vx": 0.0,
            "vy": 0.0,
            "vz": 0.0,
            "battery": aircraft.battery,
            "state": aircraft.flight_state.value,
            "link": aircraft.link_quality,
            "pos_quality": aircraft.position_quality,
        }

    def _event_id(self, drone_id: int, event_type: str) -> str:
        self._sequence += 1
        return f"sim-{drone_id}-{event_type}-{self._sequence}"

    def _bridge(self) -> AutonomyRelayBridge:
        if self.bridge is None:
            raise RuntimeError("simulator ingress is not attached to a relay bridge")
        return self.bridge

    def _require_active(self, drone_id: int) -> None:
        if drone_id not in self._active:
            raise ValueError(f"simulated aircraft {drone_id} is not active")


def create_m14_sim_app(
    settings: RelaySettings | None = None,
    *,
    clock: Clock | None = None,
    event_ids: EventIdFactory | None = None,
    initial_snapshot: FleetSnapshot | None = None,
    auto_start_nodes: bool = True,
) -> FastAPI:
    active_settings = settings or RelaySettings.from_env()
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
        adapter_keys=active_settings.adapter_keys,
        auto_start_nodes=auto_start_nodes,
    )
    application = create_app(
        active_settings,
        clock=clock,
        event_ids=event_ids,
        intent_sink_factory=factory,
        shutdown_callback=factory.close,
    )
    application.state.sim_bridge_factory = factory
    return application


@dataclass(slots=True)
class _LocalWatchdog:
    state: NodeWatchdogState
    action: LossBehavior | None = None


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
