from dataclasses import replace

import pytest

from evals.language_corpus import StaticResponseTransport
from language.compiler import InMemoryAuditSink, TranscriptCompiler
from language.contracts import CompilerReason, OutcomeKind, validate_model_outcome
from language.test_compiler import _hydrate_relay_from_snapshot
from language.test_provider_contract import _facts
from planner.controller import PreparedExecutionRouter
from planner.models import TranslationGrounding, TranslationPolicy
from relay.audit import SessionAuditLog
from relay.session import RelayLimits, RelaySession
from tests.autonomy_fixtures import make_snapshot, make_stack


@pytest.mark.parametrize("number", [10**400, -(10**400), float("inf"), -float("inf"), float("nan")])
@pytest.mark.parametrize("field", ["dx", "dy", "altitude", "spacing", "nested_box"])
def test_unrepresentable_model_argument_refuses_before_preparation(tmp_path, number, field):
    snapshot = make_snapshot(1)
    controller, _, _, _, flight, camera = make_stack(snapshot)
    router = PreparedExecutionRouter(controller, current_snapshot=lambda: snapshot)
    relay = RelaySession(
        session_id="numeric-boundary",
        audit_log=SessionAuditLog(tmp_path, "numeric-boundary"),
        limits=RelayLimits(5_000, 5_000, 1_000, 1_000),
        clock=lambda: snapshot.now_ms,
        intent_sink=router,
    )
    _hydrate_relay_from_snapshot(relay, snapshot)
    if field in {"dx", "dy"}:
        name, args = "translate", {"dx": 0, "dy": 0, field: number}
    elif field == "nested_box":
        name, args = "sweep", {"box": {"vertices": [{"x": number}]}}
    else:
        name, args = field, {"delta": number}
    compiler = TranscriptCompiler(
        StaticResponseTransport(
            {
                "kind": "plan",
                "intents": [{"name": name, "args": args, "selection": [1], "mode": "indoor"}],
            }
        ),
        audit=InMemoryAuditSink(),
    )

    outcome, compiled = compiler.compile(
        "Move as requested.",
        relay.current_state(),
        capability_version="sim-v1",
        translation=TranslationGrounding(
            policy=TranslationPolicy(frame="world", step_m=0.5), headings={}
        ),
        now_ms=snapshot.now_ms,
    )

    assert outcome.kind is OutcomeKind.REFUSE
    assert outcome.reason is CompilerReason.INVALID_MODEL_OUTPUT
    assert compiled is None
    assert not flight.calls
    assert not camera.calls


@pytest.mark.parametrize("number", [0, 1, -2, 0.5, -1.25])
def test_representable_model_arguments_retain_their_values(number):
    facts = replace(_facts(), translation_frame="world", translation_step_m=0.5)
    args = {"dx": number, "dy": 0}
    outcome = validate_model_outcome(
        {
            "kind": "plan",
            "intents": [{"name": "translate", "args": args, "selection": [1, 2], "mode": "indoor"}],
        },
        facts,
        capture_id=lambda _: "unused",
        source="synthetic",
        transcript="Move as requested.",
    )

    assert outcome.kind is OutcomeKind.PLAN
    assert dict(outcome.intents[0].args) == args
