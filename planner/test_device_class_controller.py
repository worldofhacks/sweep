"""Mixed fleet safety responses exercise the controller, arbiter, and dispatcher."""

from dataclasses import replace

import pytest

from adapters.protocols import AdapterAcknowledgement
from planner.models import CommandOperation, FleetSnapshot, LifecycleStatus
from tests.autonomy_fixtures import NOW_MS, make_mixed_snapshot, make_stack, replace_aircraft


class SafetyDevices:
    """In-memory stop/land endpoints; the real dispatcher validates every call."""

    def __init__(self, snapshot: FleetSnapshot, *, fail_hold: bool = False) -> None:
        self.snapshot = snapshot
        self.fail_hold = fail_hold
        self.calls: list[tuple[CommandOperation, int]] = []
        self.pending = False

    def hover(self, ids: list[int]) -> tuple[AdapterAcknowledgement, ...]:
        return self._acknowledge(ids, CommandOperation.HOVER)

    def land(self, ids: list[int]) -> tuple[AdapterAcknowledgement, ...]:
        assert all(self.snapshot.aircraft[drone_id].airborne for drone_id in ids)
        return self._acknowledge(ids, CommandOperation.LAND)

    def _acknowledge(
        self, ids: list[int], operation: CommandOperation
    ) -> tuple[AdapterAcknowledgement, ...]:
        self.calls.extend((operation, drone_id) for drone_id in ids)
        return tuple(
            AdapterAcknowledgement(
                drone_id,
                self.snapshot.aircraft[drone_id].connection_epoch,
                operation,
                LifecycleStatus.EXECUTING
                if self.pending
                else LifecycleStatus.FAILED
                if self.fail_hold and operation is CommandOperation.HOVER
                else LifecycleStatus.COMPLETED,
            )
            for drone_id in ids
        )


@pytest.mark.parametrize("loss_since_ms", [NOW_MS - 4_000, NOW_MS + 1_001])
def test_ground_position_loss_holds_after_dwell_and_with_invalid_clock(loss_since_ms: int) -> None:
    snapshot = replace_aircraft(
        make_mixed_snapshot(aircraft_ids=()),
        11,
        position_quality=0.0,
        position_loss_since_ms=loss_since_ms,
    )
    controller, _, _, dispatcher, _, _ = make_stack(snapshot)
    devices = SafetyDevices(snapshot)
    dispatcher.flight = devices

    result = controller.handle_positioning_loss(snapshot)

    assert result.detected and result.action == "hold"
    assert result.execution is not None
    assert result.execution.status is LifecycleStatus.COMPLETED
    assert devices.calls == [(CommandOperation.HOVER, 11), (CommandOperation.HOVER, 12)]


@pytest.mark.parametrize("fail_hold", [False, True])
def test_first_late_position_loss_stops_robots_and_still_lands_aircraft(fail_hold: bool) -> None:
    snapshot = replace_aircraft(
        make_mixed_snapshot(), 11, position_last_seen_ms=NOW_MS - 4_000
    )
    # A safety response covers the fleet regardless of the operator's selection.
    snapshot = replace(snapshot, selection=(1,))
    controller, _, _, dispatcher, _, _ = make_stack(snapshot)
    devices = SafetyDevices(snapshot, fail_hold=fail_hold)
    dispatcher.flight = devices

    result = controller.handle_positioning_loss(snapshot)

    assert result.detected and result.action == "land"
    assert result.hold_execution is not None
    assert result.hold_execution.status is (
        LifecycleStatus.FAILED if fail_hold else LifecycleStatus.COMPLETED
    )
    assert result.execution is not None
    assert result.execution.status is LifecycleStatus.COMPLETED
    assert {device_id for op, device_id in devices.calls if op is CommandOperation.HOVER} == {
        1, 2, 11, 12
    }
    assert devices.calls[-2:] == [(CommandOperation.LAND, 1), (CommandOperation.LAND, 2)]


def test_current_position_evidence_does_not_stop_a_mixed_fleet() -> None:
    snapshot = make_mixed_snapshot()
    controller, _, _, dispatcher, _, _ = make_stack(snapshot)
    devices = SafetyDevices(snapshot)
    dispatcher.flight = devices

    result = controller.handle_positioning_loss(snapshot)

    assert not result.detected
    assert result.execution is None and result.hold_execution is None
    assert devices.calls == []


def test_pending_hold_cannot_be_overtaken_by_position_loss_landing() -> None:
    snapshot = replace_aircraft(
        make_mixed_snapshot(), 11, position_last_seen_ms=NOW_MS - 4_000
    )
    controller, _, _, dispatcher, _, _ = make_stack(snapshot)
    devices = SafetyDevices(snapshot)
    devices.pending = True
    dispatcher.flight = devices

    result = controller.handle_positioning_loss(snapshot)

    assert result.action == "hold"
    assert result.execution is not None
    assert result.execution.status is LifecycleStatus.EXECUTING
    assert all(operation is CommandOperation.HOVER for operation, _ in devices.calls)
