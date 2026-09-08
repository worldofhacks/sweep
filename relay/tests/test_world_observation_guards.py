from __future__ import annotations

import copy
import json
import math
import sqlite3
from pathlib import Path

import pytest

from relay.observations import Observation
from relay.platform_observations import WorldObservationError, WorldObservationService
from relay.tests.test_platform_observations import (
    NOW,
    REFERENCE,
    SESSION,
    Clock,
    approved,
    registration,
    request,
    submission,
)


def state(
    clock: Clock,
    *,
    epoch: int = 1,
    device: int = 11,
    node_type: str = "ground_vehicle",
    sequence: int = 1,
    membership: str = "ready",
) -> dict:
    return {
        "v": 1,
        "type": "state",
        "session": SESSION,
        "t": clock(),
        "event_id": f"state-{sequence}",
        "state_sequence": sequence,
        "roster_version": sequence,
        "selection": [device],
        "drones": [
            {
                "drone_id": device,
                "connection_epoch": epoch,
                "device_class": node_type,
                "membership": membership,
                "last_seen_at": clock(),
            }
        ],
    }


def pose(clock, *, epoch=1, event_id="pose-1", device=11, node_type="ground_vehicle"):
    value = submission(event_id=event_id, epoch=epoch)
    value["device_id"] = device
    value["node_type"] = "ground" if node_type == "ground_vehicle" else node_type
    value["t_capture"] = {"clock_id": "relay-fixture", "unit": "ms", "value": clock()}
    value["t_source_receipt"] = dict(value["t_capture"])
    value["t_ingest"] = clock()
    value["payload"]["pose"].update(qz=math.sin(0.1), qw=math.cos(0.1))
    return value


def store_pose(service, raw, current):
    return service.ingest(
        SESSION,
        Observation.parse(raw),
        receipt_ms=raw["t_source_receipt"]["value"],
        capture_ms=raw["t_capture"]["value"],
        state=current,
    )


def record_request():
    return {**request(), "deviceId": 11, "connectionEpoch": 1, "tagId": 7}


@pytest.fixture
def configured(tmp_path):
    clock, bundle = Clock(), approved()
    service = WorldObservationService(
        registrations={"world-pose": registration()},
        approved_bundle=lambda _session: bundle,
        database=tmp_path / "guards.sqlite3",
        clock=clock,
    )
    return service, clock, bundle


def test_capture_expires_after_exactly_one_second_even_with_fresh_roster(configured) -> None:
    service, clock, _ = configured
    store_pose(service, pose(clock), state(clock))
    clock.value += 999
    assert len(service.positions(SESSION, request(), state(clock, sequence=2))["observations"]) == 1
    clock.value += 1
    assert service.positions(SESSION, request(), state(clock, sequence=3))["observations"] == []
    with pytest.raises(WorldObservationError, match="fresh qualified"):
        service.record(SESSION, record_request(), state(clock, sequence=3), "console")


def test_stale_state_and_device_reports_cannot_support_a_world_position(configured) -> None:
    service, clock, _ = configured
    stale = state(clock)
    stale["drones"][0]["last_seen_at"] -= 5001
    with pytest.raises(WorldObservationError, match="device report is stale"):
        store_pose(service, pose(clock), stale)
    clock.value += 1000
    with pytest.raises(WorldObservationError, match="session state is stale"):
        store_pose(service, pose(clock), stale)


def test_source_order_rate_and_event_id_replay_are_checked_independently(configured) -> None:
    service, clock, _ = configured
    first = pose(clock)
    store_pose(service, first, state(clock))
    with pytest.raises(WorldObservationError, match="timestamps did not advance"):
        store_pose(service, {**first, "event_id": "new-id"}, state(clock))
    clock.value += 49
    with pytest.raises(WorldObservationError) as limited:
        store_pose(service, pose(clock, event_id="pose-2"), state(clock, sequence=2))
    assert limited.value.status_code == 429
    clock.value += 1
    with pytest.raises(WorldObservationError, match="already accepted"):
        store_pose(service, pose(clock), state(clock, sequence=3))
    store_pose(service, pose(clock, event_id="pose-2"), state(clock, sequence=3))


def test_epoch_transition_and_return_to_old_epoch_never_revive_observations(configured) -> None:
    service, clock, _ = configured
    store_pose(service, pose(clock), state(clock))
    service.observe_state(SESSION, state(clock, epoch=2, sequence=2))
    assert (
        service.positions(SESSION, request(), state(clock, epoch=2, sequence=2))["observations"]
        == []
    )
    returned = state(clock, epoch=1, sequence=3)
    with pytest.raises(WorldObservationError, match="epoch is not current"):
        store_pose(service, pose(clock, event_id="old-origin"), returned)


def test_disconnect_then_same_epoch_and_stale_state_replay_are_refused(configured) -> None:
    service, clock, _ = configured
    active = state(clock)
    store_pose(service, pose(clock), active)
    service.observe_state(SESSION, state(clock, membership="disconnected", sequence=2))
    with pytest.raises(WorldObservationError, match="state regressed"):
        service.positions(SESSION, request(), active)
    with pytest.raises(WorldObservationError, match="epoch is not current"):
        store_pose(service, pose(clock, event_id="disconnected-origin"), state(clock, sequence=3))


def test_map_revision_a_b_a_requires_a_new_observation(configured) -> None:
    service, clock, bundle = configured
    store_pose(service, pose(clock), state(clock))
    initial = copy.deepcopy(bundle)
    bundle["reference"]["revision"] = "2"
    with pytest.raises(WorldObservationError, match="active approved revision"):
        service.positions(SESSION, request(), state(clock))
    bundle.clear()
    bundle.update(initial)
    assert service.positions(SESSION, request(), state(clock))["observations"] == []
    clock.value += 50
    store_pose(service, pose(clock, event_id="new-map-capture"), state(clock, sequence=2))
    assert len(service.positions(SESSION, request(), state(clock, sequence=2))["observations"]) == 1


def test_recording_requires_exact_selected_ground_context_and_audits_pose_reference(
    configured,
) -> None:
    service, clock, _ = configured
    active = state(clock)
    store_pose(service, pose(clock), active)
    receipt = service.record(SESSION, record_request(), active, "authenticated-console")
    assert receipt["tagId"] == 7
    assert receipt["reference"] == REFERENCE
    assert receipt["observationId"].startswith("capture-")
    assert receipt["observationId"] != "pose-1"
    audit = service.audit_records(SESSION)
    assert len(audit) == 1
    assert audit[0]["actor"] == "authenticated-console"
    assert audit[0]["receipt"] == receipt
    assert audit[0]["evidence"]["registration"]["reference"] == REFERENCE
    assert audit[0]["evidence"]["registration"]["transformId"] == "measured-transform-1"
    assert audit[0]["evidence"]["sourceObservation"]["payload"]["pose"]["z_m"] == 0.0
    assert service.audit_records("other-session") == []
    unselected = state(clock, sequence=2)
    unselected["selection"] = []
    with pytest.raises(WorldObservationError, match="exactly one selected"):
        service.record(SESSION, record_request(), unselected, "authenticated-console")


@pytest.mark.parametrize("operation", ["positions", "record"])
@pytest.mark.parametrize(
    "changed_reference",
    [
        {**REFERENCE, "bundleId": "different-map-same-labels"},
        {**REFERENCE, "revision": "2"},
        {**REFERENCE, "contentHash": "b" * 64},
    ],
)
def test_map_labels_cannot_substitute_for_exact_observation_reference(
    configured, operation, changed_reference
) -> None:
    service, clock, _ = configured
    active = state(clock)
    store_pose(service, pose(clock), active)
    value = request() if operation == "positions" else record_request()
    value["reference"] = changed_reference
    args = (SESSION, value, active) + (() if operation == "positions" else ("console",))
    with pytest.raises(WorldObservationError) as failure:
        getattr(service, operation)(*args)
    assert failure.value.code == "reference_changed"
    assert service.audit_records(SESSION) == []
    result = service.positions(SESSION, request(), active)
    assert result["reference"] == REFERENCE
    assert result["observations"][0]["reference"] == REFERENCE


@pytest.mark.parametrize("operation", ["positions", "record"])
def test_observation_reference_is_required_even_when_labels_match(configured, operation) -> None:
    service, clock, _ = configured
    value = request() if operation == "positions" else record_request()
    del value["reference"]
    args = (SESSION, value, state(clock)) + (() if operation == "positions" else ("console",))
    with pytest.raises(WorldObservationError) as failure:
        getattr(service, operation)(*args)
    assert failure.value.code == "invalid_request"


def test_aircraft_positions_can_display_but_cannot_record_drive_over_tags(tmp_path: Path) -> None:
    clock = Clock()
    service = WorldObservationService(
        registrations={"world-pose": registration()},
        approved_bundle=lambda _session: approved(),
        database=tmp_path / "aircraft.sqlite3",
        clock=clock,
    )
    active = state(clock, device=1, node_type="aircraft")
    store_pose(service, pose(clock, device=1, node_type="aircraft"), active)
    assert len(service.positions(SESSION, request(), active)["observations"]) == 1
    with pytest.raises(WorldObservationError, match="selected ground robot"):
        service.record(SESSION, {**record_request(), "deviceId": 1}, active, "console")


def test_audit_receipts_and_replay_cursors_survive_restart_without_live_pose_replay(
    configured,
) -> None:
    service, clock, _ = configured
    store_pose(service, pose(clock), state(clock))
    receipt = service.record(SESSION, record_request(), state(clock), "console")
    restored = WorldObservationService(
        registrations=service.registrations,
        approved_bundle=lambda _session: approved(),
        database=service.database,
        clock=clock,
    )
    assert restored.positions(SESSION, request(), state(clock))["observations"] == []
    assert restored.audit_records(SESSION)[0]["receipt"] == receipt
    with pytest.raises(WorldObservationError, match="timestamps did not advance"):
        store_pose(restored, pose(clock, event_id="replayed-capture"), state(clock))
    with sqlite3.connect(service.database) as connection:
        row = connection.execute("SELECT payload,binding FROM world_observations").fetchone()
    assert json.loads(row[0]) == pose(clock)
    assert json.loads(row[1])["reference"] == REFERENCE


def test_capture_expiring_during_storage_wait_is_not_recorded(configured) -> None:
    service, clock, _ = configured
    active = state(clock)
    store_pose(service, pose(clock), active)
    times = iter([NOW, NOW + 1000])
    service.clock = lambda: next(times)
    with pytest.raises(WorldObservationError, match="expired before"):
        service.record(SESSION, record_request(), active, "console")
    assert service.audit_records(SESSION) == []


def test_approval_changing_before_capture_commit_is_refused(configured) -> None:
    service, clock, _ = configured
    active = state(clock)
    store_pose(service, pose(clock), active)
    changed = approved()
    changed["reference"]["revision"] = "2"
    replies = iter([approved(), changed])
    service.approved_bundle = lambda _session: next(replies)
    with pytest.raises(WorldObservationError, match="registration does not match"):
        service.record(SESSION, record_request(), active, "console")
    assert service.audit_records(SESSION) == []


def test_failed_audit_storage_cannot_publish_an_accepted_pose(configured) -> None:
    service, clock, _ = configured
    service.database.write_bytes(b"invalid sqlite test fixture")
    with pytest.raises(WorldObservationError) as failure:
        store_pose(service, pose(clock), state(clock))
    assert failure.value.code == "storage_unavailable"
    assert failure.value.status_code == 503
    assert service._latest == {}


def test_old_publication_callback_cannot_retire_a_newer_http_observation(configured) -> None:
    service, clock, _ = configured
    old = state(clock, membership="disconnected")
    current = state(clock, epoch=2, sequence=2)
    store_pose(service, pose(clock, epoch=2), current)
    service.observe_state(SESSION, old)
    assert len(service.positions(SESSION, request(), current)["observations"]) == 1


def test_explicit_map_invalidation_requires_a_new_pose_even_for_same_revision(configured) -> None:
    service, clock, _ = configured
    current = state(clock)
    store_pose(service, pose(clock), current)
    service.invalidate(SESSION)
    assert service.positions(SESSION, request(), current)["observations"] == []
