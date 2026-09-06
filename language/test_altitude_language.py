from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from evals.language_corpus import StaticResponseTransport
from language.compiler import (
    CompiledPlan,
    ConfirmationError,
    ConfirmedPlan,
    InMemoryAuditSink,
    TranscriptCompiler,
)
from language.contracts import CompilerReason, OutcomeKind, ProposedIntent, intent_payload
from language.test_compiler import (
    _case,
    _hydrate_relay_from_snapshot,
    _lifecycle,
    _prepare_and_confirm,
    _snapshot_at,
    _state,
    _with_execution_positions,
)
from planner.controller import PreparedExecutionRouter
from planner.models import CommandOperation, LifecycleStatus, Position
from planner.planner import DeterministicPlanner
from relay.audit import SessionAuditLog
from relay.auth import Principal
from relay.capabilities import C1_CAPABILITY_PROFILE, CapabilityProfile
from relay.intent_v1 import AcceptedIntent, IntentName, Mode, RejectedIntent, validate_intent
from relay.session import RelayLimits, RelaySession
from tests.autonomy_fixtures import make_snapshot, make_stack, planning_config, replace_aircraft

NO_ALTITUDE_PROFILE = CapabilityProfile(
    "c1_without_altitude",
    C1_CAPABILITY_PROFILE.enabled_intent_names - {IntentName.ALTITUDE},
)


def _altitude_response(
    delta: object,
    *,
    selection: tuple[int, ...] = (1, 2),
) -> dict[str, object]:
    return {
        "kind": "plan",
        "intents": [
            {
                "name": "altitude",
                "args": {"delta": delta},
                "selection": list(selection),
                "mode": "indoor",
            }
        ],
    }


def _state_for_snapshot(case, snapshot, profile: CapabilityProfile) -> dict[str, object]:
    state = {**_state(case), "selection": list(snapshot.selection)}
    state.update(profile.state_value())
    state = _with_execution_positions(
        state,
        {
            drone_id: (aircraft.pose.x, aircraft.pose.y, aircraft.pose.z)
            for drone_id, aircraft in snapshot.aircraft.items()
        },
        {
            drone_id: (aircraft.home.x, aircraft.home.y, aircraft.home.z)
            for drone_id, aircraft in snapshot.aircraft.items()
            if aircraft.home is not None
        },
    )
    aircraft = snapshot.aircraft
    state["drones"] = [
        {**drone, "flight_state": aircraft[drone["drone_id"]].flight_state.value}
        for drone in state["drones"]
    ]
    return state


def _compile(
    transcript: str,
    response: dict[str, object],
    *,
    config=None,
    snapshot=None,
    requested_profile: CapabilityProfile = C1_CAPABILITY_PROFILE,
    state: dict[str, object] | None = None,
):
    case = _case("translate-selected")
    config = config or planning_config()
    snapshot = snapshot or _snapshot_at(make_snapshot(2), case.now_ms)
    controller, _, _, _, flight, _ = make_stack(
        snapshot,
        config=config,
        capability_profile=requested_profile,
    )
    profile = controller.planner.capability_profile
    state = state or _state_for_snapshot(case, snapshot, profile)
    altitude = config.altitude_grounding() if profile.supports(IntentName.ALTITUDE) else None
    outcome, plan = TranscriptCompiler(
        StaticResponseTransport(response), audit=InMemoryAuditSink()
    ).compile(
        transcript,
        state,
        capability_version=case.capability_version,
        rooms=case.rooms,
        altitude=altitude,
        capability_profile=profile,
        now_ms=case.now_ms,
    )
    return case, snapshot, config, state, controller, flight, outcome, plan


@pytest.mark.parametrize(
    ("transcript", "delta"),
    [
        ("fly up 1 foot", 0.6096),
        ("move down two feet", -1.2192),
        ("go up 1 metre", 2.0),
        ("go down 2 steps", -2.0),
        ("move up", 0.6096),
        ("fly one foot up", 0.6096),
        ("Please\u00a0go\u00a0up\u00a0one\u00a0foot！！！", 0.6096),
    ],
)
def test_relative_altitude_phrases_preserve_units_and_default_one_foot(
    transcript: str, delta: float
) -> None:
    *_, outcome, plan = _compile(transcript, _altitude_response(delta))

    assert outcome.kind is OutcomeKind.PLAN
    assert plan is not None
    assert dict(outcome.intents[-1].args) == {"delta": delta}


@pytest.mark.parametrize(
    ("transcript", "intents"),
    [
        (
            "select both drones, then go down 2 steps",
            [
                {"name": "select", "args": {"ids": [1, 2]}, "selection": [1, 2], "mode": "indoor"},
                {"name": "altitude", "args": {"delta": -2}, "selection": [1, 2], "mode": "indoor"},
            ],
        ),
        (
            "fly drone one up 1 foot",
            [
                {"name": "select", "args": {"ids": [1]}, "selection": [1], "mode": "indoor"},
                {"name": "altitude", "args": {"delta": 0.6096}, "selection": [1], "mode": "indoor"},
            ],
        ),
    ],
)
def test_relative_altitude_binds_explicit_aircraft_selection(
    transcript: str, intents: list[dict[str, object]]
) -> None:
    *_, outcome, plan = _compile(transcript, {"kind": "plan", "intents": intents})

    assert outcome.kind is OutcomeKind.PLAN
    assert plan is not None


@pytest.mark.parametrize(
    "transcript",
    [
        "go up 0 feet",
        "go up -1 foot",
        "go up 1 yard",
        f"go up {'9' * 400} metres",
        "go up 1 foot and land",
        "turn then go up 1 foot",
        "move 1 foot",
        "hover",
        "hover at 5 feet",
    ],
)
def test_altitude_plan_is_fail_closed_when_transcript_is_not_exact_relative_motion(
    transcript: str,
) -> None:
    *_, outcome, plan = _compile(transcript, _altitude_response(1))

    assert outcome.kind is OutcomeKind.REFUSE
    assert outcome.reason is CompilerReason.INVALID_MODEL_OUTPUT
    assert plan is None


@pytest.mark.parametrize("delta", [-0.6096, 1, 0.6])
def test_provider_cannot_change_relative_altitude_sign_or_distance(delta: float) -> None:
    *_, outcome, plan = _compile("fly up 1 foot", _altitude_response(delta))

    assert outcome.kind is OutcomeKind.REFUSE
    assert outcome.reason is CompilerReason.INVALID_MODEL_OUTPUT
    assert plan is None


def test_absolute_height_argument_never_reenters_frozen_intent_v1() -> None:
    case, _, _, _, controller, _, outcome, plan = _compile(
        "hover at 5 feet",
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
        },
    )
    assert outcome.kind is OutcomeKind.REFUSE
    assert plan is None

    payload = intent_payload(
        ProposedIntent(IntentName.ALTITUDE, {"delta": 1}, (1, 2), Mode.INDOOR),
        session="language-eval",
        intent_id="frozen-altitude",
        timestamp_ms=case.now_ms,
    )
    assert isinstance(
        validate_intent(payload, capability_profile=controller.planner.capability_profile),
        AcceptedIntent,
    )
    payload["args"] = {"height_m": 1.524}
    assert isinstance(
        validate_intent(payload, capability_profile=controller.planner.capability_profile),
        RejectedIntent,
    )


def test_plain_hover_remains_hold_when_altitude_is_enabled() -> None:
    response = {
        "kind": "plan",
        "intents": [{"name": "hold", "args": {}, "selection": [1, 2], "mode": "indoor"}],
    }
    *_, outcome, plan = _compile("hover", response)

    assert outcome.kind is OutcomeKind.PLAN
    assert outcome.intents[0].name is IntentName.HOLD
    assert plan is not None


def test_effective_profile_disables_ungrounded_altitude_and_rejects_profile_drift() -> None:
    disabled = replace(
        planning_config(),
        altitude_step_m=None,
        altitude_floor_z_m=None,
        altitude_configuration_id=None,
        altitude_completion_tolerance_m=None,
    )
    *_, outcome, plan = _compile("go up one foot", _altitude_response(0.6096), config=disabled)
    assert outcome.kind is OutcomeKind.REFUSE
    assert plan is None

    case = _case("translate-selected")
    snapshot = _snapshot_at(make_snapshot(2), case.now_ms)
    config = planning_config()
    controller, _, _, _, _, _ = make_stack(snapshot, config=config)
    state = _state_for_snapshot(case, snapshot, controller.planner.capability_profile)
    state["enabled_intent_names"] = state["enabled_intent_names"][:-1]
    refused, refused_plan = TranscriptCompiler(
        StaticResponseTransport(_altitude_response(0.6096)), audit=InMemoryAuditSink()
    ).compile(
        "go up one foot",
        state,
        capability_version=case.capability_version,
        rooms=case.rooms,
        altitude=config.altitude_grounding(),
        capability_profile=controller.planner.capability_profile,
        now_ms=case.now_ms,
    )
    assert refused.kind is OutcomeKind.REFUSE
    assert refused.reason is CompilerReason.STALE_STATE
    assert refused_plan is None


@pytest.mark.parametrize("advertised_fields", [{"capability_profile"}, {"enabled_intent_names"}])
def test_partial_advertised_profile_is_rejected_even_with_an_explicit_profile(
    advertised_fields: set[str],
) -> None:
    case = _case("translate-selected")
    snapshot = _snapshot_at(make_snapshot(2), case.now_ms)
    config = planning_config()
    state = _state_for_snapshot(case, snapshot, C1_CAPABILITY_PROFILE)
    for field in {"capability_profile", "enabled_intent_names"} - advertised_fields:
        state.pop(field)

    outcome, plan = TranscriptCompiler(
        StaticResponseTransport(_altitude_response(0.6096)), audit=InMemoryAuditSink()
    ).compile(
        "go up one foot",
        state,
        capability_version=case.capability_version,
        rooms=case.rooms,
        altitude=config.altitude_grounding(),
        capability_profile=C1_CAPABILITY_PROFILE,
        now_ms=case.now_ms,
    )

    assert outcome.kind is OutcomeKind.REFUSE
    assert outcome.reason is CompilerReason.STALE_STATE
    assert plan is None


def test_advertised_profile_is_bound_when_the_argument_is_omitted() -> None:
    case = _case("translate-selected")
    snapshot = _snapshot_at(make_snapshot(2), case.now_ms)
    state = _state_for_snapshot(case, snapshot, C1_CAPABILITY_PROFILE)
    response = {
        "kind": "plan",
        "intents": [{"name": "hold", "args": {}, "selection": [1, 2], "mode": "indoor"}],
    }

    outcome, plan = TranscriptCompiler(
        StaticResponseTransport(response), audit=InMemoryAuditSink()
    ).compile(
        "hover",
        state,
        capability_version=case.capability_version,
        rooms=case.rooms,
        now_ms=case.now_ms,
    )

    assert outcome.kind is OutcomeKind.PLAN
    assert plan is not None
    assert plan.facts.capability_profile == C1_CAPABILITY_PROFILE
    assert plan.facts.altitude is None


def test_compiled_altitude_audit_round_trip_preserves_exact_profile_and_grounding() -> None:
    *_, outcome, plan = _compile("go up one foot", _altitude_response(0.6096))
    assert outcome.kind is OutcomeKind.PLAN and plan is not None

    restored = CompiledPlan.from_audit_event(plan.audit_record())

    assert restored == plan
    assert restored.facts.capability_profile == C1_CAPABILITY_PROFILE
    assert restored.facts.altitude == planning_config().altitude_grounding()


@pytest.mark.parametrize("mutation", ["huge_step", "duplicate_profile", "profile_order"])
def test_compiled_altitude_audit_rejects_noncanonical_or_unbounded_grounding(
    mutation: str,
) -> None:
    *_, plan = _compile("go up one foot", _altitude_response(0.6096))[-2:]
    assert plan is not None
    record = deepcopy(plan.audit_record())
    facts = record["facts"]
    if mutation == "huge_step":
        facts["altitude"]["step_m"] = 10**400
    elif mutation == "duplicate_profile":
        facts["enabled_intent_names"].append(facts["enabled_intent_names"][0])
    else:
        facts["enabled_intent_names"].reverse()

    with pytest.raises(ValueError):
        CompiledPlan.from_audit_event(record)


def test_altitude_confirmation_requires_and_exposes_exact_planner_preview() -> None:
    case, snapshot, config, state, controller, _, outcome, plan = _compile(
        "go up one foot", _altitude_response(0.6096)
    )
    assert outcome.kind is OutcomeKind.PLAN and plan is not None
    unprepared = ConfirmedPlan(plan, session="language-eval", audit=InMemoryAuditSink())
    with pytest.raises(ConfirmationError, match="issued planner result"):
        unprepared.confirm_next(
            state,
            capability_version=case.capability_version,
            rooms=case.rooms,
            now_ms=case.now_ms + 1,
            intent_id="unprepared-altitude",
            emit=lambda _intent: None,
        )

    pending = ConfirmedPlan(plan, session="language-eval", audit=InMemoryAuditSink())
    router = PreparedExecutionRouter(controller, current_snapshot=lambda: snapshot)
    prepared = pending.prepare_next(
        state,
        capability_version=case.capability_version,
        rooms=case.rooms,
        now_ms=case.now_ms + 1,
        intent_id="previewed-altitude",
        router=router,
        snapshot=snapshot,
    )

    assert prepared.execution.plan.altitude_grounding == config.altitude_grounding()
    assert [command.operation for command in prepared.execution.plan.commands] == [
        CommandOperation.GOTO,
        CommandOperation.HOVER,
    ] * 2
    assert [
        Position.from_mapping(command.parameters).z
        for command in prepared.execution.plan.commands
        if command.operation is CommandOperation.GOTO
    ] == pytest.approx([1.3048, 1.3048])


@pytest.mark.parametrize(
    "change",
    [
        {"altitude_step_m": 1.0},
        {"altitude_floor_z_m": -1.0},
        {"altitude_configuration_id": "resurvey-v2"},
        {"altitude_completion_tolerance_m": 0.1},
    ],
)
def test_altitude_configuration_drift_blocks_preview_before_adapter_io(change) -> None:
    case, snapshot, config, state, controller, flight, outcome, plan = _compile(
        "go up one foot", _altitude_response(0.6096)
    )
    assert outcome.kind is OutcomeKind.PLAN and plan is not None
    controller.planner = DeterministicPlanner(replace(config, **change))
    pending = ConfirmedPlan(plan, session="language-eval", audit=InMemoryAuditSink())

    with pytest.raises(ConfirmationError, match="altitude configuration"):
        pending.prepare_next(
            state,
            capability_version=case.capability_version,
            rooms=case.rooms,
            now_ms=case.now_ms + 1,
            intent_id="drifted-altitude",
            router=PreparedExecutionRouter(controller, current_snapshot=lambda: snapshot),
            snapshot=snapshot,
        )
    assert flight.calls == []


def test_capability_profile_drift_blocks_confirmed_altitude_before_adapter_io() -> None:
    case, snapshot, config, state, controller, flight, outcome, plan = _compile(
        "go up one foot", _altitude_response(0.6096)
    )
    assert outcome.kind is OutcomeKind.PLAN and plan is not None
    pending = ConfirmedPlan(plan, session="language-eval", audit=InMemoryAuditSink())
    router = PreparedExecutionRouter(controller, current_snapshot=lambda: snapshot)
    prepared = pending.prepare_next(
        state,
        capability_version=case.capability_version,
        rooms=case.rooms,
        now_ms=case.now_ms + 1,
        intent_id="profile-drift-altitude",
        router=router,
        snapshot=snapshot,
    )
    with TemporaryDirectory() as directory:
        relay = RelaySession(
            session_id="language-eval",
            audit_log=SessionAuditLog(Path(directory), "language-eval"),
            limits=RelayLimits(5_000, 5_000, 1_000, 1_000),
            clock=lambda: case.now_ms + 1,
            intent_sink=router,
        )
        _hydrate_relay_from_snapshot(relay, snapshot)
        emitter = router.relay_emitter(
            relay, Principal(source="console", drone_id=None, signing_key=b"x" * 32)
        )
        controller.planner = DeterministicPlanner(config, NO_ALTITUDE_PROFILE)
        with pytest.raises(ConfirmationError, match="issued planner result"):
            pending.confirm_next(
                state,
                capability_version=case.capability_version,
                rooms=case.rooms,
                now_ms=case.now_ms + 1,
                intent_id="profile-drift-altitude",
                emit=emitter,
                prepared=prepared,
            )
    assert flight.calls == []


def _issued_altitude(delta: float = 0.6096):
    case, snapshot, config, state, controller, _, outcome, plan = _compile(
        "go up one foot", _altitude_response(delta)
    )
    assert outcome.kind is OutcomeKind.PLAN and plan is not None
    pending = ConfirmedPlan(plan, session="language-eval", audit=InMemoryAuditSink())
    emitted = _prepare_and_confirm(
        pending,
        state,
        case,
        controller=controller,
        snapshot=snapshot,
        now_ms=case.now_ms + 1,
        intent_id="confirmed-altitude",
    )
    return case, state, pending, emitted


@pytest.mark.parametrize(
    ("offset", "flight_state", "passes"),
    [
        ((0.0, 0.0, 0.0), "hovering", True),
        ((0.02, 0.02, 0.02), "hovering", True),
        ((0.051, 0.0, 0.0), "hovering", False),
        ((0.0, 0.051, 0.0), "hovering", False),
        ((0.0, 0.0, 0.051), "hovering", False),
        ((0.0, 0.0, 0.0), "airborne", False),
    ],
)
def test_confirmed_altitude_completion_requires_3d_configured_tolerance_and_hover(
    offset: tuple[float, float, float], flight_state: str, passes: bool
) -> None:
    case, state, pending, emitted = _issued_altitude()
    targets = {1: (0.0, 0.0, 1.3048), 2: (2.0, 0.0, 1.3048)}
    completed = _with_execution_positions(
        {**state, "t": emitted.t + 2, "event_id": "altitude-completed"},
        {
            drone_id: tuple(value + change for value, change in zip(target, offset, strict=True))
            for drone_id, target in targets.items()
        },
    )
    completed["drones"] = [
        {**drone, "flight_state": flight_state} if drone["drone_id"] == 1 else drone
        for drone in completed["drones"]
    ]

    def acknowledge() -> None:
        pending.acknowledge(
            {
                **_lifecycle(case, emitted.intent_id, "completed", source="autonomy"),
                "t": emitted.t + 1,
            },
            completed,
            capability_version=case.capability_version,
            rooms=case.rooms,
            now_ms=emitted.t + 2,
        )

    if passes:
        acknowledge()
        assert pending.remaining == 0
    else:
        with pytest.raises(ConfirmationError, match="position"):
            acknowledge()


def test_confirmed_altitude_waits_for_fresh_post_dispatch_telemetry() -> None:
    case, state, pending, emitted = _issued_altitude()
    target = {1: (0.0, 0.0, 1.3048), 2: (2.0, 0.0, 1.3048)}
    completed = {
        **_lifecycle(case, emitted.intent_id, "completed", source="autonomy"),
        "t": emitted.t + 1,
    }
    stale = _with_execution_positions(
        {**state, "t": emitted.t + 1, "event_id": "stale-altitude"}, target
    )
    stale["drones"] = [
        {**drone, "telemetry": {**drone["telemetry"], "t": emitted.t}} for drone in stale["drones"]
    ]
    pending.acknowledge(
        completed,
        stale,
        capability_version=case.capability_version,
        rooms=case.rooms,
        now_ms=emitted.t + 1,
    )
    assert pending.remaining == 0
    assert pending.audit.records[-1]["event"] != "intent_accepted"

    fresh = _with_execution_positions(
        {**state, "t": emitted.t + 2, "event_id": "fresh-altitude"}, target
    )
    pending.acknowledge(
        completed,
        fresh,
        capability_version=case.capability_version,
        rooms=case.rooms,
        now_ms=emitted.t + 2,
    )
    assert pending.audit.records[-1]["event"] == "intent_accepted"


def test_relative_altitude_runs_compiler_through_planner_arbiter_and_simulator() -> None:
    case, snapshot, config, state, controller, flight, outcome, plan = _compile(
        "go up one foot", _altitude_response(0.6096)
    )
    assert outcome.kind is OutcomeKind.PLAN and plan is not None

    def current_snapshot():
        now_ms = case.now_ms + max(1, len(flight.calls))
        position_time_ms = case.now_ms if not flight.calls else now_ms
        current = replace(snapshot, now_ms=now_ms, operator_last_seen_ms=now_ms)
        for drone_id, simulated in flight.aircraft.items():
            current = replace_aircraft(
                current,
                drone_id,
                pose=simulated.pose,
                flight_state=simulated.flight_state,
                armed=simulated.armed,
                position_last_seen_ms=position_time_ms,
                link_last_seen_ms=now_ms,
            )
        return current

    accepted = validate_intent(
        intent_payload(
            outcome.intents[0],
            session="language-eval",
            intent_id="compiler-sim-altitude",
            timestamp_ms=case.now_ms + 1,
        ),
        capability_profile=controller.planner.capability_profile,
    )
    assert isinstance(accepted, AcceptedIntent)
    result = controller.execute(
        accepted.intent,
        snapshot,
        current_snapshot=current_snapshot,
    )

    assert result.status is LifecycleStatus.COMPLETED
    assert result.plan is not None
    assert result.plan.altitude_grounding == config.altitude_grounding()
    assert [call.operation for call in flight.calls] == [
        CommandOperation.GOTO,
        CommandOperation.HOVER,
    ] * 2
    assert [aircraft.pose.z for aircraft in flight.aircraft.values()] == pytest.approx(
        [1.3048, 1.3048]
    )
