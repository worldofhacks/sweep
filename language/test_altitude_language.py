from dataclasses import replace

import pytest

from evals.language_corpus import StaticResponseTransport
from language.compiler import (
    ConfirmationError,
    ConfirmedPlan,
    InMemoryAuditSink,
    TranscriptCompiler,
)
from language.contracts import OutcomeKind, intent_payload
from language.test_compiler import (
    _case,
    _lifecycle,
    _prepare_and_confirm,
    _snapshot_at,
    _state,
    _with_execution_positions,
)
from planner.controller import PreparedExecutionRouter
from planner.models import LifecycleStatus
from planner.planner import DeterministicPlanner
from relay.intent_v1 import AcceptedIntent, IntentName, validate_intent
from tests.autonomy_fixtures import make_snapshot, make_stack, planning_config


def _config(*, floor_z_m: float | None = 0.0):
    return replace(
        planning_config(),
        altitude_step_m=0.5,
        altitude_floor_z_m=floor_z_m,
        altitude_configuration_id="survey-floor-v1",
    )


@pytest.mark.parametrize(
    ("transcript", "args", "expected_z"),
    [
        ("hover at 5 feet", {"height_m": 1.524}, 1.524),
        ("fly up 1 foot", {"delta": 0.6096}, 1.3048),
        ("move up", {"delta": 0.6096}, 1.3048),
        ("fly down", {"delta": -0.6096}, 0.6952),
    ],
)
def test_synthetic_altitude_phrases_run_through_compiler_planner_arbiter_and_adapter(
    transcript, args, expected_z
) -> None:
    case = _case("translate-selected")
    snapshot = _snapshot_at(make_snapshot(2), case.now_ms)
    config = _config()
    state = _with_execution_positions(
        _state(case),
        {
            drone_id: (aircraft.pose.x, aircraft.pose.y, aircraft.pose.z)
            for drone_id, aircraft in snapshot.aircraft.items()
        },
    )
    outcome, _plan = TranscriptCompiler(
        StaticResponseTransport(
            {
                "kind": "plan",
                "intents": [
                    {"name": "altitude", "args": args, "selection": [1, 2], "mode": "indoor"}
                ],
            }
        ),
        audit=InMemoryAuditSink(),
    ).compile(
        transcript,
        state,
        capability_version=case.capability_version,
        rooms=case.rooms,
        altitude=config.altitude_grounding(),
        now_ms=case.now_ms,
    )

    assert outcome.kind is OutcomeKind.PLAN
    assert outcome.source == "synthetic"
    proposal = outcome.intents[0]
    controller, _, _, _, flight, _ = make_stack(snapshot, config=config)
    accepted = validate_intent(
        intent_payload(
            proposal,
            session="language-eval",
            intent_id="altitude-language",
            timestamp_ms=case.now_ms,
        ),
        capability_profile=controller.planner.capability_profile,
    )
    assert isinstance(accepted, AcceptedIntent)
    result = controller.execute(accepted.intent, snapshot)

    assert result.status is LifecycleStatus.COMPLETED
    assert result.plan.altitude_grounding == config.altitude_grounding()
    assert all(aircraft.pose.z == expected_z for aircraft in flight.aircraft.values())


def test_plain_hover_remains_hold_when_altitude_is_enabled() -> None:
    case = _case("translate-selected")
    config = _config()
    outcome, _plan = TranscriptCompiler(
        StaticResponseTransport(
            {
                "kind": "plan",
                "intents": [{"name": "hold", "args": {}, "selection": [1, 2], "mode": "indoor"}],
            }
        ),
        audit=InMemoryAuditSink(),
    ).compile(
        "hover",
        _state(case),
        capability_version=case.capability_version,
        rooms=case.rooms,
        altitude=config.altitude_grounding(),
        now_ms=case.now_ms,
    )

    assert outcome.kind is OutcomeKind.PLAN
    assert outcome.intents[0].name is IntentName.HOLD


def test_hover_at_height_requires_surveyed_floor() -> None:
    case = _case("translate-selected")
    outcome, plan = TranscriptCompiler(
        StaticResponseTransport(
            {
                "kind": "plan",
                "intents": [
                    {
                        "name": "altitude",
                        "args": {"height_m": 1.524},
                        "selection": [1, 2],
                        "mode": "indoor",
                    }
                ],
            }
        ),
        audit=InMemoryAuditSink(),
    ).compile(
        "hover at 5 feet",
        _state(case),
        capability_version=case.capability_version,
        rooms=case.rooms,
        altitude=_config(floor_z_m=None).altitude_grounding(),
        now_ms=case.now_ms,
    )

    assert outcome.kind is OutcomeKind.REFUSE
    assert plan is None


def test_incomplete_hover_at_is_refused_without_defaulting_to_one_foot() -> None:
    case = _case("translate-selected")
    outcome, plan = TranscriptCompiler(
        StaticResponseTransport(
            {
                "kind": "plan",
                "intents": [
                    {
                        "name": "altitude",
                        "args": {"height_m": 0.3048},
                        "selection": [1, 2],
                        "mode": "indoor",
                    }
                ],
            }
        ),
        audit=InMemoryAuditSink(),
    ).compile(
        "hover at",
        _state(case),
        capability_version=case.capability_version,
        rooms=case.rooms,
        altitude=_config().altitude_grounding(),
        now_ms=case.now_ms,
    )

    assert outcome.kind is OutcomeKind.REFUSE
    assert plan is None


def test_altitude_configuration_change_invalidates_compiler_preview() -> None:
    case = _case("translate-selected")
    snapshot = _snapshot_at(make_snapshot(2), case.now_ms)
    config = _config()
    state = _with_execution_positions(
        _state(case),
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
                        "name": "altitude",
                        "args": {"height_m": 1.524},
                        "selection": [1, 2],
                        "mode": "indoor",
                    }
                ],
            }
        ),
        audit=InMemoryAuditSink(),
    ).compile(
        "hover at 5 feet",
        state,
        capability_version=case.capability_version,
        rooms=case.rooms,
        altitude=config.altitude_grounding(),
        now_ms=case.now_ms,
    )

    assert outcome.kind is OutcomeKind.PLAN
    assert plan is not None
    controller, _, _, _, _, _ = make_stack(snapshot, config=config)
    controller.planner = DeterministicPlanner(
        replace(config, altitude_configuration_id="resurvey-v2")
    )
    with pytest.raises(ConfirmationError, match="altitude configuration"):
        ConfirmedPlan(plan, session="language-eval", audit=InMemoryAuditSink()).prepare_next(
            state,
            capability_version=case.capability_version,
            rooms=case.rooms,
            now_ms=case.now_ms,
            intent_id="altitude-config-drift",
            router=PreparedExecutionRouter(controller, current_snapshot=lambda: snapshot),
            snapshot=snapshot,
        )


@pytest.mark.parametrize("completed_z,completed_x", [(1.524, 0.0), (1.4, 0.0), (1.524, 0.2)])
@pytest.mark.parametrize("stale_first", [False, True])
def test_confirmed_altitude_requires_fresh_target_position(
    completed_z: float, completed_x: float, stale_first: bool
) -> None:
    case = _case("translate-selected")
    snapshot = _snapshot_at(make_snapshot(2), case.now_ms)
    config = _config()
    state = _with_execution_positions(
        _state(case),
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
                        "name": "altitude",
                        "args": {"height_m": 1.524},
                        "selection": [1, 2],
                        "mode": "indoor",
                    }
                ],
            }
        ),
        audit=InMemoryAuditSink(),
    ).compile(
        "hover at 5 feet",
        state,
        capability_version=case.capability_version,
        rooms=case.rooms,
        altitude=config.altitude_grounding(),
        now_ms=case.now_ms,
    )
    assert outcome.kind is OutcomeKind.PLAN and plan is not None
    controller, _, _, _, _, _ = make_stack(snapshot, config=config)
    pending = ConfirmedPlan(plan, session="language-eval", audit=InMemoryAuditSink())
    _prepare_and_confirm(
        pending,
        state,
        case,
        controller=controller,
        snapshot=snapshot,
        now_ms=case.now_ms,
        intent_id="confirmed-altitude",
        capability_profile=controller.planner.capability_profile,
    )
    completed = _with_execution_positions(
        {**state, "t": case.now_ms + 2, "event_id": "altitude-completed"},
        {1: (completed_x, 0, completed_z), 2: (2, 0, completed_z)},
    )
    outcome = {
        **_lifecycle(case, "confirmed-altitude", "completed", source="autonomy"),
        "t": case.now_ms + 1,
    }
    if stale_first:
        stale = {
            **completed,
            "drones": [
                {**drone, "telemetry": {**drone["telemetry"], "t": case.now_ms}}
                for drone in completed["drones"]
            ],
        }
        pending.acknowledge(
            outcome,
            stale,
            capability_version=case.capability_version,
            rooms=case.rooms,
            now_ms=case.now_ms + 2,
        )
        assert pending.audit.records[-1]["event"] != "intent_accepted"

    def acknowledge() -> None:
        pending.acknowledge(
            outcome,
            completed,
            capability_version=case.capability_version,
            rooms=case.rooms,
            now_ms=case.now_ms + 2,
        )

    if (completed_z, completed_x) == (1.524, 0.0):
        acknowledge()
        assert pending.audit.records[-1]["event"] == "intent_accepted"
    else:
        with pytest.raises(ConfirmationError, match="position"):
            acknowledge()
