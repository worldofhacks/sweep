from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from threading import RLock
from typing import Protocol

from language.contracts import (
    CompilerOutcome,
    CompilerReason,
    GroundingFacts,
    OutcomeKind,
    ProposedIntent,
    build_grounding_facts,
    intent_payload,
    validate_model_outcome,
)
from language.telemetry import TraceSink, get_default_trace_sink
from language.transport import PINNED_COMPILER_MODEL, ModelRequest, ModelTransport, TransportError
from relay.audit import SessionAuditLog
from relay.contracts import LifecycleStatus
from relay.intent_v1 import AcceptedIntent, IntentV1, validate_intent

MAX_TRANSCRIPT_CHARS = 4_000


class AuditSink(Protocol):
    def append(self, event: Mapping[str, object]) -> None: ...


class InMemoryAuditSink:
    def __init__(self) -> None:
        self.records: list[dict[str, object]] = []

    def append(self, event: Mapping[str, object]) -> None:
        self.records.append(dict(event))


class SessionCompilerAudit:
    def __init__(self, log: SessionAuditLog, event_ids: Callable[[], str]) -> None:
        self._log = log
        self._event_ids = event_ids

    def append(self, event: Mapping[str, object]) -> None:
        self._log.append(
            {
                **event,
                "session": self._log.session,
                "event_id": self._event_ids(),
            }
        )


@dataclass(frozen=True, slots=True)
class CompiledPlan:
    intents: tuple[ProposedIntent, ...]
    facts: GroundingFacts
    digest: str
    expires_at_ms: int
    correlation_id: str
    state_max_age_ms: int


class TranscriptCompiler:
    def __init__(
        self,
        transport: ModelTransport,
        *,
        audit: AuditSink,
        tracer: TraceSink | None = None,
        plan_ttl_ms: int = 30_000,
        state_max_age_ms: int = 2_000,
    ) -> None:
        if plan_ttl_ms <= 0 or state_max_age_ms <= 0:
            raise ValueError("plan TTL and state maximum age must be positive")
        self._transport = transport
        self._tracer = tracer or get_default_trace_sink()
        self.audit = audit
        self._plan_ttl_ms = plan_ttl_ms
        self._state_max_age_ms = state_max_age_ms

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
    ) -> tuple[CompilerOutcome, CompiledPlan | None]:
        correlation = correlation_id or uuid.uuid4().hex
        if (
            not isinstance(transcript, str)
            or not transcript.strip()
            or len(transcript) > MAX_TRANSCRIPT_CHARS
        ):
            return self._refusal(correlation, CompilerReason.INVALID_MODEL_OUTPUT)
        try:
            facts = build_grounding_facts(
                relay_state,
                capability_version=capability_version,
                rooms=rooms,
            )
        except ValueError:
            return self._refusal(correlation, CompilerReason.STALE_STATE)
        state_age_ms = now_ms - facts.state_time_ms
        if state_age_ms < 0 or state_age_ms > self._state_max_age_ms:
            return self._refusal(correlation, CompilerReason.STALE_STATE)

        request = ModelRequest(transcript=transcript.strip(), facts=facts.model_dict())
        self._trace(
            {
                "event": "compiler_started",
                "correlation_id": correlation,
                "model": PINNED_COMPILER_MODEL,
                "state_digest": facts.state_digest,
                "session_id": session_id,
            }
        )
        started = time.monotonic()
        try:
            response = self._transport.complete(request)
        except TransportError:
            return self._refusal(correlation, CompilerReason.MODEL_UNAVAILABLE)
        outcome = validate_model_outcome(response.payload, facts)
        elapsed_ms = int((time.monotonic() - started) * 1_000)
        self._trace(
            {
                "event": "compiler_completed",
                "correlation_id": correlation,
                "model": PINNED_COMPILER_MODEL,
                "state_digest": facts.state_digest,
                "outcome": outcome.kind.value,
                "reason": None if outcome.reason is None else outcome.reason.value,
                "source": outcome.source,
                "grounded": int(outcome.kind is OutcomeKind.PLAN),
                "input_units": response.input_units,
                "output_units": response.output_units,
                "provider_latency_ms": response.latency_ms,
                "elapsed_ms": elapsed_ms,
            }
        )
        if outcome.kind is not OutcomeKind.PLAN:
            self.audit.append(
                {
                    "event": "compiler_outcome",
                    "correlation_id": correlation,
                    "state_digest": facts.state_digest,
                    "outcome": outcome.kind.value,
                    "reason": None if outcome.reason is None else outcome.reason.value,
                }
            )
            return outcome, None
        semantic = [intent.semantic_dict() for intent in outcome.intents]
        digest = hashlib.sha256(
            json.dumps(
                {
                    "facts": facts.state_digest,
                    "capabilities": facts.capability_version,
                    "intents": semantic,
                },
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        ).hexdigest()
        compiled = CompiledPlan(
            intents=outcome.intents,
            facts=facts,
            digest=digest,
            expires_at_ms=now_ms + self._plan_ttl_ms,
            correlation_id=correlation,
            state_max_age_ms=self._state_max_age_ms,
        )
        self.audit.append(
            {
                "event": "plan_compiled",
                "correlation_id": correlation,
                "plan_digest": digest,
                "state_digest": facts.state_digest,
                "state_version": facts.state_version,
                "capability_version": facts.capability_version,
                "expires_at_ms": compiled.expires_at_ms,
                "intents": semantic,
            }
        )
        return outcome, compiled

    def _refusal(self, correlation_id: str, reason: CompilerReason) -> tuple[CompilerOutcome, None]:
        self._trace(
            {
                "event": "compiler_completed",
                "correlation_id": correlation_id,
                "model": PINNED_COMPILER_MODEL,
                "outcome": OutcomeKind.REFUSE.value,
                "reason": reason.value,
                "source": "template",
                "grounded": 0,
            }
        )
        self.audit.append(
            {
                "event": "compiler_outcome",
                "correlation_id": correlation_id,
                "outcome": OutcomeKind.REFUSE.value,
                "reason": reason.value,
            }
        )
        return CompilerOutcome(
            kind=OutcomeKind.REFUSE,
            reason=reason,
            source="template",
        ), None

    def _trace(self, event: Mapping[str, object]) -> None:
        try:
            self._tracer.record(event)
        except Exception:
            return


class ConfirmationError(RuntimeError):
    pass


class ConfirmedPlan:
    def __init__(
        self,
        compiled: CompiledPlan,
        *,
        session: str,
        audit: AuditSink,
    ) -> None:
        if not session:
            raise ValueError("session must be non-empty")
        self._compiled = compiled
        self._session = session
        self._next = 0
        self._awaiting_outcome = False
        self._awaiting_intent_id: str | None = None
        self._terminal = False
        self._expected_facts = compiled.facts
        self.audit = audit
        self._lock = RLock()

    @property
    def remaining(self) -> int:
        with self._lock:
            return len(self._compiled.intents) - self._next

    def confirm_next(
        self,
        relay_state: object,
        *,
        capability_version: str,
        rooms: tuple[str, ...],
        now_ms: int,
        intent_id: str,
        emit: Callable[[IntentV1], None],
    ) -> IntentV1:
        with self._lock:
            return self._confirm_next(
                relay_state,
                capability_version=capability_version,
                rooms=rooms,
                now_ms=now_ms,
                intent_id=intent_id,
                emit=emit,
            )

    def _confirm_next(
        self,
        relay_state: object,
        *,
        capability_version: str,
        rooms: tuple[str, ...],
        now_ms: int,
        intent_id: str,
        emit: Callable[[IntentV1], None],
    ) -> IntentV1:
        if self._terminal:
            raise ConfirmationError("plan is closed")
        if self._next >= len(self._compiled.intents):
            raise ConfirmationError("plan is complete")
        if self._awaiting_outcome:
            raise ConfirmationError("previous intent is awaiting a relay outcome")
        if now_ms > self._compiled.expires_at_ms:
            raise ConfirmationError("plan confirmation expired")
        try:
            facts = build_grounding_facts(
                relay_state,
                capability_version=capability_version,
                rooms=rooms,
            )
        except ValueError:
            raise ConfirmationError("current state is invalid") from None
        if (
            facts.state_digest != self._expected_facts.state_digest
            or facts.capability_version != self._expected_facts.capability_version
        ):
            raise ConfirmationError("state or capabilities changed after preview")
        state_age_ms = now_ms - facts.state_time_ms
        if state_age_ms < 0 or state_age_ms > self._compiled.state_max_age_ms:
            raise ConfirmationError("current state is stale")
        raw = intent_payload(
            self._compiled.intents[self._next],
            session=self._session,
            intent_id=intent_id,
            timestamp_ms=now_ms,
        )
        validated = validate_intent(raw)
        if not isinstance(validated, AcceptedIntent):
            raise ConfirmationError("intent failed validation before emission")
        self._awaiting_outcome = True
        self._awaiting_intent_id = validated.intent.intent_id
        try:
            emit(validated.intent)
        except Exception:
            self._awaiting_outcome = False
            self._awaiting_intent_id = None
            raise
        self._next += 1
        try:
            self.audit.append(
                {
                    "event": "intent_emitted",
                    "correlation_id": self._compiled.correlation_id,
                    "plan_digest": self._compiled.digest,
                    "intent_index": self._next - 1,
                    "intent_id": validated.intent.intent_id,
                    "state_digest": facts.state_digest,
                }
            )
        except Exception:
            self._terminal = True
            raise ConfirmationError("intent was emitted but its audit record failed") from None
        return validated.intent

    def acknowledge(
        self,
        outcome: object,
        relay_state: object,
        *,
        capability_version: str,
        rooms: tuple[str, ...],
        now_ms: int,
    ) -> None:
        with self._lock:
            self._acknowledge(
                outcome,
                relay_state,
                capability_version=capability_version,
                rooms=rooms,
                now_ms=now_ms,
            )

    def _acknowledge(
        self,
        outcome: object,
        relay_state: object,
        *,
        capability_version: str,
        rooms: tuple[str, ...],
        now_ms: int,
    ) -> None:
        if self._terminal:
            raise ConfirmationError("plan is closed")
        if not self._awaiting_outcome or self._awaiting_intent_id is None:
            raise ConfirmationError("no emitted intent is awaiting a relay outcome")
        if not isinstance(outcome, Mapping):
            raise ConfirmationError("relay outcome must be an event")
        outcome_type = outcome.get("type")
        if (
            outcome_type not in {"acknowledgement", "refusal"}
            or outcome.get("session") != self._session
        ):
            raise ConfirmationError("relay outcome does not belong to this session")
        if outcome.get("intent_id") != self._awaiting_intent_id:
            raise ConfirmationError("relay outcome intent does not match the emitted intent")
        if (
            outcome.get("v") != 1
            or not isinstance(outcome.get("event_id"), str)
            or not outcome["event_id"]
            or not isinstance(outcome.get("t"), int)
            or isinstance(outcome["t"], bool)
            or outcome["t"] < 0
            or outcome.get("source") != "relay"
            or outcome.get("roster_version") != self._expected_facts.state_version
        ):
            raise ConfirmationError("relay outcome envelope is invalid")
        try:
            status = LifecycleStatus(outcome.get("status"))
        except (TypeError, ValueError):
            raise ConfirmationError("relay outcome status is invalid") from None
        if outcome_type == "refusal" and status is not LifecycleStatus.REFUSED:
            raise ConfirmationError("relay refusal status is invalid")
        if outcome_type == "acknowledgement" and status is LifecycleStatus.REFUSED:
            raise ConfirmationError("relay acknowledgement status is invalid")
        if status not in {LifecycleStatus.ACCEPTED, LifecycleStatus.COMPLETED}:
            self._terminal = True
            self.audit.append(
                {
                    "event": "intent_rejected",
                    "correlation_id": self._compiled.correlation_id,
                    "plan_digest": self._compiled.digest,
                    "intent_id": self._awaiting_intent_id,
                    "status": status.value,
                    "reason": outcome.get("reason"),
                }
            )
            raise ConfirmationError(f"relay returned terminal status {status.value}")
        try:
            facts = build_grounding_facts(
                relay_state,
                capability_version=capability_version,
                rooms=rooms,
            )
        except ValueError:
            raise ConfirmationError("relay outcome state is invalid") from None
        state_age_ms = now_ms - facts.state_time_ms
        if state_age_ms < 0 or state_age_ms > self._compiled.state_max_age_ms:
            raise ConfirmationError("relay outcome state is stale")
        if facts.state_version != self._expected_facts.state_version:
            raise ConfirmationError("fleet roster changed during plan execution")
        old_drones = {
            drone["drone_id"]: (
                drone["membership"],
                drone["selectable"],
                drone["camera_patterns"],
                drone["flight_available"],
            )
            for drone in self._expected_facts.drones
        }
        new_drones = {
            drone["drone_id"]: (
                drone["membership"],
                drone["selectable"],
                drone["camera_patterns"],
                drone["flight_available"],
            )
            for drone in facts.drones
        }
        if (
            old_drones != new_drones
            or facts.capability_version != self._expected_facts.capability_version
        ):
            raise ConfirmationError("fleet capabilities changed during plan execution")
        emitted = self._compiled.intents[self._next - 1]
        expected_selection = self._expected_facts.selection
        if emitted.name.value == "select":
            expected_selection = tuple(emitted.args["ids"])
        if facts.selection != expected_selection:
            raise ConfirmationError("relay selection does not match the accepted intent")
        expected_armed = True if emitted.name.value == "arm" else self._expected_facts.armed
        if facts.armed is not expected_armed:
            raise ConfirmationError("relay armed state does not match the accepted intent")
        expected_estop = True if emitted.name.value == "estop" else self._expected_facts.estop
        if facts.estop is not expected_estop:
            raise ConfirmationError("relay estop state does not match the accepted intent")
        accepted_intent_id = self._awaiting_intent_id
        try:
            self.audit.append(
                {
                    "event": "intent_accepted",
                    "correlation_id": self._compiled.correlation_id,
                    "plan_digest": self._compiled.digest,
                    "completed_intents": self._next,
                    "intent_id": accepted_intent_id,
                    "status": status.value,
                    "state_digest": facts.state_digest,
                }
            )
        except Exception:
            self._terminal = True
            raise ConfirmationError("relay outcome audit failed") from None
        self._expected_facts = facts
        self._awaiting_outcome = False
        self._awaiting_intent_id = None
