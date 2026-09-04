import pytest

from language.test_compiler import _hydrate_relay_from_snapshot
from planner.controller import PreparedExecutionRouter
from planner.models import CommandOperation
from relay.audit import SessionAuditLog
from relay.auth import Principal
from relay.session import RelayLimits, RelaySession
from tests.autonomy_fixtures import make_snapshot, make_stack


@pytest.mark.parametrize("source", ["keyboard", "console"])
def test_independent_authenticated_stop_reaches_adapter_without_compiler(tmp_path, source):
    snapshot = make_snapshot(2)
    controller, _, _, _, flight, _ = make_stack(snapshot)
    router = PreparedExecutionRouter(controller, current_snapshot=lambda: snapshot)
    relay = RelaySession(
        session_id="language-eval",
        audit_log=SessionAuditLog(tmp_path, "language-eval"),
        limits=RelayLimits(5_000, 5_000, 1_000, 1_000),
        clock=lambda: snapshot.now_ms,
        intent_sink=router,
    )
    _hydrate_relay_from_snapshot(relay, snapshot)
    events = relay.process_frame(
        {
            "v": 1,
            "t": snapshot.now_ms,
            "type": "intent",
            "intent_id": "independent-stop",
            "retry_of": None,
            "source": source,
            "session": relay.session_id,
            "name": "estop",
            "args": {},
            "selection": [],
            "mode": "indoor",
            "confirm": False,
        },
        Principal(source=source, drone_id=None, signing_key=b"x" * 32),
    )

    assert events[-1]["status"] == "completed"
    assert flight.calls
    assert {call.operation for call in flight.calls} <= {
        CommandOperation.HOVER,
        CommandOperation.LAND,
        CommandOperation.ESTOP,
    }
    assert relay.current_state()["estop"] is True


def test_synchronous_hold_completion_unlocks_next_public_confirmation(tmp_path):
    from evals.language_corpus import StaticResponseTransport
    from language.compiler import ConfirmedPlan, InMemoryAuditSink, TranscriptCompiler

    snapshot = make_snapshot(2)
    controller, _, _, _, flight, _ = make_stack(snapshot)
    router = PreparedExecutionRouter(controller, current_snapshot=lambda: snapshot)
    relay = RelaySession(
        session_id="language-eval",
        audit_log=SessionAuditLog(tmp_path, "language-eval"),
        limits=RelayLimits(5_000, 5_000, 1_000, 1_000),
        clock=lambda: snapshot.now_ms,
        intent_sink=router,
    )
    _hydrate_relay_from_snapshot(relay, snapshot)
    _, plan = TranscriptCompiler(
        StaticResponseTransport(
            {
                "kind": "plan",
                "intents": [
                    {"name": "hold", "args": {}, "selection": [1, 2], "mode": "indoor"},
                    {"name": "hold", "args": {}, "selection": [1, 2], "mode": "indoor"},
                ],
            }
        ),
        audit=InMemoryAuditSink(),
    ).compile(
        "Hold, then hold again.",
        relay.current_state(),
        capability_version="test",
        rooms=(),
        now_ms=snapshot.now_ms,
    )
    assert plan is not None
    audit = InMemoryAuditSink()
    pending = ConfirmedPlan(plan, session=relay.session_id, audit=audit)
    emitter = router.relay_emitter(
        relay, Principal(source="console", drone_id=None, signing_key=b"x" * 32)
    )
    for index in range(2):
        state = relay.current_state()
        prepared = pending.prepare_next(
            state,
            capability_version="test",
            rooms=(),
            now_ms=snapshot.now_ms,
            intent_id=f"hold-{index}",
            router=router,
            snapshot=snapshot,
        )
        pending.confirm_next(
            state,
            capability_version="test",
            rooms=(),
            now_ms=snapshot.now_ms,
            intent_id=f"hold-{index}",
            emit=emitter,
            prepared=prepared,
        )
    assert len(flight.calls) == 4
    assert all(call.operation is CommandOperation.HOVER for call in flight.calls)
    assert pending.remaining == 0
    assert [
        event["intent_id"] for event in audit.records if event["event"] == "intent_accepted"
    ] == ["hold-0", "hold-1"]


def test_accepted_stop_latches_before_safety_enrichment_failure(tmp_path):
    snapshot = make_snapshot(2)
    controller, _, _, _, flight, _ = make_stack(snapshot)

    def unavailable_snapshot():
        raise RuntimeError("safety enrichment unavailable")

    router = PreparedExecutionRouter(controller, current_snapshot=unavailable_snapshot)
    relay = RelaySession(
        session_id="language-eval",
        audit_log=SessionAuditLog(tmp_path, "language-eval"),
        limits=RelayLimits(5_000, 5_000, 1_000, 1_000),
        clock=lambda: snapshot.now_ms,
        intent_sink=router,
    )
    _hydrate_relay_from_snapshot(relay, snapshot)
    events = relay.process_frame(
        {
            "v": 1,
            "t": snapshot.now_ms,
            "type": "intent",
            "intent_id": "stop-without-enrichment",
            "retry_of": None,
            "source": "keyboard",
            "session": relay.session_id,
            "name": "estop",
            "args": {},
            "selection": [],
            "mode": "indoor",
            "confirm": False,
        },
        Principal(source="keyboard", drone_id=None, signing_key=b"x" * 32),
    )
    assert events[0]["status"] == "accepted"
    assert events[-1]["status"] == "refused"
    assert relay.current_state()["estop"] is True
    assert flight.calls == []
