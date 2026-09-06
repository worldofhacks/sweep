import json

import pytest

from planner.models import (
    ExecutionResult,
    FleetSnapshot,
    FlightState,
    LifecycleStatus,
    MembershipState,
    Position,
    RelayAircraftSafetyEnrichment,
    RelaySnapshotEnrichment,
)


def relay_state() -> dict[str, object]:
    return {
        "v": 1,
        "t": 0,
        "type": "state",
        "event_id": "state-1",
        "session": "session-1",
        "roster_version": 3,
        "armed": False,
        "estop": False,
        "selection": [1],
        "formation": "line",
        "spacing": 0.8,
        "mode": "indoor",
        "pending": None,
        "accepted_plan": None,
        "drones": [
            {
                "drone_id": 1,
                "connection_epoch": 2,
                "membership": "ready",
                "readiness_reasons": [],
                "flight_state": "disarmed",
                "heading_deg": 90.0,
                "battery": 0.8,
                "link": 0.9,
                "pos_quality": 0.85,
                "control_authority": True,
                "last_seen_at": 99,
                "camera_patterns": ["pano_360", "reconstruct_8"],
                "selectable": True,
                "adapter_id": "sim-1",
                "adapter_capabilities": ["flight", "camera"],
                "home_pose": {"x": 1.0, "y": 2.0, "z": 0.0},
                "rc_safety_operator_present": True,
                "telemetry": {
                    "v": 1,
                    "t": 0,
                    "type": "telemetry",
                    "drone": 1,
                    "x": 1.0,
                    "y": 2.0,
                    "z": 0.0,
                    "vx": 0.0,
                    "vy": 0.0,
                    "vz": 0.0,
                    "heading_deg": 90.0,
                    "battery": 0.8,
                    "state": "disarmed",
                    "link": 0.9,
                    "pos_quality": 0.85,
                },
                "membership_history": [],
            }
        ],
    }


def enrichment() -> RelaySnapshotEnrichment:
    return RelaySnapshotEnrichment(
        operator_present=True,
        operator_last_seen_ms=0,
        aircraft={
            1: RelayAircraftSafetyEnrichment(
                drone_id=1,
                armed=False,
                physical_rc_available=True,
                storage_remaining_bytes=5_000_000,
                camera_ready=True,
                active_task_id=None,
                position_loss_since_ms=None,
                last_link_seen_ms=88,
                last_position_seen_ms=77,
            )
        },
    )


def test_relay_projection_requires_explicit_safety_enrichment() -> None:
    snapshot = FleetSnapshot.from_relay_state(relay_state(), enrichment=enrichment())

    aircraft = snapshot.aircraft[1]
    assert snapshot.now_ms == 0
    assert aircraft.link_last_seen_ms == 0
    assert aircraft.position_last_seen_ms == 0
    assert aircraft.flight_state is FlightState.DISARMED
    assert aircraft.membership is MembershipState.READY
    assert aircraft.pose == Position(1.0, 2.0, 0.0)
    assert aircraft.heading_deg == 90.0
    assert aircraft.physical_rc_available is True


def test_relay_projection_fails_closed_without_enrichment() -> None:
    missing = RelaySnapshotEnrichment(
        operator_present=True,
        operator_last_seen_ms=0,
        aircraft={},
    )

    with pytest.raises(ValueError, match="missing safety enrichment"):
        FleetSnapshot.from_relay_state(relay_state(), enrichment=missing)


def test_nullable_relay_telemetry_requires_last_known_enrichment() -> None:
    raw = relay_state()
    drone = raw["drones"][0]  # type: ignore[index]
    drone["telemetry"] = None  # type: ignore[index]
    drone["battery"] = None  # type: ignore[index]
    drone["link"] = None  # type: ignore[index]
    drone["pos_quality"] = None  # type: ignore[index]

    with pytest.raises(ValueError, match="missing pose"):
        FleetSnapshot.from_relay_state(raw, enrichment=enrichment())


def test_non_null_malformed_relay_telemetry_cannot_fall_back_to_enrichment() -> None:
    raw = relay_state()
    drone = raw["drones"][0]  # type: ignore[index]
    drone["telemetry"] = "malformed"  # type: ignore[index]

    with pytest.raises(ValueError, match="malformed telemetry"):
        FleetSnapshot.from_relay_state(raw, enrichment=enrichment())


def test_relay_projection_rejects_divergent_flight_state_alias() -> None:
    raw = relay_state()
    drone = raw["drones"][0]  # type: ignore[index]
    drone["flight_state"] = "hovering"  # type: ignore[index]
    drone["telemetry"]["state"] = "landing"  # type: ignore[index]

    with pytest.raises(ValueError, match="divergent flight state"):
        FleetSnapshot.from_relay_state(raw, enrichment=enrichment())


def test_relay_projection_rejects_unknown_nested_flight_state() -> None:
    raw = relay_state()
    drone = raw["drones"][0]  # type: ignore[index]
    drone["flight_state"] = "unknown"  # type: ignore[index]
    drone["telemetry"]["state"] = "unknown"  # type: ignore[index]

    with pytest.raises(ValueError, match="not a valid FlightState"):
        FleetSnapshot.from_relay_state(raw, enrichment=enrichment())


def test_null_relay_telemetry_uses_only_explicit_last_known_enrichment() -> None:
    raw = relay_state()
    drone = raw["drones"][0]  # type: ignore[index]
    drone["telemetry"] = None  # type: ignore[index]
    drone["flight_state"] = "emergency"  # type: ignore[index]
    drone["battery"] = 0.0  # type: ignore[index]
    drone["link"] = 0.0  # type: ignore[index]
    drone["pos_quality"] = 0.0  # type: ignore[index]
    explicit = RelaySnapshotEnrichment(
        operator_present=True,
        operator_last_seen_ms=0,
        aircraft={
            1: RelayAircraftSafetyEnrichment(
                drone_id=1,
                armed=False,
                physical_rc_available=True,
                storage_remaining_bytes=5_000_000,
                camera_ready=True,
                active_task_id=None,
                position_loss_since_ms=None,
                last_known_pose=Position(1.0, 2.0, 0.0),
                last_known_home=Position(1.0, 2.0, 0.0),
                last_known_flight_state="disarmed",
                last_known_battery=0.8,
                last_known_link_quality=0.9,
                last_known_position_quality=0.85,
                last_link_seen_ms=0,
                last_position_seen_ms=0,
            )
        },
    )

    snapshot = FleetSnapshot.from_relay_state(raw, enrichment=explicit)

    aircraft = snapshot.aircraft[1]
    assert aircraft.flight_state is FlightState.DISARMED
    assert aircraft.battery == 0.8
    assert aircraft.link_quality == 0.9
    assert aircraft.position_quality == 0.85


def test_snapshot_projection_is_json_native_and_deterministic() -> None:
    snapshot = FleetSnapshot.from_relay_state(relay_state(), enrichment=enrichment())

    first = snapshot.to_dict()
    second = snapshot.to_dict()

    assert first == second
    assert json.loads(json.dumps(first, sort_keys=True))["aircraft"][0]["membership"] == "ready"
    assert first["fleet_observation_complete"] is True


def test_snapshot_observation_completeness_is_strict_and_round_trips() -> None:
    raw = FleetSnapshot.from_relay_state(relay_state(), enrichment=enrichment()).to_dict()
    raw["fleet_observation_complete"] = False

    snapshot = FleetSnapshot.from_mapping(raw)

    assert snapshot.fleet_observation_complete is False
    raw["fleet_observation_complete"] = 1
    with pytest.raises(ValueError, match="fleet_observation_complete"):
        FleetSnapshot.from_mapping(raw)


def test_execution_projection_rejects_nondeterministic_iterable_bundle() -> None:
    result = ExecutionResult(
        intent_id="intent-1",
        roster_version=1,
        status=LifecycleStatus.COMPLETED,
        capture_bundle={"unordered", "values"},
    )

    with pytest.raises(TypeError, match="ordered"):
        result.to_dict()
