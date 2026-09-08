"""Qualified map observations stay distinct from motion and unregistered telemetry."""

from __future__ import annotations

import copy
import json
import sqlite3
from pathlib import Path

import pytest

from relay.auth import Principal
from relay.platform_observations import WorldObservationError, WorldObservationService
from spatial.contracts import FrameDeclaration, FrameKind, NodeType
from spatial.observations import Observation, ObservationSource

NOW = 1_756_700_000_000
SESSION = "qualified-map-test"
REFERENCE = {"bundleId": "map-test", "revision": "1", "contentHash": "a" * 64}


class Clock:
    value = NOW

    def __call__(self) -> int:
        return self.value


def approved() -> dict:
    return {
        "reference": dict(REFERENCE),
        "approval": {"auditId": "approved-1", "reference": dict(REFERENCE)},
        "bundle": {
            "manifest": {
                "mapVersion": "map-v1",
                "floorId": "floor-1",
                "frame": "world",
                "units": "m",
                "registration": {
                    "sourceFrame": "survey-frame",
                    "transformId": "measured-transform-1",
                    "residualM": 0.01,
                    "thresholdM": 0.02,
                    "evidence": "survey-record",
                },
            }
        },
    }


def registration() -> dict:
    return {
        "reference": dict(REFERENCE),
        "mapVersion": "map-v1",
        "floorId": "floor-1",
        "sourceFrame": "survey-frame",
        "transformId": "measured-transform-1",
        "qualifiedWorldPose": True,
    }


def source(device: int = 11, node_type: NodeType = NodeType.GROUND_VEHICLE) -> ObservationSource:
    return ObservationSource(
        "world-pose",
        "localization",
        device,
        node_type,
        (FrameDeclaration("world", FrameKind.WORLD),),
        ("pose",),
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


def pose(
    clock: Clock,
    *,
    epoch: int = 1,
    event_id: str = "pose-1",
    device: int = 11,
    node_type: str = "ground_vehicle",
) -> dict:
    return {
        "v": 1,
        "type": "observation",
        "t": clock(),
        "event_id": event_id,
        "session": SESSION,
        "drone_id": device,
        "connection_epoch": epoch,
        "source_id": "world-pose",
        "node_type": node_type,
        "t_capture": clock(),
        "frame": "world",
        "confidence": 0.9,
        "payload": {
            "kind": "pose",
            "position": {"frame": "world", "x_m": 1.25, "y_m": 2.5, "z_m": 0.0},
            "yaw_rad": 0.2,
        },
    }


def principal(device: int = 11, role: str = "localization") -> Principal:
    return Principal(role, device, b"isolated-localization-credential-32")


def request() -> dict:
    return {"mapVersion": "map-v1", "floorId": "floor-1", "reference": dict(REFERENCE)}


def record_request() -> dict:
    return {**request(), "deviceId": 11, "connectionEpoch": 1, "tagId": 7}


@pytest.fixture
def configured(tmp_path: Path):
    clock, bundle = Clock(), approved()
    service = WorldObservationService(
        sources={"world-pose": source()},
        registrations={"world-pose": registration()},
        approved_bundle=lambda _session: bundle,
        database=tmp_path / "observations.sqlite3",
        clock=clock,
    )
    return service, clock, bundle


def test_no_sources_means_no_observe_or_record_capability(tmp_path: Path) -> None:
    service = WorldObservationService.from_env(
        {},
        approved_bundle=lambda _session: approved(),
        database=tmp_path / "disabled.sqlite3",
        clock=Clock(),
    )
    assert not service.available
    assert not service.database.exists()
    with pytest.raises(WorldObservationError, match="no qualified"):
        service.positions(SESSION, request(), state(Clock()))


def test_exact_host_configuration_is_required_and_unknown_fields_are_refused(
    tmp_path: Path,
) -> None:
    declaration = {
        "principal_source": "localization",
        "drone_id": 11,
        "node_type": "ground_vehicle",
        "frames": [{"id": "world", "kind": "world"}],
        "payload_types": ["pose"],
    }
    config = {
        "sources": {"world-pose": declaration},
        "registrations": {"world-pose": registration()},
    }
    service = WorldObservationService.from_env(
        {"SWEEP_WORLD_OBSERVATION_SOURCES": json.dumps(config)},
        approved_bundle=lambda _session: approved(),
        database=tmp_path / "configured.sqlite3",
        clock=Clock(),
    )
    assert service.available
    for invalid in [
        {**config, "qualified": True},
        {**config, "registrations": {}},
        {
            **config,
            "registrations": {"world-pose": {**registration(), "qualifiedWorldPose": False}},
        },
        {**config, "sources": {"world-pose": {**declaration, "qualified": True}}},
        {
            **config,
            "sources": {
                "world-pose": {**declaration, "frames": [{"id": "body-11", "kind": "device_body"}]}
            },
        },
    ]:
        with pytest.raises((WorldObservationError, ValueError)):
            WorldObservationService.from_env(
                {"SWEEP_WORLD_OBSERVATION_SOURCES": json.dumps(invalid)},
                approved_bundle=lambda _session: approved(),
                database=tmp_path / "invalid.sqlite3",
                clock=Clock(),
            )


def test_accepted_envelope_stays_diagnostic_and_projection_requires_host_binding(
    configured,
) -> None:
    service, clock, _ = configured
    original_state = state(clock)
    before = copy.deepcopy(original_state)
    accepted = service.ingest(SESSION, pose(clock), principal(), original_state)
    assert Observation.parse(accepted).to_dict() == accepted
    assert accepted["authority"] == "diagnostic"
    assert accepted["frame_provenance"]["transform_id"] is None
    assert original_state == before
    values = service.positions(SESSION, request(), original_state)["observations"]
    assert len(values) == 1
    assert values[0]["frameAssociationVerified"] is True
    assert values[0]["position"] == {"x": 1.25, "y": 2.5}
    assert values[0]["observationId"] == "pose-1"
    assert values[0]["reference"] == REFERENCE
    accepted["payload"]["position"]["x_m"] = 999
    assert (
        service.positions(SESSION, request(), original_state)["observations"][0]["position"]["x"]
        == 1.25
    )


@pytest.mark.parametrize(
    "change",
    [
        {"session": "another-session"},
        {"source_id": "unknown"},
        {"drone_id": 12},
        {"connection_epoch": 2},
        {"node_type": "aircraft"},
        {"authority": "diagnostic"},
        {"frameAssociationVerified": True},
        {"transformId": "measured-transform-1"},
        {"t_capture": NOW - 1000},
        {"t": NOW + 1},
        {"t_capture": NOW + 1},
    ],
)
def test_producer_cannot_forge_identity_registration_or_freshness(configured, change: dict) -> None:
    service, clock, _ = configured
    with pytest.raises(WorldObservationError):
        service.ingest(SESSION, {**pose(clock), **change}, principal(), state(clock))
    assert service.positions(SESSION, request(), state(clock))["observations"] == []


@pytest.mark.parametrize(
    "authenticated", [principal(12), principal(role="adapter"), principal(role="console")]
)
def test_principal_must_match_the_configured_source(configured, authenticated: Principal) -> None:
    service, clock, _ = configured
    with pytest.raises(WorldObservationError) as failure:
        service.ingest(SESSION, pose(clock), authenticated, state(clock))
    assert failure.value.status_code == 403


def test_a_frame_name_is_not_a_transform_and_legacy_xy_is_never_consumed(configured) -> None:
    service, clock, _ = configured
    for payload in [
        {"kind": "legacy_aircraft_telemetry", "x": 1, "y": 2},
        {
            "kind": "pose",
            "position": {"frame": "body-11", "x_m": 1, "y_m": 2, "z_m": 0},
            "yaw_rad": 0,
        },
    ]:
        with pytest.raises(WorldObservationError):
            service.ingest(SESSION, {**pose(clock), "payload": payload}, principal(), state(clock))


@pytest.mark.parametrize(
    "field,value",
    [
        ("transformId", "other-transform"),
        ("sourceFrame", "other-survey"),
    ],
)
def test_host_registration_must_match_approved_map_registration(
    configured, field: str, value: str
) -> None:
    service, clock, bundle = configured
    bundle["bundle"]["manifest"]["registration"][field] = value
    with pytest.raises(WorldObservationError, match="registration does not match"):
        service.ingest(SESSION, pose(clock), principal(), state(clock))


def test_capture_expires_after_exactly_one_second_even_with_fresh_roster(configured) -> None:
    service, clock, _ = configured
    service.ingest(SESSION, pose(clock), principal(), state(clock))
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
        service.ingest(SESSION, pose(clock), principal(), stale)
    clock.value += 1000
    with pytest.raises(WorldObservationError, match="session state is stale"):
        service.ingest(SESSION, pose(clock), principal(), stale)


def test_source_order_rate_and_event_id_replay_are_checked_independently(configured) -> None:
    service, clock, _ = configured
    first = pose(clock)
    service.ingest(SESSION, first, principal(), state(clock))
    with pytest.raises(WorldObservationError, match="timestamps did not advance"):
        service.ingest(SESSION, {**first, "event_id": "new-id"}, principal(), state(clock))
    clock.value += 49
    with pytest.raises(WorldObservationError) as limited:
        service.ingest(
            SESSION, pose(clock, event_id="pose-2"), principal(), state(clock, sequence=2)
        )
    assert limited.value.status_code == 429
    clock.value += 1
    with pytest.raises(WorldObservationError, match="already accepted"):
        service.ingest(SESSION, pose(clock), principal(), state(clock, sequence=3))
    service.ingest(SESSION, pose(clock, event_id="pose-2"), principal(), state(clock, sequence=3))


def test_epoch_transition_and_return_to_old_epoch_never_revive_observations(configured) -> None:
    service, clock, _ = configured
    service.ingest(SESSION, pose(clock), principal(), state(clock))
    service.observe_state(SESSION, state(clock, epoch=2, sequence=2))
    assert (
        service.positions(SESSION, request(), state(clock, epoch=2, sequence=2))["observations"]
        == []
    )
    returned = state(clock, epoch=1, sequence=3)
    with pytest.raises(WorldObservationError, match="epoch is not current"):
        service.ingest(SESSION, pose(clock, event_id="old-origin"), principal(), returned)


def test_disconnect_then_same_epoch_and_stale_state_replay_are_refused(configured) -> None:
    service, clock, _ = configured
    active = state(clock)
    service.ingest(SESSION, pose(clock), principal(), active)
    service.observe_state(SESSION, state(clock, membership="disconnected", sequence=2))
    with pytest.raises(WorldObservationError, match="state regressed"):
        service.positions(SESSION, request(), active)
    with pytest.raises(WorldObservationError, match="epoch is not current"):
        service.ingest(
            SESSION,
            pose(clock, event_id="disconnected-origin"),
            principal(),
            state(clock, sequence=3),
        )


def test_map_revision_a_b_a_requires_a_new_observation(configured) -> None:
    service, clock, bundle = configured
    service.ingest(SESSION, pose(clock), principal(), state(clock))
    initial = copy.deepcopy(bundle)
    bundle["reference"]["revision"] = "2"
    with pytest.raises(WorldObservationError, match="active approved revision"):
        service.positions(SESSION, request(), state(clock))
    bundle.clear()
    bundle.update(initial)
    assert service.positions(SESSION, request(), state(clock))["observations"] == []
    clock.value += 50
    service.ingest(
        SESSION, pose(clock, event_id="new-map-capture"), principal(), state(clock, sequence=2)
    )
    assert len(service.positions(SESSION, request(), state(clock, sequence=2))["observations"]) == 1


def test_recording_requires_exact_selected_ground_context_and_audits_pose_reference(
    configured,
) -> None:
    service, clock, _ = configured
    active = state(clock)
    service.ingest(SESSION, pose(clock), principal(), active)
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
    assert audit[0]["evidence"]["sourceObservation"]["payload"]["position"]["z_m"] == 0.0
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
    service.ingest(SESSION, pose(clock), principal(), active)
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
        sources={"world-pose": source(1, NodeType.AIRCRAFT)},
        registrations={"world-pose": registration()},
        approved_bundle=lambda _session: approved(),
        database=tmp_path / "aircraft.sqlite3",
        clock=clock,
    )
    active = state(clock, device=1, node_type="aircraft")
    service.ingest(SESSION, pose(clock, device=1, node_type="aircraft"), principal(1), active)
    assert len(service.positions(SESSION, request(), active)["observations"]) == 1
    with pytest.raises(WorldObservationError, match="selected ground robot"):
        service.record(SESSION, {**record_request(), "deviceId": 1}, active, "console")


def test_audit_receipts_and_replay_cursors_survive_restart_without_live_pose_replay(
    configured,
) -> None:
    service, clock, _ = configured
    service.ingest(SESSION, pose(clock), principal(), state(clock))
    receipt = service.record(SESSION, record_request(), state(clock), "console")
    restored = WorldObservationService(
        sources=service.sources,
        registrations=service.registrations,
        approved_bundle=lambda _session: approved(),
        database=service.database,
        clock=clock,
    )
    assert restored.positions(SESSION, request(), state(clock))["observations"] == []
    assert restored.audit_records(SESSION)[0]["receipt"] == receipt
    with pytest.raises(WorldObservationError, match="timestamps did not advance"):
        restored.ingest(
            SESSION, pose(clock, event_id="replayed-capture"), principal(), state(clock)
        )
    with sqlite3.connect(service.database) as connection:
        row = connection.execute("SELECT payload,binding FROM world_observations").fetchone()
    assert json.loads(row[0])["authority"] == "diagnostic"
    assert json.loads(row[1])["reference"] == REFERENCE


def test_capture_expiring_during_storage_wait_is_not_recorded(configured) -> None:
    service, clock, _ = configured
    active = state(clock)
    service.ingest(SESSION, pose(clock), principal(), active)
    times = iter([NOW, NOW + 1000])
    service.clock = lambda: next(times)
    with pytest.raises(WorldObservationError, match="expired before"):
        service.record(SESSION, record_request(), active, "console")
    assert service.audit_records(SESSION) == []


def test_approval_changing_before_capture_commit_is_refused(configured) -> None:
    service, clock, _ = configured
    active = state(clock)
    service.ingest(SESSION, pose(clock), principal(), active)
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
        service.ingest(SESSION, pose(clock), principal(), state(clock))
    assert failure.value.code == "storage_unavailable"
    assert failure.value.status_code == 503
    assert service._latest == {}


def test_old_publication_callback_cannot_retire_a_newer_http_observation(configured) -> None:
    service, clock, _ = configured
    old = state(clock, membership="disconnected")
    current = state(clock, epoch=2, sequence=2)
    service.ingest(SESSION, pose(clock, epoch=2), principal(), current)
    service.observe_state(SESSION, old)
    assert len(service.positions(SESSION, request(), current)["observations"]) == 1


def test_explicit_map_invalidation_requires_a_new_pose_even_for_same_revision(configured) -> None:
    service, clock, _ = configured
    current = state(clock)
    service.ingest(SESSION, pose(clock), principal(), current)
    service.invalidate(SESSION)
    assert service.positions(SESSION, request(), current)["observations"] == []
