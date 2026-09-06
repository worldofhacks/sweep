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
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from arbiter.safety import CONFIRMATION_REQUIRED_INTENTS
from language.compiler import (
    CompiledPlan,
    SessionCompilerAudit,
    TranscriptCompiler,
)
from language.contracts import (
    CompilerOutcome,
    CompilerReason,
    OutcomeKind,
    ProposedIntent,
    build_grounding_facts,
    intent_payload,
    plan_step_matches_projected_facts,
)
from language.telemetry import TraceSink
from language.transport import PINNED_COMPILER_MODEL, PROMPT_SCHEMA_VERSION, ModelTransport
from planner.models import TranslationGrounding, TranslationPolicy
from relay.capabilities import CapabilityProfile
from relay.intent_v1 import AcceptedIntent, IntentName, IntentV1, validate_intent
from relay.voice import CompilerUnavailable, VoicePlan, VoicePlanStep

if TYPE_CHECKING:
    from relay.session import RelaySession

DEFAULT_PLAN_TTL_MS = 30_000
DEFAULT_STATE_MAX_AGE_MS = 2_000
MAX_ACTIVE_VOICE_PLANS_PER_SESSION = 8

SessionResolver = Callable[[str], "RelaySession | None"]
HeadingResolver = Callable[[Mapping[str, object]], Mapping[int, float]]


def _no_headings(_relay_state: Mapping[str, object]) -> Mapping[int, float]:
    return {}


@dataclass(slots=True)
class _BoundPlan:
    compiled: CompiledPlan
    intent_ids: tuple[str, ...]
    next_index: int = 0


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
        qualified_voice_intents: tuple[str, ...] = (),
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
        if not isinstance(qualified_voice_intents, tuple):
            raise ValueError("qualified voice intents must be an immutable tuple")
        known_names = {name.value for name in IntentName}
        if (
            len(set(qualified_voice_intents)) != len(qualified_voice_intents)
            or any(name not in known_names for name in qualified_voice_intents)
            or (
                capability_profile is not None
                and any(
                    not capability_profile.supports(IntentName(name))
                    for name in qualified_voice_intents
                )
            )
        ):
            raise ValueError("qualified voice intents must be unique enabled Intent v1 names")
        self._qualified_voice_intents = tuple(sorted(qualified_voice_intents))
        self._compilers: dict[str, tuple[RelaySession, TranscriptCompiler]] = {}
        self._bound_plans: dict[str, dict[str, _BoundPlan]] = {}
        # A bound method object is otherwise recreated on every attribute read;
        # retain one stable identity for RelaySession's one-time policy binding.
        self._intent_authorizer = self.authorize_intent
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
            qualified_voice_intents=self._qualified_voice_intents,
            require_qualified_voice_intents=True,
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
        if compiled is not None:
            self._bind_plan(session, plan, compiled, now_ms=now_ms)
        return plan, compiled

    def _compiler_for(self, session_id: str, session: RelaySession) -> TranscriptCompiler:
        try:
            session.bind_language_intent_authorizer(self._intent_authorizer)
        except ValueError:
            raise CompilerUnavailable() from None
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

    def _bind_plan(
        self,
        session: RelaySession,
        plan: VoicePlan,
        compiled: CompiledPlan,
        *,
        now_ms: int,
    ) -> None:
        if plan.kind != "plan" or plan.plan_digest != compiled.digest:
            raise CompilerUnavailable()
        intent_ids = tuple(step.intent_id for step in plan.steps)
        with self._lock:
            active = self._bound_plans.setdefault(session.session_id, {})
            self._purge_expired(active, now_ms)
            # Recompiling an identical request must never reset the consumption
            # cursor and make an already-issued motion step executable again.
            if compiled.digest in active:
                raise CompilerUnavailable()
            if len(active) >= MAX_ACTIVE_VOICE_PLANS_PER_SESSION:
                raise CompilerUnavailable()
            # Persist the exact mapping before making it admissible.  A failed
            # audit write leaves no executable language plan.
            SessionCompilerAudit(session.audit_log, session.event_ids).append(
                {
                    "event": "voice_plan_bound",
                    "correlation_id": compiled.correlation_id,
                    "plan_digest": compiled.digest,
                    "state_event_id": compiled.facts.state_event_id,
                    "expires_at_ms": compiled.expires_at_ms,
                    "intent_ids": list(intent_ids),
                }
            )
            active[compiled.digest] = _BoundPlan(compiled=compiled, intent_ids=intent_ids)

    def authorize_intent(
        self,
        intent: IntentV1,
        relay_state: Mapping[str, object],
        now_ms: int,
    ) -> tuple[str, str] | None:
        """Consume one exact, audited language-plan step or fail closed."""
        if intent.source != "language":
            return "source_mismatch", "the bound compiler authorizes only language intents"
        with self._lock:
            active = self._bound_plans.get(intent.session)
            if active is None:
                return "unbound_language_intent", "no active compiler plan binds this intent"
            self._purge_expired(active, now_ms)
            match = next(
                (
                    (bound, index)
                    for bound in active.values()
                    for index, intent_id in enumerate(bound.intent_ids)
                    if intent_id == intent.intent_id
                ),
                None,
            )
            if match is None:
                return "unbound_language_intent", "no active compiler plan binds this intent"
            bound, index = match
            compiled = bound.compiled
            if now_ms >= compiled.expires_at_ms:
                return "language_plan_expired", "the bound compiler plan has expired"
            if index != bound.next_index:
                return "language_plan_out_of_order", "language plan steps must be emitted in order"
            try:
                current_facts = build_grounding_facts(
                    relay_state,
                    capability_version=compiled.facts.capability_version,
                    rooms=compiled.facts.rooms,
                    translation=self._translation(relay_state),
                    capability_profile=self._capability_profile,
                    qualified_voice_intents=self._qualified_voice_intents,
                )
            except ValueError:
                return "stale_language_plan", "current relay state is invalid for this plan"
            if not plan_step_matches_projected_facts(
                compiled.intents, compiled.facts, index, current_facts
            ):
                return "stale_language_plan", "state or capabilities changed after preview"
            proposal = compiled.intents[index]
            payload = intent_payload(
                proposal,
                session=compiled.facts.session,
                intent_id=bound.intent_ids[index],
                timestamp_ms=intent.t,
                source="language",
            )
            profile = self._capability_profile or current_facts.capability_profile
            expected = (
                validate_intent(payload)
                if profile is None
                else validate_intent(payload, capability_profile=profile)
            )
            if not isinstance(expected, AcceptedIntent) or expected.intent != intent:
                return "language_plan_mismatch", "intent does not match the bound compiler preview"
            bound.next_index += 1
            return None

    @staticmethod
    def _purge_expired(active: dict[str, _BoundPlan], now_ms: int) -> None:
        for digest, bound in tuple(active.items()):
            if now_ms >= bound.compiled.expires_at_ms:
                del active[digest]

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
            _step(index, intent, relay_state, compiled.digest)
            for index, intent in enumerate(compiled.intents)
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


def _step(
    index: int,
    intent: ProposedIntent,
    relay_state: Mapping[str, object],
    plan_digest: str,
) -> VoicePlanStep:
    return VoicePlanStep(
        index=index,
        intent_id=_voice_intent_id(plan_digest, index),
        name=intent.name.value,
        args=dict(intent.semantic_dict()["args"]),
        selection=tuple(intent.selection),
        mode=intent.mode.value,
        confirm_required=intent.name in CONFIRMATION_REQUIRED_INTENTS,
        notes=_step_notes(index, intent, relay_state),
    )


def _voice_intent_id(plan_digest: str, index: int) -> str:
    value = uuid.uuid5(uuid.NAMESPACE_URL, f"sweep:voice-plan:{plan_digest}:{index}")
    return f"voice-{value.hex}"


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
