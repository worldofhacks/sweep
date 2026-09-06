"""The node-local deadman, driven by a clock the test owns."""

from __future__ import annotations

import pytest

from nodekit import protocol
from nodekit.node import ARMED, DISARMED, WATCHDOG_FAILSAFE, WATCHDOG_HOLD, Watchdog

HOLD_MS = 2_000
FAILSAFE_MS = 10_000


class FakeClock:
    def __init__(self) -> None:
        self.seconds = 100.0

    def __call__(self) -> float:
        return self.seconds

    def advance_ms(self, milliseconds: float) -> None:
        self.seconds += milliseconds / 1000


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def watchdog(clock: FakeClock) -> Watchdog:
    return Watchdog(HOLD_MS, FAILSAFE_MS, clock)


def test_a_disarmed_watchdog_never_acts(watchdog: Watchdog, clock: FakeClock) -> None:
    clock.advance_ms(FAILSAFE_MS * 10)

    assert watchdog.state == DISARMED
    assert watchdog.poll() is None
    assert watchdog.wire_state() == protocol.NOMINAL


def test_silence_moves_through_hold_to_failsafe(watchdog: Watchdog, clock: FakeClock) -> None:
    watchdog.arm()

    clock.advance_ms(HOLD_MS - 1)
    assert watchdog.poll() is None
    assert watchdog.wire_state() == protocol.NOMINAL

    clock.advance_ms(1)
    hold = watchdog.poll()
    assert hold is not None
    assert (hold.from_state, hold.to_state) == (ARMED, WATCHDOG_HOLD)
    assert hold.reason == protocol.WATCHDOG_HOLD
    assert hold.elapsed_ms == HOLD_MS
    assert watchdog.wire_state() == protocol.HOLD
    assert watchdog.poll() is None  # one transition, not one per tick

    clock.advance_ms(FAILSAFE_MS - HOLD_MS)
    failsafe = watchdog.poll()
    assert failsafe is not None
    assert (failsafe.from_state, failsafe.to_state) == (WATCHDOG_HOLD, WATCHDOG_FAILSAFE)
    assert failsafe.reason == protocol.WATCHDOG_FAILSAFE
    assert watchdog.wire_state() == protocol.FAILSAFE


def test_a_long_silence_jumps_straight_to_failsafe(watchdog: Watchdog, clock: FakeClock) -> None:
    watchdog.arm()
    clock.advance_ms(FAILSAFE_MS + 1)

    transition = watchdog.poll()

    assert transition is not None
    assert (transition.from_state, transition.to_state) == (ARMED, WATCHDOG_FAILSAFE)


def test_a_heartbeat_during_hold_recovers(watchdog: Watchdog, clock: FakeClock) -> None:
    watchdog.arm()
    clock.advance_ms(HOLD_MS)
    watchdog.poll()

    watchdog.heartbeat()
    recovery = watchdog.poll()

    assert recovery is not None
    assert (recovery.from_state, recovery.to_state, recovery.reason) == (
        WATCHDOG_HOLD,
        ARMED,
        None,
    )
    assert watchdog.wire_state() == protocol.NOMINAL
    clock.advance_ms(HOLD_MS - 1)
    assert watchdog.poll() is None


def test_failsafe_is_terminal_until_the_node_rejoins(watchdog: Watchdog, clock: FakeClock) -> None:
    watchdog.arm()
    clock.advance_ms(FAILSAFE_MS)
    watchdog.poll()

    watchdog.heartbeat()

    assert watchdog.state == WATCHDOG_FAILSAFE
    assert watchdog.poll() is None

    watchdog.arm()  # only a rejoin arms it again
    assert watchdog.state == ARMED
    assert watchdog.wire_state() == protocol.NOMINAL


def test_a_lost_socket_during_hold_trips_failsafe_at_once(
    watchdog: Watchdog, clock: FakeClock
) -> None:
    watchdog.arm()
    clock.advance_ms(HOLD_MS)
    watchdog.poll()

    transition = watchdog.trip_failsafe()

    assert transition is not None
    assert (transition.from_state, transition.to_state) == (WATCHDOG_HOLD, WATCHDOG_FAILSAFE)
    assert watchdog.trip_failsafe() is None


def test_the_relay_thresholds_replace_the_configured_ones(
    watchdog: Watchdog, clock: FakeClock
) -> None:
    watchdog.configure(200, 600)
    watchdog.arm()

    clock.advance_ms(200)
    hold = watchdog.poll()
    clock.advance_ms(400)
    failsafe = watchdog.poll()

    assert hold is not None and hold.to_state == WATCHDOG_HOLD
    assert failsafe is not None and failsafe.to_state == WATCHDOG_FAILSAFE
    with pytest.raises(ValueError):
        watchdog.configure(600, 200)


def test_disarming_stops_the_clock(watchdog: Watchdog, clock: FakeClock) -> None:
    watchdog.arm()
    watchdog.disarm()
    clock.advance_ms(FAILSAFE_MS * 2)

    assert watchdog.poll() is None
    assert watchdog.state == DISARMED
