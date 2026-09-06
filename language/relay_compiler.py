"""Bind the pinned transcript compiler to the relay's voice endpoint.

``RelayTranscriptCompiler`` implements the ``TranscriptCompiler`` protocol that
``relay.voice.TranscriptService`` calls after Whisper returns a transcript. It
grounds one ``language.compiler.TranscriptCompiler`` per relay session on that
session's append-only audit log (``SessionCompilerAudit``), runs the deterministic
grounding and validation the language package already performs before and after
the model, and renders the typed outcome as a ``relay.voice.VoicePlan`` preview.

Nothing here emits an intent. The console stages each step through its own
control flow, one at a time, after the operator confirms; the arbiter re-validates
every emission exactly as it does for a button press.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING

from arbiter.safety import CONFIRMATION_REQUIRED_INTENTS
from language.compiler import CompiledPlan, SessionCompilerAudit, TranscriptCompiler
from language.contracts import CompilerOutcome, CompilerReason, OutcomeKind, ProposedIntent
from language.telemetry import TraceSink
from language.transport import PINNED_COMPILER_MODEL, PROMPT_SCHEMA_VERSION, ModelTransport
from planner.models import TranslationGrounding, TranslationPolicy
from relay.capabilities import CapabilityProfile
from relay.intent_v1 import IntentName
from relay.voice import CompilerUnavailable, VoicePlan, VoicePlanStep

if TYPE_CHECKING:
    from relay.session import RelaySession

DEFAULT_PLAN_TTL_MS = 30_000
DEFAULT_STATE_MAX_AGE_MS = 2_000

SessionResolver = Callable[[str], "RelaySession | None"]
HeadingResolver = Callable[[Mapping[str, object]], Mapping[int, float]]


def _no_headings(_relay_state: Mapping[str, object]) -> Mapping[int, float]:
    return {}


class RelayTranscriptCompiler:
    """The relay's ``TranscriptCompiler``: one audited language compiler per session.

    ``sessions`` resolves a relay session by ID so the compiled-plan audit record
    lands in that session's log with the relay's own event IDs. ``transport`` is
    the pinned provider transport (``AnthropicTransport`` in production, a replay
    or synthetic transport in tests). ``translation_policy`` is the planner's frame
    and step so directional language is grounded in planner steps; ``headings``
    supplies per-aircraft headings for the aircraft-relative frame and defaults to
    none, which makes the compiler refuse aircraft-relative motion rather than
    guess. ``capability_profile`` must equal the profile the relay advertises in
    its state event; a mismatch is a stale-state refusal, never a wider plan.
    """

    def __init__(
        self,
        *,
        sessions: SessionResolver,
        transport: ModelTransport,
        translation_policy: TranslationPolicy | None = None,
        headings: HeadingResolver = _no_headings,
        capability_profile: CapabilityProfile | None = None,
        tracer: TraceSink | None = None,
        plan_ttl_ms: int = DEFAULT_PLAN_TTL_MS,
        state_max_age_ms: int = DEFAULT_STATE_MAX_AGE_MS,
    ) -> None:
        if plan_ttl_ms <= 0 or state_max_age_ms <= 0:
            raise ValueError("plan TTL and state maximum age must be positive")
        self._sessions = sessions
        self._transport = transport
        self._translation_policy = translation_policy
        self._headings = headings
        self._capability_profile = capability_profile
        self._tracer = tracer
        self._plan_ttl_ms = plan_ttl_ms
        self._state_max_age_ms = state_max_age_ms
        self._compilers: dict[str, tuple[RelaySession, TranscriptCompiler]] = {}
        self._lock = threading.Lock()

    @property
    def plan_ttl_ms(self) -> int:
        return self._plan_ttl_ms

    @property
    def state_max_age_ms(self) -> int:
        return self._state_max_age_ms

    def compile(
        self,
        transcript: str,
        relay_state: object,
        *,
        capability_version: str,
        rooms: tuple[str, ...] = (),
        now_ms: int,
        correlation_id: str | None = None,
        session_id: str | None = None,
    ) -> tuple[VoicePlan, CompiledPlan | None]:
        if not session_id or not isinstance(relay_state, Mapping):
            raise CompilerUnavailable()
        session = self._sessions(session_id)
        if session is None:
            raise CompilerUnavailable()
        compiler = self._compiler_for(session_id, session)
        correlation = correlation_id or ""
        if not correlation:
            raise CompilerUnavailable()
        outcome, compiled = compiler.compile(
            transcript,
            relay_state,
            capability_version=capability_version,
            rooms=rooms,
            translation=self._translation(relay_state),
            capability_profile=self._capability_profile,
            qualified_voice_intents=(),
            now_ms=now_ms,
            correlation_id=correlation,
            session_id=session_id,
        )
        model_unavailable = (
            outcome.kind is OutcomeKind.REFUSE
            and outcome.reason is CompilerReason.MODEL_UNAVAILABLE
        )
        if model_unavailable:
            # The provider was unreachable or unconfigured: the endpoint reports the
            # typed compiler_unavailable refusal and the console falls back locally.
            raise CompilerUnavailable()
        plan = voice_plan_from_outcome(
            outcome,
            compiled,
            transcript=transcript,
            relay_state=relay_state,
            rooms=rooms,
            now_ms=now_ms,
            correlation_id=correlation,
            session_id=session_id,
        )
        return plan, compiled

    def _compiler_for(self, session_id: str, session: RelaySession) -> TranscriptCompiler:
        with self._lock:
            cached = self._compilers.get(session_id)
            if cached is not None and cached[0] is session:
                return cached[1]
            compiler = TranscriptCompiler(
                self._transport,
                audit=SessionCompilerAudit(session.audit_log, session.event_ids),
                tracer=self._tracer,
                plan_ttl_ms=self._plan_ttl_ms,
                state_max_age_ms=self._state_max_age_ms,
            )
            self._compilers[session_id] = (session, compiler)
            return compiler

    def _translation(self, relay_state: Mapping[str, object]) -> TranslationGrounding | None:
        if self._translation_policy is None:
            return None
        headings = (
            self._headings(relay_state)
            if self._translation_policy.frame == "aircraft_relative"
            else {}
        )
        return TranslationGrounding(policy=self._translation_policy, headings=headings)


def voice_plan_from_outcome(
    outcome: CompilerOutcome,
    compiled: CompiledPlan | None,
    *,
    transcript: str,
    relay_state: Mapping[str, object],
    rooms: tuple[str, ...],
    now_ms: int,
    correlation_id: str,
    session_id: str,
) -> VoicePlan:
    """Render a compiler outcome as the preview the console shows; never an emission."""
    state_event_id = relay_state.get("event_id")
    roster_version = relay_state.get("roster_version")
    if not isinstance(state_event_id, str) or not isinstance(roster_version, int):
        raise CompilerUnavailable()
    model = PINNED_COMPILER_MODEL if compiled is None else compiled.model
    prompt_schema_version = (
        PROMPT_SCHEMA_VERSION if compiled is None else compiled.prompt_schema_version
    )
    if outcome.kind is OutcomeKind.PLAN:
        if compiled is None:
            raise CompilerUnavailable()
        steps = tuple(
            _step(index, intent, relay_state) for index, intent in enumerate(compiled.intents)
        )
        return VoicePlan(
            kind="plan",
            transcript=transcript.strip(),
            reason=None,
            detail=outcome.detail,
            options=(),
            steps=steps,
            compiled_at_ms=now_ms,
            expires_at_ms=compiled.expires_at_ms,
            state_event_id=state_event_id,
            roster_version=roster_version,
            session=session_id,
            correlation_id=correlation_id,
            plan_digest=compiled.digest,
            model=model,
            prompt_schema_version=prompt_schema_version,
            response_source=outcome.source,
        )
    if outcome.kind is OutcomeKind.CANCEL_PENDING:
        return VoicePlan(
            kind="cancel_pending",
            transcript=transcript.strip(),
            reason=None,
            detail=outcome.detail,
            options=(),
            steps=(),
            compiled_at_ms=now_ms,
            expires_at_ms=None,
            state_event_id=state_event_id,
            roster_version=roster_version,
            session=session_id,
            correlation_id=correlation_id,
            plan_digest=None,
            model=model,
            prompt_schema_version=prompt_schema_version,
            response_source=outcome.source,
            pending_intent_id=outcome.pending_intent_id,
        )
    reason = CompilerReason.INVALID_MODEL_OUTPUT if outcome.reason is None else outcome.reason
    return VoicePlan(
        kind=outcome.kind.value,
        transcript=transcript.strip(),
        reason=reason.value,
        detail=outcome.detail,
        options=_clarify_options(reason, relay_state, rooms)
        if outcome.kind is OutcomeKind.CLARIFY
        else (),
        steps=(),
        compiled_at_ms=now_ms,
        expires_at_ms=None,
        state_event_id=state_event_id,
        roster_version=roster_version,
        session=session_id,
        correlation_id=correlation_id,
        plan_digest=None,
        model=model,
        prompt_schema_version=prompt_schema_version,
        response_source=outcome.source,
    )


def _step(index: int, intent: ProposedIntent, relay_state: Mapping[str, object]) -> VoicePlanStep:
    return VoicePlanStep(
        index=index,
        name=intent.name.value,
        args=dict(intent.semantic_dict()["args"]),
        selection=tuple(intent.selection),
        mode=intent.mode.value,
        confirm_required=intent.name in CONFIRMATION_REQUIRED_INTENTS,
        notes=_step_notes(index, intent, relay_state),
    )


def _label(drone_id: int) -> str:
    return f"D-{drone_id:02d}"


def _labels(ids: tuple[int, ...]) -> str:
    return ", ".join(_label(drone_id) for drone_id in ids)


def _step_notes(
    index: int, intent: ProposedIntent, relay_state: Mapping[str, object]
) -> tuple[str, ...]:
    """Deterministic grounding notes from the validated intent and the relay state."""
    notes: list[str] = []
    selection = tuple(relay_state.get("selection", ()))  # type: ignore[arg-type]
    drones = {
        drone["drone_id"]: drone
        for drone in relay_state.get("drones", ())  # type: ignore[union-attr]
        if isinstance(drone, Mapping)
    }
    name = intent.name
    if name is IntentName.SELECT:
        ids = tuple(intent.args["ids"])
        notes.append(f"Selection membership only, no motion: {_labels(ids)}.")
    elif name in {IntentName.ARM, IntentName.ESTOP, IntentName.LAND_ALL}:
        targets = tuple(
            drone_id
            for drone_id, drone in sorted(drones.items())
            if drone.get("membership") in {"ready", "degraded"}
        )
        notes.append(
            f"Fleet-wide: targets {_labels(targets) if targets else 'no aircraft'} from the roster."
        )
    else:
        origin = (
            "the current selection"
            if tuple(sorted(intent.selection)) == tuple(sorted(selection))
            else "an earlier step's selection"
        )
        notes.append(f"Targets {_labels(intent.selection)} ({origin}).")
    states = [
        f"{_label(drone_id)} {drones[drone_id].get('flight_state') or 'unreported'}"
        for drone_id in intent.selection
        if drone_id in drones
    ]
    if states and name is not IntentName.SELECT:
        notes.append("Flight state when compiled: " + ", ".join(states) + ".")
    if name is IntentName.TAKEOFF:
        notes.append(
            "Climbs to the configured takeoff altitude; the session must be armed"
            f" (armed {'yes' if relay_state.get('armed') else 'no'} when compiled)."
        )
    elif name is IntentName.HOLD:
        notes.append("Each aircraft hovers at its current pose.")
    elif name is IntentName.LAND:
        notes.append("Lands in place.")
    elif name is IntentName.LAND_ALL:
        notes.append("Lands every ready airborne aircraft in place.")
    elif name is IntentName.COME_HOME:
        notes.append("Returns to each aircraft's captured home pose.")
    elif name is IntentName.ARM:
        notes.append("Sets the session arm flag; motors do not spin.")
    elif name is IntentName.TRANSLATE:
        notes.append(
            f"Moves dx {intent.args['dx']} dy {intent.args['dy']} planner steps;"
            " the planner applies its configured frame and metres per step."
        )
    elif name is IntentName.ALTITUDE:
        notes.append(f"Changes height by {intent.args['delta']} configured altitude steps.")
    elif name is IntentName.CAPTURE_ROOM:
        notes.append(
            f"Captures {intent.args['pattern']} in room {intent.args['room_id']}"
            f" as {intent.args['capture_id']}."
        )
    if name in CONFIRMATION_REQUIRED_INTENTS:
        notes.append("The arbiter requires operator confirmation before this step runs.")
    if index > 0:
        notes.append(f"Offered only after step {index} reaches a terminal state.")
    return tuple(notes[:8])


def _clarify_options(
    reason: CompilerReason, relay_state: Mapping[str, object], rooms: tuple[str, ...]
) -> tuple[str, ...]:
    if reason is CompilerReason.AMBIGUOUS_LOCATION:
        return tuple(rooms[:16])
    if reason is CompilerReason.AMBIGUOUS_SELECTION:
        selectable = tuple(
            drone["drone_id"]
            for drone in relay_state.get("drones", ())  # type: ignore[union-attr]
            if isinstance(drone, Mapping) and drone.get("selectable") is True
        )
        options = [_label(drone_id) for drone_id in selectable[:15]]
        if len(selectable) > 1:
            options.append("all aircraft")
        return tuple(options)
    return ()
