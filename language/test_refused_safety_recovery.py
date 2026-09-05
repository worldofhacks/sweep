from dataclasses import replace

from language.test_compiler import _hydrate_relay_from_snapshot
from planner.controller import PreparedExecutionRouter
from planner.models import LifecycleStatus, PreparedExecution
from relay.audit import SessionAuditLog
from relay.auth import Principal
from relay.intent_v1 import IntentName
from relay.session import RelayLimits, RelaySession
from relay.tests.conftest import membership_payload
from tests.autonomy_fixtures import make_aircraft, make_intent, make_snapshot, make_stack


def test_pre_io_refused_conflict_hold_does_not_leave_execution_barrier(tmp_path, monkeypatch):
    snapshot = make_snapshot(2, roster_version=4)
    controller, _, _, _, flight, _ = make_stack(snapshot)
    goto = flight.goto
    monkeypatch.setattr(
        flight,
        "goto",
        lambda *args, **kwargs: replace(goto(*args, **kwargs), status=LifecycleStatus.ACCEPTED),
    )
    join_during_snapshot = False

    def current_snapshot():
        nonlocal join_during_snapshot, snapshot
        if join_during_snapshot:
            join_during_snapshot = False
            snapshot = replace(snapshot, aircraft={**snapshot.aircraft, 3: make_aircraft(3)})
            principal = Principal(source="adapter", drone_id=3, signing_key=b"x" * 32)
            relay.process_frame(
                membership_payload(
                    action="join",
                    event_id="join-before-recovery-dispatch",
                    timestamp=snapshot.now_ms,
                    drone_id=3,
                    session=relay.session_id,
                    key=principal.signing_key,
                ),
                principal,
            )
        return snapshot

    router = PreparedExecutionRouter(controller, current_snapshot=current_snapshot)
    relay = RelaySession(
        session_id="test-session",
        audit_log=SessionAuditLog(tmp_path, "test-session"),
        limits=RelayLimits(5_000, 5_000, 1_000, 1_000),
        clock=lambda: snapshot.now_ms,
        intent_sink=router,
    )
    _hydrate_relay_from_snapshot(relay, snapshot)
    emit = router.relay_emitter(
        relay, Principal(source="console", drone_id=None, signing_key=b"x" * 32)
    )
    for intent_id, dx in (("motion", 1), ("conflict", -1)):
        intent = make_intent(IntentName.TRANSLATE, intent_id=intent_id, args={"dx": dx, "dy": 0})
        prepared = router.prepare(intent, snapshot)
        assert isinstance(prepared, PreparedExecution)
        router.bind(prepared)
        join_during_snapshot = intent_id == "conflict"
        events = emit(intent)
        assert events[-1]["status"] == ("refused" if intent_id == "conflict" else "executing")

    recovery_id = "safety:motion-conflict:conflict"
    assert any(
        event.get("intent_id") == recovery_id
        and event.get("status") == "refused"
        and event.get("reason") == "stale_roster"
        for event in events
    )
    assert [call.operation.value for call in flight.calls] == ["goto"]
    assert recovery_id not in router._running
    assert relay.current_state()["accepted_plan"] is None

    assert router._running["motion"][1].status is LifecycleStatus.INVALIDATED
    snapshot = replace(
        snapshot, roster_version=relay.registry.roster_version, now_ms=snapshot.now_ms + 501
    )
    motion = make_intent(
        IntentName.TRANSLATE,
        intent_id="motion-before-successful-stop",
        t=snapshot.now_ms,
        args={"dx": 1, "dy": 0},
    )
    prepared = router.prepare(motion, snapshot)
    assert isinstance(prepared, PreparedExecution)
    router.bind(prepared)
    assert emit(motion)[-1]["reason"] == "active_task"
    assert [call.operation.value for call in flight.calls] == ["goto"]
    for intent in (
        make_intent(IntentName.HOLD, intent_id="fresh-fleet-hold"),
        make_intent(
            IntentName.SELECT, intent_id="select-after-recovery", selection=(1,), args={"ids": (1,)}
        ),
    ):
        prepared = router.prepare(intent, snapshot)
        assert isinstance(prepared, PreparedExecution)
        router.bind(prepared)
        assert emit(intent)[-1]["status"] == "completed"

    assert [call.operation.value for call in flight.calls] == ["goto", "hover", "hover"]
    assert not router._running
    assert relay.current_state()["selection"] == [1]
    assert relay.current_state()["accepted_plan"] is None
