from __future__ import annotations

from threading import Condition, Lock
from types import SimpleNamespace

from planner.coordination import resolve_intent_group
from planner.relay_bridge import AutonomyRelayBridge, _CoordinatedIntent
from relay.contracts import LifecycleStatus as RelayStatus
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


def test_chained_motion_conflicts_form_one_neighborhood_from_every_seed() -> None:
    first = _admission(IntentName.TRANSLATE, "first", t=100, delivered=True)
    middle = _admission(IntentName.TRANSLATE, "middle", t=500, delivered=True)
    last = _admission(IntentName.TRANSLATE, "last", t=900, delivered=True)
    bridge, executed_groups = _bridge(first, middle, last)

    groups = []
    for seed in (first, middle, last):
        group, _, error = bridge._resolve_admission_group(seed)
        assert error is None
        groups.append(_intent_ids(group))

    assert groups == [("first", "middle", "last")] * 3
    assert executed_groups == [("first", "middle", "last")] * 3

    resolution = resolve_intent_group(
        tuple(item.intent for item in (first, middle, last)),
        make_snapshot(1),
        conflict_window_ms=500,
    )
    assert tuple(refusal.intent_id for refusal in resolution.refusals) == (
        "first",
        "middle",
        "last",
    )
    assert resolution.hold_required


def test_delivered_hold_survives_an_undelivered_estop_reservation() -> None:
    hold = _admission(IntentName.HOLD, "hold", t=100, delivered=True)
    estop = _admission(IntentName.ESTOP, "estop", t=200, delivered=False)

    resolution = resolve_intent_group(
        (hold.intent, estop.intent), make_snapshot(1), conflict_window_ms=500
    )
    preserved = AutonomyRelayBridge._preserve_delivered_safety_action(
        (hold, estop), resolution, make_snapshot(1), 500
    )

    assert tuple(intent.intent_id for intent in preserved.accepted) == ("hold",)
    assert preserved.invalidated_intent_ids == ()


def test_confirmed_land_all_survives_an_undelivered_estop_reservation() -> None:
    land = _admission(IntentName.LAND_ALL, "land", t=100, delivered=True)
    estop = _admission(IntentName.ESTOP, "estop", t=200, delivered=False)

    resolution = resolve_intent_group(
        (land.intent, estop.intent), make_snapshot(1), conflict_window_ms=500
    )
    preserved = AutonomyRelayBridge._preserve_delivered_safety_action(
        (land, estop), resolution, make_snapshot(1), 500
    )

    assert tuple(intent.intent_id for intent in preserved.accepted) == ("land",)
    assert preserved.invalidated_intent_ids == ()


def test_delivered_estop_still_supersedes_a_delivered_hold() -> None:
    hold = _admission(IntentName.HOLD, "hold", t=100, delivered=True)
    estop = _admission(IntentName.ESTOP, "estop", t=200, delivered=True)

    resolution = resolve_intent_group(
        (hold.intent, estop.intent), make_snapshot(1), conflict_window_ms=500
    )
    preserved = AutonomyRelayBridge._preserve_delivered_safety_action(
        (hold, estop), resolution, make_snapshot(1), 500
    )

    assert tuple(intent.intent_id for intent in preserved.accepted) == ("estop",)
    assert preserved.invalidated_intent_ids == ("hold",)


def test_undelivered_estop_does_not_restore_land_all_rejected_by_delivered_hold() -> None:
    hold = _admission(IntentName.HOLD, "hold", t=100, delivered=True)
    land = _admission(IntentName.LAND_ALL, "land", t=100, delivered=True)
    motion = _admission(IntentName.TRANSLATE, "motion", t=400, delivered=True)
    estop = _admission(IntentName.ESTOP, "estop", t=800, delivered=False)
    snapshot = make_snapshot(1)

    resolution = resolve_intent_group(
        tuple(item.intent for item in (hold, land, motion, estop)),
        snapshot,
        conflict_window_ms=500,
    )
    preserved = AutonomyRelayBridge._preserve_delivered_safety_action(
        (hold, land, motion, estop), resolution, snapshot, 500
    )

    assert tuple(intent.intent_id for intent in preserved.accepted) == ("hold", "estop")
    assert preserved.invalidated_intent_ids == ("motion", "land")


def test_delivered_hold_retires_older_motion_outside_conflict_window() -> None:
    motion = _admission(IntentName.TRANSLATE, "motion", t=100, delivered=False)
    hold = _admission(IntentName.HOLD, "hold", t=700, delivered=True)

    resolution = resolve_intent_group(
        (motion.intent, hold.intent), make_snapshot(1), conflict_window_ms=500
    )
    retired = AutonomyRelayBridge._retire_motion_preceding_delivered_hold(
        (motion, hold), resolution
    )

    assert tuple(intent.intent_id for intent in retired.accepted) == ("hold",)
    assert retired.invalidated_intent_ids == ("motion",)


def test_unclaimed_estop_bypasses_a_busy_coordinator() -> None:
    bridge, _ = _bridge()
    bridge._coordinator_active = True
    result = SimpleNamespace(status=RelayStatus.COMPLETED)
    bridge._execute_one = lambda _intent: result  # type: ignore[method-assign]
    estop = make_intent(IntentName.ESTOP, intent_id="estop", t=100)

    assert bridge._coordinate_intent(estop) is result
    assert "estop" not in bridge._admissions


def test_estop_reserved_while_hold_runs_is_not_claimed_or_later_dispatched_twice() -> None:
    hold = _admission(IntentName.HOLD, "hold", t=100, delivered=True)
    estop = _admission(IntentName.ESTOP, "estop", t=200, delivered=False)
    bridge, executed_groups = _bridge(hold, estop)

    group, _, error = bridge._resolve_admission_group(hold)

    assert error is None
    assert not estop.claimed
    assert not next(item for item in group if item.intent.intent_id == "estop").delivered
    assert executed_groups == [("hold", "estop")]

    bridge._coordinator_active = True
    result = SimpleNamespace(status=RelayStatus.COMPLETED)
    bridge._execute_one = lambda _intent: result  # type: ignore[method-assign]

    assert bridge._coordinate_intent(estop.intent) is result
    assert "estop" not in bridge._admissions


def test_claimed_estop_retires_every_motion_before_execution() -> None:
    first = _admission(IntentName.TRANSLATE, "first", t=100, delivered=True)
    last = _admission(IntentName.TRANSLATE, "last", t=900, delivered=True)
    estop = _admission(IntentName.ESTOP, "estop", t=500, delivered=True)
    resolution = resolve_intent_group(
        tuple(item.intent for item in (first, estop, last)),
        make_snapshot(1),
        conflict_window_ms=500,
    )

    prioritized = AutonomyRelayBridge._prioritize_delivered_estop((first, estop, last), resolution)

    assert tuple(intent.intent_id for intent in prioritized.accepted) == ("estop",)
    assert prioritized.invalidated_intent_ids == ("first", "last")


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
        intent=make_intent(
            name,
            intent_id=intent_id,
            t=t,
            args=args,
            confirm=name is IntentName.LAND_ALL,
        ),
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
    bridge._completed_ordering = []
    bridge.session = SimpleNamespace(
        current_state=lambda: {"t": 100},
        limits=SimpleNamespace(intent_max_age_ms=5_000),
    )
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
