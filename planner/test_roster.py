from dataclasses import replace

import pytest

from planner.models import (
    FleetSnapshot,
    FlightState,
    LossBehavior,
    MembershipState,
    Plan,
    RefusalReason,
    RelayAircraftSafetyEnrichment,
    RelaySnapshotEnrichment,
)
from planner.planner import DeterministicPlanner
from planner.roster import (
    RosterChange,
    RosterChangeKind,
    authorize_graceful_removal,
    reconcile_roster_change,
)
from relay.intent_v1 import IntentName
from tests.autonomy_fixtures import (
    make_aircraft,
    make_intent,
    make_snapshot,
    planning_config,
    replace_aircraft,
)


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"flight_state": FlightState.HOVERING}, RefusalReason.INVALID_STATE),
        ({"armed": True}, RefusalReason.INVALID_STATE),
        ({"active_task_id": "capture-1"}, RefusalReason.ACTIVE_TASK),
    ],
)
def test_graceful_removal_refuses_unsafe_aircraft(
    changes: dict[str, object], reason: RefusalReason
) -> None:
    snapshot = make_snapshot(
        1,
        flight_state=FlightState.DISARMED,
        selection=(1,),
        armed=False,
    )
    snapshot = replace_aircraft(snapshot, 1, **changes)

    authorization = authorize_graceful_removal(snapshot, 1)

    assert authorization.allowed is False
    assert authorization.refusal is not None
    assert authorization.refusal.reason is reason


def test_graceful_removal_clears_selection_and_invalidates_prior_roster_work() -> None:
    previous = make_snapshot(
        2,
        flight_state=FlightState.DISARMED,
        selection=(1, 2),
        armed=False,
    )
    current = replace(previous, roster_version=8, selection=(2,))
    current = replace_aircraft(current, 1, membership=MembershipState.DISCONNECTED)
    intent = make_intent(
        IntentName.SELECT,
        selection=(1, 2),
        args={"ids": (1, 2)},
        intent_id="prior-plan",
    )
    plan = DeterministicPlanner(planning_config()).plan(intent, previous)
    assert isinstance(plan, Plan)

    result = reconcile_roster_change(
        previous,
        current,
        RosterChange(RosterChangeKind.GRACEFUL_REMOVE, 1),
        accepted_plans=(plan,),
        pending_confirmation_versions={"confirm-1": 7, "confirm-current": 8},
        loss_behavior=LossBehavior.HOLD,
    )

    assert result.accepted is True
    assert result.invalidated_plan_ids == (plan.plan_id,)
    assert result.invalidated_confirmation_ids == ("confirm-1",)


def test_reconciliation_consumes_relay_atomic_leave_transition_shape() -> None:
    before_raw = _relay_state_for_leave(
        roster_version=3,
        membership="ready",
        selection=[1],
        pending={"intent_id": "pending-intent"},
        accepted_plan={"intent_id": "accepted-intent", "plan_id": "plan:accepted"},
    )
    after_raw = _relay_state_for_leave(
        roster_version=4,
        membership="leaving",
        selection=[],
        pending=None,
        accepted_plan=None,
    )
    after_raw.update(
        {
            "invalidated_intent_ids": ["pending-intent", "accepted-intent"],
            "invalidation_reason": "graceful_leave_roster_change",
            "prior_roster_version": 3,
            "cleared_control_fields": ["selection", "pending", "accepted_plan"],
        }
    )
    enrichment = RelaySnapshotEnrichment(
        operator_present=True,
        operator_last_seen_ms=1,
        aircraft={
            1: RelayAircraftSafetyEnrichment(
                drone_id=1,
                armed=False,
                physical_rc_available=True,
                storage_remaining_bytes=5_000_000,
                camera_ready=True,
                active_task_id=None,
                position_loss_since_ms=None,
            )
        },
    )
    previous = FleetSnapshot.from_relay_state(before_raw, enrichment=enrichment)
    current = FleetSnapshot.from_relay_state(after_raw, enrichment=enrichment)
    accepted_plan = Plan(
        plan_id="plan:accepted",
        intent_id="accepted-intent",
        intent_name=IntentName.SELECT,
        roster_version=3,
        selection=(1,),
        confirmed=False,
        commands=(),
    )

    result = reconcile_roster_change(
        previous,
        current,
        RosterChange(RosterChangeKind.GRACEFUL_REMOVE, 1),
        accepted_plans=(accepted_plan,),
        pending_confirmation_versions={"pending-intent": 3},
        loss_behavior=LossBehavior.HOLD,
    )

    assert result.accepted is True
    assert result.invalidated_plan_ids == ("plan:accepted",)
    assert result.invalidated_confirmation_ids == ("pending-intent",)
    assert after_raw["invalidated_intent_ids"] == ["pending-intent", "accepted-intent"]
    assert after_raw["invalidation_reason"] == "graceful_leave_roster_change"
    assert current.selection == ()
    assert current.aircraft[1].membership is MembershipState.LEAVING


def test_join_preserves_selection_and_accepted_plans_for_dispatch_validation() -> None:
    previous = make_snapshot(1, selection=(1,))
    joined = dict(previous.aircraft)
    joined[2] = make_aircraft(2, membership=MembershipState.REGISTERED)
    current = replace(previous, roster_version=8, aircraft=joined)
    plan = DeterministicPlanner(planning_config()).plan(
        make_intent(
            IntentName.TRANSLATE,
            selection=(1,),
            args={"dx": 1, "dy": 0},
        ),
        previous,
    )
    assert isinstance(plan, Plan)

    result = reconcile_roster_change(
        previous,
        current,
        RosterChange(RosterChangeKind.JOIN, 2),
        accepted_plans=(plan,),
        pending_confirmation_versions={"pending": 7},
        loss_behavior=LossBehavior.HOLD,
    )

    assert result.accepted is True
    assert result.invalidated_plan_ids == ()
    assert result.invalidated_confirmation_ids == ()
    assert current.selection == previous.selection


def test_unexpected_airborne_loss_stays_visible_and_requests_configured_failsafe() -> None:
    previous = make_snapshot(1, selection=(1,))
    current = replace(previous, roster_version=8)
    current = replace_aircraft(current, 1, membership=MembershipState.DISCONNECTED)

    result = reconcile_roster_change(
        previous,
        current,
        RosterChange(RosterChangeKind.UNEXPECTED_LOSS, 1),
        loss_behavior=LossBehavior.FAILSAFE,
    )

    assert result.accepted is True
    assert current.aircraft[1].airborne is True
    assert result.loss_response is not None
    assert result.loss_response.behavior is LossBehavior.FAILSAFE
    assert result.loss_response.adapter_watchdog_required is True
    assert result.loss_response.physical_rc_preserved is True


def test_rejoin_requires_connection_epoch_increment() -> None:
    previous = make_snapshot(1)
    disconnected = replace_aircraft(previous, 1, membership=MembershipState.DISCONNECTED)
    same_epoch = replace(disconnected, roster_version=8)
    incremented = replace_aircraft(
        same_epoch,
        1,
        membership=MembershipState.REGISTERED,
        connection_epoch=2,
    )

    refused = reconcile_roster_change(
        disconnected,
        same_epoch,
        RosterChange(RosterChangeKind.REJOIN, 1),
        loss_behavior=LossBehavior.HOLD,
    )
    accepted = reconcile_roster_change(
        disconnected,
        incremented,
        RosterChange(RosterChangeKind.REJOIN, 1),
        loss_behavior=LossBehavior.HOLD,
    )

    assert refused.accepted is False
    assert accepted.accepted is True


def _relay_state_for_leave(
    *,
    roster_version: int,
    membership: str,
    selection: list[int],
    pending: dict[str, str] | None,
    accepted_plan: dict[str, str] | None,
) -> dict[str, object]:
    return {
        "v": 1,
        "t": 1,
        "type": "state",
        "event_id": f"state-{roster_version}",
        "session": "session-1",
        "roster_version": roster_version,
        "armed": False,
        "estop": False,
        "selection": selection,
        "formation": "line",
        "spacing": 0.8,
        "mode": "indoor",
        "pending": pending,
        "accepted_plan": accepted_plan,
        "drones": [
            {
                "drone_id": 1,
                "connection_epoch": 1,
                "membership": membership,
                "readiness_reasons": [],
                "flight_state": "disarmed",
                "battery": 0.9,
                "link": 0.9,
                "pos_quality": 0.9,
                "control_authority": True,
                "last_seen_at": 1,
                "camera_patterns": ["pano_360", "reconstruct_8"],
                "selectable": membership == "ready",
                "adapter_id": "sim-1",
                "adapter_capabilities": ["flight", "camera"],
                "home_pose": {"x": 0.0, "y": 0.0, "z": 0.0},
                "rc_safety_operator_present": True,
                "telemetry": {
                    "v": 1,
                    "t": 1,
                    "type": "telemetry",
                    "drone": 1,
                    "x": 0.0,
                    "y": 0.0,
                    "z": 0.0,
                    "vx": 0.0,
                    "vy": 0.0,
                    "vz": 0.0,
                    "battery": 0.9,
                    "state": "disarmed",
                    "link": 0.9,
                    "pos_quality": 0.9,
                },
                "membership_history": [],
            }
        ],
    }
