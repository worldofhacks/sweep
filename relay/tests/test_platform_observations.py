"""World projection consumes the relay observation contract after admission."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from relay.observation_ingress import ObservationConfiguration, ObservationIngress
from relay.observations import (
    ClockMapping,
    FrameDeclaration,
    FrameRegistry,
    ObservationError,
    ObservationSubmission,
    SourceBinding,
)
from relay.platform_observations import WorldObservationError, WorldObservationService

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


def state(clock: Clock, *, epoch: int = 1, sequence: int = 1) -> dict:
    return {
        "v": 1,
        "type": "state",
        "session": SESSION,
        "t": clock(),
        "event_id": f"state-{sequence}",
        "state_sequence": sequence,
        "roster_version": sequence,
        "selection": [11],
        "drones": [
            {
                "drone_id": 11,
                "connection_epoch": epoch,
                "device_class": "ground_vehicle",
                "membership": "ready",
                "last_seen_at": clock(),
            }
        ],
    }


def submission(*, event_id: str = "pose-1", epoch: int = 1, frame: str = "world") -> dict:
    return {
        "v": 1,
        "type": "observation",
        "event_id": event_id,
        "session": SESSION,
        "device_id": 11,
        "connection_epoch": epoch,
        "source_id": "world-pose",
        "node_type": "ground",
        "frame": frame,
        "confidence": 0.9,
        "t_capture": {"clock_id": "native", "unit": "ns", "value": 100},
        "t_source_receipt": {"clock_id": "native", "unit": "ns", "value": 101},
        "clock_mapping_id": "native-clock",
        "payload": {
            "kind": "pose",
            "pose": {
                "parent_frame": frame,
                "child_frame": "body",
                "x_m": 1.25,
                "y_m": 2.5,
                "z_m": 0.0,
                "qx": 0.0,
                "qy": 0.0,
                "qz": 0.0,
                "qw": 1.0,
            },
        },
    }


def ingress(clock: Clock) -> ObservationIngress:
    return ObservationIngress(
        ObservationConfiguration(
            bindings=(
                SourceBinding(
                    SESSION,
                    11,
                    1,
                    "world-pose",
                    "ground",
                    ("world", "body"),
                    ("pose",),
                    "map-test",
                    "map-v1",
                    "survey-datum",
                    ("native-clock",),
                    producer_role="localization",
                ),
            ),
            frames=FrameRegistry(
                (
                    FrameDeclaration(
                        "world",
                        "world",
                        "right_handed_z_up",
                        "m",
                        map_id="map-test",
                        map_version="map-v1",
                        physical_datum="survey-datum",
                    ),
                    FrameDeclaration(
                        "body", "body", "forward_left_up", "m", SESSION, 11, 1, "world-pose"
                    ),
                )
            ),
            clock_mappings=(
                ClockMapping("native-clock", "native", "ns", 100, clock(), 1, 1_000_000, 1),
            ),
        ),
        SESSION,
        1_000,
    )


@pytest.fixture
def configured(tmp_path: Path):
    clock = Clock()
    service = WorldObservationService(
        registrations={"world-pose": registration()},
        approved_bundle=lambda _session: approved(),
        database=tmp_path / "observations.sqlite3",
        clock=clock,
    )
    return service, clock, ingress(clock)


def admit(
    service: WorldObservationService,
    clock: Clock,
    gate: ObservationIngress,
    *,
    event_id: str = "pose-1",
) -> dict:
    accepted = gate.accept(
        ObservationSubmission.parse(submission(event_id=event_id)),
        now=clock(),
        producer_role="localization",
    )
    receipt_ms, capture_ms = gate.host_times(accepted)
    return service.ingest(
        SESSION, accepted, receipt_ms=receipt_ms, capture_ms=capture_ms, state=state(clock)
    )


def request() -> dict:
    return {"mapVersion": "map-v1", "floorId": "floor-1", "reference": dict(REFERENCE)}


def test_only_registration_configuration_is_accepted(tmp_path: Path) -> None:
    config = {"registrations": {"world-pose": registration()}}
    service = WorldObservationService.from_env(
        {"SWEEP_WORLD_OBSERVATION_SOURCES": json.dumps(config)},
        approved_bundle=lambda _session: approved(),
        database=tmp_path / "obs.sqlite3",
        clock=Clock(),
    )
    assert service.available
    legacy_config = {**config, "sources": {"world-pose": {"legacy": "ignored"}}}
    assert WorldObservationService.from_env(
        {"SWEEP_WORLD_OBSERVATION_SOURCES": json.dumps(legacy_config)},
        approved_bundle=lambda _session: approved(),
        database=tmp_path / "legacy.sqlite3",
        clock=Clock(),
    ).available


def test_admitted_canonical_event_is_preserved_and_projected_with_host_capture_time(
    configured,
) -> None:
    service, clock, gate = configured
    accepted = admit(service, clock, gate)
    assert accepted["payload"]["pose"]["qx"] == 0.0
    assert accepted["t_capture"] == {"clock_id": "native", "unit": "ns", "value": 100}
    projected = service.positions(SESSION, request(), state(clock))["observations"]
    assert projected[0]["position"] == {"x": 1.25, "y": 2.5}
    assert projected[0]["tCapture"] == NOW
    with sqlite3.connect(service.database) as database:
        encoded = database.execute("SELECT payload FROM world_observations").fetchone()[0]
    assert json.loads(encoded) == accepted


def test_legacy_wire_cannot_reach_platform_storage(configured) -> None:
    service, clock, _ = configured
    with pytest.raises(WorldObservationError, match="admitted canonical"):
        service.ingest(SESSION, submission(), receipt_ms=NOW, capture_ms=NOW, state=state(clock))  # type: ignore[arg-type]


def test_ingress_enforces_source_frame_clock_and_epoch_before_world_projection(configured) -> None:
    service, clock, gate = configured
    for changed in (
        {"source_id": "unknown"},
        {"frame": "odom"},
        {"connection_epoch": 2},
        {"clock_mapping_id": None},
    ):
        raw = submission() | changed
        if changed.get("frame") == "odom":
            raw["payload"] = {
                "kind": "pose",
                "pose": {**raw["payload"]["pose"], "parent_frame": "odom"},
            }
        with pytest.raises(ObservationError):
            accepted = gate.accept(
                ObservationSubmission.parse(raw), now=clock(), producer_role="localization"
            )
            gate.host_times(accepted)
    assert service.positions(SESSION, request(), state(clock))["observations"] == []


def test_registration_is_an_additional_host_gate(configured) -> None:
    service, clock, gate = configured
    changed = approved()
    changed["bundle"]["manifest"]["registration"]["transformId"] = "different"
    service.approved_bundle = lambda _session: changed
    accepted = gate.accept(
        ObservationSubmission.parse(submission()), now=clock(), producer_role="localization"
    )
    receipt_ms, capture_ms = gate.host_times(accepted)
    with pytest.raises(WorldObservationError, match="registration does not match"):
        service.ingest(
            SESSION, accepted, receipt_ms=receipt_ms, capture_ms=capture_ms, state=state(clock)
        )


def test_historical_storage_rows_do_not_become_live_observations(configured) -> None:
    service, clock, gate = configured
    admit(service, clock, gate)
    restored = WorldObservationService(
        registrations=service.registrations,
        approved_bundle=lambda _session: approved(),
        database=service.database,
        clock=clock,
    )
    assert restored.positions(SESSION, request(), state(clock))["observations"] == []
