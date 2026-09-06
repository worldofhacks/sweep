"""Late first loss observations must stop ground devices as well as land aircraft."""

from dataclasses import replace

import pytest

from adapters.dispatch import AdapterDispatcher
from adapters.protocols import AdapterAcknowledgement
from arbiter.safety import SafetyArbiter
from planner.controller import AutonomyController
from planner.models import CommandOperation, DriveState, LifecycleStatus, RefusalReason
from planner.planner import DeterministicPlanner
from tests.autonomy_fixtures import (
    NOW_MS,
    make_mixed_snapshot,
    planning_config,
    replace_aircraft,
    safety_config,
)


class StopRecorder:
    """Only stop operations exist, so accidental motion cannot silently pass."""

    def __init__(self, snapshot, *, fail_ground_hold=False, pending=False):
        self.snapshot = snapshot
        self.fail_ground_hold = fail_ground_hold
        self.pending = pending
        self.calls = []

    def _record(self, ids, operation):
        acknowledgements = []
        for drone_id in ids:
            self.calls.append((drone_id, operation))
            failed = (
                self.fail_ground_hold and drone_id == 11 and operation is CommandOperation.HOVER
            )
            acknowledgements.append(
                AdapterAcknowledgement(
                    drone_id,
                    self.snapshot.aircraft[drone_id].connection_epoch,
                    operation,
                    LifecycleStatus.FAILED
                    if failed
                    else LifecycleStatus.ACCEPTED
                    if self.pending
                    else LifecycleStatus.COMPLETED,
                )
            )
        return tuple(acknowledgements)

    def hover(self, ids):
        return self._record(ids, CommandOperation.HOVER)

    def land(self, ids):
        assert all(drone_id < 11 for drone_id in ids), "a ground robot cannot land"
        return self._record(ids, CommandOperation.LAND)


def stack(snapshot, *, fail_ground_hold=False, pending=False):
    planner = DeterministicPlanner(planning_config())
    arbiter = SafetyArbiter(safety_config())
    flight = StopRecorder(snapshot, fail_ground_hold=fail_ground_hold, pending=pending)
    dispatcher = AdapterDispatcher(flight=flight, camera=object(), arbiter=arbiter)
    controller = AutonomyController(planner=planner, arbiter=arbiter, dispatcher=dispatcher)
    return controller, flight


def lost_snapshot(aircraft_ids, *, future=False):
    snapshot = make_mixed_snapshot(
        aircraft_ids=aircraft_ids, ground_ids=(11, 12, 13), drive_state=DriveState.MOVING
    )
    return replace_aircraft(
        snapshot,
        11,
        position_quality=0.0,
        position_loss_since_ms=NOW_MS + 1_001 if future else NOW_MS - 4_000,
    )


@pytest.mark.parametrize("aircraft_ids", [(), (1, 2)])
@pytest.mark.parametrize("future", [False, True])
def test_first_loss_after_dwell_or_invalid_clock_stops_all_ground_devices(aircraft_ids, future):
    snapshot = lost_snapshot(aircraft_ids, future=future)
    controller, flight = stack(snapshot)

    result = controller.handle_positioning_loss(snapshot)

    assert result.detected is True
    assert result.execution is not None
    assert result.execution.status is LifecycleStatus.COMPLETED, result.execution.refusal
    expected_holds = [(drone_id, CommandOperation.HOVER) for drone_id in sorted(snapshot.aircraft)]
    expected_lands = [(drone_id, CommandOperation.LAND) for drone_id in aircraft_ids]
    assert flight.calls == expected_holds + expected_lands
    if aircraft_ids:
        assert result.action == "land"
        assert result.hold_execution is not None
        assert result.hold_execution.status is LifecycleStatus.COMPLETED
    else:
        assert result.action == "hold"
        assert result.hold_execution is None


def test_ground_hold_failure_is_visible_and_does_not_suppress_aircraft_landing():
    snapshot = lost_snapshot((1, 2))
    controller, flight = stack(snapshot, fail_ground_hold=True)

    result = controller.handle_positioning_loss(snapshot)

    assert result.hold_execution is not None
    assert result.hold_execution.status is LifecycleStatus.FAILED
    assert result.execution is not None
    assert result.execution.status is LifecycleStatus.COMPLETED
    assert flight.calls[-2:] == [(1, CommandOperation.LAND), (2, CommandOperation.LAND)]
    assert {
        drone_id for drone_id, operation in flight.calls if operation is CommandOperation.HOVER
    } == {1, 2, 11, 12, 13}


@pytest.mark.parametrize("aircraft_ids", [(), (1, 2)])
def test_pending_first_ack_does_not_delay_any_ground_stop_before_serialized_landing(aircraft_ids):
    snapshot = lost_snapshot(aircraft_ids)
    controller, flight = stack(snapshot, pending=True)

    result = controller.handle_positioning_loss(snapshot)

    assert flight.calls == [
        *((drone_id, CommandOperation.HOVER) for drone_id in sorted(snapshot.aircraft)),
        *((drone_id, CommandOperation.LAND) for drone_id in aircraft_ids[:1]),
    ]
    assert result.execution.status is LifecycleStatus.EXECUTING
    pending = result.hold_execution or result.execution
    before_calls = list(flight.calls)
    assert len(pending.acknowledgements) == len(pending.plan.commands)
    # Remote devices may finish in any order; completion must never resend a HOLD.
    for ack in reversed(pending.acknowledgements):
        terminal = replace(ack, status=LifecycleStatus.COMPLETED)
        pending = controller.dispatcher.resume_after_completion(
            pending.plan, pending, terminal, snapshot
        )
    assert pending.status is LifecycleStatus.COMPLETED, pending.refusal
    assert flight.calls == before_calls
    if aircraft_ids:
        # Landing remains serialized so HOLD can still cancel an unissued suffix.
        pending = result.execution
        assert len(pending.acknowledgements) == 1
        for _ in aircraft_ids:
            terminal = replace(pending.acknowledgements[-1], status=LifecycleStatus.COMPLETED)
            pending = controller.dispatcher.resume_after_completion(
                pending.plan, pending, terminal, snapshot
            )
        assert pending.status is LifecycleStatus.COMPLETED, pending.refusal
        assert flight.calls == [
            *((drone_id, CommandOperation.HOVER) for drone_id in sorted(snapshot.aircraft)),
            *((drone_id, CommandOperation.LAND) for drone_id in aircraft_ids),
        ]


def test_failed_stop_does_not_skip_other_pending_stops_or_report_success():
    snapshot = lost_snapshot(())
    controller, flight = stack(snapshot, fail_ground_hold=True, pending=True)

    result = controller.handle_positioning_loss(snapshot)

    assert {drone_id for drone_id, _ in flight.calls} == {11, 12, 13}
    assert result.execution.status is LifecycleStatus.FAILED
    assert result.execution.refusal is not None


def test_epoch_change_before_one_stop_does_not_bypass_it_or_skip_other_eligible_devices():
    snapshot = lost_snapshot(())
    changed = replace_aircraft(snapshot, 11, connection_epoch=2)
    controller, flight = stack(snapshot, pending=True)
    plan = controller.planner.fleet_position_loss_plan(
        intent_id="epoch-checked-stops", snapshot=snapshot, land=False
    )
    calls = 0

    def current():
        nonlocal calls
        calls += 1
        return snapshot if calls == 1 else changed

    result = controller.dispatcher.dispatch(plan, snapshot, current_snapshot=current)

    assert flight.calls == [(12, CommandOperation.HOVER), (13, CommandOperation.HOVER)]
    assert result.status is LifecycleStatus.FAILED
    assert result.refusal.reason is RefusalReason.STALE_CONNECTION_EPOCH
    assert {ack.drone_id for ack in result.acknowledgements} == {11, 12, 13}


def test_pending_stop_cannot_accept_a_late_completion_from_an_old_epoch():
    snapshot = lost_snapshot(())
    controller, flight = stack(snapshot, pending=True)
    result = controller.handle_positioning_loss(snapshot).execution
    terminal = replace(result.acknowledgements[0], status=LifecycleStatus.COMPLETED)
    changed = replace_aircraft(snapshot, 11, connection_epoch=2)
    before_calls = list(flight.calls)

    refused = controller.dispatcher.resume_after_completion(result.plan, result, terminal, changed)

    assert refused.status is LifecycleStatus.REFUSED
    assert refused.refusal is not None
    assert flight.calls == before_calls


def test_position_loss_does_not_expand_arbiter_permissions_for_either_stop_phase():
    snapshot = lost_snapshot((1, 2))
    controller, flight = stack(snapshot)
    result = controller.handle_positioning_loss(snapshot)
    hold = result.hold_execution.plan
    land = result.execution.plan
    assert hold is not None and land is not None
    assert {command.operation for command in hold.commands} == {CommandOperation.HOVER}
    assert {command.operation for command in land.commands} == {CommandOperation.LAND}

    malicious_hold = replace(
        hold,
        commands=(replace(hold.commands[0], operation=CommandOperation.GOTO), *hold.commands[1:]),
    )
    malicious_land = replace(
        land,
        commands=(*land.commands, replace(hold.commands[-1], operation=CommandOperation.LAND)),
    )
    before_calls = list(flight.calls)
    for plan in (malicious_hold, malicious_land):
        refused = controller.dispatcher.dispatch(plan, snapshot)
        assert refused.status is LifecycleStatus.REFUSED
        assert refused.refusal is not None
        assert refused.refusal.reason is RefusalReason.INVALID_PLAN
    assert flight.calls == before_calls
