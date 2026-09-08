"""Custom hardware readings survive transport without becoming control authority."""

from __future__ import annotations

import copy

import pytest

from nodekit.telemetry import device_telemetry_payload
from relay.contracts import ContractError, parse_adapter_acknowledgement, parse_node_status
from relay.tests.conftest import acknowledgement_payload, membership_payload, node_status_payload

READINGS = {
    "adapter": "ohmni",
    "lidar": {
        "present": True,
        "scan_age_ms": 42,
        "ranges_cm": [0, 213, 345],
        "health": "good",
    },
    "battery": {"charge_percent": None, "voltage": 16.2},
    "safety": {"blocked": True, "reasons": ["lidar_coverage_incomplete"]},
}


def test_optional_readings_round_trip_and_do_not_alias_input() -> None:
    legacy = node_status_payload(event_id="legacy")
    assert parse_node_status(legacy).to_event() == legacy
    assert "device_telemetry" not in parse_node_status(legacy).state_payload()

    readings = copy.deepcopy(READINGS)
    payload = node_status_payload(event_id="custom", device_telemetry=readings)
    frame = parse_node_status(payload)
    assert frame.to_event() == payload
    readings["lidar"]["ranges_cm"][1] = 999
    assert frame.to_event()["device_telemetry"] == READINGS
    published = frame.state_payload()
    published["device_telemetry"]["lidar"]["ranges_cm"][1] = 888
    assert frame.to_event()["device_telemetry"] == READINGS


@pytest.mark.parametrize(
    "readings",
    [
        None,
        [],
        {"value": float("nan")},
        {"value": float("inf")},
        {"value": 10**1000},
        {"value": 2**53},
        {"invalid-key": 1},
        {"a": {"b": {"c": {"d": 1}}}},
        {"value": "x" * 513},
        {"value": [0] * 513},
        {f"key_{i}": 0 for i in range(129)},
        {f"key_{i}": "界" * 512 for i in range(11)},
        {"value": (1, 2)},
    ],
)
def test_invalid_readings_are_rejected_before_state_mutation(readings) -> None:
    with pytest.raises(ValueError):
        device_telemetry_payload(readings)
    with pytest.raises(ContractError) as caught:
        parse_node_status(node_status_payload(event_id="bad", device_telemetry=readings))
    assert caught.value.code == "invalid_node_status"


def test_readings_survive_fanout_state_and_audit_without_granting_authority(
    relay_session, adapter_principal, clock
) -> None:
    relay_session.process_membership(
        membership_payload(action="join", event_id="join", timestamp=clock()),
        adapter_principal,
    )
    events = relay_session.process_node_frame(
        node_status_payload(
            event_id="status",
            timestamp=clock(),
            device_telemetry={**READINGS, "control_authority": True},
        ),
        adapter_principal,
    )
    assert [event["type"] for event in events] == ["node_status", "state"]
    expected = {**READINGS, "control_authority": True}
    assert events[0]["device_telemetry"] == expected
    drone = events[1]["drones"][0]
    assert drone["node_status"]["device_telemetry"] == expected
    assert drone["control_authority"] is False
    assert drone["selectable"] is False
    audited = [record["event"] for record in relay_session.replay()["events"]]
    assert next(e for e in audited if e["type"] == "node_status")["device_telemetry"] == expected
    assert audited[-1]["drones"][0]["node_status"]["device_telemetry"] == expected


def test_bad_payload_does_not_poison_session_and_old_epoch_cannot_restore_readings(
    relay_session, adapter_principal, clock
) -> None:
    def join(event_id):
        relay_session.process_membership(
            membership_payload(action="join", event_id=event_id, timestamp=clock()),
            adapter_principal,
        )

    def status(event_id, readings, epoch=1):
        return relay_session.process_node_frame(
            node_status_payload(
                event_id=event_id,
                timestamp=clock(),
                connection_epoch=epoch,
                device_telemetry=readings,
            ),
            adapter_principal,
        )

    join("join")
    refused = status("bad", {"value": float("inf")})
    assert refused[0]["reason"] == "invalid_node_status"
    assert status("good", READINGS)[0]["type"] == "node_status"
    assert relay_session.current_state()["drones"][0]["node_status"] is not None
    relay_session.handle_adapter_disconnect(drone_id=1, connection_epoch=1)
    join("rejoin")
    assert relay_session.current_state()["drones"][0]["node_status"] is None
    assert status("stale", READINGS)[0]["reason"] == "stale_connection_epoch"
    assert relay_session.current_state()["drones"][0]["node_status"] is None
    assert status("fresh", READINGS, epoch=2)[0]["type"] == "node_status"


@pytest.mark.parametrize(
    "reason",
    [
        "unsupported_operation",
        "motion_timeout",
        "motion_failed",
        "motion_superseded",
        "device_failure",
    ],
)
def test_actual_node_failure_reasons_remain_failures_in_relay_contract(reason):
    raw = acknowledgement_payload(
        event_id=f"fixture-{reason}",
        status="failed",
        reason=reason,
        detail="isolated fixture failure",
    )
    parsed = parse_adapter_acknowledgement(raw)
    assert parsed.status.value == "failed" and parsed.reason == reason
