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
