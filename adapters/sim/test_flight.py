from threading import Event, Thread, current_thread
from time import sleep

from adapters.protocols import NodeWatchdogState, WatchdogConfig
from adapters.sim.flight import InjectedFlightFailure, SimFlightAdapter
from planner.models import CommandOperation, FlightState, LifecycleStatus, LossBehavior, Position
from tests.autonomy_fixtures import make_snapshot, replace_aircraft


def test_relay_watchdog_holds_then_runs_configured_failsafe() -> None:
    snapshot = make_snapshot(2)
    adapter = SimFlightAdapter.from_snapshot(snapshot)
    config = WatchdogConfig(
        hold_after_ms=2_000,
        failsafe_after_ms=10_000,
        loss_behavior=LossBehavior.FAILSAFE,
    )
    activity = NodeWatchdogState(drone_id=1, connection_epoch=1, last_activity_ms=0)

    assert adapter.apply_node_watchdog(activity, now_ms=1_999, config=config) is None
    assert adapter.apply_node_watchdog(activity, now_ms=2_000, config=config) is LossBehavior.HOLD
    assert adapter.aircraft[1].flight_state is FlightState.HOVERING
    assert adapter.aircraft[2].flight_state is FlightState.HOVERING
    assert (
        adapter.apply_node_watchdog(activity, now_ms=10_000, config=config) is LossBehavior.FAILSAFE
    )
    assert adapter.aircraft[1].flight_state is FlightState.LANDED
    assert adapter.aircraft[2].flight_state is FlightState.HOVERING


def test_node_watchdog_does_not_make_a_grounded_aircraft_airborne() -> None:
    snapshot = make_snapshot(1, flight_state=FlightState.DISARMED, armed=False)
    adapter = SimFlightAdapter.from_snapshot(snapshot)
    config = WatchdogConfig(
        hold_after_ms=2_000,
        failsafe_after_ms=10_000,
        loss_behavior=LossBehavior.FAILSAFE,
    )
    activity = NodeWatchdogState(drone_id=1, connection_epoch=1, last_activity_ms=0)

    assert adapter.apply_node_watchdog(activity, now_ms=2_000, config=config) is LossBehavior.HOLD
    assert adapter.aircraft[1].flight_state is FlightState.DISARMED


def test_land_cannot_restore_a_stale_pose_after_node_failsafe(monkeypatch) -> None:
    snapshot = replace_aircraft(
        make_snapshot(1, flight_state=FlightState.AIRBORNE, armed=True),
        1,
        pose=Position(4.0, 2.0, 1.0),
    )
    adapter = SimFlightAdapter.from_snapshot(snapshot)
    config = WatchdogConfig(
        hold_after_ms=2_000,
        failsafe_after_ms=10_000,
        loss_behavior=LossBehavior.FAILSAFE,
    )
    activity = NodeWatchdogState(drone_id=1, connection_epoch=1, last_activity_ms=0)
    land_entered = Event()
    release_land = Event()
    original_require = adapter._require_aircraft

    def delayed_require(drone_id: int):  # type: ignore[no-untyped-def]
        aircraft = original_require(drone_id)
        if current_thread().name == "delayed-land" and not land_entered.is_set():
            land_entered.set()
            assert release_land.wait(timeout=2)
        return aircraft

    monkeypatch.setattr(adapter, "_require_aircraft", delayed_require)
    land = Thread(target=lambda: adapter.land([1]), name="delayed-land")
    failsafe = Thread(
        target=lambda: adapter.apply_node_watchdog(activity, now_ms=10_000, config=config)
    )
    land.start()
    assert land_entered.wait(timeout=1)
    failsafe.start()
    sleep(0.05)
    release_land.set()
    land.join(timeout=2)
    failsafe.join(timeout=2)

    assert not land.is_alive() and not failsafe.is_alive()
    assert adapter.aircraft[1].pose == adapter.aircraft[1].home
    assert adapter.aircraft[1].flight_state is FlightState.LANDED


def test_estop_latches_every_aircraft_when_one_adapter_times_out() -> None:
    adapter = SimFlightAdapter.from_snapshot(make_snapshot(2))
    adapter.inject_failure(1, CommandOperation.ESTOP, InjectedFlightFailure.TIMEOUT)

    acknowledgements = adapter.estop()
    first_motion = adapter.goto(1, 0.5, 0.0, 1.0, 0.5)
    second_motion = adapter.goto(2, 2.5, 0.0, 1.0, 0.5)

    assert [ack.status for ack in acknowledgements] == [
        LifecycleStatus.FAILED,
        LifecycleStatus.COMPLETED,
    ]
    assert first_motion.status is LifecycleStatus.FAILED
    assert second_motion.status is LifecycleStatus.FAILED


def test_land_remains_available_after_estop() -> None:
    adapter = SimFlightAdapter.from_snapshot(make_snapshot(2))
    adapter.estop()

    acknowledgements = adapter.land([1, 2])

    assert all(ack.status is LifecycleStatus.COMPLETED for ack in acknowledgements)
    assert all(
        aircraft.flight_state is FlightState.LANDED and not aircraft.armed
        for aircraft in adapter.aircraft.values()
    )


def test_estop_preserves_a_grounded_disarmed_state() -> None:
    adapter = SimFlightAdapter.from_snapshot(
        make_snapshot(1, flight_state=FlightState.DISARMED, armed=False)
    )

    adapter.estop()

    assert adapter.aircraft[1].flight_state is FlightState.DISARMED
    assert adapter.aircraft[1].armed is False


def test_rejoin_does_not_clear_an_asserted_estop() -> None:
    adapter = SimFlightAdapter.from_snapshot(make_snapshot(1))
    adapter.estop()
    adapter.update_connection_epoch(1, 2)

    acknowledgement = adapter.goto(1, 0.5, 0.0, 1.0, 0.5)

    assert acknowledgement.status is LifecycleStatus.FAILED
    assert adapter.aircraft[1].pose.x == 0.0
