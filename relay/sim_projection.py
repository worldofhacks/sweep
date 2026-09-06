from dataclasses import replace

from adapters.sim.flight import SimFlightAdapter
from planner.models import FleetSnapshot, FlightState
from relay.auth import Principal
from relay.session import RelaySession


def sim_snapshot(snapshot: FleetSnapshot, flight: SimFlightAdapter) -> FleetSnapshot:
    aircraft = dict(snapshot.aircraft)
    for sample in flight.telemetry():
        current = aircraft.get(sample.drone_id)
        if current is None or current.connection_epoch != sample.connection_epoch:
            continue
        aircraft[sample.drone_id] = replace(
            current,
            pose=sample.pose,
            flight_state=FlightState(sample.flight_state),
            position_last_seen_ms=snapshot.now_ms,
            link_last_seen_ms=snapshot.now_ms,
            heading_deg=sample.yaw_deg,
        )
    return replace(snapshot, aircraft=aircraft)


def record_sim_telemetry(
    session: RelaySession, flight: SimFlightAdapter
) -> list[dict[str, object]]:
    events = []
    for sample in flight.telemetry():
        events.extend(
            session.process_telemetry(
                {
                    "v": 1,
                    "type": "telemetry",
                    "session": session.session_id,
                    "event_id": session.event_ids(),
                    "t": session.clock(),
                    "drone": sample.drone_id,
                    "connection_epoch": sample.connection_epoch,
                    "x": sample.pose.x,
                    "y": sample.pose.y,
                    "z": sample.pose.z,
                    "vx": 0.0,
                    "vy": 0.0,
                    "vz": 0.0,
                    "battery": sample.battery,
                    "state": sample.flight_state,
                    "link": sample.link_quality,
                    "pos_quality": sample.position_quality,
                },
                Principal("adapter", sample.drone_id, b"internal-simulator"),
            )
        )
    return events
