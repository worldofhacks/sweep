from __future__ import annotations

import json

import pytest

from relay.ground_navigation_identity import GroundNavigationIdentityStore
from relay.observations import Observation

IDENTITY = {
    "odom_origin_id": "origin-1",
    "pose_source_id": "qualified-world",
    "registration_id": "registration-1",
    "configuration_sha256": "a" * 64,
}


def observation(*, epoch=1, timestamp=1000, detail=None):
    return Observation.parse(
        {
            "v": 1,
            "type": "observation",
            "event_id": f"identity-{epoch}-{timestamp}",
            "session": "ground-session",
            "device_id": 5,
            "connection_epoch": epoch,
            "source_id": "ground-local",
            "node_type": "ground",
            "frame": "odom-ground-5",
            "confidence": 1.0,
            "t_capture": None,
            "t_source_receipt": {"value": timestamp, "clock_id": "node-clock", "unit": "ms"},
            "clock_mapping_id": None,
            "t_ingest": timestamp,
            "payload": {
                "kind": "status",
                "code": "ground_navigation_identity",
                "detail": json.dumps(IDENTITY if detail is None else detail),
                "capabilities": ["navigate"],
            },
        }
    )


def test_current_ground_identity_requires_matching_epoch_source_and_configuration():
    store = GroundNavigationIdentityStore("ground-session")
    store.accept(observation())
    store.require(5, 1, "ground-local", IDENTITY, now_ms=1100, max_age_ms=500)
    for epoch, source, identity, now in (
        (2, "ground-local", IDENTITY, 1100),
        (1, "other-source", IDENTITY, 1100),
        (1, "ground-local", {**IDENTITY, "odom_origin_id": "origin-2"}, 1100),
        (1, "ground-local", IDENTITY, 1500),
    ):
        with pytest.raises(ValueError, match="identity"):
            store.require(5, epoch, source, identity, now_ms=now, max_age_ms=500)


def test_changed_or_malformed_identity_retires_previous_ground_authority():
    store = GroundNavigationIdentityStore("ground-session")
    store.accept(observation())
    store.accept(observation(timestamp=1010, detail={"configuration_sha256": "b" * 64}))
    with pytest.raises(ValueError, match="identity"):
        store.require(5, 1, "ground-local", IDENTITY, now_ms=1100, max_age_ms=500)


def test_origin_reset_and_restore_cannot_revive_a_frozen_identity_binding():
    store = GroundNavigationIdentityStore("ground-session")
    store.accept(observation())
    generation = store.require(5, 1, "ground-local", IDENTITY, now_ms=1000, max_age_ms=500)
    assert type(generation) is int
    store.accept(observation(timestamp=1010, detail={**IDENTITY, "odom_origin_id": "reset"}))
    store.accept(observation(timestamp=1020))
    with pytest.raises(ValueError, match="identity"):
        store.require(
            5,
            1,
            "ground-local",
            IDENTITY,
            now_ms=1100,
            max_age_ms=500,
            expected_generation=generation,
        )
    fresh = store.require(5, 1, "ground-local", IDENTITY, now_ms=1100, max_age_ms=500)
    assert fresh > generation
