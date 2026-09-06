"""Grounded language compilation at the relay boundary without command emission."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass

from language.compiler import CompiledPlan, InMemoryAuditSink, TranscriptCompiler
from language.contracts import CompilerOutcome, CompilerReason, OutcomeKind, intent_payload
from language.navigation import NavigationGrounding
from language.telemetry import TraceSink
from language.transport import AnthropicTransport, ModelTransport
from planner.models import AltitudeGrounding, FleetSnapshot, TranslationGrounding
from relay.capabilities import CapabilityProfile, IntentName


@dataclass(frozen=True, slots=True)
class LanguageCompilationOutcome:
    """A serializable compiler result for staging in the console."""

    kind: OutcomeKind
    source: str
    reason: CompilerReason | None
    detail: str | None
    pending_intent_id: str | None
    intents: tuple[dict[str, object], ...]
    plan_digest: str | None
    expires_at_ms: int | None
    state_digest: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "source": self.source,
            "reason": None if self.reason is None else self.reason.value,
            "detail": self.detail,
            "pending_intent_id": self.pending_intent_id,
            "intents": [dict(intent) for intent in self.intents],
            "plan_digest": self.plan_digest,
            "expires_at_ms": self.expires_at_ms,
            "state_digest": self.state_digest,
        }


class LanguageRuntime:
    """Compile grounded text into console-stageable intents without dispatching them."""

    def __init__(
        self, transport: ModelTransport | None = None, *, tracer: TraceSink | None = None
    ) -> None:
        self._transport = AnthropicTransport() if transport is None else transport
        self._tracer = tracer

    @classmethod
    def from_env(cls) -> LanguageRuntime:
        return cls(AnthropicTransport())

    def compile(
        self,
        text: str,
        snapshot: FleetSnapshot,
        navigation: NavigationGrounding | None,
        rooms: tuple[str, ...],
        *,
        session_id: str,
        state_event_id: str,
        capability_profile: CapabilityProfile,
        translation: TranslationGrounding,
        altitude_grounding: AltitudeGrounding | None,
        correlation_id: str | None = None,
    ) -> LanguageCompilationOutcome:
        if not isinstance(snapshot, FleetSnapshot):
            raise ValueError("language compilation requires the current fleet snapshot")
        if not isinstance(capability_profile, CapabilityProfile):
            raise ValueError("language compilation requires the current capability profile")
        if not isinstance(translation, TranslationGrounding):
            raise ValueError("language compilation requires current translation grounding")
        if navigation is not None and navigation.capability_profile != capability_profile:
            raise ValueError("navigation grounding does not match the current capability profile")
        relay_state = _relay_state(snapshot, session_id=session_id, event_id=state_event_id)
        relay_state.update(capability_profile.state_value())
        compiler = TranscriptCompiler(
            self._transport, audit=InMemoryAuditSink(), tracer=self._tracer
        )
        compile_kwargs: dict[str, object] = {
            "capability_version": _capability_version(capability_profile),
            "rooms": rooms,
            "translation": translation,
            "navigation": navigation,
            "now_ms": snapshot.now_ms,
            "correlation_id": correlation_id,
            "session_id": session_id,
        }
        compile_kwargs["capability_profile"] = capability_profile
        compile_kwargs["altitude"] = (
            altitude_grounding if capability_profile.supports(IntentName.ALTITUDE) else None
        )
        outcome, plan = compiler.compile(text, relay_state, **compile_kwargs)
        if any(not capability_profile.supports(intent.name) for intent in outcome.intents):
            return _refusal(CompilerReason.CAPABILITY_UNAVAILABLE)
        if (
            any(intent.name.value == "altitude" for intent in outcome.intents)
            and altitude_grounding is None
        ):
            return _refusal(CompilerReason.CAPABILITY_UNAVAILABLE)
        return _serialize(outcome, plan, session_id=session_id, now_ms=snapshot.now_ms)


def _relay_state(snapshot: FleetSnapshot, *, session_id: str, event_id: str) -> dict[str, object]:
    return {
        "v": 1,
        "t": snapshot.now_ms,
        "type": "state",
        "event_id": event_id,
        "session": session_id,
        "roster_version": snapshot.roster_version,
        "armed": snapshot.armed,
        "estop": snapshot.estop_active,
        "selection": list(snapshot.selection),
        "mode": "indoor",
        "drones": [
            {
                "drone_id": aircraft.drone_id,
                "membership": aircraft.membership.value,
                "selectable": aircraft.membership.value == "ready",
                "flight_state": aircraft.flight_state.value,
                "heading_deg": aircraft.heading_deg,
                "camera_patterns": [],
                "adapter_capabilities": ["flight"] if aircraft.control_authority else [],
                "telemetry": {**aircraft.pose.to_dict(), "t": aircraft.position_last_seen_ms},
                "home_pose": None if aircraft.home is None else aircraft.home.to_dict(),
            }
            for aircraft in snapshot.aircraft.values()
        ],
    }


def _capability_version(profile: CapabilityProfile) -> str:
    encoded = json.dumps(profile.state_value(), sort_keys=True, separators=(",", ":")).encode()
    return f"{profile.name}-{hashlib.sha256(encoded).hexdigest()[:16]}"


def _refusal(reason: CompilerReason) -> LanguageCompilationOutcome:
    return LanguageCompilationOutcome(
        kind=OutcomeKind.REFUSE,
        source="template",
        reason=reason,
        detail=None,
        pending_intent_id=None,
        intents=(),
        plan_digest=None,
        expires_at_ms=None,
        state_digest=None,
    )


def _staged_intent_id(digest: str, index: int) -> str:
    value = uuid.uuid5(uuid.NAMESPACE_URL, f"{digest}:{index}")
    return f"language-stage-{value.hex}"


def _serialize(
    outcome: CompilerOutcome,
    plan: CompiledPlan | None,
    *,
    session_id: str,
    now_ms: int,
) -> LanguageCompilationOutcome:
    intents = ()
    if plan is not None:
        intents = tuple(
            intent_payload(
                proposal,
                session=session_id,
                intent_id=_staged_intent_id(plan.digest, index),
                timestamp_ms=now_ms,
            )
            for index, proposal in enumerate(plan.intents)
        )
    return LanguageCompilationOutcome(
        kind=outcome.kind,
        source=outcome.source,
        reason=outcome.reason,
        detail=outcome.detail,
        pending_intent_id=outcome.pending_intent_id,
        intents=intents,
        plan_digest=None if plan is None else plan.digest,
        expires_at_ms=None if plan is None else plan.expires_at_ms,
        state_digest=None if plan is None else plan.facts.state_digest,
    )
