from __future__ import annotations

from dataclasses import replace

import pytest

from planner.models import CommandOperation
from relay.state import RegistryError
from relay.tests.conftest import SESSION
from relay.tests.test_ground_node_foundation import _ground_join, _ground_readiness
from relay.tests.test_ground_release import issue
from relay.tests.test_ground_release import ready_session as ready_session


def observe(
    session, clock, event_id, *, source_id="ohmni-pose", frame="odom", session_id=SESSION, epoch=1
):
    session.registry.apply_ground_pose_observation(
        drone_id=9,
        connection_epoch=epoch,
        event_id=event_id,
        session=session_id,
        source_id=source_id,
        frame=frame,
        t=clock(),
    )


def match(session, clock, event_id, *, source_id="ohmni-pose", frame="odom", epoch=1):
    request = _ground_readiness(9, f"ready-{event_id}")
    return session.registry.apply_readiness(
        replace(
            request,
            connection_epoch=epoch,
            pose_identity=replace(
                request.pose_identity,
                event_id=event_id,
                source_id=source_id,
                frame=frame,
                connection_epoch=epoch,
            ),
        ),
        received_at=clock(),
    )


def test_new_pose_keeps_the_previous_fresh_matched_pair_ready_until_readiness_arrives(
    ready_session,
):
    session, clock = ready_session
    qualified = session.registry.ready_ground_identity(9, clock())
    roster = session.registry.roster_version
    clock.advance(100)

    observe(session, clock, "pose-2")

    state = session.current_state()["drones"][0]
    assert state["selectable"] is True
    assert state["readiness_reasons"] == []
    assert state["last_seen_at"] == clock()
    assert session.registry.ready_ground_identity(9, clock()) == qualified
    assert issue(session, CommandOperation.GROUND_VELOCITY)["operation"] == "ground_velocity"

    assert match(session, clock, "pose-2").readiness_reasons == ()
    assert session.registry.ready_ground_identity(9, clock()).event_id == "pose-2"
    assert session.registry.roster_version == roster


def test_unmatched_pose_stream_cannot_extend_the_matched_pairs_expiry(ready_session):
    session, clock = ready_session
    for index in range(1, 6):
        clock.advance(200)
        observe(session, clock, f"unmatched-{index}")
        assert session.registry.ready_ground_identity(9, clock()).event_id == "pose-1"
    clock.advance(1)
    observe(session, clock, "unmatched-final")

    state = session.current_state()["drones"][0]
    assert state["selectable"] is False
    assert state["last_seen_at"] == clock()
    assert state["readiness_reasons"] == ["pose_observation_stale"]
    assert session.registry.ready_ground_identity(9, clock()) is None
    with pytest.raises(RegistryError, match="pose_observation_stale"):
        issue(session, CommandOperation.GROUND_VELOCITY)

    assert match(session, clock, "unmatched-final").readiness_reasons == ()
    assert session.registry.ready_ground_identity(9, clock()).event_id == "unmatched-final"


@pytest.mark.parametrize(
    "provenance",
    [{"source_id": "other-pose"}, {"frame": "other-odom"}, {"session_id": "other-session"}],
)
def test_provenance_roundtrip_cannot_revive_a_pair_without_matching_readiness(
    ready_session, provenance
):
    session, clock = ready_session
    clock.advance(100)
    observe(session, clock, "changed-provenance", **provenance)

    assert session.current_state()["drones"][0]["selectable"] is False
    assert session.registry.ready_ground_identity(9, clock()) is None
    with pytest.raises(RegistryError, match="pose_identity_not_accepted"):
        session.registry.check_ground_release(9, 1, now_ms=clock())

    clock.advance(100)
    observe(session, clock, "restored-provenance")

    assert session.current_state()["drones"][0]["selectable"] is False
    assert session.registry.ready_ground_identity(9, clock()) is None
    with pytest.raises(RegistryError, match="pose_identity_not_accepted"):
        issue(session, CommandOperation.GROUND_VELOCITY)

    assert match(session, clock, "restored-provenance").readiness_reasons == ()
    assert session.registry.ready_ground_identity(9, clock()).event_id == "restored-provenance"


@pytest.mark.parametrize("unmatched", ["never-observed", "pose-1"])
def test_readiness_for_any_noncurrent_candidate_withdraws_the_qualified_pair(
    ready_session, unmatched
):
    session, clock = ready_session
    clock.advance(100)
    observe(session, clock, "pose-2")

    assert match(session, clock, unmatched).readiness_reasons == ("pose_identity_not_accepted",)
    assert session.registry.ready_ground_identity(9, clock()) is None
    with pytest.raises(RegistryError, match="pose_identity_not_accepted"):
        issue(session, CommandOperation.GROUND_VELOCITY)

    assert match(session, clock, "pose-2").readiness_reasons == ()
    assert session.registry.ready_ground_identity(9, clock()).event_id == "pose-2"


def test_clearing_pose_evidence_requires_a_new_observation_and_matching_readiness(ready_session):
    session, clock = ready_session
    session.registry.clear_ground_pose_observation(drone_id=9, connection_epoch=1)

    assert session.registry.ready_ground_identity(9, clock()) is None
    assert match(session, clock, "pose-1").readiness_reasons == ("pose_identity_not_accepted",)
    with pytest.raises(RegistryError, match="pose_identity_not_accepted"):
        issue(session, CommandOperation.GROUND_VELOCITY)

    clock.advance(100)
    observe(session, clock, "new-pose")
    assert session.registry.ready_ground_identity(9, clock()) is None
    assert match(session, clock, "new-pose").readiness_reasons == ()


def test_rejoin_cannot_reuse_the_previous_epochs_pose_or_qualified_pair(ready_session):
    session, clock = ready_session
    session.registry.disconnect(drone_id=9, connection_epoch=1, t=clock(), event_id="disconnected")
    clock.advance(100)
    session.registry.apply_join(replace(_ground_join(9, "rejoined"), t=clock()))

    assert session.registry.ready_ground_identity(9, clock()) is None
    assert match(session, clock, "pose-1", epoch=2).readiness_reasons == (
        "pose_identity_not_accepted",
    )
    with pytest.raises(RegistryError, match="pose_identity_not_accepted"):
        session.registry.check_ground_release(9, 2, now_ms=clock())

    observe(session, clock, "new-epoch-pose", epoch=2)
    assert session.registry.ready_ground_identity(9, clock()) is None
    assert match(session, clock, "new-epoch-pose", epoch=2).readiness_reasons == ()
    session.registry.check_ground_release(9, 2, now_ms=clock())
    assert session.registry.ready_ground_identity(9, clock()).connection_epoch == 2
