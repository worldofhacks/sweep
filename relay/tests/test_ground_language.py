"""Synthetic ground vocabulary evidence only; never qualifies live speech or hardware."""

from copy import deepcopy

import pytest

from evals.language_corpus import StaticResponseTransport
from language.compiler import CompiledPlan
from language.ground import pulse_arguments
from language.relay_compiler import RelayTranscriptCompiler
from relay.capabilities import C1_CAPABILITY_PROFILE, with_ground_capabilities
from relay.intent_v1 import AcceptedIntent, validate_intent
from relay.tests.conftest import SESSION
from relay.voice import compiler_capability_version

PROFILE = with_ground_capabilities(C1_CAPABILITY_PROFILE)


def state(now):
    return {
        "v": 1,
        "type": "state",
        "event_id": "ground-state",
        "t": now,
        "session": SESSION,
        "mode": "indoor",
        "roster_version": 10,
        "armed": False,
        "estop": False,
        "selection": [11],
        **PROFILE.state_value(),
        "drones": [
            {
                "drone_id": 11,
                "unit": 1,
                "node_type": "ground",
                "connection_epoch": 4,
                "membership": "ready",
                "selectable": True,
                "control_authority": True,
                "ground_readiness": {"source_id": "isolated-ground-pose"},
                "adapter_capabilities": ["ground_drive"],
                "flight_state": None,
                "camera_patterns": [],
                "telemetry": None,
                "home_pose": None,
            }
        ],
    }


def compiler_for(session, name="ground_velocity", args=None, qualified=True):
    payload = {
        "kind": "plan",
        "intents": [
            {
                "name": name,
                "args": args if args is not None else pulse_arguments("pulse forward"),
                "selection": [11],
                "mode": "indoor",
            }
        ],
    }
    return RelayTranscriptCompiler(
        sessions=lambda session_id: session if session_id == SESSION else None,
        transport=StaticResponseTransport(payload),
        capability_profile=PROFILE,
        qualified_voice_intents=(name,) if qualified else (),
    )


def compile_plan(compiler, current, transcript="pulse forward"):
    return compiler.compile(
        transcript,
        current,
        capability_version=compiler_capability_version(current),
        now_ms=current["t"],
        correlation_id="isolated-ground-vocabulary",
        session_id=SESSION,
    )


def intent_for(plan, now):
    step = plan.steps[0]
    result = validate_intent(
        {
            "v": 1,
            "type": "intent",
            "t": now,
            "intent_id": step.intent_id,
            "retry_of": None,
            "source": "language",
            "session": SESSION,
            "name": step.name,
            "args": dict(step.args),
            "selection": list(step.selection),
            "mode": "indoor",
            "confirm": True,
        },
        capability_profile=PROFILE,
    )
    assert isinstance(result, AcceptedIntent)
    return result.intent


def test_exact_ground_plan_persists_reloads_and_consumes_once(relay_session, clock):
    current = state(clock.value)
    compiler = compiler_for(relay_session)
    plan, compiled = compile_plan(compiler, current)
    assert plan.kind == "plan" and compiled is not None
    assert plan.steps[0].confirm_required is True
    assert plan.steps[0].selection == (11,)
    assert any("G-01 (wire ID 11)" in note for note in plan.steps[0].notes)
    assert compiled.facts.drones[0]["position"] is None
    records = [record["event"] for record in relay_session.audit_log.replay()]
    restored = CompiledPlan.from_audit_event(
        next(record for record in records if record.get("event") == "plan_compiled")
    )
    assert restored.digest == compiled.digest
    intent = intent_for(plan, clock.value)
    assert compiler.authorize_intent(intent, current, clock.value) is None
    assert (
        compiler.authorize_intent(intent, current, clock.value)[0] == "language_plan_out_of_order"
    )


@pytest.mark.parametrize(
    "change", ["epoch", "source", "authority", "unit", "selection", "capability", "expiry"]
)
def test_ground_voice_preview_binds_authoritative_identity_and_admission(
    relay_session, clock, change
):
    current = state(clock.value)
    compiler = compiler_for(relay_session)
    plan, _ = compile_plan(compiler, current)
    intent = intent_for(plan, clock.value)
    changed = deepcopy(current)
    drone = changed["drones"][0]
    if change == "epoch":
        drone["connection_epoch"] += 1
    if change == "source":
        drone["ground_readiness"]["source_id"] = "replacement"
    if change == "authority":
        drone["control_authority"] = False
    if change == "unit":
        drone["unit"] = 2
    if change == "selection":
        changed["selection"] = []
    if change == "capability":
        changed["enabled_intent_names"].remove("ground_velocity")
    assert (
        compiler.authorize_intent(
            intent, changed, clock.value + (30001 if change == "expiry" else 0)
        )
        is not None
    )


@pytest.mark.parametrize(
    "phrase",
    [
        "move forward two metres",
        "turn left ninety degrees",
        "pulse forward 2 metres",
        "do not pulse forward",
        "return home",
        "pulse forward then return home",
    ],
)
def test_model_cannot_convert_unrepresented_language_to_guessed_pulses(
    relay_session, clock, phrase
):
    plan, compiled = compile_plan(compiler_for(relay_session), state(clock.value), phrase)
    assert plan.kind != "plan" and compiled is None


def test_unqualified_spoken_ground_pair_stays_unavailable(relay_session, clock):
    plan, compiled = compile_plan(compiler_for(relay_session, qualified=False), state(clock.value))
    assert plan.kind == "unsupported" and compiled is None


def test_return_plan_carries_no_client_route_and_requires_ground_identity(relay_session, clock):
    plan, compiled = compile_plan(
        compiler_for(relay_session, name="come_home", args={}), state(clock.value), "return home"
    )
    assert plan.kind == "plan" and compiled is not None
    assert dict(plan.steps[0].args) == {}
    assert plan.steps[0].confirm_required is True
    assert any("separately approved return" in note for note in plan.steps[0].notes)


@pytest.mark.parametrize("source", ["console", "webcam", "language"])
def test_ground_source_ceiling_requires_exact_scope_and_confirmation(source):
    envelope = {
        "v": 1,
        "type": "intent",
        "t": 1,
        "intent_id": "scope",
        "retry_of": None,
        "source": source,
        "session": SESSION,
        "name": "ground_velocity",
        "args": pulse_arguments("pulse left"),
        "selection": [11],
        "mode": "indoor",
        "confirm": True,
    }
    assert isinstance(validate_intent(envelope, capability_profile=PROFILE), AcceptedIntent)
    assert not isinstance(
        validate_intent({**envelope, "confirm": False}, capability_profile=PROFILE), AcceptedIntent
    )
    assert not isinstance(
        validate_intent({**envelope, "selection": [11, 12]}, capability_profile=PROFILE),
        AcceptedIntent,
    )
    assert not isinstance(
        validate_intent(envelope, capability_profile=C1_CAPABILITY_PROFILE), AcceptedIntent
    )


def test_transcript_service_preserves_ground_projection_across_provider_round_trip(
    relay_session, clock
):
    from relay.voice import TranscriptService

    class IsolatedTranscription:
        def transcribe(self, _upload):
            return "pulse forward"

    compiler = compiler_for(relay_session)
    service = TranscriptService(
        transcription=IsolatedTranscription(), compiler=compiler, duration_probe=lambda _: 100
    )
    outcome = service.process(
        session_id=SESSION,
        correlation_id="isolated-provider-roundtrip",
        content_type="audio/webm",
        body=b"synthetic audio fixture",
        relay_state=state(clock.value),
        now_ms=clock.value,
        refresh_state=lambda: (state(clock.value + 1), clock.value + 1),
    )
    assert outcome.plan is not None and outcome.plan.kind == "plan"
    step = outcome.plan.steps[0]
    assert step.selection == (11,) and step.name == "ground_velocity"
    intent = intent_for(outcome.plan, clock.value + 1)
    assert compiler.authorize_intent(intent, state(clock.value + 1), clock.value + 1) is None


def test_supervised_profile_metadata_survives_voice_projection_and_audit():
    from language.contracts import GroundingFacts, build_grounding_facts
    from relay.supervised_vertical import SUPERVISED_VERTICAL_PROFILE
    from relay.voice import compiler_relay_state

    raw = state(100)
    raw.update(SUPERVISED_VERTICAL_PROFILE.state_value())
    projected = compiler_relay_state(raw)
    assert projected["requires_home_pose"] is False
    facts = build_grounding_facts(
        projected,
        capability_version="isolated-supervised",
        capability_profile=SUPERVISED_VERTICAL_PROFILE,
    )
    assert facts.capability_profile.requires_home_pose is False
    assert (
        GroundingFacts.from_record(facts.record_dict()).capability_profile
        == SUPERVISED_VERTICAL_PROFILE
    )


@pytest.mark.parametrize("qualified", [False, True])
def test_supervised_transcript_factory_preserves_qualification_gate(
    relay_session, clock, qualified
):
    from types import SimpleNamespace

    from relay.autonomy import AutonomyConfig
    from relay.main import build_transcript_service
    from relay.supervised_vertical import SUPERVISED_VERTICAL_PROFILE, SupervisedVerticalConfig

    class IsolatedTranscription:
        def transcribe(self, _upload):
            return "pulse forward"

    config = AutonomyConfig(
        supervised_vertical=SupervisedVerticalConfig(
            takeoff_altitude_m=1.8,
            maximum_height_m=2.0,
            operator_declared_vertical_clearance_m=2.0,
            min_battery_fraction=0.3,
            min_link_quality=0.5,
            max_link_age_ms=5000,
            max_local_height_age_ms=500,
            operator_timeout_ms=5000,
            max_future_clock_skew_ms=1000,
            motion_conflict_window_ms=500,
        )
    )
    runtime = SimpleNamespace(
        capability_profile=SUPERVISED_VERTICAL_PROFILE, sessions={SESSION: relay_session}
    )
    payload = {
        "kind": "plan",
        "intents": [
            {
                "name": "ground_velocity",
                "args": pulse_arguments("pulse forward"),
                "selection": [11],
                "mode": "indoor",
            }
        ],
    }
    service = build_transcript_service(
        runtime,
        config=config,
        environ={"SWEEP_QUALIFIED_VOICE_INTENTS": "ground_velocity" if qualified else ""},
        transport=StaticResponseTransport(payload),
        transcription=IsolatedTranscription(),
    )
    assert isinstance(service._compiler, RelayTranscriptCompiler)
    assert service._compiler._translation_policy is None
    current = state(clock.value)
    current.update(SUPERVISED_VERTICAL_PROFILE.state_value())
    plan, compiled = compile_plan(service._compiler, current)
    assert plan.kind == ("plan" if qualified else "unsupported")
    assert (compiled is not None) is qualified


def test_voice_ground_projection_supports_a_mixed_roster_and_preserves_wire_bounds():
    from relay.intent_v1 import MAX_INTENT_DRONE_IDS
    from relay.voice import compiler_relay_state

    raw = state(100)
    ground = raw["drones"][0]
    raw["drones"] = [{**ground, "drone_id": 11 + index, "unit": index + 1} for index in range(3)]
    raw["drones"] += [
        {
            "drone_id": index,
            "membership": "ready",
            "selectable": True,
            "flight_state": "hovering",
            "camera_patterns": [],
            "adapter_capabilities": ["flight"],
        }
        for index in (1, 2)
    ]
    projected = compiler_relay_state(raw)
    assert [device["drone_id"] for device in projected["drones"]] == [11, 12, 13, 1, 2]
    assert projected["drones"][0]["unit"] == 1
    assert projected["drones"][0]["ground_readiness"] == {"source_id": "isolated-ground-pose"}
    assert "ground_readiness" not in projected["drones"][-1]
    raw["drones"] = [{**ground, "drone_id": index + 1} for index in range(MAX_INTENT_DRONE_IDS + 1)]
    with pytest.raises(ValueError, match="bounded"):
        compiler_relay_state(raw)


def test_aircraft_return_retains_c1_confirmation_metadata(relay_session, clock):
    current = state(clock.value)
    current["armed"] = True
    current["drones"][0].update(
        node_type="aircraft", flight_state="hovering", adapter_capabilities=["flight"]
    )
    plan, compiled = compile_plan(
        compiler_for(relay_session, name="come_home", args={}), current, "return home"
    )
    assert plan.kind == "plan" and compiled is not None
    assert plan.steps[0].confirm_required is False
