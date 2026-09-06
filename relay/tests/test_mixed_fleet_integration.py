"""Mixed roster integration preserves aircraft control and rejects ground body pulses."""

import pytest

from planner.models import Command, CommandOperation, DeviceClass
from relay.app import RelayRuntime
from relay.auth import Principal
from relay.autonomy import relay_snapshot
from relay.contracts import ContractError
from relay.media import stream_name
from relay.settings import RelaySettings
from relay.tests.conftest import (
    CONSOLE_KEY,
    SESSION,
    ground_membership_payload,
    ground_telemetry_payload,
    intent_payload,
    membership_payload,
    profiled_sink,
    telemetry_payload,
)

DEVICE_IDS = (1, 2, 11, 12, 13)
KEYS = {
    drone_id: f"mixed-test-device-{drone_id}-key-at-least-32-bytes".encode()
    for drone_id in DEVICE_IDS
}
PULSE_ARGS = {"forward_mm_s": 250, "duration_ms": 500}


@pytest.fixture
def mixed_session(tmp_path, clock, event_ids):
    settings = RelaySettings(
        relay_token=CONSOLE_KEY,
        adapter_keys=KEYS,
        device_classes={drone_id: DeviceClass.GROUND_VEHICLE for drone_id in (11, 12, 13)},
        log_dir=tmp_path,
    )
    runtime = RelayRuntime(
        settings,
        clock=clock,
        event_ids=event_ids,
        intent_sink_factory=lambda _session: profiled_sink(lambda _intent, _state: None),
        min_home_position_quality=0.7,
        max_home_position_age_ms=500,
    )
    session = runtime.session(SESSION)
    for drone_id in DEVICE_IDS:
        payload = ground_membership_payload if drone_id >= 11 else membership_payload
        capabilities = ["body_pulse_v1", "flight"]
        if drone_id >= 11:
            capabilities += ["class:ground_vehicle", "ground_drive"]
        principal = Principal("adapter", drone_id, KEYS[drone_id])
        result = session.process_membership(
            payload(
                action="join",
                event_id=f"join-{drone_id}",
                drone_id=drone_id,
                key=KEYS[drone_id],
                capabilities=capabilities,
            ),
            principal,
        )
        assert result[0]["type"] == "membership"
        session.process_membership(
            payload(
                action="readiness",
                event_id=f"ready-{drone_id}",
                drone_id=drone_id,
                key=KEYS[drone_id],
            ),
            principal,
        )
    return session


def test_two_aircraft_and_three_ground_devices_keep_distinct_ids_units_and_camera_paths(
    mixed_session,
):
    state = mixed_session.current_state()
    assert [drone["drone_id"] for drone in state["drones"]] == list(DEVICE_IDS)
    assert [drone["unit"] for drone in state["drones"]] == [1, 2, 1, 2, 3]
    assert [
        stream_name(DeviceClass(drone["device_class"]), drone["unit"]) for drone in state["drones"]
    ] == ["drone1", "drone2", "ground1", "ground2", "ground3"]
    assert mixed_session.replay()["events"]  # Five-device audit projection remains valid.


@pytest.mark.parametrize("ground_state", ["idle", "hovering", "landed"])
def test_ground_class_stays_outside_flight_snapshot_even_with_flight_claims(
    mixed_session, ground_state
):
    for drone_id in DEVICE_IDS:
        payload = ground_telemetry_payload if drone_id >= 11 else telemetry_payload
        mixed_session.process_telemetry(
            payload(
                event_id=f"telemetry-{drone_id}",
                drone_id=drone_id,
                state=ground_state if drone_id >= 11 else "landed",
                pos_quality=0.7,
            ),
            Principal("adapter", drone_id, KEYS[drone_id]),
        )
    state = mixed_session.current_state()
    assert len(state["drones"]) == 5
    assert set(relay_snapshot(state, operator_last_seen_ms=None).aircraft) == {1, 2}


def test_ground_home_confirmation_waits_for_fresh_usable_stationary_evidence(mixed_session, clock):
    principal = Principal("adapter", 11, KEYS[11])
    for quality, drive_state in [(0.0, "idle"), (0.69, "idle"), (0.7, "moving")]:
        mixed_session.process_telemetry(
            ground_telemetry_payload(
                event_id=f"weak-{quality}-{drive_state}",
                state=drive_state,
                pos_quality=quality,
            ),
            principal,
        )
        assert mixed_session.current_state()["drones"][2]["home_pose"] is None
    clock.advance(501)
    mixed_session.process_telemetry(
        ground_telemetry_payload(event_id="old-ground", state="stopped", pos_quality=0.7),
        principal,
    )
    assert mixed_session.current_state()["drones"][2]["home_pose"] is None
    mixed_session.process_telemetry(
        ground_telemetry_payload(
            event_id="valid-ground",
            timestamp=clock() + 17,
            state="stopped",
            pos_quality=0.7,
        ),
        principal,
    )
    assert mixed_session.current_state()["drones"][2]["home_pose"] is not None


@pytest.mark.parametrize("selection", [[11], [1, 11]])
@pytest.mark.parametrize("source", ["console", "webcam"])
def test_ground_pulse_intent_is_refused_before_downstream_admission(
    mixed_session, selection, source
):
    raw = {
        **intent_payload(source=source),
        "name": "body_pulse",
        "args": PULSE_ARGS,
        "selection": selection,
        "confirm": True,
    }
    events = mixed_session.process_intent(raw, Principal(source, None, CONSOLE_KEY))
    assert events[0]["type"] == "refusal"
    assert events[0]["reason"] == "unsupported_for_device_class"
    assert mixed_session.metrics()["accepted_intents"] == 0
    assert mixed_session.metrics()["commands_issued"] == 0


def test_ground_pulse_cannot_be_registered_or_signed_even_with_a_flight_capability(mixed_session):
    command = Command(
        "pulse-ground",
        "intent-ground",
        mixed_session.registry.roster_version,
        11,
        1,
        CommandOperation.BODY_PULSE,
        PULSE_ARGS,
    )
    with pytest.raises(ContractError, match="only for aircraft"):
        mixed_session.register_dispatched_command(command)
    with pytest.raises(ContractError, match="only for aircraft"):
        mixed_session.issue_command(
            command_id=command.command_id,
            intent_id=command.intent_id,
            roster_version=command.roster_version,
            drone_id=command.drone_id,
            connection_epoch=command.connection_epoch,
            operation=command.operation,
            args=PULSE_ARGS,
            signing_key=KEYS[11],
        )
    assert mixed_session.metrics()["commands_issued"] == 0
    assert all(row["event"]["type"] != "command" for row in mixed_session.replay()["events"])
    aircraft_frame = mixed_session.issue_command(
        command_id="pulse-aircraft",
        intent_id="intent-aircraft",
        roster_version=mixed_session.registry.roster_version,
        drone_id=1,
        connection_epoch=1,
        operation=CommandOperation.BODY_PULSE,
        args=PULSE_ARGS,
        signing_key=KEYS[1],
    )
    assert aircraft_frame["drone_id"] == 1 and aircraft_frame["seq"] == 1
