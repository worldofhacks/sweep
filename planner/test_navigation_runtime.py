from dataclasses import replace

import pytest

from planner.models import Plan, Position, Refusal
from planner.navigation_authorization import NavigationApproval
from planner.navigation_runtime import (
    NavigationExecutionConfig,
    NavigationFrame,
    NavigationRuntime,
    navigation_configuration_digest,
)
from planner.test_navigation import MOTION, PERMISSION, artifact
from relay.auth import sign_event
from relay.intent_v1 import IntentName
from tests.autonomy_fixtures import make_intent, make_snapshot, replace_aircraft

KEY = b"navigation-test-approval-key-00001"
IDENTITY = ((1.0, 0.0, 0.0, 0.0), (0.0, 1.0, 0.0, 0.0), (0.0, 0.0, 1.0, 0.0), (0.0, 0.0, 0.0, 1.0))


def setup_runtime(*, transform=IDENTITY):
    geometry = [artifact()]
    config = NavigationExecutionConfig(
        "level_1",
        MOTION,
        0.2,
        0.05,
        500,
        0.5,
        5000,
        (NavigationFrame(1, "measured-enu-world", transform),),
    )
    raw = {
        "v": 1,
        "type": "navigation_approval",
        "approval_id": "operator-approved-test",
        "session": "test-session",
        "mode": "simulation",
        "configuration_sha256": navigation_configuration_digest(
            geometry[0], config, PERMISSION, "atrium"
        ),
        "issued_at_ms": 99000,
        "expires_at_ms": 110000,
        "epochs": [[1, 1]],
        "evidence_sha256": [],
    }
    approval = NavigationApproval.verify({**raw, "signature": sign_event(raw, KEY)}, KEY)
    runtime = NavigationRuntime(
        lambda: geometry[0],
        config,
        PERMISSION,
        approval,
        session="test-session",
        home_zone_id="atrium",
    )
    snapshot = replace_aircraft(make_snapshot(1), 1, pose=Position(0.5, 1.5, 1.0))
    intent = make_intent(IntentName.COME_HOME, selection=(1,), confirm=True)
    return runtime, snapshot, intent, geometry


def test_signed_approval_plans_and_checks_actual_route_without_changing_preview_evidence():
    runtime, snapshot, intent, _ = setup_runtime()
    plan = runtime.prepare(intent, snapshot)
    assert isinstance(plan, Plan)
    assert plan.navigation.route.dispatch_eligible is False
    assert plan.navigation.route.evidence.flight_approved is False
    assert runtime.check(plan, plan.commands[0], snapshot) is None
    assert plan.commands[0].parameters["x"] == 6.5


def test_world_route_converts_back_to_aircraft_enu():
    transform = (
        (0.0, -1.0, 0.0, 2.0),
        (1.0, 0.0, 0.0, 0.0),
        (0.0, 0.0, 1.0, 0.0),
        (0.0, 0.0, 0.0, 1.0),
    )
    runtime, snapshot, intent, _ = setup_runtime(transform=transform)
    snapshot = replace_aircraft(snapshot, 1, pose=Position(1.5, 1.5, 1.0))
    plan = runtime.prepare(intent, snapshot)
    assert isinstance(plan, Plan)
    assert plan.navigation.route.selected[0].pose.xyz == (0.5, 1.5, 1.0)
    assert plan.commands[0].parameters["x"] == 1.5
    assert plan.commands[0].parameters["y"] == -4.5


@pytest.mark.parametrize("change", ["expiry", "epoch", "geometry", "permission", "shape"])
def test_route_refuses_changed_execution_inputs(change):
    runtime, snapshot, intent, geometry = setup_runtime()
    plan = runtime.prepare(intent, snapshot)
    assert isinstance(plan, Plan)
    if change == "expiry":
        snapshot = replace(snapshot, now_ms=110000)
    elif change == "epoch":
        snapshot = replace_aircraft(snapshot, 1, connection_epoch=2)
    elif change == "geometry":
        geometry[0] = artifact(blocked=frozenset({(3, 1)}))
    elif change == "permission":
        runtime.permission = replace(PERMISSION, permitted_zone_ids=frozenset())
    else:
        plan = replace(
            plan,
            commands=(
                replace(plan.commands[0], parameters={**plan.commands[0].parameters, "x": 2.0}),
                *plan.commands[1:],
            ),
        )
    assert isinstance(runtime.check(plan, plan.commands[0], snapshot), Refusal)


def test_completion_requires_fresh_arrival_after_command_issue():
    runtime, snapshot, intent, _ = setup_runtime()
    plan = runtime.prepare(intent, snapshot)
    assert isinstance(plan, Plan)
    command = plan.commands[0]
    assert isinstance(
        runtime.check(plan, command, snapshot, completed=True, issued_at_ms=snapshot.now_ms),
        Refusal,
    )
    arrived = replace_aircraft(snapshot, 1, pose=Position(6.5, 1.5, 1.0))
    assert isinstance(
        runtime.check(plan, command, arrived, completed=True, issued_at_ms=snapshot.now_ms), Refusal
    )
    arrived = replace_aircraft(replace(arrived, now_ms=100100), 1, position_last_seen_ms=100100)
    assert (
        runtime.check(plan, command, arrived, completed=True, issued_at_ms=snapshot.now_ms) is None
    )
    assert isinstance(runtime.check(plan, command, arrived, completed=True), Refusal)
    assert runtime.check(plan, plan.commands[-1], arrived) is None


def test_arrival_rechecks_changed_geometry_and_external_approval():
    runtime, snapshot, intent, geometry = setup_runtime()
    plan = runtime.prepare(intent, snapshot)
    arrived = replace_aircraft(
        replace(snapshot, now_ms=100100),
        1,
        pose=Position(6.5, 1.5, 1.0),
        position_last_seen_ms=100100,
    )
    geometry[0] = artifact(blocked=frozenset({(3, 1)}))
    assert isinstance(
        runtime.check(plan, plan.commands[0], arrived, completed=True, issued_at_ms=100000), Refusal
    )


def test_approval_rejects_tampered_document():
    runtime, _, _, _ = setup_runtime()
    with pytest.raises(ValueError):
        NavigationApproval.verify({"mode": "flight"}, KEY)
    assert runtime.approval.mode == "simulation"


def test_two_aircraft_line_routes_check_each_actual_segment_and_arrival():
    from dataclasses import asdict

    from planner.planner import DeterministicPlanner
    from planner.test_navigation import arrival
    from tests.autonomy_fixtures import planning_config

    runtime, _, _, geometry = setup_runtime()
    geometry[0] = artifact(slots=(arrival("line-a", 6.5, 1.5), arrival("line-b", 6.5, 3.5)))
    runtime.config = replace(
        runtime.config,
        frames=(*runtime.config.frames, NavigationFrame(2, "measured-enu-world-2", IDENTITY)),
        line_zone_id="atrium",
    )
    raw = asdict(runtime.approval)
    raw.update(
        v=1,
        type="navigation_approval",
        epochs=[[1, 1], [2, 1]],
        evidence_sha256=[],
        configuration_sha256=navigation_configuration_digest(
            geometry[0], runtime.config, PERMISSION, "atrium"
        ),
    )
    runtime.approval = NavigationApproval.verify({**raw, "signature": sign_event(raw, KEY)}, KEY)
    snapshot = replace_aircraft(
        replace(make_snapshot(2), spacing=2.0), 1, pose=Position(0.5, 1.5, 1.0)
    )
    snapshot = replace_aircraft(snapshot, 2, pose=Position(2.5, 1.5, 1.0))
    intent = make_intent(IntentName.FORMATION_SET, args={"name": "line"}, confirm=True)
    plan = DeterministicPlanner(planning_config(), navigation_runtime=runtime).plan(
        intent, snapshot
    )
    assert isinstance(plan, Plan)
    assert plan.formation_update == "line"
    assert {route.arrival_slot.slot_id for route in plan.navigation.route.routes} == {
        "line-a",
        "line-b",
    }
    from arbiter.safety import SafetyArbiter
    from tests.autonomy_fixtures import safety_config

    assert SafetyArbiter(safety_config()).check_plan(plan, snapshot) is None
    for command in plan.commands:
        assert runtime.check(plan, command, snapshot) is None
        issued = snapshot.now_ms
        snapshot = replace(snapshot, now_ms=issued + 10)
        changes = {"position_last_seen_ms": snapshot.now_ms}
        if "x" in command.parameters:
            changes["pose"] = Position(*(command.parameters[axis] for axis in ("x", "y", "z")))
        snapshot = replace_aircraft(snapshot, command.drone_id, **changes)
        assert runtime.check(plan, command, snapshot, completed=True, issued_at_ms=issued) is None
    assert sorted(
        (item.pose.x, item.pose.y, item.pose.z) for item in snapshot.aircraft.values()
    ) == [(6.5, 1.5, 1.0), (6.5, 3.5, 1.0)]
    assert isinstance(
        runtime.check(plan, plan.commands[-1], replace(snapshot, spacing=1.0)), Refusal
    )


def test_live_tracking_accepts_progress_and_refuses_departure_from_frozen_segment():
    from relay.tests.test_navigation_wire import _publisher

    publisher, plan, snapshots, poses, clock = _publisher()
    runtime = publisher.runtime
    clock.value = 100100
    poses[0] = replace(poses[0], t=100100, pose_time_ms=100050, fix_time_ms=100050, x_mm=2000)
    current = replace_aircraft(
        replace(snapshots[0], now_ms=100100),
        1,
        pose=Position(2.0, 1.5, 1.0),
        position_last_seen_ms=100100,
    )
    assert isinstance(runtime.check(plan, plan.commands[0], current), Refusal)
    assert runtime.check_tracking(plan, plan.commands[0], current, poses[0]) is None
    poses[0] = replace(poses[0], y_mm=1700)
    current = replace_aircraft(current, 1, pose=Position(2.0, 1.7, 1.0))
    assert isinstance(runtime.check_tracking(plan, plan.commands[0], current, poses[0]), Refusal)


def test_navigation_execution_uses_an_explicit_bounded_aircraft_limit():
    frames = tuple(
        NavigationFrame(drone_id, f"world-{drone_id}", IDENTITY) for drone_id in range(1, 6)
    )

    with pytest.raises(ValueError, match="unique aircraft frames"):
        NavigationExecutionConfig("level_1", MOTION, 0.2, 0.05, 500, 0.5, 5000, frames)

    config = NavigationExecutionConfig(
        "level_1", MOTION, 0.2, 0.05, 500, 0.5, 5000, frames, max_aircraft=5
    )
    assert config.max_aircraft == 5
