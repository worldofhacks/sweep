from dataclasses import replace

import pytest

from planner.models import Position
from relay.tests.conftest import membership_payload
from relay.tests.test_navigation_wire import _publisher
from tests.autonomy_fixtures import replace_aircraft


@pytest.mark.parametrize("captured", [False, True])
def test_navigation_keeps_its_pose_when_localization_advances_during_validation(
    monkeypatch, captured
):
    publisher, plan, snapshots, poses, clock = _publisher()
    runtime = publisher.runtime
    snapshot = snapshots[0]
    if captured:
        snapshot = replace(snapshot, control_poses={1: poses[0]})
    assert runtime.check(plan, plan.commands[0], snapshot) is None
    approved_artifact = runtime._approved_artifact

    def update_during_validation():
        artifact = approved_artifact()
        clock.value += 75
        poses[0] = replace(
            poses[0],
            t=clock(),
            event_id="pose-during-validation",
            pose_time_ms=clock() - 50,
            fix_time_ms=clock() - 50,
        )
        return artifact

    monkeypatch.setattr(runtime, "_approved_artifact", update_during_validation)
    assert runtime.check(plan, plan.commands[0], snapshot) is None
    assert poses[0].t > snapshot.now_ms


@pytest.mark.parametrize("captured", [False, True])
def test_arrival_cannot_borrow_a_newer_time_from_a_different_position(monkeypatch, captured):
    publisher, plan, snapshots, poses, _ = _publisher()
    runtime = publisher.runtime
    command = plan.commands[0]
    target = plan.navigation.route.routes[0].swept_segments[0].end.xyz
    snapshot = replace_aircraft(
        replace(snapshots[0], now_ms=100_100),
        1,
        pose=Position(*target),
        position_last_seen_ms=100_100,
    )
    poses[0] = replace(
        poses[0],
        t=100_090,
        pose_time_ms=99_990,
        fix_time_ms=99_990,
        x_mm=round(target[0] * 1000),
        y_mm=round(target[1] * 1000),
        z_mm=round(target[2] * 1000),
    )
    if captured:
        snapshot = replace(snapshot, control_poses={1: poses[0]})
    positions = runtime._positions

    def advance_after_position_check(*args, **kwargs):
        checked = positions(*args, **kwargs)
        poses[0] = replace(
            poses[0],
            t=100_100,
            pose_time_ms=100_050,
            fix_time_ms=100_050,
            x_mm=poses[0].x_mm + 1000,
        )
        return checked

    monkeypatch.setattr(runtime, "_positions", advance_after_position_check)
    refused = runtime.check(plan, command, snapshot, completed=True, issued_at_ms=100_000)
    assert refused is not None
    assert refused.detail == "arrival needs timely position evidence captured after dispatch"


def test_captured_control_poses_cannot_be_replaced_through_the_source_mapping():
    _, _, snapshots, poses, _ = _publisher()
    original = poses[0]
    evidence = {1: original}
    snapshot = replace(snapshots[0], control_poses=evidence)
    evidence.clear()
    assert snapshot.control_poses[1] is original
    with pytest.raises(TypeError):
        snapshot.control_poses[1] = replace(original, t=original.t + 1)
    assert "control_poses" not in snapshot.to_dict()


def test_empty_captured_pose_set_cannot_use_evidence_that_arrived_later():
    publisher, plan, snapshots, _, _ = _publisher()
    snapshot = replace(snapshots[0], control_poses={})
    refused = publisher.runtime.check(plan, plan.commands[0], snapshot)
    assert refused is not None
    assert refused.detail == "navigation requires a ready current-epoch control pose"


def test_navigation_state_copies_only_current_epoch_poses(relay_session, adapter_principal):
    relay_session.process_membership(
        membership_payload(action="join", event_id="navigation-snapshot-join"), adapter_principal
    )
    _, _, _, poses, _ = _publisher()
    pose = replace(poses[0], session=relay_session.session_id)
    relay_session._control_pose[1] = pose
    relay_session._control_pose[2] = replace(pose, drone_id=2)

    state, captured = relay_session.capture_navigation_state()
    relay_session._control_pose[1] = replace(pose, connection_epoch=2)

    assert state["drones"][0]["connection_epoch"] == 1
    assert captured == {1: pose}
    assert relay_session.capture_navigation_state()[1] == {}
