from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from types import MappingProxyType

import pytest

from adapters.dispatch import AdapterDispatcher
from adapters.protocols import AdapterAcknowledgement, AdapterError
from adapters.sim.flight import InjectedFlightFailure, SimFlightAdapter
from planner.models import (
    Command,
    CommandOperation,
    FlightState,
    HoldScope,
    LifecycleStatus,
    Plan,
    Position,
    RefusalReason,
)
from planner.planner import DeterministicPlanner
from relay.intent_v1 import IntentName
from tests.autonomy_fixtures import (
    make_intent,
    make_snapshot,
    make_stack,
    planning_config,
    replace_aircraft,
)


def translate_plan(snapshot: object) -> Plan:
    assert hasattr(snapshot, "selection")
    plan = DeterministicPlanner(planning_config()).plan(
        make_intent(
            IntentName.TRANSLATE,
            selection=snapshot.selection,  # type: ignore[attr-defined]
            args={"dx": 1, "dy": 0},
        ),
        snapshot,  # type: ignore[arg-type]
    )
    assert isinstance(plan, Plan)
    return plan


def test_stale_roster_refuses_before_adapter_io() -> None:
    snapshot = make_snapshot(2)
    plan = translate_plan(snapshot)
    current = replace(snapshot, roster_version=snapshot.roster_version + 1)
    _, _, _, dispatcher, flight, camera = make_stack(snapshot)

    result = dispatcher.dispatch(plan, current)

    assert result.status is LifecycleStatus.REFUSED
    assert result.refusal is not None
    assert result.refusal.reason is RefusalReason.STALE_ROSTER
    assert flight.calls == []
    assert camera.calls == []


def test_prior_epoch_command_refuses_before_adapter_io() -> None:
    snapshot = make_snapshot(1, selection=(1,))
    plan = translate_plan(snapshot)
    current = replace_aircraft(snapshot, 1, connection_epoch=2)
    _, _, _, dispatcher, flight, _ = make_stack(snapshot)

    result = dispatcher.dispatch(plan, current)

    assert result.refusal is not None
    assert result.refusal.reason is RefusalReason.STALE_CONNECTION_EPOCH
    assert flight.calls == []


def test_prior_epoch_acknowledgement_is_refused() -> None:
    snapshot = replace_aircraft(make_snapshot(1, selection=(1,)), 1, connection_epoch=2)
    plan = translate_plan(snapshot)
    _, _, _, dispatcher, flight, _ = make_stack(snapshot)
    flight.override_ack_epoch(1, 1)

    result = dispatcher.dispatch(plan, snapshot)

    assert result.status is LifecycleStatus.REFUSED
    assert result.refusal is not None
    assert result.refusal.reason is RefusalReason.STALE_CONNECTION_EPOCH
    assert result.refusal.status is LifecycleStatus.REFUSED
    assert [call.operation for call in flight.calls] == [
        CommandOperation.GOTO,
        CommandOperation.HOVER,
    ]


@pytest.mark.parametrize(
    "changes",
    [
        {"drone_id": True},
        {"connection_epoch": True},
        {"operation": "goto"},
        {"status": "completed"},
        {"detail": 1},
    ],
)
def test_raw_adapter_acknowledgement_requires_strict_runtime_types(
    changes: dict[str, object],
) -> None:
    snapshot = make_snapshot(1, selection=(1,))
    command = translate_plan(snapshot).commands[0]
    _, _, _, dispatcher, _, _ = make_stack(snapshot)
    raw = AdapterAcknowledgement(
        drone_id=1,
        connection_epoch=1,
        operation=CommandOperation.GOTO,
        status=LifecycleStatus.COMPLETED,
    )

    acknowledgement = dispatcher.validate_acknowledgement(
        command,
        replace(raw, **changes),  # type: ignore[arg-type]
        snapshot,
    )

    assert acknowledgement.status is LifecycleStatus.FAILED
    assert acknowledgement.reason is RefusalReason.ADAPTER_FAILURE


def test_live_snapshot_change_mid_capture_stops_before_next_io() -> None:
    snapshot = make_snapshot(1, selection=(1,))
    intent = make_intent(
        IntentName.CAPTURE_ROOM,
        selection=(1,),
        args={"room_id": "room", "capture_id": "capture", "pattern": "pano_360"},
        confirm=True,
    )
    _, planner, _, dispatcher, flight, camera = make_stack(snapshot)
    plan = planner.plan(intent, snapshot)
    assert isinstance(plan, Plan)
    changed = replace(snapshot, roster_version=snapshot.roster_version + 1)

    def provider():  # type: ignore[no-untyped-def]
        return changed if camera.calls else snapshot

    result = dispatcher.dispatch(plan, snapshot, current_snapshot=provider)

    assert result.status is LifecycleStatus.REFUSED
    assert result.refusal is not None
    assert result.refusal.reason is RefusalReason.STALE_ROSTER
    assert camera.calls == [("capabilities", 1, None)]
    assert [call.operation for call in flight.calls] == [CommandOperation.HOVER]


def test_live_epoch_change_mid_capture_stops_before_next_io() -> None:
    snapshot = make_snapshot(1, selection=(1,))
    intent = make_intent(
        IntentName.CAPTURE_ROOM,
        selection=(1,),
        args={"room_id": "room", "capture_id": "capture", "pattern": "pano_360"},
        confirm=True,
    )
    _, planner, _, dispatcher, flight, camera = make_stack(snapshot)
    plan = planner.plan(intent, snapshot)
    assert isinstance(plan, Plan)
    changed = replace_aircraft(snapshot, 1, connection_epoch=2)

    def provider():  # type: ignore[no-untyped-def]
        return changed if camera.calls else snapshot

    result = dispatcher.dispatch(plan, snapshot, current_snapshot=provider)

    assert result.status is LifecycleStatus.REFUSED
    assert result.refusal is not None
    assert result.refusal.reason is RefusalReason.STALE_CONNECTION_EPOCH
    assert camera.calls == [("capabilities", 1, None)]
    assert [call.operation for call in flight.calls] == [CommandOperation.HOVER]


class ExecutingFlight(SimFlightAdapter):
    def goto(
        self, drone_id: int, x: float, y: float, z: float, speed: float
    ) -> AdapterAcknowledgement:
        completed = super().goto(drone_id, x, y, z, speed)
        return replace(completed, status=LifecycleStatus.EXECUTING)


def test_executing_ack_does_not_advance_dependent_or_other_commands() -> None:
    snapshot = make_snapshot(2)
    plan = translate_plan(snapshot)
    _, _, arbiter, _, _, camera = make_stack(snapshot)
    flight = ExecutingFlight.from_snapshot(snapshot)
    dispatcher = AdapterDispatcher(flight=flight, camera=camera, arbiter=arbiter)

    result = dispatcher.dispatch(plan, snapshot)

    assert result.status is LifecycleStatus.EXECUTING
    assert len(flight.calls) == 1
    assert flight.calls[0].drone_ids == (plan.commands[0].drone_id,)


class ExecutingOnceFlight(SimFlightAdapter):
    def __init__(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        super().__init__(*args, **kwargs)
        self._returned_executing = False

    def goto(
        self, drone_id: int, x: float, y: float, z: float, speed: float
    ) -> AdapterAcknowledgement:
        completed = super().goto(drone_id, x, y, z, speed)
        if not self._returned_executing:
            self._returned_executing = True
            return replace(completed, status=LifecycleStatus.EXECUTING)
        return completed


def test_terminal_ack_resumes_without_resending_accepted_command() -> None:
    snapshot = make_snapshot(2)
    plan = translate_plan(snapshot)
    _, _, arbiter, _, _, camera = make_stack(snapshot)
    flight = ExecutingOnceFlight.from_snapshot(snapshot)
    dispatcher = AdapterDispatcher(flight=flight, camera=camera, arbiter=arbiter)
    pending = dispatcher.dispatch(plan, snapshot)
    terminal = replace(
        pending.acknowledgements[-1],
        status=LifecycleStatus.COMPLETED,
    )

    result = dispatcher.resume_after_completion(plan, pending, terminal, snapshot)

    assert result.status is LifecycleStatus.COMPLETED
    assert [call.drone_ids for call in flight.calls] == [
        (command.drone_id,) for command in plan.commands
    ]
    assert [ack.command_id for ack in result.acknowledgements] == [
        plan.commands[0].command_id,
        plan.commands[1].command_id,
    ]


def test_estop_activation_before_resume_blocks_remaining_motion_without_resending() -> None:
    snapshot = make_snapshot(2)
    plan = translate_plan(snapshot)
    _, _, arbiter, _, _, camera = make_stack(snapshot)
    flight = ExecutingOnceFlight.from_snapshot(snapshot)
    dispatcher = AdapterDispatcher(flight=flight, camera=camera, arbiter=arbiter)
    pending = dispatcher.dispatch(plan, snapshot)
    terminal = replace(pending.acknowledgements[-1], status=LifecycleStatus.COMPLETED)
    stopped = replace(snapshot, estop_active=True)

    result = dispatcher.resume_after_completion(plan, pending, terminal, stopped)

    assert result.status is LifecycleStatus.REFUSED
    assert result.refusal is not None
    assert result.refusal.reason is RefusalReason.ESTOP_ACTIVE
    assert sum(call.operation is CommandOperation.GOTO for call in flight.calls) == 1
    assert all(call.drone_ids != (plan.commands[1].drone_id,) for call in flight.calls)


@pytest.mark.parametrize(
    "changes",
    [
        {"roster_version": True},
        {"drone_id": True},
        {"connection_epoch": True},
        {"status": "completed"},
    ],
)
def test_resume_rejects_type_smuggled_domain_ack_without_new_io(
    changes: dict[str, object],
) -> None:
    snapshot = make_snapshot(2)
    plan = translate_plan(snapshot)
    _, _, arbiter, _, _, camera = make_stack(snapshot)
    flight = ExecutingOnceFlight.from_snapshot(snapshot)
    dispatcher = AdapterDispatcher(flight=flight, camera=camera, arbiter=arbiter)
    pending = dispatcher.dispatch(plan, snapshot)
    calls_before_resume = tuple(flight.calls)
    terminal = replace(
        pending.acknowledgements[-1],
        **{"status": LifecycleStatus.COMPLETED, **changes},
    )

    result = dispatcher.resume_after_completion(plan, pending, terminal, snapshot)

    assert result.status is LifecycleStatus.REFUSED
    assert result.refusal is not None
    assert result.refusal.reason is RefusalReason.INVALID_RESUME
    assert tuple(flight.calls) == calls_before_resume


class ExecutingHoverOnceFlight(SimFlightAdapter):
    def __init__(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        super().__init__(*args, **kwargs)
        self._returned_executing = False

    def hover(self, ids: list[int]) -> tuple[AdapterAcknowledgement, ...]:
        completed = super().hover(ids)
        if not self._returned_executing:
            self._returned_executing = True
            return (replace(completed[0], status=LifecycleStatus.EXECUTING),)
        return completed


def test_safety_plan_resume_proves_full_targets_without_resending() -> None:
    snapshot = make_snapshot(2)
    plan = DeterministicPlanner(planning_config()).plan(make_intent(IntentName.HOLD), snapshot)
    assert isinstance(plan, Plan)
    _, _, arbiter, _, _, camera = make_stack(snapshot)
    flight = ExecutingHoverOnceFlight.from_snapshot(snapshot)
    dispatcher = AdapterDispatcher(flight=flight, camera=camera, arbiter=arbiter)
    pending = dispatcher.dispatch(plan, snapshot)
    terminal = replace(pending.acknowledgements[-1], status=LifecycleStatus.COMPLETED)

    result = dispatcher.resume_after_completion(plan, pending, terminal, snapshot)

    assert result.status is LifecycleStatus.COMPLETED
    assert [call.drone_ids for call in flight.calls] == [(1,), (2,)]
    assert [ack.command_id for ack in result.acknowledgements] == [
        plan.commands[0].command_id,
        plan.commands[1].command_id,
    ]


class ExecutingLandOnceFlight(SimFlightAdapter):
    def __init__(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        super().__init__(*args, **kwargs)
        self._returned_executing = False

    def land(self, ids: list[int]) -> tuple[AdapterAcknowledgement, ...]:
        completed = super().land(ids)
        if not self._returned_executing:
            self._returned_executing = True
            return (replace(completed[0], status=LifecycleStatus.EXECUTING),)
        return completed


def test_land_all_resume_accepts_proven_already_landed_target() -> None:
    snapshot = make_snapshot(2)
    plan = DeterministicPlanner(planning_config()).plan(
        make_intent(IntentName.LAND_ALL, confirm=True), snapshot
    )
    assert isinstance(plan, Plan)
    _, _, arbiter, _, _, camera = make_stack(snapshot)
    flight = ExecutingLandOnceFlight.from_snapshot(snapshot)
    dispatcher = AdapterDispatcher(flight=flight, camera=camera, arbiter=arbiter)
    pending = dispatcher.dispatch(plan, snapshot)
    terminal = replace(pending.acknowledgements[-1], status=LifecycleStatus.COMPLETED)
    current = replace_aircraft(
        snapshot,
        1,
        flight_state=FlightState.LANDED,
        armed=False,
        pose=snapshot.aircraft[1].home,
    )

    result = dispatcher.resume_after_completion(plan, pending, terminal, current)

    assert result.status is LifecycleStatus.COMPLETED
    assert [call.drone_ids for call in flight.calls] == [(1,), (2,)]


class ExecutingEstopFlight(SimFlightAdapter):
    def estop(self) -> tuple[AdapterAcknowledgement, ...]:
        completed = super().estop()
        return (replace(completed[0], status=LifecycleStatus.EXECUTING), *completed[1:])


def test_estop_resume_updates_global_ack_set_without_resending() -> None:
    snapshot = make_snapshot(2)
    plan = DeterministicPlanner(planning_config()).plan(make_intent(IntentName.ESTOP), snapshot)
    assert isinstance(plan, Plan)
    _, _, arbiter, _, _, camera = make_stack(snapshot)
    flight = ExecutingEstopFlight.from_snapshot(snapshot)
    dispatcher = AdapterDispatcher(flight=flight, camera=camera, arbiter=arbiter)
    pending = dispatcher.dispatch(plan, snapshot)
    terminal = replace(pending.acknowledgements[0], status=LifecycleStatus.COMPLETED)

    result = dispatcher.resume_after_completion(plan, pending, terminal, snapshot)

    assert result.status is LifecycleStatus.COMPLETED
    assert [call.operation for call in flight.calls] == [CommandOperation.ESTOP]
    assert all(ack.status is LifecycleStatus.COMPLETED for ack in result.acknowledgements)


def test_mid_plan_roster_refusal_holds_already_moved_aircraft() -> None:
    snapshot = make_snapshot(2)
    plan = translate_plan(snapshot)
    changed = replace(snapshot, roster_version=snapshot.roster_version + 1)
    _, _, _, dispatcher, flight, _ = make_stack(snapshot)
    calls = 0

    def provider():  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        return snapshot if calls <= 3 else changed

    result = dispatcher.dispatch(plan, snapshot, current_snapshot=provider)

    assert result.status is LifecycleStatus.REFUSED
    assert result.refusal is not None
    assert result.refusal.reason is RefusalReason.STALE_ROSTER
    assert [call.operation for call in flight.calls] == [
        CommandOperation.GOTO,
        CommandOperation.HOVER,
    ]
    assert all(call.drone_ids != (plan.commands[1].drone_id,) for call in flight.calls)


def test_resumed_plan_refusal_holds_proven_completed_prefix() -> None:
    snapshot = make_snapshot(2)
    plan = translate_plan(snapshot)
    stale_target = plan.commands[1].drone_id
    changed = replace_aircraft(
        snapshot,
        stale_target,
        link_last_seen_ms=snapshot.now_ms - 2_000,
    )
    _, _, arbiter, _, _, camera = make_stack(snapshot)
    flight = ExecutingOnceFlight.from_snapshot(snapshot)
    dispatcher = AdapterDispatcher(flight=flight, camera=camera, arbiter=arbiter)
    pending = dispatcher.dispatch(plan, snapshot)
    terminal = replace(pending.acknowledgements[-1], status=LifecycleStatus.COMPLETED)
    calls = 0

    def provider():  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        return snapshot if calls == 1 else changed

    result = dispatcher.resume_after_completion(
        plan,
        pending,
        terminal,
        snapshot,
        current_snapshot=provider,
    )

    assert result.status is LifecycleStatus.REFUSED
    assert result.refusal is not None
    assert result.refusal.reason is RefusalReason.LINK_STALE
    assert [call.operation for call in flight.calls] == [
        CommandOperation.GOTO,
        CommandOperation.HOVER,
    ]


def test_roster_change_while_waiting_invalidates_and_holds_completed_prefix() -> None:
    snapshot = make_snapshot(2)
    plan = translate_plan(snapshot)
    changed = replace(snapshot, roster_version=snapshot.roster_version + 1)
    _, _, arbiter, _, _, camera = make_stack(snapshot)
    flight = ExecutingOnceFlight.from_snapshot(snapshot)
    dispatcher = AdapterDispatcher(flight=flight, camera=camera, arbiter=arbiter)
    pending = dispatcher.dispatch(plan, snapshot)
    terminal = replace(pending.acknowledgements[-1], status=LifecycleStatus.COMPLETED)

    result = dispatcher.resume_after_completion(
        plan,
        pending,
        terminal,
        snapshot,
        current_snapshot=lambda: changed,
    )

    assert result.status is LifecycleStatus.INVALIDATED
    assert result.refusal is not None
    assert result.refusal.status is LifecycleStatus.INVALIDATED
    assert result.refusal.reason is RefusalReason.STALE_ROSTER
    assert [call.operation for call in flight.calls] == [
        CommandOperation.GOTO,
        CommandOperation.HOVER,
    ]
    assert all(call.drone_ids != (plan.commands[1].drone_id,) for call in flight.calls)


def test_timeout_holds_affected_aircraft_and_continues_safe_other_target() -> None:
    snapshot = make_snapshot(2)
    plan = translate_plan(snapshot)
    _, _, _, dispatcher, flight, _ = make_stack(snapshot)
    failed_id = plan.commands[0].drone_id
    surviving_id = plan.commands[1].drone_id
    flight.inject_failure(failed_id, CommandOperation.GOTO, InjectedFlightFailure.TIMEOUT)

    result = dispatcher.dispatch(plan, snapshot)

    assert result.status is LifecycleStatus.FAILED
    assert result.degraded_aircraft == (failed_id,)
    assert [call.operation for call in flight.calls] == [
        CommandOperation.GOTO,
        CommandOperation.HOVER,
        CommandOperation.GOTO,
    ]
    expected_x = snapshot.aircraft[surviving_id].pose.x + 0.5
    assert flight.aircraft[surviving_id].pose.x == expected_x


def test_timeout_allows_a_bvc_deflected_remaining_target() -> None:
    snapshot = make_snapshot(2)
    first = Command(
        command_id="plan:test:command:0001",
        intent_id="test",
        roster_version=snapshot.roster_version,
        drone_id=1,
        connection_epoch=1,
        operation=CommandOperation.GOTO,
        parameters={"x": 4.0, "y": 0.0, "z": 1.0, "speed": 0.5},
    )
    second = Command(
        command_id="plan:test:command:0002",
        intent_id="test",
        roster_version=snapshot.roster_version,
        drone_id=2,
        connection_epoch=1,
        operation=CommandOperation.GOTO,
        parameters={"x": 0.5, "y": 0.0, "z": 1.0, "speed": 0.5},
    )
    plan = Plan(
        plan_id="plan:test",
        intent_id="test",
        intent_name=IntentName.TRANSLATE,
        roster_version=snapshot.roster_version,
        selection=(1, 2),
        confirmed=True,
        commands=(first, second),
    )
    _, _, _, dispatcher, flight, _ = make_stack(snapshot)
    flight.inject_failure(1, CommandOperation.GOTO, InjectedFlightFailure.TIMEOUT)

    result = dispatcher.dispatch(plan, snapshot)

    assert result.refusal is not None
    assert result.refusal.reason is RefusalReason.ADAPTER_TIMEOUT
    assert [call.operation for call in flight.calls] == [
        CommandOperation.GOTO,
        CommandOperation.HOVER,
        CommandOperation.GOTO,
    ]
    assert flight.aircraft[2].pose.distance_to(flight.aircraft[1].pose) >= 0.8


class MixedEstopFlight(SimFlightAdapter):
    def estop(self) -> tuple[AdapterAcknowledgement, ...]:
        acknowledgements = super().estop()
        return (
            replace(acknowledgements[0], status=LifecycleStatus.EXECUTING),
            replace(acknowledgements[1], status=LifecycleStatus.FAILED),
        )


def test_estop_terminal_failure_is_not_masked_by_an_executing_ack() -> None:
    snapshot = make_snapshot(2)
    plan = DeterministicPlanner(planning_config()).plan(make_intent(IntentName.ESTOP), snapshot)
    assert isinstance(plan, Plan)
    _, _, arbiter, _, _, camera = make_stack(snapshot)
    flight = MixedEstopFlight.from_snapshot(snapshot)
    dispatcher = AdapterDispatcher(flight=flight, camera=camera, arbiter=arbiter)

    result = dispatcher.dispatch(plan, snapshot)

    assert result.status is LifecycleStatus.FAILED
    assert result.plan is not None and result.plan.estop_update is True
    assert result.refusal is not None
    assert result.refusal.drone_id == 2


def test_translate_plan_cannot_smuggle_a_safety_land_command() -> None:
    snapshot = make_snapshot(1, selection=(1,))
    command = Command(
        command_id="plan:malformed:command:0001",
        intent_id="malformed",
        roster_version=snapshot.roster_version,
        drone_id=1,
        connection_epoch=1,
        operation=CommandOperation.LAND,
        safety_action=True,
    )
    plan = Plan(
        plan_id="plan:malformed",
        intent_id="malformed",
        intent_name=IntentName.TRANSLATE,
        roster_version=snapshot.roster_version,
        selection=(1,),
        confirmed=True,
        commands=(command,),
    )
    _, _, _, dispatcher, flight, camera = make_stack(snapshot)

    result = dispatcher.dispatch(plan, snapshot)

    assert result.status is LifecycleStatus.REFUSED
    assert result.refusal is not None
    assert result.refusal.reason is RefusalReason.INVALID_PLAN
    assert flight.calls == []
    assert camera.calls == []


def test_flagged_motion_during_estop_is_refused_before_all_adapter_io() -> None:
    snapshot = replace(make_snapshot(1, selection=(1,)), estop_active=True)
    plan = translate_plan(snapshot)
    flagged = replace(plan.commands[0], safety_action=True)
    _, _, _, dispatcher, flight, camera = make_stack(snapshot)

    result = dispatcher.dispatch(replace(plan, commands=(flagged,)), snapshot)

    assert result.status is LifecycleStatus.REFUSED
    assert result.refusal is not None
    assert result.refusal.reason is RefusalReason.INVALID_PLAN
    assert flight.calls == []
    assert camera.calls == []


def test_malformed_motion_parameters_refuse_before_adapter_io() -> None:
    snapshot = make_snapshot(1, selection=(1,))
    command = Command(
        command_id="plan:malformed-parameters:command:0001",
        intent_id="malformed-parameters",
        roster_version=snapshot.roster_version,
        drone_id=1,
        connection_epoch=1,
        operation=CommandOperation.GOTO,
        parameters={"x": "not-a-number", "y": 0.0, "z": 1.0, "speed": 0.5},
    )
    plan = Plan(
        plan_id="plan:malformed-parameters",
        intent_id="malformed-parameters",
        intent_name=IntentName.TRANSLATE,
        roster_version=snapshot.roster_version,
        selection=(1,),
        confirmed=True,
        commands=(command,),
    )
    _, _, _, dispatcher, flight, camera = make_stack(snapshot)

    result = dispatcher.dispatch(plan, snapshot)

    assert result.status is LifecycleStatus.REFUSED
    assert result.refusal is not None
    assert result.refusal.reason is RefusalReason.INVALID_PLAN
    assert flight.calls == []
    assert camera.calls == []


@pytest.mark.parametrize(
    ("snapshot_change", "aircraft_change"),
    [({"armed": False}, {}), ({}, {"armed": False})],
)
def test_dispatch_rechecks_arm_authorization_before_adapter_io(
    snapshot_change: dict[str, object], aircraft_change: dict[str, object]
) -> None:
    snapshot = make_snapshot(1, selection=(1,))
    plan = translate_plan(snapshot)
    current = replace(snapshot, **snapshot_change)
    if aircraft_change:
        current = replace_aircraft(current, 1, **aircraft_change)
    _, _, _, dispatcher, flight, camera = make_stack(snapshot)

    result = dispatcher.dispatch(plan, current)

    assert result.status is LifecycleStatus.REFUSED
    assert result.refusal is not None
    assert result.refusal.reason is RefusalReason.ARMED_REQUIRED
    assert flight.calls == []
    assert camera.calls == []


@pytest.mark.parametrize(
    ("intent_name", "selection", "target_ids"),
    [
        (IntentName.HOLD, (1, 2), (1,)),
        (IntentName.HOLD, (1,), (1, 1)),
        (IntentName.HOLD, (1,), (2,)),
        (IntentName.LAND_ALL, (1,), (1,)),
        (IntentName.ESTOP, (1,), (1,)),
    ],
)
def test_safety_plan_must_cover_exact_required_aircraft_once(
    intent_name: IntentName,
    selection: tuple[int, ...],
    target_ids: tuple[int, ...],
) -> None:
    snapshot = make_snapshot(2, selection=selection)
    operation = {
        IntentName.HOLD: CommandOperation.HOVER,
        IntentName.LAND_ALL: CommandOperation.LAND,
        IntentName.ESTOP: CommandOperation.ESTOP,
    }[intent_name]
    commands = tuple(
        Command(
            command_id=f"plan:coverage:command:{index:04d}",
            intent_id="coverage",
            roster_version=snapshot.roster_version,
            drone_id=drone_id,
            connection_epoch=1,
            operation=operation,
            safety_action=True,
        )
        for index, drone_id in enumerate(target_ids, start=1)
    )
    plan = Plan(
        plan_id="plan:coverage",
        intent_id="coverage",
        intent_name=intent_name,
        roster_version=snapshot.roster_version,
        selection=selection,
        confirmed=True,
        commands=commands,
        estop_update=True if intent_name is IntentName.ESTOP else None,
    )
    _, _, _, dispatcher, flight, camera = make_stack(snapshot)

    result = dispatcher.dispatch(plan, snapshot)

    assert result.status is LifecycleStatus.REFUSED
    assert result.refusal is not None
    assert result.refusal.reason is RefusalReason.INVALID_PLAN
    assert flight.calls == []
    assert camera.calls == []


def test_plan_selection_change_refuses_before_adapter_io() -> None:
    snapshot = make_snapshot(2, selection=(1,))
    plan = translate_plan(snapshot)
    current = replace(snapshot, selection=(2,))
    _, _, _, dispatcher, flight, camera = make_stack(snapshot)

    result = dispatcher.dispatch(plan, current)

    assert result.status is LifecycleStatus.REFUSED
    assert result.refusal is not None
    assert result.refusal.reason is RefusalReason.STALE_SELECTION
    assert flight.calls == []
    assert camera.calls == []


def test_motion_plan_cannot_smuggle_session_state_update() -> None:
    snapshot = make_snapshot(1, selection=(1,))
    plan = replace(translate_plan(snapshot), armed_update=True)
    _, _, _, dispatcher, flight, camera = make_stack(snapshot)

    result = dispatcher.dispatch(plan, snapshot)

    assert result.status is LifecycleStatus.REFUSED
    assert result.refusal is not None
    assert result.refusal.reason is RefusalReason.INVALID_PLAN
    assert flight.calls == []
    assert camera.calls == []


def test_truthy_non_boolean_confirmation_is_invalid_and_emits_no_io() -> None:
    snapshot = make_snapshot(
        1,
        selection=(1,),
        flight_state=FlightState.DISARMED,
        armed=True,
    )
    planner = DeterministicPlanner(planning_config())
    plan = planner.plan(
        make_intent(IntentName.TAKEOFF, selection=(1,), confirm=True),
        snapshot,
    )
    assert isinstance(plan, Plan)
    malformed = replace(plan, confirmed="yes")  # type: ignore[arg-type]
    _, _, _, dispatcher, flight, camera = make_stack(snapshot)

    result = dispatcher.dispatch(malformed, snapshot)

    assert result.status is LifecycleStatus.REFUSED
    assert result.refusal is not None
    assert result.refusal.reason is RefusalReason.INVALID_PLAN
    assert flight.calls == []
    assert camera.calls == []


@pytest.mark.parametrize(
    "status",
    [
        LifecycleStatus.REFUSED,
        LifecycleStatus.EXECUTING,
        LifecycleStatus.COMPLETED,
        LifecycleStatus.FAILED,
        LifecycleStatus.INVALIDATED,
    ],
)
def test_nonaccepted_plan_status_never_dispatches(status: LifecycleStatus) -> None:
    snapshot = make_snapshot(1, selection=(1,))
    malformed = replace(translate_plan(snapshot), status=status)
    _, _, _, dispatcher, flight, camera = make_stack(snapshot)

    result = dispatcher.dispatch(malformed, snapshot)

    assert result.status is LifecycleStatus.REFUSED
    assert result.refusal is not None
    assert result.refusal.reason is RefusalReason.INVALID_PLAN
    assert flight.calls == []
    assert camera.calls == []


def test_strict_plan_and_command_types_reject_bool_and_container_smuggling() -> None:
    snapshot = make_snapshot(1, selection=(1,))
    plan = translate_plan(snapshot)
    command = plan.commands[0]
    malformed = (
        replace(plan, roster_version=True),  # type: ignore[arg-type]
        replace(plan, selection=[1]),  # type: ignore[arg-type]
        replace(plan, commands=[command]),  # type: ignore[arg-type]
        replace(plan, commands=(replace(command, drone_id=True),)),  # type: ignore[arg-type]
        replace(plan, commands=(replace(command, connection_epoch=True),)),  # type: ignore[arg-type]
        replace(plan, commands=(replace(command, operation="goto"),)),  # type: ignore[arg-type]
        replace(plan, commands=(replace(command, safety_action=1),)),  # type: ignore[arg-type]
    )

    for candidate in malformed:
        _, _, _, dispatcher, flight, camera = make_stack(snapshot)
        result = dispatcher.dispatch(candidate, snapshot)
        assert result.status is LifecycleStatus.REFUSED
        assert result.refusal is not None
        assert result.refusal.reason is RefusalReason.INVALID_PLAN
        assert flight.calls == []
        assert camera.calls == []


@pytest.mark.parametrize("keep_commands", [0, 1])
def test_partial_translate_plan_is_refused_before_adapter_io(keep_commands: int) -> None:
    snapshot = make_snapshot(2)
    plan = translate_plan(snapshot)
    malformed = replace(plan, commands=plan.commands[:keep_commands])
    _, _, _, dispatcher, flight, camera = make_stack(snapshot)

    result = dispatcher.dispatch(malformed, snapshot)

    assert result.status is LifecycleStatus.REFUSED
    assert result.refusal is not None
    assert result.refusal.reason is RefusalReason.INVALID_PLAN
    assert flight.calls == []
    assert camera.calls == []


def test_empty_capture_plan_is_refused_before_adapter_io() -> None:
    snapshot = make_snapshot(1, selection=(1,))
    planner = DeterministicPlanner(planning_config())
    plan = planner.plan(
        make_intent(
            IntentName.CAPTURE_ROOM,
            selection=(1,),
            args={"room_id": "room", "capture_id": "capture", "pattern": "pano_360"},
            confirm=True,
        ),
        snapshot,
    )
    assert isinstance(plan, Plan)
    _, _, _, dispatcher, flight, camera = make_stack(snapshot)

    result = dispatcher.dispatch(replace(plan, commands=()), snapshot)

    assert result.status is LifecycleStatus.REFUSED
    assert result.refusal is not None
    assert result.refusal.reason is RefusalReason.INVALID_PLAN
    assert flight.calls == []
    assert camera.calls == []


def test_live_selection_change_is_rechecked_before_first_adapter_io() -> None:
    snapshot = make_snapshot(2)
    plan = translate_plan(snapshot)
    changed = replace(snapshot, selection=(2,))
    _, _, _, dispatcher, flight, camera = make_stack(snapshot)
    reads = 0

    def provider():  # type: ignore[no-untyped-def]
        nonlocal reads
        reads += 1
        return snapshot if reads == 1 else changed

    result = dispatcher.dispatch(plan, snapshot, current_snapshot=provider)

    assert result.status is LifecycleStatus.REFUSED
    assert result.refusal is not None
    assert result.refusal.reason is RefusalReason.STALE_SELECTION
    assert flight.calls == []
    assert camera.calls == []


@pytest.mark.parametrize(
    ("intent_name", "snapshot_change", "aircraft_change", "reason"),
    [
        (IntentName.ARM, {"operator_present": False}, {}, RefusalReason.OPERATOR_ABSENT),
        (IntentName.SELECT, {"operator_present": False}, {}, RefusalReason.OPERATOR_ABSENT),
        (
            IntentName.ARM,
            {},
            {"flight_state": FlightState.HOVERING, "armed": True},
            RefusalReason.INVALID_STATE,
        ),
    ],
)
def test_zero_command_plan_rechecks_live_safety_state_before_projection_update(
    intent_name: IntentName,
    snapshot_change: dict[str, object],
    aircraft_change: dict[str, object],
    reason: RefusalReason,
) -> None:
    snapshot = make_snapshot(1, selection=(1,), flight_state=FlightState.DISARMED, armed=False)
    _, planner, _, dispatcher, flight, camera = make_stack(snapshot)
    intent = make_intent(
        intent_name,
        selection=() if intent_name is IntentName.ARM else (1,),
        args={} if intent_name is IntentName.ARM else {"ids": (1,)},
    )
    plan = planner.plan(intent, snapshot)
    assert isinstance(plan, Plan)
    current = replace(snapshot, **snapshot_change)
    if aircraft_change:
        current = replace_aircraft(current, 1, **aircraft_change)

    result = dispatcher.dispatch(plan, snapshot, current_snapshot=lambda: current)

    assert result.status is LifecycleStatus.REFUSED
    assert result.refusal is not None
    assert result.refusal.reason is reason
    assert flight.calls == []
    assert camera.calls == []


@pytest.mark.parametrize(
    ("intent_name", "snapshot_change", "aircraft_change", "reason"),
    [
        (IntentName.ARM, {"estop_active": True}, {}, RefusalReason.ESTOP_ACTIVE),
        (IntentName.SELECT, {"estop_active": True}, {}, RefusalReason.ESTOP_ACTIVE),
        (IntentName.ARM, {"operator_present": False}, {}, RefusalReason.OPERATOR_ABSENT),
        (IntentName.SELECT, {"operator_present": False}, {}, RefusalReason.OPERATOR_ABSENT),
        (
            IntentName.ARM,
            {},
            {"flight_state": FlightState.HOVERING, "armed": True},
            RefusalReason.INVALID_STATE,
        ),
    ],
)
def test_zero_command_plan_rechecks_safety_after_initial_preflight(
    intent_name: IntentName,
    snapshot_change: dict[str, object],
    aircraft_change: dict[str, object],
    reason: RefusalReason,
) -> None:
    snapshot = make_snapshot(1, selection=(1,), flight_state=FlightState.DISARMED, armed=False)
    _, planner, _, dispatcher, flight, camera = make_stack(snapshot)
    intent = make_intent(
        intent_name,
        selection=() if intent_name is IntentName.ARM else (1,),
        args={} if intent_name is IntentName.ARM else {"ids": (1,)},
    )
    plan = planner.plan(intent, snapshot)
    assert isinstance(plan, Plan)
    changed = replace(snapshot, **snapshot_change)
    if aircraft_change:
        changed = replace_aircraft(changed, 1, **aircraft_change)
    reads = 0

    def provider():  # type: ignore[no-untyped-def]
        nonlocal reads
        reads += 1
        return snapshot if reads == 1 else changed

    result = dispatcher.dispatch(plan, snapshot, current_snapshot=provider)

    assert result.status is LifecycleStatus.REFUSED
    assert result.refusal is not None
    assert result.refusal.reason is reason
    assert flight.calls == []
    assert camera.calls == []


def test_takeoff_rechecks_explicit_ground_state_before_adapter_io() -> None:
    snapshot = make_snapshot(
        1,
        selection=(1,),
        flight_state=FlightState.DISARMED,
        armed=True,
    )
    planner = DeterministicPlanner(planning_config())
    plan = planner.plan(
        make_intent(IntentName.TAKEOFF, selection=(1,), confirm=True),
        snapshot,
    )
    assert isinstance(plan, Plan)
    emergency = replace_aircraft(snapshot, 1, flight_state=FlightState.EMERGENCY)
    _, _, _, dispatcher, flight, camera = make_stack(snapshot)
    reads = 0

    def provider():  # type: ignore[no-untyped-def]
        nonlocal reads
        reads += 1
        return snapshot if reads == 1 else emergency

    result = dispatcher.dispatch(plan, snapshot, current_snapshot=provider)

    assert result.status is LifecycleStatus.REFUSED
    assert result.refusal is not None
    assert result.refusal.reason is RefusalReason.INVALID_STATE
    assert flight.calls == []
    assert camera.calls == []


def test_sequential_spacing_preflight_deflects_later_collision() -> None:
    snapshot = make_snapshot(3)
    commands = tuple(
        Command(
            command_id=f"plan:spacing:command:{index:04d}",
            intent_id="spacing",
            roster_version=snapshot.roster_version,
            drone_id=drone_id,
            connection_epoch=1,
            operation=CommandOperation.GOTO,
            parameters={"x": x, "y": 0.0, "z": 1.0, "speed": 0.5},
        )
        for index, (drone_id, x) in enumerate(((1, 0.5), (2, 3.5), (3, 4.5)), start=1)
    )
    plan = Plan(
        plan_id="plan:spacing",
        intent_id="spacing",
        intent_name=IntentName.TRANSLATE,
        roster_version=snapshot.roster_version,
        selection=(1, 2, 3),
        confirmed=True,
        commands=commands,
    )
    _, _, _, dispatcher, flight, camera = make_stack(snapshot)

    result = dispatcher.dispatch(plan, snapshot)

    assert result.status is LifecycleStatus.COMPLETED
    assert all(call.operation is CommandOperation.GOTO for call in flight.calls)
    assert camera.calls == []
    positions = {drone_id: aircraft.pose for drone_id, aircraft in flight.aircraft.items()}
    assert all(
        positions[left].distance_to(positions[right]) >= 0.8
        for left in positions
        for right in positions
        if left < right
    )


def test_takeoff_spacing_counts_an_earlier_projected_airborne_peer() -> None:
    snapshot = replace_aircraft(
        make_snapshot(2, flight_state=FlightState.DISARMED, armed=True),
        2,
        pose=Position(0.5, 0.0, 0.0),
        home=Position(0.5, 0.0, 0.0),
    )
    planner = DeterministicPlanner(planning_config())
    plan = planner.plan(
        make_intent(IntentName.TAKEOFF, selection=(1, 2), confirm=True),
        snapshot,
    )
    assert isinstance(plan, Plan)
    _, _, _, dispatcher, flight, camera = make_stack(snapshot)

    result = dispatcher.dispatch(plan, snapshot)

    assert result.status is LifecycleStatus.REFUSED
    assert result.refusal is not None
    assert result.refusal.reason is RefusalReason.SPACING
    assert flight.calls == []
    assert camera.calls == []


def test_planned_group_translate_preserves_spacing_during_sequential_io() -> None:
    snapshot = replace_aircraft(
        make_snapshot(2),
        2,
        pose=Position(1.0, 0.0, 1.0),
        home=Position(1.0, 0.0, 0.0),
    )
    controller, _, _, _, flight, _ = make_stack(snapshot)

    result = controller.execute(
        make_intent(
            IntentName.TRANSLATE,
            selection=(1, 2),
            args={"dx": 1, "dy": 0},
        ),
        snapshot,
    )

    assert result.status is LifecycleStatus.COMPLETED
    assert [call.drone_ids for call in flight.calls] == [(2,), (1,)]
    assert flight.aircraft[1].pose.x < 0.5
    assert flight.aircraft[1].pose.y < 0.0
    assert flight.aircraft[2].pose.x == 1.5


def test_capture_pose_drift_before_first_step_aborts_to_hold() -> None:
    snapshot = make_snapshot(1, selection=(1,))
    planner = DeterministicPlanner(planning_config())
    plan = planner.plan(
        make_intent(
            IntentName.CAPTURE_ROOM,
            selection=(1,),
            args={"room_id": "room", "capture_id": "capture", "pattern": "pano_360"},
            confirm=True,
        ),
        snapshot,
    )
    assert isinstance(plan, Plan)
    drifted = replace_aircraft(snapshot, 1, pose=Position(0.3, 0.0, 1.0))
    _, _, _, dispatcher, flight, camera = make_stack(snapshot)
    reads = 0

    def provider():  # type: ignore[no-untyped-def]
        nonlocal reads
        reads += 1
        return snapshot if reads == 1 else drifted

    result = dispatcher.dispatch(plan, snapshot, current_snapshot=provider)

    assert result.status is LifecycleStatus.REFUSED
    assert result.refusal is not None
    assert result.refusal.reason is RefusalReason.INVALID_STATE
    assert camera.calls == []
    assert [call.operation for call in flight.calls] == [CommandOperation.HOVER]


def test_capture_pose_drift_during_final_retrieve_aborts_to_hold() -> None:
    snapshot = make_snapshot(1, selection=(1,))
    planner = DeterministicPlanner(planning_config())
    plan = planner.plan(
        make_intent(
            IntentName.CAPTURE_ROOM,
            selection=(1,),
            args={"room_id": "room", "capture_id": "capture", "pattern": "pano_360"},
            confirm=True,
        ),
        snapshot,
    )
    assert isinstance(plan, Plan)
    drifted = replace_aircraft(snapshot, 1, pose=Position(5.0, 0.0, 1.0))
    _, _, _, dispatcher, flight, camera = make_stack(snapshot)
    current = snapshot
    retrieve = camera.retrieve

    def drifting_retrieve(drone_id: int, file_id: str):  # type: ignore[no-untyped-def]
        nonlocal current
        result = retrieve(drone_id, file_id)
        current = drifted
        return result

    camera.retrieve = drifting_retrieve  # type: ignore[method-assign]

    result = dispatcher.dispatch(plan, snapshot, current_snapshot=lambda: current)

    assert result.status is LifecycleStatus.REFUSED
    assert result.refusal is not None
    assert result.refusal.reason is RefusalReason.INVALID_STATE
    assert camera.calls[-1] == ("retrieve", 1, "capture-pano-360")
    assert [call.operation for call in flight.calls] == [CommandOperation.HOVER]


def test_capture_plan_cannot_smuggle_pose_tolerance_above_safety_limit() -> None:
    snapshot = make_snapshot(1, selection=(1,))
    planner = DeterministicPlanner(planning_config())
    plan = planner.plan(
        make_intent(
            IntentName.CAPTURE_ROOM,
            selection=(1,),
            args={"room_id": "room", "capture_id": "capture", "pattern": "pano_360"},
            confirm=True,
        ),
        snapshot,
    )
    assert isinstance(plan, Plan)
    capture = plan.commands[3]
    parameters = dict(capture.parameters)
    parameters["pose_tolerance"] = 100.0
    commands = (*plan.commands[:3], replace(capture, parameters=parameters), plan.commands[4])
    _, _, _, dispatcher, flight, camera = make_stack(snapshot)

    result = dispatcher.dispatch(replace(plan, commands=commands), snapshot)

    assert result.status is LifecycleStatus.REFUSED
    assert result.refusal is not None
    assert result.refusal.reason is RefusalReason.INVALID_PLAN
    assert flight.calls == []
    assert camera.calls == []


def test_capture_anchor_is_deeply_immutable() -> None:
    snapshot = make_snapshot(1, selection=(1,))
    planner = DeterministicPlanner(planning_config())
    plan = planner.plan(
        make_intent(
            IntentName.CAPTURE_ROOM,
            selection=(1,),
            args={"room_id": "room", "capture_id": "capture", "pattern": "pano_360"},
            confirm=True,
        ),
        snapshot,
    )
    assert isinstance(plan, Plan)
    approved_pose = plan.commands[3].parameters["approved_pose"]
    assert isinstance(approved_pose, MappingProxyType)

    with pytest.raises(TypeError):
        approved_pose["x"] = 9.0  # type: ignore[index]


def test_command_rejects_nondeterministic_set_parameter() -> None:
    snapshot = make_snapshot(1, selection=(1,))

    with pytest.raises(TypeError, match="ordered"):
        Command(
            command_id="command:set",
            intent_id="set",
            roster_version=snapshot.roster_version,
            drone_id=1,
            connection_epoch=1,
            operation=CommandOperation.GOTO,
            parameters={"values": {1, 2}},
        )


@pytest.mark.parametrize("malformation", ["source_link", "duplicate_yaw", "zero_overlap"])
def test_malformed_capture_sequence_is_refused_before_io(malformation: str) -> None:
    snapshot = make_snapshot(1, selection=(1,))
    planner = DeterministicPlanner(planning_config())
    pattern = "pano_360" if malformation == "source_link" else "reconstruct_8"
    plan = planner.plan(
        make_intent(
            IntentName.CAPTURE_ROOM,
            selection=(1,),
            args={"room_id": "room", "capture_id": "capture", "pattern": pattern},
            confirm=True,
        ),
        snapshot,
    )
    assert isinstance(plan, Plan)
    commands = list(plan.commands)
    if malformation == "source_link":
        commands[4] = replace(commands[4], parameters={"source_command_id": "cross-linked-command"})
    elif malformation == "duplicate_yaw":
        parameters = dict(commands[6].parameters)
        parameters["yaw"] = commands[2].parameters["yaw"]
        commands[6] = replace(commands[6], parameters=parameters)
    else:
        parameters = dict(commands[2].parameters)
        parameters["min_overlap"] = 0.0
        commands[2] = replace(commands[2], parameters=parameters)
    _, _, _, dispatcher, flight, camera = make_stack(snapshot)

    result = dispatcher.dispatch(replace(plan, commands=tuple(commands)), snapshot)

    assert result.status is LifecycleStatus.REFUSED
    assert result.refusal is not None
    assert result.refusal.reason is RefusalReason.INVALID_PLAN
    assert flight.calls == []
    assert camera.calls == []


def test_reconstruct_capture_anchors_must_match_before_io() -> None:
    snapshot = make_snapshot(1, selection=(1,))
    planner = DeterministicPlanner(planning_config())
    plan = planner.plan(
        make_intent(
            IntentName.CAPTURE_ROOM,
            selection=(1,),
            args={"room_id": "room", "capture_id": "capture", "pattern": "reconstruct_8"},
            confirm=True,
        ),
        snapshot,
    )
    assert isinstance(plan, Plan)
    commands = list(plan.commands)
    parameters = dict(commands[8].parameters)
    parameters["approved_pose"] = {"x": 0.05, "y": 0.0, "z": 1.0}
    commands[8] = replace(commands[8], parameters=parameters)
    _, _, _, dispatcher, flight, camera = make_stack(snapshot)

    result = dispatcher.dispatch(replace(plan, commands=tuple(commands)), snapshot)

    assert result.status is LifecycleStatus.REFUSED
    assert result.refusal is not None
    assert result.refusal.reason is RefusalReason.INVALID_PLAN
    assert flight.calls == []
    assert camera.calls == []


@pytest.mark.parametrize(
    "scope",
    [HoldScope.OPERATOR_SELECTION, HoldScope.FLEET_SAFETY, HoldScope.TARGETED_SAFETY],
)
def test_empty_hold_cannot_self_declare_away_eligible_targets(scope: HoldScope) -> None:
    snapshot = make_snapshot(1, selection=())
    plan = Plan(
        plan_id="plan:empty-hold",
        intent_id="empty-hold",
        intent_name=IntentName.HOLD,
        roster_version=snapshot.roster_version,
        selection=(),
        confirmed=True,
        commands=(),
        hold_scope=scope,
    )
    _, _, _, dispatcher, flight, camera = make_stack(snapshot)

    result = dispatcher.dispatch(plan, snapshot)

    assert result.status is LifecycleStatus.REFUSED
    assert result.refusal is not None
    assert result.refusal.reason is RefusalReason.INVALID_PLAN
    assert flight.calls == []
    assert camera.calls == []


def test_non_hold_plan_cannot_smuggle_hold_scope() -> None:
    snapshot = make_snapshot(1, selection=(1,))
    plan = replace(translate_plan(snapshot), hold_scope=HoldScope.FLEET_SAFETY)
    _, _, _, dispatcher, flight, camera = make_stack(snapshot)

    result = dispatcher.dispatch(plan, snapshot)

    assert result.status is LifecycleStatus.REFUSED
    assert result.refusal is not None
    assert result.refusal.reason is RefusalReason.INVALID_PLAN
    assert flight.calls == []
    assert camera.calls == []


class ScopedFlight(SimFlightAdapter):
    """Simulator that, like the remote adapter, only acts inside a bound intent scope."""

    def __init__(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        super().__init__(*args, **kwargs)
        self.scopes: list[tuple[str, int]] = []
        self._bound: tuple[str, int] | None = None

    @contextmanager
    def for_intent(self, intent_id: str, roster_version: int) -> Iterator[None]:
        if self._bound is not None:
            raise AdapterError("an intent context is already bound")
        self._bound = (intent_id, roster_version)
        self.scopes.append(self._bound)
        try:
            yield
        finally:
            self._bound = None

    def goto(
        self, drone_id: int, x: float, y: float, z: float, speed: float
    ) -> AdapterAcknowledgement:
        self._require_scope()
        return super().goto(drone_id, x, y, z, speed)

    def hover(self, ids: list[int]) -> tuple[AdapterAcknowledgement, ...]:
        self._require_scope()
        return super().hover(ids)

    def estop(self) -> tuple[AdapterAcknowledgement, ...]:
        self._require_scope()
        return super().estop()

    def _require_scope(self) -> None:
        if self._bound is None:
            raise AdapterError("no intent context is bound")


def test_dispatch_binds_an_intent_scope_around_every_command_and_safety_hold() -> None:
    snapshot = make_snapshot(2)
    plan = translate_plan(snapshot)
    _, _, arbiter, _, _, camera = make_stack(snapshot)
    flight = ScopedFlight.from_snapshot(snapshot)
    dispatcher = AdapterDispatcher(flight=flight, camera=camera, arbiter=arbiter)
    failed_id = plan.commands[0].drone_id
    flight.inject_failure(failed_id, CommandOperation.GOTO, InjectedFlightFailure.TIMEOUT)

    result = dispatcher.dispatch(plan, snapshot)

    assert result.status is LifecycleStatus.FAILED
    assert result.degraded_aircraft == (failed_id,)
    assert [call.operation for call in flight.calls] == [
        CommandOperation.GOTO,
        CommandOperation.HOVER,
        CommandOperation.GOTO,
    ]
    assert flight.scopes == [
        (plan.intent_id, plan.roster_version),
        (plan.intent_id, snapshot.roster_version),
        (plan.intent_id, plan.roster_version),
    ]


def test_dispatch_binds_the_estop_plan_scope_once_for_the_fleet_stop() -> None:
    snapshot = make_snapshot(2)
    plan = DeterministicPlanner(planning_config()).plan(make_intent(IntentName.ESTOP), snapshot)
    assert isinstance(plan, Plan)
    _, _, arbiter, _, _, camera = make_stack(snapshot)
    flight = ScopedFlight.from_snapshot(snapshot)
    dispatcher = AdapterDispatcher(flight=flight, camera=camera, arbiter=arbiter)

    result = dispatcher.dispatch(plan, snapshot)

    assert result.status is LifecycleStatus.COMPLETED
    assert [call.operation for call in flight.calls] == [CommandOperation.ESTOP]
    assert flight.scopes == [(plan.intent_id, plan.roster_version)]
