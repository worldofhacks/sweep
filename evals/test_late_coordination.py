from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import planner.relay_bridge as bridge_module
from evals.test_m14_button_to_sim import CONSOLE_KEY, SESSION, Clock, Harness
from planner.models import FlightState
from relay.auth import Principal
from relay.session import RelaySession
from tests.autonomy_fixtures import make_snapshot


@pytest.fixture
def airborne_session(tmp_path: Path) -> Iterator[tuple[Harness, RelaySession]]:
    harness = Harness(
        tmp_path,
        make_snapshot(1, selection=(), now_ms=Clock().value),
        auto_start_nodes=True,
    )
    with TestClient(harness.app):
        session = harness.app.state.relay_runtime.session(SESSION)
        for intent in (
            harness.intent("arm", selection=[]),
            harness.intent("select", selection=[], args={"ids": [1]}),
        ):
            _admit(session, intent)
            assert _execute(session, intent)["status"] == "completed"
        harness.flight.calls.clear()
        yield harness, session


def _admit(session: RelaySession, intent: dict[str, object]) -> None:
    source = "keyboard" if intent["source"] == "keyboard" else "console"
    outcome = session.process_frame(intent, Principal(source, None, CONSOLE_KEY))
    assert outcome[0]["status"] == "accepted", outcome


def _execute(session: RelaySession, intent: dict[str, object]) -> dict[str, object]:
    intent_id = intent["intent_id"]
    assert isinstance(intent_id, str)
    session.mark_pending_intent_delivered(intent_id)
    outcomes = session.execute_pending_intent(intent_id)
    return next(
        event
        for event in reversed(outcomes)
        if event.get("intent_id") == intent_id
        and event.get("status") in {"completed", "refused", "invalidated", "failed"}
    )


def _fail_delivery(session: RelaySession, intent: dict[str, object]) -> None:
    intent_id = intent["intent_id"]
    assert isinstance(intent_id, str)
    outcome = session.fail_pending_intent(
        intent_id,
        reason="acceptance_delivery_failed",
        detail="acceptance socket closed before delivery",
    )
    assert outcome[-1]["reason"] == "acceptance_delivery_failed", outcome


@pytest.mark.parametrize(
    ("stop_name", "recovery_name", "expected_calls"),
    [
        ("hold", "come_home", ["hover", "goto"]),
        ("estop", "land_all", ["estop", "land"]),
        ("hold", "land", ["hover", "land"]),
        ("estop", "land", ["estop", "land"]),
    ],
)
def test_completed_stop_allows_immediate_explicit_recovery(
    airborne_session: tuple[Harness, RelaySession],
    stop_name: str,
    recovery_name: str,
    expected_calls: list[str],
) -> None:
    harness, session = airborne_session
    stop = harness.intent(stop_name, selection=[1] if stop_name == "hold" else [])
    _admit(session, stop)
    assert _execute(session, stop)["status"] == "completed"
    harness.clock.advance(1)
    recovery = harness.intent(
        recovery_name,
        selection=[1] if recovery_name in {"come_home", "land"} else [],
        confirm=recovery_name in {"land", "land_all"},
    )
    _admit(session, recovery)

    outcome = _execute(session, recovery)

    assert outcome["status"] == "completed", outcome
    assert [call.operation.value for call in harness.flight.calls] == expected_calls
    aircraft = harness.flight.aircraft[1]
    if recovery_name in {"land", "land_all"}:
        assert aircraft.flight_state is FlightState.LANDED
        assert aircraft.armed is False
        assert session.current_state()["estop"] is (stop_name == "estop")
    else:
        assert aircraft.pose.x == aircraft.home.x
        assert aircraft.pose.y == aircraft.home.y


@pytest.mark.parametrize(
    ("stop_name", "recovery_name", "operation"),
    [
        ("hold", "come_home", "hover"),
        ("estop", "land_all", "estop"),
        ("hold", "land", "hover"),
        ("estop", "land", "estop"),
    ],
)
@pytest.mark.parametrize("stop_delay_ms", [0, 1])
def test_precreated_recovery_at_or_before_completed_stop_stays_superseded(
    airborne_session: tuple[Harness, RelaySession],
    stop_name: str,
    recovery_name: str,
    operation: str,
    stop_delay_ms: int,
) -> None:
    harness, session = airborne_session
    recovery = harness.intent(
        recovery_name,
        selection=[1] if recovery_name in {"come_home", "land"} else [],
        confirm=recovery_name in {"land", "land_all"},
    )
    harness.clock.advance(stop_delay_ms)
    stop = harness.intent(stop_name, selection=[1] if stop_name == "hold" else [])
    _admit(session, stop)
    assert _execute(session, stop)["status"] == "completed"

    _admit(session, recovery)
    outcome = _execute(session, recovery)

    assert outcome["status"] == "invalidated", outcome
    assert outcome["reason"] == "superseded"
    assert [call.operation.value for call in harness.flight.calls] == [operation]
    assert harness.flight.aircraft[1].flight_state is FlightState.HOVERING


@pytest.mark.parametrize(("name", "operation"), [("hold", "hover"), ("land_all", "land")])
def test_undelivered_hold_cannot_erase_delivered_recovery(
    airborne_session: tuple[Harness, RelaySession], name: str, operation: str
) -> None:
    harness, session = airborne_session
    recovery = harness.intent(
        name, selection=[1] if name == "hold" else [], confirm=name == "land_all"
    )
    _admit(session, recovery)
    harness.clock.advance(100)
    reserved = harness.intent("hold", selection=[1], source="keyboard")
    _admit(session, reserved)

    completed = _execute(session, recovery)
    _fail_delivery(session, reserved)

    assert completed["status"] == "completed", completed
    assert [call.operation.value for call in harness.flight.calls] == [operation]


def test_undelivered_estop_preserves_conflicting_motion_hold(
    airborne_session: tuple[Harness, RelaySession],
) -> None:
    harness, session = airborne_session
    motions = [
        harness.intent("translate", selection=[1], args={"dx": dx, "dy": dy})
        for dx, dy in ((1, 0), (0, 1))
    ]
    for motion in motions:
        _admit(session, motion)
        session.mark_pending_intent_delivered(str(motion["intent_id"]))
    harness.clock.advance(100)
    reserved = harness.intent("estop", selection=[], source="keyboard")
    _admit(session, reserved)

    outcomes = [_execute(session, motion) for motion in motions]
    _fail_delivery(session, reserved)

    assert [outcome.get("reason") for outcome in outcomes] == ["conflicting_motion"] * 2
    assert [call.operation.value for call in harness.flight.calls] == ["hover"]
    assert session.current_state()["estop"] is False


def test_failed_newest_estop_does_not_consume_an_older_reserved_estop(
    airborne_session: tuple[Harness, RelaySession],
) -> None:
    harness, session = airborne_session
    hold = harness.intent("hold", selection=[1])
    _admit(session, hold)
    reservations = []
    for _ in range(2):
        harness.clock.advance(100)
        estop = harness.intent("estop", selection=[], source="keyboard")
        _admit(session, estop)
        reservations.append(estop)

    held = _execute(session, hold)
    _fail_delivery(session, reservations[1])
    stopped = _execute(session, reservations[0])

    assert held["status"] == "completed", held
    assert stopped["status"] == "completed", stopped
    assert session.current_state()["estop"] is True
    assert [call.operation.value for call in harness.flight.calls] == ["hover", "estop"]


def test_precreated_motion_arriving_after_completed_hold_cannot_move(
    airborne_session: tuple[Harness, RelaySession],
) -> None:
    harness, session = airborne_session
    delayed = harness.intent("translate", selection=[1], args={"dx": 1, "dy": 0})
    harness.clock.advance(600)
    hold = harness.intent("hold", selection=[1])
    _admit(session, hold)
    assert _execute(session, hold)["status"] == "completed"

    _admit(session, delayed)
    outcome = _execute(session, delayed)

    assert outcome["status"] in {"invalidated", "refused"}, outcome
    assert outcome.get("reason") != "downstream_error"
    assert [call.operation.value for call in harness.flight.calls] == ["hover"]
    assert harness.flight.aircraft[1].pose.x == 0.0


@pytest.mark.parametrize("initial_count", [1, 2])
def test_precreated_neighbor_cannot_restart_a_completed_motion_group(
    airborne_session: tuple[Harness, RelaySession], initial_count: int
) -> None:
    harness, session = airborne_session
    motions = []
    for index in range(initial_count + 1):
        motions.append(harness.intent("translate", selection=[1], args={"dx": 1, "dy": 0}))
        if index < initial_count:
            harness.clock.advance(400)
    for motion in motions[:initial_count]:
        _admit(session, motion)
        session.mark_pending_intent_delivered(str(motion["intent_id"]))
    initial = [_execute(session, motion) for motion in motions[:initial_count]]
    if initial_count == 1:
        assert initial[0]["status"] == "completed", initial
    else:
        assert [outcome.get("reason") for outcome in initial] == ["conflicting_motion"] * 2
    initial_x = harness.flight.aircraft[1].pose.x
    initial_gotos = sum(call.operation.value == "goto" for call in harness.flight.calls)

    _admit(session, motions[-1])
    outcome = _execute(session, motions[-1])

    assert outcome["status"] in {"invalidated", "refused"}, outcome
    assert outcome.get("reason") != "downstream_error"
    assert sum(call.operation.value == "goto" for call in harness.flight.calls) == initial_gotos
    assert harness.flight.calls[-1].operation.value == "hover"
    assert harness.flight.aircraft[1].pose.x == initial_x


def test_fresh_motion_after_hold_window_remains_executable(
    airborne_session: tuple[Harness, RelaySession],
) -> None:
    harness, session = airborne_session
    hold = harness.intent("hold", selection=[1])
    _admit(session, hold)
    assert _execute(session, hold)["status"] == "completed"
    harness.clock.advance(501)
    fresh = harness.intent("translate", selection=[1], args={"dx": 1, "dy": 0})
    _admit(session, fresh)

    outcome = _execute(session, fresh)

    assert outcome["status"] == "completed", outcome
    assert [call.operation.value for call in harness.flight.calls] == ["hover", "goto"]
    assert harness.flight.aircraft[1].pose.x > 0.0


@pytest.mark.parametrize(("name", "operation"), [("hold", "hover"), ("estop", "estop")])
def test_history_conflict_does_not_remove_delivered_safety_from_group(
    airborne_session: tuple[Harness, RelaySession], name: str, operation: str
) -> None:
    harness, session = airborne_session
    original = harness.intent("translate", selection=[1], args={"dx": 1, "dy": 0})
    harness.clock.advance(400)
    delayed = harness.intent("translate", selection=[1], args={"dx": 0, "dy": 1})
    _admit(session, original)
    assert _execute(session, original)["status"] == "completed"
    harness.clock.advance(100)
    safety = harness.intent(name, selection=[1] if name == "hold" else [], source="keyboard")
    for intent in (delayed, safety):
        _admit(session, intent)
        session.mark_pending_intent_delivered(str(intent["intent_id"]))
    harness.flight.calls.clear()

    rejected = _execute(session, delayed)

    assert rejected["status"] in {"invalidated", "refused"}, rejected
    assert any(call.operation.value == operation for call in harness.flight.calls)
    assert _execute(session, safety)["status"] == "completed"
    assert all(call.operation.value != "goto" for call in harness.flight.calls)


def test_failed_future_safety_reservation_does_not_block_new_recovery(
    airborne_session: tuple[Harness, RelaySession],
) -> None:
    harness, session = airborne_session
    hold = harness.intent("hold", selection=[1])
    reserved = harness.intent("estop", selection=[], source="keyboard")
    reserved["t"] = harness.clock.value + 400
    _admit(session, hold)
    _admit(session, reserved)
    assert _execute(session, hold)["status"] == "completed"
    _fail_delivery(session, reserved)

    harness.clock.advance(501)
    recovery = harness.intent("translate", selection=[1], args={"dx": 1, "dy": 0})
    _admit(session, recovery)
    outcome = _execute(session, recovery)

    assert outcome["status"] == "completed", outcome
    assert [call.operation.value for call in harness.flight.calls] == ["hover", "goto"]


@pytest.mark.parametrize("reserved_name", ["hold", "estop"])
def test_delivered_recovery_does_not_release_motion_suppressed_by_reserved_safety(
    airborne_session: tuple[Harness, RelaySession], reserved_name: str
) -> None:
    harness, session = airborne_session
    hold = harness.intent("hold", selection=[1])
    _admit(session, hold)
    harness.clock.advance(400)
    reserved = harness.intent(
        reserved_name, selection=[1] if reserved_name == "hold" else [], source="keyboard"
    )
    _admit(session, reserved)
    harness.clock.advance(400)
    motion = harness.intent("translate", selection=[1], args={"dx": 1, "dy": 0})
    _admit(session, motion)
    for intent in (hold, motion):
        session.mark_pending_intent_delivered(str(intent["intent_id"]))

    held = _execute(session, hold)
    suppressed = _execute(session, motion)
    _fail_delivery(session, reserved)

    assert held["status"] == "completed", held
    assert suppressed["status"] == "invalidated", suppressed
    assert suppressed["reason"] == "superseded"
    assert [call.operation.value for call in harness.flight.calls] == ["hover"]
    assert harness.flight.aircraft[1].pose.x == 0.0


@pytest.mark.parametrize("offset_ms", [1, 500])
@pytest.mark.parametrize("precreated", [False, True])
def test_completed_hold_suppresses_the_full_conflict_window(
    airborne_session: tuple[Harness, RelaySession], offset_ms: int, precreated: bool
) -> None:
    harness, session = airborne_session
    hold = harness.intent("hold", selection=[1])
    if precreated:
        motion = harness.intent("translate", selection=[1], args={"dx": 1, "dy": 0})
        motion["t"] = harness.clock.value + offset_ms
    _admit(session, hold)
    assert _execute(session, hold)["status"] == "completed"
    harness.clock.advance(offset_ms)
    if not precreated:
        motion = harness.intent("translate", selection=[1], args={"dx": 1, "dy": 0})

    _admit(session, motion)
    outcome = _execute(session, motion)

    assert outcome["status"] == "invalidated", outcome
    assert outcome["reason"] == "superseded"
    assert [call.operation.value for call in harness.flight.calls] == ["hover"]
    assert harness.flight.aircraft[1].pose.x == 0.0


def test_future_dated_motion_history_survives_pruning_before_late_related_admission(
    airborne_session: tuple[Harness, RelaySession], monkeypatch: pytest.MonkeyPatch
) -> None:
    harness, session = airborne_session
    real_monotonic = bridge_module.monotonic
    elapsed = [0.0]
    monkeypatch.setattr(bridge_module, "monotonic", lambda: real_monotonic() + elapsed[0])
    original = harness.intent("translate", selection=[1], args={"dx": 1, "dy": 0})
    original["t"] = harness.clock.value + session.limits.future_clock_skew_ms
    related = harness.intent("translate", selection=[1], args={"dx": 1, "dy": 0})
    related["t"] = original["t"] + 500
    _admit(session, original)
    assert _execute(session, original)["status"] == "completed"
    original_x = harness.flight.aircraft[1].pose.x

    elapsed[0] = 5.6
    for increment in [500] * 11 + [100]:
        harness.clock.advance(increment)
        harness.factory.nodes[SESSION].periodic_events()
    unrelated = harness.intent("select", selection=[1], args={"ids": [1]})
    _admit(session, unrelated)
    assert _execute(session, unrelated)["status"] == "completed"
    harness.flight.calls.clear()

    _admit(session, related)
    outcome = _execute(session, related)

    assert outcome["status"] == "refused", outcome
    assert outcome["reason"] == "conflicting_motion"
    assert [call.operation.value for call in harness.flight.calls] == ["hover"]
    assert harness.flight.aircraft[1].pose.x == original_x
