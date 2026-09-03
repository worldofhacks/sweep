from adapters.protocols import NodeWatchdogState, WatchdogConfig
from adapters.sim.flight import SimFlightAdapter
from planner.models import FlightState, LossBehavior
from tests.autonomy_fixtures import make_snapshot


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
