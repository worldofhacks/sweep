from dataclasses import replace

from evals.language_corpus import LEGACY_CORPUS_PATH, StaticResponseTransport, load_corpus
from language.compiler import ConfirmedPlan, InMemoryAuditSink, TranscriptCompiler
from language.contracts import plan_step_matches_facts
from language.test_compiler import (
    _hydrate_relay_from_snapshot,
    _snapshot_at,
    _with_execution_positions,
)
from planner.controller import PreparedExecutionRouter
from relay.audit import SessionAuditLog
from relay.auth import Principal
from relay.session import RelayLimits, RelaySession
from tests.autonomy_fixtures import make_snapshot, make_stack


def test_reversed_select_advances_to_hold_through_the_relay(tmp_path) -> None:
    case = next(
        item for item in load_corpus(LEGACY_CORPUS_PATH) if item.case_id == "hold-current-selection"
    )
    snapshot = _snapshot_at(make_snapshot(2, selection=(1, 2)), case.now_ms)
    state = _with_execution_positions(
        {**case.relay_state, "v": 1, "event_id": "select-state", "session": "language-eval"},
        {
            drone_id: (aircraft.pose.x, aircraft.pose.y, aircraft.pose.z)
            for drone_id, aircraft in snapshot.aircraft.items()
        },
    )
    outcome, plan = TranscriptCompiler(
        StaticResponseTransport(
            {
                "kind": "plan",
                "intents": [
                    {
                        "name": "select",
                        "args": {"ids": [2, 1]},
                        "selection": [2, 1],
                        "mode": "indoor",
                    },
                    {"name": "hold", "args": {}, "selection": [2, 1], "mode": "indoor"},
                ],
            }
        ),
        audit=InMemoryAuditSink(),
    ).compile(
        "Select drones two then one.",
        state,
        capability_version=case.capability_version,
        rooms=case.rooms,
        now_ms=case.now_ms,
    )
    assert outcome.intents and plan is not None
    controller, _, _, _, flight, _ = make_stack(snapshot)
    current_snapshot = [snapshot]
    router = PreparedExecutionRouter(controller, current_snapshot=lambda: current_snapshot[0])
    relay = RelaySession(
        session_id="language-eval",
        audit_log=SessionAuditLog(tmp_path, "language-eval"),
        limits=RelayLimits(5_000, 5_000, 1_000, 1_000),
        clock=lambda: case.now_ms + 1,
        intent_sink=router,
    )
    _hydrate_relay_from_snapshot(relay, snapshot)
    pending = ConfirmedPlan(plan, session="language-eval", audit=InMemoryAuditSink())
    emitter = router.relay_emitter(
        relay, Principal(source="console", drone_id=None, signing_key=b"x" * 32)
    )
    prepared = pending.prepare_next(
        state,
        capability_version=case.capability_version,
        rooms=case.rooms,
        now_ms=case.now_ms + 1,
        intent_id="select-canonical-order",
        router=router,
        snapshot=current_snapshot[0],
    )
    pending.confirm_next(
        state,
        capability_version=case.capability_version,
        rooms=case.rooms,
        now_ms=case.now_ms + 1,
        intent_id="select-canonical-order",
        emit=emitter,
        prepared=prepared,
    )
    canonical_state = relay.current_state()
    assert canonical_state["selection"] == [1, 2]
    assert plan_step_matches_facts(plan.intents[1], pending._expected_facts)
    current_snapshot[0] = replace(snapshot, now_ms=canonical_state["t"])

    next_prepared = pending.prepare_next(
        canonical_state,
        capability_version=case.capability_version,
        rooms=case.rooms,
        now_ms=canonical_state["t"],
        intent_id="hold-after-reversed-select",
        router=router,
        snapshot=current_snapshot[0],
    )
    pending.confirm_next(
        canonical_state,
        capability_version=case.capability_version,
        rooms=case.rooms,
        now_ms=canonical_state["t"],
        intent_id="hold-after-reversed-select",
        emit=emitter,
        prepared=next_prepared,
    )

    assert [call.operation.value for call in flight.calls] == ["hover", "hover"]
