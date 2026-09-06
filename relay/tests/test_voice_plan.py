"""The relay-side plan compiler behind ``POST /api/sessions/{id}/transcripts``.

Provider calls are replayed or synthetic; nothing here reaches a network.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from evals.language_corpus import (
    CorpusCase,
    StaticResponseTransport,
    load_corpus,
    load_synthetic_responses,
)
from language.compiler import TranscriptCompiler
from language.contracts import NEGATED_TRANSCRIPT_DETAIL, build_grounding_facts
from language.relay_compiler import RelayTranscriptCompiler
from language.transport import (
    PINNED_COMPILER_MODEL,
    PROMPT_SCHEMA_VERSION,
    ModelRequest,
    ModelResponse,
    RecordingTransport,
    ReplayTransport,
    TransportError,
)
from planner.models import TranslationGrounding, TranslationPolicy
from relay.app import RelayRuntime, create_app
from relay.auth import Principal
from relay.autonomy import AutonomyConfig, create_autonomy_app
from relay.capabilities import C1_IMPLEMENTED_INTENT_NAMES, IntentName
from relay.intent_v1 import AcceptedIntent, validate_intent
from relay.main import (
    _qualified_voice_intents,
    build_transcript_service,
    transcript_service_factory,
)
from relay.session import RelaySession
from relay.settings import AdapterBackend, RelaySettings, SettingsError
from relay.tests.conftest import (
    ADAPTER_KEY,
    CONSOLE_KEY,
    SESSION,
    EventIds,
    MutableClock,
    membership_payload,
    profiled_sink,
    telemetry_payload,
)
from relay.voice import (
    AudioUpload,
    CompilerUnavailable,
    TranscriptService,
    UnavailableTranscriptCompiler,
    VoiceOutcome,
    VoicePlan,
    VoicePlanStep,
    compiler_capability_version,
    compiler_relay_state,
    parse_voice_outcome,
    parse_voice_plan,
)
from tests.autonomy_fixtures import planning_config, safety_config

_CORRELATION = "voice-plan-1"
_HEADERS = {
    "Authorization": f"Bearer {CONSOLE_KEY.decode()}",
    "Content-Type": "audio/webm",
    "X-Sweep-Correlation-Id": _CORRELATION,
}
_QUALIFIED_TEST_INTENTS = ",".join(
    sorted(name.value for name in C1_IMPLEMENTED_INTENT_NAMES if name is not IntentName.ESTOP)
)


@dataclass
class ClockedTranscription:
    """Returns a fixed transcript and advances the clock like a provider round trip.

    ``after`` runs once the clock has moved, standing in for the node telemetry that
    keeps streaming at 10 Hz while Whisper answers; without it the aircraft's last
    telemetry goes stale and it correctly stops being selectable.
    """

    transcript: str
    clock: MutableClock | None = None
    latency_ms: int = 3_000
    after: Callable[[], None] | None = None
    uploads: list[AudioUpload] = field(default_factory=list)

    def transcribe(self, upload: AudioUpload) -> str:
        self.uploads.append(upload)
        if self.clock is not None:
            self.clock.advance(self.latency_ms)
        if self.after is not None:
            self.after()
        return self.transcript


class FailingModelTransport:
    def complete(self, request: ModelRequest) -> ModelResponse:
        raise TransportError("provider offline")


def _duration(_upload: AudioUpload) -> int:
    return 1_000


def _config() -> AutonomyConfig:
    return AutonomyConfig(planning=planning_config(), safety=safety_config(), sim_camera=None)


def _settings(log_dir: Path) -> RelaySettings:
    return RelaySettings(
        relay_token=CONSOLE_KEY,
        adapter_keys={1: ADAPTER_KEY},
        log_dir=log_dir,
        adapter_backend=AdapterBackend.REMOTE,
    )


def _runtime(tmp_path: Path, clock: MutableClock, **kwargs: object) -> RelayRuntime:
    return RelayRuntime(
        _settings(tmp_path),
        clock=clock,
        event_ids=EventIds(),
        capability_profile=_config().planning.effective_capability_profile(),
        **kwargs,  # type: ignore[arg-type]
    )


_ADAPTER = Principal(source="adapter", drone_id=1, signing_key=ADAPTER_KEY)
_TELEMETRY_IDS = iter(range(1, 10_000))


def _fresh_telemetry(session: RelaySession, clock: MutableClock, state: str = "landed") -> None:
    session.process_telemetry(
        telemetry_payload(
            event_id=f"telemetry-{next(_TELEMETRY_IDS)}", timestamp=clock.value, state=state
        ),
        _ADAPTER,
    )


def _ready_landed_aircraft(session: RelaySession, clock: MutableClock) -> None:
    session.process_membership(
        membership_payload(action="join", event_id="join-1", timestamp=clock.value), _ADAPTER
    )
    _fresh_telemetry(session, clock)
    session.process_membership(
        membership_payload(action="readiness", event_id="ready-1", timestamp=clock.value),
        _ADAPTER,
    )
    session.update_control_projection(selection=(1,), armed=True)
    state = session.current_state()
    assert state["drones"][0]["selectable"] is True
    assert state["drones"][0]["flight_state"] == "landed"


def _post(app, body: bytes = b"audio") -> httpx.Response:
    async def request() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            return await client.post(
                f"/api/sessions/{SESSION}/transcripts", headers=_HEADERS, content=body
            )

    return asyncio.run(request())


def _wired_app(
    tmp_path: Path,
    clock: MutableClock,
    *,
    transcript: str,
    payload: object | None,
    transport: object | None = None,
    rooms: tuple[str, ...] = (),
    environ: dict[str, str] | None = None,
):
    settings = _settings(tmp_path)
    app = create_app(settings)
    runtime = _runtime(tmp_path, clock, authoritative_rooms_factory=lambda _session: rooms)
    app.state.relay_runtime = runtime
    session = runtime.session(SESSION)
    app.state.transcript_service = build_transcript_service(
        runtime,
        config=_config(),
        environ=(
            {"SWEEP_QUALIFIED_VOICE_INTENTS": _QUALIFIED_TEST_INTENTS}
            if environ is None
            else environ
        ),
        transport=transport if transport is not None else StaticResponseTransport(payload),
        transcription=ClockedTranscription(
            transcript, clock, after=lambda: _fresh_telemetry(session, clock)
        ),
    )
    app.state.transcript_service._duration_probe = _duration
    return app, runtime, session


def _takeoff_payload(selection: list[int]) -> dict[str, object]:
    return {
        "kind": "plan",
        "intents": [{"name": "takeoff", "args": {}, "selection": selection, "mode": "indoor"}],
    }


def _language_intent(
    plan: Mapping[str, object], *, timestamp: int, step_index: int = 0
) -> dict[str, object]:
    raw_steps = plan["steps"]
    assert isinstance(raw_steps, list)
    step = raw_steps[step_index]
    assert isinstance(step, Mapping)
    return {
        "v": 1,
        "t": timestamp,
        "type": "intent",
        "intent_id": step["intent_id"],
        "retry_of": None,
        "source": "language",
        "session": plan["session"],
        "name": step["name"],
        "args": _thaw(step["args"]),
        "selection": _thaw(step["selection"]),
        "mode": step["mode"],
        "confirm": True,
    }


def test_relay_main_factory_keeps_compiler_unavailable_without_anthropic_key(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    config = _config()
    app, composition = create_autonomy_app(
        settings, config, transcript_service_factory=transcript_service_factory(config, {})
    )
    with TestClient(app):
        service = app.state.transcript_service
        assert isinstance(service, TranscriptService)
        assert isinstance(service._compiler, UnavailableTranscriptCompiler)
    composition.close()

    keyed_app, keyed_composition = create_autonomy_app(
        settings,
        config,
        transcript_service_factory=transcript_service_factory(
            config, {"ANTHROPIC_API_KEY": "test-key-never-sent"}
        ),
    )
    with TestClient(keyed_app):
        keyed_service = keyed_app.state.transcript_service
        assert isinstance(keyed_service._compiler, RelayTranscriptCompiler)
        assert keyed_service._compiler.plan_ttl_ms == 30_000
        assert keyed_service._compiler.state_max_age_ms == 2_000
    keyed_composition.close()


def test_speech_pair_qualification_configuration_is_closed_and_immutable() -> None:
    profile = _config().planning.effective_capability_profile()

    assert _qualified_voice_intents({}, profile) == ()
    assert _qualified_voice_intents(
        {"SWEEP_QUALIFIED_VOICE_INTENTS": "takeoff, hold"}, profile
    ) == ("hold", "takeoff")
    for raw in ("takeoff,takeoff", "takeoff,", "unknown", "disarm", "map_area"):
        with pytest.raises(SettingsError, match="unique enabled Intent v1 names"):
            _qualified_voice_intents({"SWEEP_QUALIFIED_VOICE_INTENTS": raw}, profile)


def test_endpoint_without_anthropic_key_returns_typed_compiler_unavailable(
    tmp_path: Path, clock: MutableClock
) -> None:
    settings = _settings(tmp_path)
    app = create_app(settings)
    runtime = _runtime(tmp_path, clock)
    app.state.relay_runtime = runtime
    session = runtime.session(SESSION)
    app.state.transcript_service = build_transcript_service(
        runtime,
        config=_config(),
        environ={"ANTHROPIC_API_KEY": ""},
        transcription=ClockedTranscription(
            "Take off.", clock, after=lambda: _fresh_telemetry(session, clock)
        ),
    )
    app.state.transcript_service._duration_probe = _duration
    _ready_landed_aircraft(session, clock)

    response = _post(app)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "refused"
    assert body["reason"] == "compiler_unavailable"
    assert body["transcript"] == "Take off."
    assert body["emissions"] == []
    assert body["plan"] is None
    parse_voice_outcome(body, session_id=SESSION, correlation_id=_CORRELATION)


def test_endpoint_compiles_a_takeoff_plan_grounded_on_a_fresh_state_event(
    tmp_path: Path, clock: MutableClock
) -> None:
    app, runtime, session = _wired_app(
        tmp_path, clock, transcript="Take off.", payload=_takeoff_payload([1])
    )
    _ready_landed_aircraft(session, clock)
    started_ms = clock.value

    response = _post(app)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "transcribed"
    assert body["source"] == "whisper"
    assert body["reason"] is None
    assert body["transcript"] == "Take off."
    assert body["emissions"] == []
    plan = body["plan"]
    assert plan["v"] == 1
    assert plan["kind"] == "plan"
    assert plan["session"] == SESSION
    assert plan["correlation_id"] == _CORRELATION
    assert plan["transcript"] == "Take off."
    assert plan["reason"] is None
    assert plan["options"] == []
    assert plan["pending_intent_id"] is None
    assert plan["model"] == PINNED_COMPILER_MODEL
    assert plan["prompt_schema_version"] == PROMPT_SCHEMA_VERSION
    assert plan["response_source"] == "synthetic"
    assert plan["roster_version"] == session.registry.roster_version
    # The transcription advanced the clock past the compiler's two-second state age,
    # so the plan can only exist because the endpoint re-read the state afterwards.
    assert plan["compiled_at_ms"] == started_ms + 3_000
    assert plan["expires_at_ms"] == plan["compiled_at_ms"] + 30_000
    assert len(plan["plan_digest"]) == 64
    assert plan["steps"][0]["intent_id"].startswith("voice-")
    assert plan["steps"] == [
        {
            "index": 0,
            "intent_id": plan["steps"][0]["intent_id"],
            "name": "takeoff",
            "args": {},
            "selection": [1],
            "mode": "indoor",
            "confirm_required": True,
            "notes": [
                "Targets D-01 (the current selection).",
                "Flight state when compiled: D-01 landed.",
                "Climbs to the configured takeoff altitude; the session must be armed"
                " (armed yes when compiled).",
                "The arbiter requires operator confirmation before this step runs.",
            ],
        }
    ]
    parsed = parse_voice_outcome(body, session_id=SESSION, correlation_id=_CORRELATION)
    assert parsed.plan is not None and parsed.plan.to_dict() == plan
    assert CONSOLE_KEY.decode() not in response.text
    assert ADAPTER_KEY.decode() not in response.text

    records = runtime.replay(SESSION)["events"]
    compiled = [
        record["event"] for record in records if record["event"].get("event") == "plan_compiled"
    ]
    assert len(compiled) == 1
    assert compiled[0]["session"] == SESSION
    assert compiled[0]["correlation_id"] == _CORRELATION
    assert compiled[0]["plan_digest"] == plan["plan_digest"]
    assert compiled[0]["event_id"].startswith("server-event-")
    assert "transcript" not in json.dumps(compiled[0])
    assert compiled[0]["facts"]["state_event_id"] == plan["state_event_id"]
    bound = [
        record["event"] for record in records if record["event"].get("event") == "voice_plan_bound"
    ]
    assert len(bound) == 1
    assert bound[0]["plan_digest"] == plan["plan_digest"]
    assert bound[0]["intent_ids"] == [plan["steps"][0]["intent_id"]]


def test_language_emission_requires_the_exact_audited_step_and_is_single_use(
    tmp_path: Path, clock: MutableClock
) -> None:
    app, runtime, session = _wired_app(
        tmp_path, clock, transcript="Take off.", payload=_takeoff_payload([1])
    )
    _ready_landed_aircraft(session, clock)
    session.intent_sink = profiled_sink(lambda _intent, _state: None)
    principal = Principal(source="language", drone_id=None, signing_key=CONSOLE_KEY)

    tampered_plan = _post(app).json()["plan"]
    tampered = _language_intent(tampered_plan, timestamp=clock.value)
    tampered["selection"] = [2]
    refused = session.process_intent(tampered, principal)
    assert refused[0]["reason"] == "language_plan_mismatch"

    exact_plan = _post(app).json()["plan"]
    exact = _language_intent(exact_plan, timestamp=clock.value)
    accepted = session.process_intent(exact, principal)
    replayed = session.process_intent(exact, principal)
    assert accepted[0]["status"] == "accepted"
    assert accepted[0]["intent_id"] == exact_plan["steps"][0]["intent_id"]
    assert replayed[0]["reason"] == "duplicate_intent"

    stale_plan = _post(app).json()["plan"]
    session.update_control_projection(armed=False)
    stale = session.process_intent(_language_intent(stale_plan, timestamp=clock.value), principal)
    assert stale[0]["reason"] == "stale_language_plan"

    records = runtime.replay(SESSION)["events"]
    assert (
        len([record for record in records if record["event"].get("event") == "voice_plan_bound"])
        == 3
    )


def test_unqualified_speech_pair_cannot_mint_an_executable_plan(
    tmp_path: Path, clock: MutableClock
) -> None:
    app, runtime, session = _wired_app(
        tmp_path,
        clock,
        transcript="Take off.",
        payload=_takeoff_payload([1]),
        environ={},
    )
    _ready_landed_aircraft(session, clock)

    plan = _post(app).json()["plan"]

    assert (plan["kind"], plan["reason"], plan["steps"]) == (
        "unsupported",
        "capability_unavailable",
        [],
    )
    records = runtime.replay(SESSION)["events"]
    assert not any(
        record["event"].get("event") in {"plan_compiled", "voice_plan_bound"} for record in records
    )


def test_bound_plan_capacity_fails_explicitly_without_evicting_an_active_plan(
    tmp_path: Path, clock: MutableClock
) -> None:
    runtime = _runtime(tmp_path, clock)
    session = runtime.session(SESSION)
    _ready_landed_aircraft(session, clock)
    profile = _config().planning.effective_capability_profile()
    compiler = RelayTranscriptCompiler(
        sessions=lambda session_id: session if session_id == SESSION else None,
        transport=StaticResponseTransport(_takeoff_payload([1])),
        capability_profile=profile,
        qualified_voice_intents=("takeoff",),
    )

    plans = []
    for index in range(8):
        state = session.current_state()
        plan, _compiled = compiler.compile(
            "Take off.",
            state,
            capability_version=compiler_capability_version(state),
            now_ms=clock.value,
            correlation_id=f"capacity-{index}",
            session_id=SESSION,
        )
        plans.append(plan)

    state = session.current_state()
    with pytest.raises(CompilerUnavailable):
        compiler.compile(
            "Take off.",
            state,
            capability_version=compiler_capability_version(state),
            now_ms=clock.value,
            correlation_id="capacity-overflow",
            session_id=SESSION,
        )

    assert len({plan.plan_digest for plan in plans}) == 8
    records = session.audit_log.replay()
    assert (
        len([record for record in records if record["event"].get("event") == "voice_plan_bound"])
        == 8
    )


def test_recompiling_the_same_digest_cannot_reset_a_bound_plan(
    tmp_path: Path, clock: MutableClock
) -> None:
    runtime = _runtime(tmp_path, clock)
    session = runtime.session(SESSION)
    _ready_landed_aircraft(session, clock)
    profile = _config().planning.effective_capability_profile()
    compiler = RelayTranscriptCompiler(
        sessions=lambda session_id: session if session_id == SESSION else None,
        transport=StaticResponseTransport(_takeoff_payload([1])),
        capability_profile=profile,
        qualified_voice_intents=("takeoff",),
    )
    state = session.current_state()
    arguments = {
        "capability_version": compiler_capability_version(state),
        "now_ms": clock.value,
        "correlation_id": "duplicate-bound-plan",
        "session_id": SESSION,
    }

    compiler.compile("Take off.", state, **arguments)
    with pytest.raises(CompilerUnavailable):
        compiler.compile("Take off.", state, **arguments)

    assert (
        len(
            [
                record
                for record in session.audit_log.replay()
                if record["event"].get("event") == "voice_plan_bound"
            ]
        )
        == 1
    )


def test_bound_plan_steps_are_ordered_and_revalidate_the_projected_state(
    tmp_path: Path, clock: MutableClock
) -> None:
    runtime = _runtime(tmp_path, clock)
    session = runtime.session(SESSION)
    _ready_landed_aircraft(session, clock)
    session.intent_sink = profiled_sink(lambda _intent, _state: None)
    profile = _config().planning.effective_capability_profile()
    compiler = RelayTranscriptCompiler(
        sessions=lambda session_id: session if session_id == SESSION else None,
        transport=StaticResponseTransport(
            {
                "kind": "plan",
                "intents": [
                    {"name": "takeoff", "args": {}, "selection": [1], "mode": "indoor"},
                    {"name": "hold", "args": {}, "selection": [1], "mode": "indoor"},
                ],
            }
        ),
        capability_profile=profile,
        qualified_voice_intents=("hold", "takeoff"),
    )
    state = session.current_state()
    plan, _compiled = compiler.compile(
        "Take off and hold.",
        state,
        capability_version=compiler_capability_version(state),
        now_ms=clock.value,
        correlation_id="ordered-plan",
        session_id=SESSION,
    )
    wire = plan.to_dict()
    principal = Principal(source="language", drone_id=None, signing_key=CONSOLE_KEY)
    second = validate_intent(
        _language_intent(wire, timestamp=clock.value, step_index=1),
        capability_profile=profile,
    )
    assert isinstance(second, AcceptedIntent)
    assert compiler.authorize_intent(second.intent, session.current_state(), clock.value) == (
        "language_plan_out_of_order",
        "language plan steps must be emitted in order",
    )

    first_result = session.process_intent(_language_intent(wire, timestamp=clock.value), principal)
    assert first_result[0]["status"] == "accepted"
    _fresh_telemetry(session, clock, state="hovering")
    second_result = session.process_intent(
        _language_intent(wire, timestamp=clock.value, step_index=1), principal
    )
    assert second_result[0]["status"] == "accepted"


def test_stale_state_without_refresh_is_a_typed_refusal_not_a_plan(
    tmp_path: Path, clock: MutableClock
) -> None:
    runtime = _runtime(tmp_path, clock)
    session = runtime.session(SESSION)
    _ready_landed_aircraft(session, clock)
    service = build_transcript_service(
        runtime,
        config=_config(),
        environ={"SWEEP_QUALIFIED_VOICE_INTENTS": "takeoff"},
        transport=StaticResponseTransport(_takeoff_payload([1])),
        transcription=ClockedTranscription(
            "Take off.", clock, after=lambda: _fresh_telemetry(session, clock)
        ),
    )
    service._duration_probe = _duration
    state = session.current_state()

    outcome = service.process(
        session_id=SESSION,
        correlation_id=_CORRELATION,
        content_type="audio/webm",
        body=b"audio",
        relay_state=state,
        now_ms=clock.value + 3_000,
    )

    assert outcome.status == "transcribed"
    assert outcome.plan is not None
    assert (outcome.plan.kind, outcome.plan.reason) == ("refuse", "stale_state")
    assert outcome.plan.steps == ()


def test_ambiguous_transcript_returns_options_and_no_steps(
    tmp_path: Path, clock: MutableClock
) -> None:
    app, _runtime_, session = _wired_app(
        tmp_path,
        clock,
        transcript="Capture this room.",
        payload={"kind": "clarify", "reason": "ambiguous_location"},
        rooms=("living-room", "bedroom"),
    )
    _ready_landed_aircraft(session, clock)

    body = _post(app).json()

    assert body["status"] == "transcribed"
    plan = body["plan"]
    assert plan["kind"] == "clarify"
    assert plan["reason"] == "ambiguous_location"
    assert plan["options"] == ["living-room", "bedroom"]
    assert plan["steps"] == []
    assert plan["expires_at_ms"] is None
    assert plan["plan_digest"] is None
    parse_voice_outcome(body, session_id=SESSION, correlation_id=_CORRELATION)


def test_ambiguous_selection_offers_the_selectable_aircraft(
    tmp_path: Path, clock: MutableClock
) -> None:
    app, _runtime_, session = _wired_app(
        tmp_path,
        clock,
        transcript="Take off the one by the door.",
        payload={"kind": "clarify", "reason": "ambiguous_selection"},
    )
    _ready_landed_aircraft(session, clock)

    plan = _post(app).json()["plan"]

    assert plan["kind"] == "clarify"
    assert plan["options"] == ["D-01"]


def test_unsafe_phrases_are_refused_without_steps(tmp_path: Path, clock: MutableClock) -> None:
    estop_app, _runtime_, session = _wired_app(
        tmp_path,
        clock,
        transcript="Emergency stop.",
        payload={
            "kind": "plan",
            "intents": [{"name": "estop", "args": {}, "selection": [], "mode": "indoor"}],
        },
    )
    _ready_landed_aircraft(session, clock)

    plan = _post(estop_app).json()["plan"]

    assert (plan["kind"], plan["reason"]) == ("unsupported", "capability_unavailable")
    assert plan["steps"] == []

    stopped_app, _runtime_, stopped_session = _wired_app(
        tmp_path / "stopped",
        clock,
        transcript="Take off now.",
        payload=_takeoff_payload([1]),
    )
    _ready_landed_aircraft(stopped_session, clock)
    stopped_session.update_control_projection(estop=True)

    stopped_plan = _post(stopped_app).json()["plan"]

    assert (stopped_plan["kind"], stopped_plan["reason"]) == ("refuse", "invalid_model_output")
    assert stopped_plan["detail"] == "The proposed plan did not pass deterministic validation."
    assert stopped_plan["steps"] == []


_NEGATED_LIVE_TRANSCRIPTS = tuple(
    case.transcript for case in load_corpus() if case.category == "negation" and case.live_demo
)


@pytest.mark.parametrize("transcript", _NEGATED_LIVE_TRANSCRIPTS)
def test_negated_transcript_never_yields_steps_even_when_the_model_proposes_them(
    tmp_path: Path, clock: MutableClock, transcript: str
) -> None:
    # The corpus's negated live-demo utterances, against a mocked model that wrongly
    # answers a takeoff plan for each of them.
    assert len(_NEGATED_LIVE_TRANSCRIPTS) == 3
    app, runtime, session = _wired_app(
        tmp_path, clock, transcript=transcript, payload=_takeoff_payload([1])
    )
    _ready_landed_aircraft(session, clock)

    body = _post(app).json()

    assert body["status"] == "transcribed"
    assert body["transcript"] == transcript
    assert body["emissions"] == []
    plan = body["plan"]
    assert plan["kind"] == "clarify"
    assert plan["reason"] == "ambiguous_action"
    assert plan["detail"] == NEGATED_TRANSCRIPT_DETAIL
    assert plan["steps"] == []
    assert plan["options"] == []
    assert plan["expires_at_ms"] is None
    assert plan["plan_digest"] is None
    parse_voice_outcome(body, session_id=SESSION, correlation_id=_CORRELATION)
    records = runtime.replay(SESSION)["events"]
    assert not any(record["event"].get("event") == "plan_compiled" for record in records)


def test_provider_outage_is_compiler_unavailable_with_the_transcript_kept(
    tmp_path: Path, clock: MutableClock
) -> None:
    app, _runtime_, session = _wired_app(
        tmp_path,
        clock,
        transcript="Hold position.",
        payload=None,
        transport=FailingModelTransport(),
    )
    _ready_landed_aircraft(session, clock)

    body = _post(app).json()

    assert body["status"] == "refused"
    assert body["reason"] == "compiler_unavailable"
    assert body["transcript"] == "Hold position."
    assert body["plan"] is None


def test_refresh_failure_after_transcription_is_a_typed_state_refusal() -> None:
    def broken_refresh() -> tuple[object, int]:
        raise RuntimeError("session unusable")

    outcome = TranscriptService(
        transcription=ClockedTranscription("Hold position."),
        compiler=UnavailableTranscriptCompiler(),
        duration_probe=_duration,
    ).process(
        session_id=SESSION,
        correlation_id=_CORRELATION,
        content_type="audio/webm",
        body=b"audio",
        relay_state={
            "v": 1,
            "t": 1_756_700_000_000,
            "type": "state",
            "event_id": "state-1",
            "session": SESSION,
            "roster_version": 1,
            "armed": False,
            "estop": False,
            "selection": [],
            "mode": "indoor",
            "drones": [],
        },
        now_ms=1_756_700_000_000,
        refresh_state=broken_refresh,
    )

    assert (outcome.status, outcome.reason, outcome.transcript, outcome.plan) == (
        "refused",
        "invalid_relay_state",
        "Hold position.",
        None,
    )


def _plan(**changes: object) -> VoicePlan:
    values: dict[str, object] = {
        "kind": "plan",
        "transcript": "Take off.",
        "reason": None,
        "detail": None,
        "options": (),
        "steps": (
            VoicePlanStep(
                index=0,
                intent_id="voice-plan-step-0",
                name="takeoff",
                args={},
                selection=(1,),
                mode="indoor",
                confirm_required=True,
                notes=("Targets D-01 (the current selection).",),
            ),
        ),
        "compiled_at_ms": 1_756_700_000_000,
        "expires_at_ms": 1_756_700_030_000,
        "state_event_id": "state-1",
        "roster_version": 3,
        "session": SESSION,
        "correlation_id": _CORRELATION,
        "plan_digest": "a" * 64,
        "model": PINNED_COMPILER_MODEL,
        "prompt_schema_version": PROMPT_SCHEMA_VERSION,
        "response_source": "anthropic",
    }
    values.update(changes)
    return VoicePlan(**values)  # type: ignore[arg-type]


def test_voice_outcome_wire_shape_round_trips_and_rejects_widening() -> None:
    plan = _plan()
    outcome = VoiceOutcome("transcribed", "whisper", None, "Take off.", (), plan)
    wire = outcome.to_dict(session_id=SESSION, correlation_id=_CORRELATION)

    assert wire["emissions"] == []
    assert wire["plan"]["steps"][0]["confirm_required"] is True
    restored = parse_voice_outcome(wire, session_id=SESSION, correlation_id=_CORRELATION)
    assert restored == outcome
    assert parse_voice_plan(wire["plan"]) == plan

    deepgram = VoiceOutcome("transcribed", "deepgram", None, "Take off.")
    assert (
        parse_voice_outcome(deepgram.to_dict(session_id=SESSION, correlation_id=_CORRELATION))
        == deepgram
    )

    legacy = VoiceOutcome("refused", "template", "compiler_unavailable", "hold").to_dict(
        session_id=SESSION, correlation_id=_CORRELATION
    )
    assert legacy["plan"] is None
    assert parse_voice_outcome(legacy).plan is None

    with pytest.raises(ValueError, match="never carries emissions"):
        parse_voice_outcome({**wire, "emissions": [{"name": "takeoff"}]})
    with pytest.raises(ValueError, match="unexpected fields"):
        parse_voice_outcome({**wire, "extra": 1})
    with pytest.raises(ValueError, match="belong to its transcribed outcome"):
        parse_voice_outcome({**wire, "correlation_id": "other"})
    with pytest.raises(ValueError, match="belong to its transcribed outcome"):
        parse_voice_outcome({**wire, "status": "refused"})
    with pytest.raises(ValueError, match="unexpected fields"):
        parse_voice_plan({**wire["plan"], "emissions": []})
    with pytest.raises(ValueError, match="indexed in order"):
        parse_voice_plan({**wire["plan"], "steps": [{**wire["plan"]["steps"][0], "index": 1}]})
    with pytest.raises(ValueError, match="JSON-native"):
        _plan(
            steps=(
                VoicePlanStep(
                    0,
                    "voice-plan-step-0",
                    "takeoff",
                    {"z": object()},
                    (1,),
                    "indoor",
                    True,
                ),
            )
        )
    with pytest.raises(ValueError, match="requires steps"):
        _plan(steps=())
    with pytest.raises(ValueError, match="only a compiled plan"):
        _plan(kind="refuse", reason="stale_state")
    with pytest.raises(ValueError, match="typed reason"):
        _plan(kind="clarify", steps=(), expires_at_ms=None, plan_digest=None)
    with pytest.raises(ValueError, match="expire after"):
        _plan(expires_at_ms=1_756_700_000_000)
    with pytest.raises(ValueError, match="kind is unsupported"):
        _plan(kind="emit")

    eight_steps = tuple(
        VoicePlanStep(
            index=index,
            intent_id=f"voice-plan-step-{index}",
            name="hold",
            args={},
            selection=(1,),
            mode="indoor",
            confirm_required=False,
        )
        for index in range(8)
    )
    assert len(_plan(steps=eight_steps).steps) == 8
    with pytest.raises(ValueError, match="too many steps"):
        _plan(
            steps=(
                *eight_steps,
                VoicePlanStep(8, "voice-plan-step-8", "hold", {}, (1,), "indoor", False),
            )
        )


def test_compiler_result_bound_to_another_session_is_not_returned(
    tmp_path: Path, clock: MutableClock
) -> None:
    class ForeignPlanCompiler:
        def compile(self, transcript: str, relay_state: object, **_kwargs: object) -> object:
            return _plan(transcript=transcript, session="other-session"), None

    runtime = _runtime(tmp_path, clock)
    session = runtime.session(SESSION)
    outcome = TranscriptService(
        transcription=ClockedTranscription("Take off."),
        compiler=ForeignPlanCompiler(),
        duration_probe=_duration,
    ).process(
        session_id=SESSION,
        correlation_id=_CORRELATION,
        content_type="audio/webm",
        body=b"audio",
        relay_state=session.current_state(),
        now_ms=clock.value,
    )

    assert (outcome.status, outcome.reason, outcome.plan) == (
        "refused",
        "compiler_unavailable",
        None,
    )


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_thaw(item) for item in value]
    return value


def _corpus_state(case: CorpusCase) -> dict[str, object]:
    """The corpus state as the relay would project it: JSON-native, session bound."""
    state = _thaw(case.relay_state)
    assert isinstance(state, dict)
    return {**state, "v": 1, "event_id": f"state-{case.case_id}", "session": SESSION}


def _corpus_policy(case: CorpusCase) -> TranslationPolicy | None:
    if case.translation_frame is None:
        return None
    assert case.translation_step_m is not None
    return TranslationPolicy(frame=case.translation_frame, step_m=case.translation_step_m)


def _corpus_headings(case: CorpusCase) -> dict[int, float]:
    return {
        drone["drone_id"]: drone["heading_deg"]
        for drone in case.relay_state["drones"]
        if "heading_deg" in drone
    }


def test_live_demo_corpus_previews_through_the_relay_path_with_replay(
    tmp_path: Path,
) -> None:
    corpus = load_corpus()
    responses = load_synthetic_responses(corpus=corpus)
    live = [case for case in corpus if case.live_demo]
    assert len(live) == 23
    cassette = tmp_path / "relay-language-replay.json"

    for case in live:
        grounded = compiler_relay_state(_corpus_state(case))
        policy = _corpus_policy(case)
        qualified = tuple(
            sorted(
                {
                    intent["name"]
                    for intent in case.expected.get("intents", [])
                    if isinstance(intent, Mapping) and isinstance(intent.get("name"), str)
                }
            )
        )
        facts = build_grounding_facts(
            grounded,
            capability_version=compiler_capability_version(grounded),
            rooms=case.rooms,
            translation=(
                None
                if policy is None
                else TranslationGrounding(policy=policy, headings=_corpus_headings(case))
            ),
            qualified_voice_intents=qualified,
        )
        RecordingTransport(StaticResponseTransport(responses[case.case_id]), cassette).complete(
            ModelRequest(transcript=case.transcript, facts=facts.model_dict())
        )
    replay = ReplayTransport(cassette)

    seen_kinds: set[str] = set()
    compiled_records: list[dict[str, object]] = []
    for case in live:
        headings = _corpus_headings(case)
        case_runtime = _runtime(tmp_path / case.case_id, MutableClock(case.now_ms))
        relay_session = case_runtime.session(SESSION)
        qualified = tuple(
            sorted(
                {
                    intent["name"]
                    for intent in case.expected.get("intents", [])
                    if isinstance(intent, Mapping) and isinstance(intent.get("name"), str)
                }
            )
        )
        compiler = RelayTranscriptCompiler(
            sessions=lambda session_id, bound=relay_session: (
                bound if session_id == SESSION else None
            ),
            transport=replay,
            translation_policy=_corpus_policy(case),
            headings=lambda _state, headings=headings: headings,
            qualified_voice_intents=qualified,
        )
        outcome = TranscriptService(
            transcription=ClockedTranscription(case.transcript),
            compiler=compiler,
            duration_probe=_duration,
        ).process(
            session_id=SESSION,
            correlation_id=case.case_id,
            content_type="audio/webm",
            body=case.transcript.encode(),
            relay_state=_corpus_state(case),
            rooms=case.rooms,
            now_ms=case.now_ms,
        )

        assert outcome.status == "transcribed", case.case_id
        assert outcome.transcript == case.transcript
        plan = outcome.plan
        assert plan is not None, case.case_id
        assert plan.kind == case.expected["kind"], case.case_id
        assert plan.reason == case.expected.get("reason"), case.case_id
        assert plan.response_source == "replay"
        assert plan.state_event_id == f"state-{case.case_id}"
        seen_kinds.add(plan.kind)
        if plan.kind == "plan":
            assert plan.expires_at_ms == case.now_ms + 30_000
            expected = _thaw(case.expected["intents"])
            assert isinstance(expected, list)
            assert len(plan.steps) == len(expected)
            for step, want in zip(plan.steps, expected, strict=True):
                args = dict(step.args)
                want_args = dict(want["args"])
                if step.name == "capture_room":
                    # The relay mints the capture ID from its own correlation ID.
                    assert isinstance(args.pop("capture_id"), str)
                    want_args.pop("capture_id", None)
                assert (step.name, args, list(step.selection), step.mode) == (
                    want["name"],
                    want_args,
                    want["selection"],
                    want["mode"],
                ), case.case_id
                assert step.confirm_required is (
                    step.name in {"takeoff", "land", "land_all", "capture_room", "sweep"}
                )
                assert step.notes, case.case_id
        else:
            assert plan.steps == ()
            assert plan.options == (
                tuple(case.rooms) if plan.reason == "ambiguous_location" else ()
            ), case.case_id
        parse_voice_outcome(outcome.to_dict(session_id=SESSION, correlation_id=case.case_id))
        compiled_records.extend(
            record["event"]
            for record in relay_session.audit_log.replay()
            if record["event"].get("event") == "plan_compiled"
        )

    assert seen_kinds == {"plan", "clarify"}
    assert len(compiled_records) == 18
    assert all(record["response_source"] == "replay" for record in compiled_records)


def test_relay_compiler_uses_one_audited_language_compiler_per_session(
    relay_session: RelaySession,
) -> None:
    compiler = RelayTranscriptCompiler(
        sessions=lambda session_id: relay_session if session_id == SESSION else None,
        transport=StaticResponseTransport(_takeoff_payload([1])),
    )
    first = compiler._compiler_for(SESSION, relay_session)
    assert isinstance(first, TranscriptCompiler)
    assert compiler._compiler_for(SESSION, relay_session) is first
    with pytest.raises(CompilerUnavailable):
        compiler.compile(
            "Take off.",
            {"event_id": "state-1", "roster_version": 1},
            capability_version="relay-capabilities-x",
            now_ms=1,
            correlation_id=_CORRELATION,
            session_id="unknown-session",
        )
