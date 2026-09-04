from __future__ import annotations

from threading import Condition, Lock
from types import SimpleNamespace

from planner.coordination import resolve_intent_group
from planner.relay_bridge import AutonomyRelayBridge, _CoordinatedIntent
from relay.intent_v1 import IntentName
from tests.autonomy_fixtures import make_intent, make_snapshot


def test_undelivered_early_admission_cannot_truncate_estop_neighborhood() -> None:
    early = _admission(IntentName.SELECT, "early", t=100, delivered=False)
    motion = _admission(IntentName.TRANSLATE, "motion", t=400, delivered=True)
    estop = _admission(IntentName.ESTOP, "estop", t=800, delivered=True)
    bridge, executed_groups = _bridge(early, motion, estop)

    group, _, error = bridge._resolve_admission_group(motion)

    assert error is None
    assert _intent_ids(group) == ("motion", "estop")
    assert executed_groups == [("motion", "estop")]


def test_select_seed_cannot_hide_estop_near_delivered_motion() -> None:
    early = _admission(IntentName.SELECT, "early", t=100, delivered=True)
    motion = _admission(IntentName.TRANSLATE, "motion", t=400, delivered=True)
    estop = _admission(IntentName.ESTOP, "estop", t=800, delivered=False)
    bridge, executed_groups = _bridge(early, motion, estop)

    group, _, error = bridge._resolve_admission_group(early)

    assert error is None
    assert _intent_ids(group) == ("motion", "estop")
    assert executed_groups == [("motion", "estop")]


def test_undelivered_later_estop_cannot_supersede_delivered_estop() -> None:
    delivered = _admission(IntentName.ESTOP, "delivered", t=100, delivered=True)
    undelivered = _admission(IntentName.ESTOP, "undelivered", t=200, delivered=False)
    bridge, executed_groups = _bridge(delivered, undelivered)

    group, _, error = bridge._resolve_admission_group(delivered)

    assert error is None
    assert _intent_ids(group) == ("delivered",)
    assert executed_groups == [("delivered",)]


def _admission(
    name: IntentName,
    intent_id: str,
    *,
    t: int,
    delivered: bool,
) -> _CoordinatedIntent:
    args = {"ids": [1]} if name is IntentName.SELECT else {"dx": 0.5, "dy": 0.0}
    if name is IntentName.ESTOP:
        args = {}
    return _CoordinatedIntent(
        intent=make_intent(name, intent_id=intent_id, t=t, args=args),
        admitted_at=-1.0,
        delivered=delivered,
    )


def _bridge(
    *admissions: _CoordinatedIntent,
) -> tuple[AutonomyRelayBridge, list[tuple[str, ...]]]:
    bridge = object.__new__(AutonomyRelayBridge)
    bridge.controller = SimpleNamespace(
        arbiter=SimpleNamespace(config=SimpleNamespace(motion_conflict_window_ms=500))
    )
    bridge._coordination = Condition(Lock())
    bridge._admissions = {admission.intent.intent_id: admission for admission in admissions}
    executed_groups: list[tuple[str, ...]] = []

    def execute_group(group: tuple[_CoordinatedIntent, ...]) -> dict[str, object]:
        executed_groups.append(_intent_ids(group))
        return {}

    bridge._execute_group = execute_group  # type: ignore[method-assign]
    return bridge, executed_groups


def _intent_ids(admissions: tuple[_CoordinatedIntent, ...]) -> tuple[str, ...]:
    return tuple(admission.intent.intent_id for admission in admissions)


def test_stop_covers_both_sides_of_its_window_without_absorbing_distant_motion() -> None:
    admissions = (
        _admission(IntentName.TRANSLATE, "before", t=100, delivered=True),
        _admission(IntentName.ESTOP, "stop", t=500, delivered=True),
        _admission(IntentName.TRANSLATE, "after", t=900, delivered=True),
        _admission(IntentName.TRANSLATE, "distant", t=1100, delivered=True),
    )

    result = resolve_intent_group(
        tuple(item.intent for item in admissions), make_snapshot(1), conflict_window_ms=500
    )

    assert tuple(intent.intent_id for intent in result.accepted) == ("stop", "distant")
    assert result.invalidated_intent_ids == ("before", "after")


def test_distant_motion_keeps_its_order_before_estop() -> None:
    motion = _admission(IntentName.TRANSLATE, "motion", t=100, delivered=True)
    stop = _admission(IntentName.ESTOP, "stop", t=800, delivered=True)

    result = resolve_intent_group(
        (motion.intent, stop.intent), make_snapshot(1), conflict_window_ms=500
    )

    assert tuple(intent.intent_id for intent in result.accepted) == ("motion", "stop")
    assert result.invalidated_intent_ids == ()
