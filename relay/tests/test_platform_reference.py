"""World projection pins the exact approved map reference."""

from pathlib import Path

import pytest

from relay.observation_ingress import ObservationConfiguration, ObservationIngress
from relay.observations import (
    ClockMapping,
    FrameDeclaration,
    FrameRegistry,
    ObservationSubmission,
    SourceBinding,
)
from relay.platform_observations import WorldObservationError, WorldObservationService
from relay.tests.test_platform_observations import SESSION, Clock, registration, state, submission


def test_public_positions_refuse_another_map_with_identical_labels(tmp_path: Path) -> None:
    clock = Clock()
    first = {"bundleId": "first", "revision": "1", "contentHash": "a" * 64}
    second = {"bundleId": "second", "revision": "1", "contentHash": "a" * 64}
    bundle = {
        "reference": first,
        "approval": {"auditId": "approved", "reference": first},
        "bundle": {
            "manifest": {
                "mapVersion": "map-v1",
                "floorId": "floor-1",
                "frame": "world",
                "units": "m",
                "registration": {
                    "sourceFrame": "survey-frame",
                    "transformId": "measured-transform-1",
                },
            }
        },
    }
    service = WorldObservationService(
        registrations={"world-pose": {**registration(), "reference": first}},
        approved_bundle=lambda _session: bundle,
        database=tmp_path / "observations.sqlite3",
        clock=clock,
    )
    ingress = ObservationIngress(
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
                    "first",
                    "map-v1",
                    "datum",
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
                        map_id="first",
                        map_version="map-v1",
                        physical_datum="datum",
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
    accepted = ingress.accept(
        ObservationSubmission.parse(submission()), now=clock(), producer_role="localization"
    )
    receipt, capture = ingress.host_times(accepted)
    service.ingest(SESSION, accepted, receipt_ms=receipt, capture_ms=capture, state=state(clock))
    request = {"mapVersion": "map-v1", "floorId": "floor-1", "reference": second}
    with pytest.raises(WorldObservationError, match="active approved revision"):
        service.positions(SESSION, request, state(clock))
