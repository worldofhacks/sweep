from dataclasses import replace

from adapters.dispatch import AdapterDispatcher
from adapters.sim.camera import SimCamera
from adapters.test_dispatch import ExecutingOnceFlight
from arbiter.safety import SafetyArbiter
from planner.controller import AutonomyController
from planner.models import LifecycleStatus, Plan, Position, PreparedExecution, RefusalReason
from planner.planner import DeterministicPlanner
from planner.test_navigation import artifact
from planner.test_navigation_runtime import setup_runtime
from tests.autonomy_fixtures import camera_config, planning_config, replace_aircraft, safety_config


def _camera(snapshot, flight):
    return SimCamera(
        drone_epochs={
            drone_id: aircraft.connection_epoch for drone_id, aircraft in snapshot.aircraft.items()
        },
        pose_provider=flight.camera_pose,
        config=camera_config(),
    )


def test_navigation_resume_rechecks_each_arrival_with_the_original_issue_time() -> None:
    runtime, snapshot, intent, _ = setup_runtime()
    plan = runtime.prepare(intent, snapshot)
    assert isinstance(plan, Plan)
    flight = ExecutingOnceFlight.from_snapshot(snapshot)
    dispatcher = AdapterDispatcher(
        flight=flight,
        camera=_camera(snapshot, flight),
        arbiter=SafetyArbiter(safety_config()),
        navigation_runtime=runtime,
    )
    arrived = replace_aircraft(
        replace(snapshot, now_ms=100_050),
        1,
        pose=Position(6.5, 1.5, 1.0),
        position_last_seen_ms=100_050,
    )
    hovered = replace(arrived, now_ms=100_100)
    hovered = replace_aircraft(hovered, 1, position_last_seen_ms=100_100)

    def current():
        if len(flight.calls) == 0:
            return snapshot
        return arrived if len(flight.calls) == 1 else hovered

    pending = dispatcher.dispatch(plan, snapshot, current_snapshot=current)
    assert pending.status is LifecycleStatus.EXECUTING
    terminal = replace(pending.acknowledgements[-1], status=LifecycleStatus.COMPLETED)

    result = dispatcher.resume_after_completion(
        plan, pending, terminal, snapshot, current_snapshot=current
    )

    assert result.status is LifecycleStatus.COMPLETED
    assert [call.operation.value for call in flight.calls] == ["goto", "hover"]


def test_navigation_completion_without_the_original_issue_time_is_invalidated() -> None:
    runtime, snapshot, intent, _ = setup_runtime()
    plan = runtime.prepare(intent, snapshot)
    assert isinstance(plan, Plan)
    flight = ExecutingOnceFlight.from_snapshot(snapshot)
    dispatcher = AdapterDispatcher(
        flight=flight,
        camera=_camera(snapshot, flight),
        arbiter=SafetyArbiter(safety_config()),
        navigation_runtime=runtime,
    )
    pending = dispatcher.dispatch(plan, snapshot)
    terminal = replace(pending.acknowledgements[-1], status=LifecycleStatus.COMPLETED)
    resumed = AdapterDispatcher(
        flight=flight,
        camera=_camera(snapshot, flight),
        arbiter=SafetyArbiter(safety_config()),
        navigation_runtime=runtime,
    )

    result = resumed.resume_after_completion(plan, pending, terminal, snapshot)

    assert result.status is LifecycleStatus.INVALIDATED
    assert result.refusal is not None
    assert result.refusal.reason is RefusalReason.ADAPTER_TIMEOUT
    assert [call.operation.value for call in flight.calls] == ["goto", "hover"]


def test_controller_wires_and_revalidates_the_frozen_navigation_execution() -> None:
    runtime, snapshot, intent, geometry = setup_runtime()
    flight = ExecutingOnceFlight.from_snapshot(snapshot)
    dispatcher = AdapterDispatcher(
        flight=flight,
        camera=_camera(snapshot, flight),
        arbiter=SafetyArbiter(safety_config()),
    )
    controller = AutonomyController(
        planner=DeterministicPlanner(planning_config(), navigation_runtime=runtime),
        arbiter=SafetyArbiter(safety_config()),
        dispatcher=dispatcher,
    )
    prepared = controller.prepare(intent, snapshot)
    assert isinstance(prepared, PreparedExecution)
    assert prepared.plan.confirmed
    assert prepared.plan.navigation is not None
    assert dispatcher.navigation_runtime is runtime
    geometry[0] = artifact(blocked=frozenset({(3, 1)}))

    result = controller.dispatch_prepared(prepared)

    assert result.status is LifecycleStatus.REFUSED
    assert result.refusal is not None
    assert flight.calls == []


def test_simulation_approval_never_reaches_a_non_simulation_flight_adapter() -> None:
    runtime, snapshot, intent, _ = setup_runtime()
    plan = runtime.prepare(intent, snapshot)
    assert isinstance(plan, Plan)
    dispatcher = AdapterDispatcher(
        flight=object(),
        camera=object(),
        arbiter=SafetyArbiter(safety_config()),
        navigation_runtime=runtime,
    )

    result = dispatcher.dispatch(plan, snapshot)

    assert result.status is LifecycleStatus.REFUSED
    assert result.refusal is not None
    assert result.refusal.reason is RefusalReason.INVALID_PLAN
