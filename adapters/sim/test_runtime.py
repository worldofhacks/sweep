from __future__ import annotations

import pytest

from adapters.sim.runtime import _unsimulated_enrichment, create_m14_sim_app
from planner.models import (
    DeviceClass,
    DriveState,
    FleetSnapshot,
    RelayAircraftSafetyEnrichment,
    RelaySnapshotEnrichment,
)
from relay.settings import RelaySettings

SIM_NOW_MS = 100_000
SIM_GROUND_ID = 11


def test_m14_sim_arbiter_uses_configured_relay_freshness(tmp_path) -> None:
    app = create_m14_sim_app(
        RelaySettings(
            relay_token=b"m14-simulator-freshness-test-key",
            log_dir=tmp_path,
            telemetry_freshness_ms=250,
        )
    )

    safety = app.state.sim_bridge_factory.safety
    assert safety.max_link_age_ms == 250
    assert safety.max_position_age_ms == 250


def _sim_relay_drone(
    drone_id: int, *, device_class: str, state: str, x: float, y: float, z: float
) -> dict[str, object]:
    """One device as the relay projects it into the sim composition's snapshot."""
    return {
        "drone_id": drone_id,
        "device_class": device_class,
        "unit": 1,
        "connection_epoch": 1,
        "membership": "ready",
        "control_authority": True,
        "rc_safety_operator_present": True,
        "home_pose": {"x": x, "y": y, "z": 0.0},
        "telemetry": {
            "x": x,
            "y": y,
            "z": z,
            "battery": 0.9,
            "link": 0.9,
            "pos_quality": 0.6,
            "state": state,
            "t": SIM_NOW_MS,
        },
    }


def _sim_relay_state(ground_state: str = "idle") -> dict[str, object]:
    return {
        "t": SIM_NOW_MS,
        "roster_version": 3,
        "armed": True,
        "estop": False,
        "formation": "none",
        "spacing": 0.8,
        "selection": [1, SIM_GROUND_ID],
        "drones": [
            _sim_relay_drone(1, device_class="aircraft", state="hovering", x=0.0, y=0.0, z=1.0),
            _sim_relay_drone(
                SIM_GROUND_ID,
                device_class="ground_vehicle",
                state=ground_state,
                x=0.0,
                y=6.0,
                z=0.0,
            ),
        ],
    }


def _simulated_aircraft() -> dict[int, RelayAircraftSafetyEnrichment]:
    return {
        1: RelayAircraftSafetyEnrichment(
            drone_id=1,
            armed=True,
            physical_rc_available=True,
            storage_remaining_bytes=50_000_000,
            camera_ready=True,
            active_task_id=None,
            position_loss_since_ms=None,
        )
    }


def _sim_snapshot(state: dict[str, object]) -> FleetSnapshot:
    simulated = _simulated_aircraft()
    return FleetSnapshot.from_relay_state(
        state,
        enrichment=RelaySnapshotEnrichment(
            operator_present=True,
            operator_last_seen_ms=SIM_NOW_MS,
            aircraft={**simulated, **_unsimulated_enrichment(state, simulated)},
        ),
    )


def test_a_device_the_simulator_does_not_model_still_carries_safety_enrichment() -> None:
    """A ground vehicle joined to a sim session must not break the whole session's snapshot."""
    state = _sim_relay_state()

    snapshot = _sim_snapshot(state)

    assert set(snapshot.aircraft) == {1, SIM_GROUND_ID}
    robot = snapshot.aircraft[SIM_GROUND_ID]
    assert robot.device_class is DeviceClass.GROUND_VEHICLE
    assert robot.drive_state is DriveState.IDLE
    assert robot.armed is True, "wheels are enabled in every drive state but docked and fault"
    assert robot.camera_ready is False and robot.storage_remaining_bytes == 0


def test_an_unsimulated_device_is_disarmed_by_its_own_class_vocabulary() -> None:
    docked = _sim_snapshot(_sim_relay_state(ground_state="docked"))

    assert docked.aircraft[SIM_GROUND_ID].armed is False
    assert docked.aircraft[SIM_GROUND_ID].mobile is False


def test_a_snapshot_without_the_unsimulated_entry_cannot_be_built() -> None:
    """The entry is what keeps one unsimulated device from failing every intent in the session."""
    state = _sim_relay_state()

    with pytest.raises(ValueError, match=str(SIM_GROUND_ID)):
        FleetSnapshot.from_relay_state(
            state,
            enrichment=RelaySnapshotEnrichment(
                operator_present=True,
                operator_last_seen_ms=SIM_NOW_MS,
                aircraft=_simulated_aircraft(),
            ),
        )
