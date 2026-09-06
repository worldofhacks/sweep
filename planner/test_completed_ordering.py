from __future__ import annotations

from dataclasses import replace
from threading import Condition, Lock
from types import SimpleNamespace

import pytest

import planner.relay_bridge as bridge_module
from adapters.protocols import WatchdogConfig
from language.test_compiler import _hydrate_relay_from_snapshot
from planner.coordination import resolve_intent_group
from planner.models import LossBehavior, RelayAircraftSafetyEnrichment, RelaySnapshotEnrichment
from planner.relay_bridge import AutonomyRelayBridge, _CoordinatedIntent
from relay.audit import SessionAuditLog
from relay.auth import Principal
from relay.contracts import LifecycleStatus
from relay.intent_v1 import IntentName
from relay.session import IntentSinkResult, RelayLimits, RelaySession
from tests.autonomy_fixtures import NOW_MS, make_intent, make_snapshot, make_stack


def test_delayed_delivery_cannot_outlive_the_stop_that_retired_its_timestamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [0.0]
    monkeypatch.setattr(bridge_module, "monotonic", lambda: now[0])
    bridge = object.__new__(AutonomyRelayBridge)
    bridge.controller = SimpleNamespace(
        arbiter=SimpleNamespace(config=SimpleNamespace(motion_conflict_window_ms=500))
    )
    bridge.session = SimpleNamespace(
        limits=SimpleNamespace(intent_max_age_ms=5_000, future_clock_skew_ms=1_000)
    )
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
    now[0] = 8.0
    fresh = make_intent(
        IntentName.TRANSLATE,
        intent_id="fresh-motion",
        t=9_000,
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


@pytest.mark.parametrize("equal_timestamps", [False, True])
@pytest.mark.parametrize("older_admitted_first", [False, True])
def test_delayed_select_cannot_replace_a_completed_newer_selection(
    tmp_path, equal_timestamps: bool, older_admitted_first: bool
) -> None:
    session, bridge = _session_with_bridge(tmp_path)
    older = make_intent(
        IntentName.SELECT,
        intent_id="select-a",
        t=NOW_MS,
        args={"ids": [1]},
    )
    newer = make_intent(
        IntentName.SELECT,
        intent_id="select-z",
        t=NOW_MS if equal_timestamps else NOW_MS + 100,
        args={"ids": [2]},
    )
    principal = Principal(source="console", drone_id=None, signing_key=b"x" * 32)

    if older_admitted_first:
        _admit(session, older, principal)
    _admit(session, newer, principal)
    newer_events = session.execute_pending_intent(newer.intent_id)
    later = make_intent(
        IntentName.SELECT,
        intent_id="select-later",
        t=NOW_MS + 200,
        args={"ids": [1]},
    )
    if older_admitted_first:
        _admit(session, later, principal)
        later_events = session.execute_pending_intent(later.intent_id)
        older_events = session.execute_pending_intent(older.intent_id)
    else:
        _admit(session, older, principal)
        older_events = session.execute_pending_intent(older.intent_id)
        _admit(session, later, principal)
        later_events = session.execute_pending_intent(later.intent_id)

    assert any(event.get("status") == "completed" for event in newer_events)
    assert any(event.get("status") == "invalidated" for event in older_events)
    assert any(event.get("status") == "completed" for event in later_events)
    assert session.current_state()["selection"] == [1]
    assert len(bridge._completed_ordering) == 1


def test_completed_selects_advance_authoritative_selection(tmp_path) -> None:
    session, _ = _session_with_bridge(tmp_path)
    principal = Principal(source="console", drone_id=None, signing_key=b"x" * 32)
    older = make_intent(
        IntentName.SELECT,
        intent_id="select-a",
        t=NOW_MS,
        args={"ids": [1]},
    )
    newer = make_intent(
        IntentName.SELECT,
        intent_id="select-z",
        t=NOW_MS + 100,
        args={"ids": [2]},
    )

    _admit(session, older, principal)
    older_events = session.execute_pending_intent(older.intent_id)
    _admit(session, newer, principal)
    newer_events = session.execute_pending_intent(newer.intent_id)

    assert any(event.get("status") == "completed" for event in older_events)
    assert any(event.get("status") == "completed" for event in newer_events)
    assert session.current_state()["selection"] == [2]


def _session_with_bridge(tmp_path) -> tuple[RelaySession, AutonomyRelayBridge]:
    snapshot = make_snapshot(2, selection=(1, 2))
    controller, _, arbiter, _, _, _ = make_stack(snapshot)
    arbiter.config = replace(arbiter.config, motion_conflict_window_ms=0)
    session = RelaySession(
        session_id="test-session",
        audit_log=SessionAuditLog(tmp_path, "test-session"),
        limits=RelayLimits(5_000, 5_000, 1_000, 1_000),
        clock=lambda: NOW_MS,
        capability_profile=controller.planner.capability_profile,
    )
    _hydrate_relay_from_snapshot(session, snapshot)
    bridge = AutonomyRelayBridge(
        session=session,
        controller=controller,
        enrichment=_enrichment(snapshot),
        watchdog_config=WatchdogConfig(0, 1, LossBehavior.HOLD),
        node_activity=lambda *_args: None,
        node_safety_events=lambda: [],
    )
    session.intent_sink = bridge
    return session, bridge


def _admit(session: RelaySession, intent, principal: Principal) -> None:
    events = session.process_intent(
        {
            "v": intent.v,
            "t": intent.t,
            "type": intent.type,
            "intent_id": intent.intent_id,
            "retry_of": intent.retry_of,
            "source": intent.source,
            "session": intent.session,
            "name": intent.name.value,
            "args": {
                key: list(value) if isinstance(value, tuple) else value
                for key, value in intent.args.items()
            },
            "selection": list(intent.selection),
            "mode": intent.mode.value,
            "confirm": intent.confirm,
        },
        principal,
    )
    assert events[0]["status"] == "accepted"


def _enrichment(snapshot):
    aircraft = {
        drone_id: RelayAircraftSafetyEnrichment(
            drone_id=drone_id,
            armed=state.armed,
            physical_rc_available=state.physical_rc_available,
            storage_remaining_bytes=state.storage_remaining_bytes,
            camera_ready=state.camera_ready,
            active_task_id=state.active_task_id,
            position_loss_since_ms=state.position_loss_since_ms,
            last_known_pose=state.pose,
            last_known_home=state.home,
            last_known_flight_state=state.flight_state.value,
            last_known_battery=state.battery,
            last_known_link_quality=state.link_quality,
            last_known_position_quality=state.position_quality,
            last_link_seen_ms=state.link_last_seen_ms,
            last_position_seen_ms=state.position_last_seen_ms,
        )
        for drone_id, state in snapshot.aircraft.items()
    }
    return lambda state: RelaySnapshotEnrichment(
        operator_present=True,
        operator_last_seen_ms=int(state["t"]),
        aircraft=aircraft,
    )
