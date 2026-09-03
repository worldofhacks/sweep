from dataclasses import replace

import pytest

from adapters.protocols import NodeWatchdogState, WatchdogConfig
from planner.models import FlightState, LifecycleStatus, LossBehavior, MembershipState
from planner.roster import RosterChange, RosterChangeKind, reconcile_roster_change
from relay.intent_v1 import IntentName
from tests.autonomy_fixtures import (
    make_intent,
    make_snapshot,
    make_stack,
    replace_aircraft,
)


def sync_from_sim(snapshot, flight):  # type: ignore[no-untyped-def]
    for drone_id, simulated in flight.aircraft.items():
        snapshot = replace_aircraft(
            snapshot,
            drone_id,
            pose=simulated.pose,
            flight_state=simulated.flight_state,
            armed=simulated.armed,
        )
    return snapshot


@pytest.mark.parametrize("count", [1, 2, 3, 4])
def test_registry_sized_checkpoint_mission_is_deterministic(count: int) -> None:
    ids = tuple(range(1, count + 1))
    snapshot = make_snapshot(
        count,
        selection=(),
        flight_state=FlightState.DISARMED,
        armed=False,
    )
    controller, _, _, _, flight, _ = make_stack(snapshot)

    arm = controller.execute(make_intent(IntentName.ARM, selection=()), snapshot)
    assert arm.status is LifecycleStatus.COMPLETED
    snapshot = replace(snapshot, armed=True)

    select = controller.execute(
        make_intent(IntentName.SELECT, selection=(), args={"ids": ids}), snapshot
    )
    assert select.status is LifecycleStatus.COMPLETED
    snapshot = replace(snapshot, selection=ids)

    takeoff = controller.execute(
        make_intent(IntentName.TAKEOFF, selection=ids, confirm=True), snapshot
    )
    assert takeoff.status is LifecycleStatus.COMPLETED
    snapshot = sync_from_sim(snapshot, flight)

    translate = controller.execute(
        make_intent(
            IntentName.TRANSLATE,
            selection=ids,
            args={"dx": 1, "dy": 0},
        ),
        snapshot,
    )
    assert translate.status is LifecycleStatus.COMPLETED
    snapshot = sync_from_sim(snapshot, flight)

    hold = controller.execute(make_intent(IntentName.HOLD, selection=ids), snapshot)
    assert hold.status is LifecycleStatus.COMPLETED
    snapshot = sync_from_sim(snapshot, flight)

    home = controller.execute(make_intent(IntentName.COME_HOME, selection=ids), snapshot)
    assert home.status is LifecycleStatus.COMPLETED
    snapshot = sync_from_sim(snapshot, flight)

    land = controller.execute(
        make_intent(IntentName.LAND_ALL, selection=(), confirm=True), snapshot
    )
    assert land.status is LifecycleStatus.COMPLETED
    assert all(
        simulated.flight_state is FlightState.LANDED for simulated in flight.aircraft.values()
    )


def test_unexpected_loss_composes_to_the_epoch_bound_node_failsafe() -> None:
    previous = make_snapshot(2)
    current = replace(previous, roster_version=previous.roster_version + 1)
    current = replace_aircraft(current, 1, membership=MembershipState.DISCONNECTED)
    reconciliation = reconcile_roster_change(
        previous,
        current,
        RosterChange(RosterChangeKind.UNEXPECTED_LOSS, 1),
        loss_behavior=LossBehavior.FAILSAFE,
    )
    assert reconciliation.loss_response is not None
    _, _, _, _, flight, _ = make_stack(previous)
    config = WatchdogConfig(
        hold_after_ms=2_000,
        failsafe_after_ms=10_000,
        loss_behavior=LossBehavior.FAILSAFE,
    )

    node_activity = NodeWatchdogState(
        drone_id=1,
        connection_epoch=previous.aircraft[1].connection_epoch,
        last_activity_ms=previous.now_ms,
    )
    holding = flight.apply_node_watchdog(
        node_activity,
        now_ms=previous.now_ms + 2_000,
        config=config,
    )
    failed_safe = flight.apply_node_watchdog(
        node_activity,
        now_ms=previous.now_ms + 10_000,
        config=config,
    )

    assert reconciliation.loss_response.adapter_watchdog_required is True
    assert reconciliation.loss_response.drone_id == node_activity.drone_id
    assert reconciliation.loss_response.connection_epoch == node_activity.connection_epoch
    assert reconciliation.loss_response.behavior is config.loss_behavior
    assert holding is LossBehavior.HOLD
    assert failed_safe is LossBehavior.FAILSAFE
    assert flight.aircraft[1].flight_state is FlightState.LANDED
    assert flight.aircraft[1].pose == flight.aircraft[1].home
    assert flight.aircraft[2].flight_state is FlightState.HOVERING
