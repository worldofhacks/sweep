from dataclasses import replace

import pytest

from evals.language_corpus import StaticResponseTransport, load_corpus, load_synthetic_responses
from language.compiler import ConfirmedPlan, InMemoryAuditSink, TranscriptCompiler
from language.test_compiler import _hydrate_relay_from_snapshot
from planner.controller import PreparedExecutionRouter
from planner.models import Position
from relay.audit import SessionAuditLog
from relay.auth import Principal
from relay.session import RelayLimits, RelaySession
from tests.autonomy_fixtures import make_snapshot, make_stack, planning_config, replace_aircraft


@pytest.mark.parametrize(
    ("case_id", "expected_offsets"),
    [
        ("move-right-half-meter", {1: (0.0, -0.5), 2: (0.5, 0.0)}),
        ("move-left-half-meter", {1: (0.0, 0.3048), 2: (-0.3048, 0.0)}),
        ("move-forward-half-meter", {1: (0.5, 0.0), 2: (0.0, 0.5)}),
        ("move-right-one-meter", {1: (-1.0, 0.0)}),
    ],
)
def test_reviewed_movement_reaches_physical_targets_through_confirmation(
    tmp_path, case_id, expected_offsets
):
    corpus = load_corpus()
    case = next(case for case in corpus if case.case_id == case_id)
    payload = load_synthetic_responses(corpus=corpus)[case_id]
    snapshot = make_snapshot(len(case.relay_state["drones"]))
    for drone in case.relay_state["drones"]:
        snapshot = replace_aircraft(snapshot, drone["drone_id"], heading_deg=drone["heading_deg"])
    _exercise(tmp_path, case.transcript, payload, snapshot, expected_offsets)


@pytest.mark.parametrize("step_m", [0.5, 0.8])
def test_explicit_feet_displacement_is_independent_of_configured_step(tmp_path, step_m):
    snapshot = replace_aircraft(make_snapshot(2), 2, heading_deg=90.0)
    _exercise(
        tmp_path,
        "Move forward two feet.",
        {
            "kind": "plan",
            "intents": [
                {
                    "name": "translate",
                    "args": {"dx": 0.6096 / step_m, "dy": 0},
                    "selection": [1, 2],
                    "mode": "indoor",
                }
            ],
        },
        snapshot,
        {1: (0.6096, 0.0), 2: (0.0, 0.6096)},
        step_m=step_m,
    )


def _exercise(tmp_path, transcript, payload, snapshot, expected_offsets, *, step_m=0.5):
    config = replace(
        planning_config(translation_frame="aircraft_relative"), translation_step_m=step_m
    )
    controller, _, _, _, flight, _ = make_stack(snapshot, config=config)
    router = PreparedExecutionRouter(controller, current_snapshot=lambda: snapshot)
    relay = RelaySession(
        session_id="language-eval",
        audit_log=SessionAuditLog(tmp_path, "language-eval"),
        limits=RelayLimits(5000, 5000, 1000, 1000),
        clock=lambda: snapshot.now_ms,
        intent_sink=router,
    )
    _hydrate_relay_from_snapshot(relay, snapshot)
    grounding = config.translation_grounding(snapshot)
    _, plan = TranscriptCompiler(
        StaticResponseTransport(payload), audit=InMemoryAuditSink()
    ).compile(
        transcript,
        relay.current_state(),
        capability_version="test",
        translation=grounding,
        now_ms=snapshot.now_ms,
    )
    assert plan is not None
    pending = ConfirmedPlan(plan, session=relay.session_id, audit=InMemoryAuditSink())
    prepared = pending.prepare_next(
        relay.current_state(),
        capability_version="test",
        rooms=(),
        now_ms=snapshot.now_ms,
        intent_id="movement",
        router=router,
        snapshot=snapshot,
    )
    pending.confirm_next(
        relay.current_state(),
        capability_version="test",
        rooms=(),
        now_ms=snapshot.now_ms,
        intent_id="movement",
        prepared=prepared,
        emit=router.relay_emitter(
            relay, Principal(source="console", drone_id=None, signing_key=b"x" * 32)
        ),
    )
    assert len(flight.calls) == len(expected_offsets)
    for drone_id, (dx, dy) in expected_offsets.items():
        initial = snapshot.aircraft[drone_id].pose
        actual = flight.aircraft[drone_id].pose
        expected = Position(initial.x + dx, initial.y + dy, initial.z)
        assert actual.x == pytest.approx(expected.x)
        assert actual.y == pytest.approx(expected.y)
        assert actual.z == pytest.approx(expected.z)
