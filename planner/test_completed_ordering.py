from __future__ import annotations

from threading import Condition, Lock
from types import SimpleNamespace

import pytest

import planner.relay_bridge as bridge_module
from planner.coordination import resolve_intent_group
from planner.relay_bridge import AutonomyRelayBridge, _CoordinatedIntent
from relay.contracts import LifecycleStatus
from relay.intent_v1 import IntentName
from relay.session import IntentSinkResult
from tests.autonomy_fixtures import make_intent, make_snapshot


def test_delayed_delivery_cannot_outlive_the_stop_that_retired_its_timestamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [0.0]
    monkeypatch.setattr(bridge_module, "monotonic", lambda: now[0])
    bridge = object.__new__(AutonomyRelayBridge)
    bridge.controller = SimpleNamespace(
        arbiter=SimpleNamespace(config=SimpleNamespace(motion_conflict_window_ms=500))
    )
    bridge.session = SimpleNamespace(limits=SimpleNamespace(intent_max_age_ms=5_000))
    bridge._coordination = Condition(Lock())
    bridge._admissions = {}
    bridge._completed_ordering = []
    hold = make_intent(IntentName.HOLD, intent_id="completed-hold", t=1_000)
    bridge._record_completed_ordering(
        (_CoordinatedIntent(hold, admitted_at=0.0, delivered=True),),
        {hold.intent_id: IntentSinkResult(status=LifecycleStatus.COMPLETED, source="autonomy")},
    )
    now[0] = 1.0
    motion = make_intent(
        IntentName.TRANSLATE,
        intent_id="delayed-old-motion",
        t=900,
        args={"dx": 0.5, "dy": 0.0},
    )
    bridge.admit_intent(motion)
    now[0] = 6.0
    fresh = make_intent(
        IntentName.TRANSLATE,
        intent_id="fresh-motion",
        t=7_000,
        args={"dx": 0.5, "dy": 0.0},
    )
    bridge.admit_intent(fresh)
    fresh_admission = bridge._admissions[fresh.intent_id]
    fresh_resolution = resolve_intent_group((fresh,), make_snapshot(1), conflict_window_ms=500)
    bridge._apply_completed_ordering((fresh_admission,), fresh_resolution, make_snapshot(1))
    bridge.intent_delivered(motion.intent_id)
    admission = bridge._admissions[motion.intent_id]
    snapshot = make_snapshot(1)
    resolution = resolve_intent_group((motion,), snapshot, conflict_window_ms=500)

    resolved = bridge._apply_completed_ordering((admission,), resolution, snapshot)

    assert resolved.accepted == ()
    assert motion.intent_id in resolved.invalidated_intent_ids
