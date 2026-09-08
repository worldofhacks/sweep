"""A signed VS-only loss may admit LAND, never move or replace RC authority."""

from dataclasses import replace

import pytest

from arbiter.safety import SafetyArbiter
from planner.models import AircraftState, LandingRecoveryEvidence, Plan
from planner.planner import DeterministicPlanner
from relay.intent_v1 import IntentName
from relay.supervised_vertical import SupervisedVerticalArbiter, SupervisedVerticalPlanner
from relay.tests.test_supervised_vertical import _vertical_config
from tests.autonomy_fixtures import make_intent, make_snapshot, planning_config, safety_config


@pytest.fixture(params=["world", "vertical"])
def recovery_stack(request):
    if request.param == "world":
        return DeterministicPlanner(planning_config()), SafetyArbiter(safety_config())
    config = _vertical_config()
    return SupervisedVerticalPlanner(config), SupervisedVerticalArbiter(config)


def recovery_snapshot():
    snapshot = make_snapshot(1, selection=(1,))
    aircraft = replace(
        snapshot.aircraft[1],
        control_authority=False,
        landing_recovery=LandingRecoveryEvidence(snapshot.now_ms, "virtual_stick_dropped"),
    )
    return replace(snapshot, aircraft={1: aircraft})


def test_land_recovery_survives_each_admission_boundary(recovery_stack):
    planner, arbiter = recovery_stack
    snapshot = recovery_snapshot()
    intent = make_intent(IntentName.LAND, selection=(1,), confirm=True)
    assert arbiter.check_intent(intent, snapshot) is None
    plan = planner.plan(intent, snapshot)
    assert isinstance(plan, Plan)
    assert arbiter.check_plan(plan, snapshot) is None
    assert arbiter.check_command(plan, plan.commands[0], snapshot) is None
    assert snapshot.aircraft[1].control_authority is False
    assert AircraftState.from_mapping(snapshot.aircraft[1].to_dict()) == snapshot.aircraft[1]


@pytest.mark.parametrize(
    "change",
    [
        {"landing_recovery": None},
        {"physical_rc_available": False},
        {"rc_safety_operator_present": False},
        {"link_quality": 0.0},
        {"link_last_seen_ms": 0},
    ],
)
def test_recovery_preserves_required_safety_evidence(recovery_stack, change):
    planner, arbiter = recovery_stack
    snapshot = recovery_snapshot()
    snapshot = replace(snapshot, aircraft={1: replace(snapshot.aircraft[1], **change)})
    intent = make_intent(IntentName.LAND, selection=(1,), confirm=True)
    assert arbiter.check_intent(intent, snapshot) is not None
    fresh = recovery_snapshot()
    plan = planner.plan(intent, fresh)
    assert isinstance(plan, Plan)
    assert arbiter.check_command(plan, plan.commands[0], snapshot) is not None


@pytest.mark.parametrize("offset", [-6000, 1001])
def test_recovery_rejects_stale_or_future_node_status(recovery_stack, offset):
    _, arbiter = recovery_stack
    snapshot = recovery_snapshot()
    aircraft = replace(
        snapshot.aircraft[1],
        landing_recovery=LandingRecoveryEvidence(
            snapshot.now_ms + offset,
            "virtual_stick_dropped",
        ),
    )
    snapshot = replace(snapshot, aircraft={1: aircraft})
    assert (
        arbiter.check_intent(make_intent(IntentName.LAND, selection=(1,), confirm=True), snapshot)
        is not None
    )


def test_recovery_does_not_remove_operator_confirmation(recovery_stack):
    _, arbiter = recovery_stack
    assert (
        arbiter.check_intent(make_intent(IntentName.LAND, selection=(1,)), recovery_snapshot())
        is not None
    )


@pytest.mark.parametrize(
    "name,args",
    [
        (IntentName.TAKEOFF, {}),
        (IntentName.TRANSLATE, {"dx": 1, "dy": 0}),
        (IntentName.ALTITUDE, {"delta": 1}),
    ],
)
def test_recovery_never_grants_motion(recovery_stack, name, args):
    _, arbiter = recovery_stack
    assert (
        arbiter.check_intent(
            make_intent(name, selection=(1,), args=args, confirm=True), recovery_snapshot()
        )
        is not None
    )


@pytest.mark.parametrize(
    "reason", ["rc_takeover", "rc_pause", "not_granted", "virtual_stick_authority_lost", ""]
)
def test_recovery_model_cannot_describe_other_authority_loss(reason):
    with pytest.raises(ValueError, match="virtual_stick_dropped"):
        LandingRecoveryEvidence(1000, reason)
