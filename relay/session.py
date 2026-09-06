"""Relay-session orchestration around pure contracts and fleet state."""

from __future__ import annotations

import json
import logging
import queue
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field, replace
from math import isfinite
from threading import Lock, RLock

from planner.models import CommandOperation
from relay.audit import LIVE_REPLAY_TIMEOUT_SECONDS, AuditLogError, SessionAuditLog
from relay.auth import Principal, sign_event, verify_event_signature
from relay.capabilities import C1_CAPABILITY_PROFILE, CapabilityProfile
from relay.captures import (
    MAX_CAPTURE_ENTRIES,
    MAX_MEDIA_FILES_PER_CAPTURE,
    CaptureEntry,
    CaptureKey,
    CaptureLedger,
    CaptureLedgerError,
)
from relay.contracts import (
    MAX_CAPABILITY_LIST_ITEMS,
    NODE_FRAME_TYPES,
    AdapterAcknowledgement,
    CapabilitiesFrame,
    CaptureBundleFrame,
    CaptureReadinessFrame,
    ContractError,
    LifecycleStatus,
    MediaFileFrame,
    MediaFileRecord,
    MembershipAction,
    MembershipRequest,
    NodeStatusFrame,
    acknowledgement_event,
    command_event,
    parse_adapter_acknowledgement,
    parse_capabilities,
    parse_capture_bundle,
    parse_capture_readiness,
    parse_media_file,
    parse_membership_request,
    parse_node_status,
    parse_telemetry,
    refusal_event,
)
from relay.control_frames import ControlLocalizationFrame, sign_control_pose
from relay.control_localization import (
    ControlLocalizationProjector,
    ControlPose,
    LocalizationProjectionError,
)
from relay.control_localization_contracts import session_identifier
from relay.intent_v1 import (
    MAX_INTENT_IDENTIFIER_CHARS,
    REGISTERED_SOURCES,
    AcceptedIntent,
    IntentName,
    IntentV1,
    RejectedIntent,
    validate_intent,
)
from relay.media import MediaEvidenceProvider
from relay.state import (
    MAX_MEMBERSHIP_HISTORY_LIMIT,
    MAX_PHYSICAL_AIRCRAFT,
    FleetRegistry,
    MembershipTransition,
    RegistryError,
)

Clock = Callable[[], int]
EventIdFactory = Callable[[], str]
ControlPoseSigningKey = Callable[[int], bytes | None]
_LOGGER = logging.getLogger(__name__)
# The composed capture bundle is recorded by the autonomy boundary, not by a node.
LIFECYCLE_SOURCE_AUTONOMY = "autonomy"
MAX_AUDIT_STATE_INTERVAL_MS = 10_000


@dataclass(frozen=True, slots=True)
class IntentSinkResult:
    status: LifecycleStatus
    source: str
    result: Mapping[str, object] = field(default_factory=dict)
    events: tuple[Mapping[str, object], ...] = ()
    selection_update: tuple[int, ...] | None = None
    armed_update: bool | None = None
    estop_update: bool | None = None
    formation_update: str | None = None
    spacing_update: float | None = None
    reason: str | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        if self.status is LifecycleStatus.ACCEPTED:
            raise ValueError("sink result must advance beyond relay acceptance")
        if not self.source:
            raise ValueError("sink result source must be non-empty")
        if not isinstance(self.result, Mapping):
            raise ValueError("sink result evidence must be a mapping")
        if not isinstance(self.events, tuple) or any(
            not isinstance(event, Mapping) for event in self.events
        ):
            raise ValueError("sink result events must be a tuple of mappings")
        if self.formation_update is not None and (
            not isinstance(self.formation_update, str) or not self.formation_update
        ):
            raise ValueError("formation update must be a non-empty string")
        if self.spacing_update is not None and (
            isinstance(self.spacing_update, bool)
            or not isinstance(self.spacing_update, int | float)
            or not isfinite(self.spacing_update)
            or self.spacing_update <= 0
        ):
            raise ValueError("spacing update must be a finite positive number")


IntentSink = Callable[[IntentV1, dict[str, object]], object]
LeaveAuthorizer = Callable[[int, int, dict[str, object]], bool]
LanguageIntentAuthorizer = Callable[[IntentV1, Mapping[str, object], int], tuple[str, str] | None]
NodeFrame = (
    CapabilitiesFrame
    | CaptureBundleFrame
    | MediaFileFrame
    | CaptureReadinessFrame
    | NodeStatusFrame
)
_UNSET = object()
_TERMINAL_STATUSES = frozenset(
    {
        LifecycleStatus.REFUSED,
        LifecycleStatus.COMPLETED,
        LifecycleStatus.FAILED,
        LifecycleStatus.INVALIDATED,
    }
)
_NODE_FRAME_PARSERS: dict[str, Callable[[object], NodeFrame]] = {
    "capabilities": parse_capabilities,
    "capture_bundle": parse_capture_bundle,
    "media_file": parse_media_file,
    "capture_readiness": parse_capture_readiness,
    "node_status": parse_node_status,
}
_COMMAND_LEDGER_MAX = 4_096
_COMMAND_RETENTION_MIN_MS = 60_000
_COMMAND_RETENTION_MULTIPLIER = 30
_TRANSPORT_REPLAY_LEDGER_MAX = 8_192
_TRANSPORT_REPLAY_PRUNE_AT = 4_096


def _declared_sink_capability_profile(sink: IntentSink) -> CapabilityProfile | None:
    profile = getattr(sink, "capability_profile", None)
    if profile is None:
        owner = getattr(sink, "__self__", None)
        profile = getattr(owner, "capability_profile", None)
    return profile if isinstance(profile, CapabilityProfile) else None


def _sink_capability_profile(sink: IntentSink) -> CapabilityProfile:
    profile = _declared_sink_capability_profile(sink)
    if profile is None:
        raise ValueError("intent sink must declare an immutable capability profile")
    return profile


@dataclass(frozen=True, slots=True)
class CapabilityBoundIntentSink:
    """Give an otherwise opaque downstream callable an explicit immutable contract."""

    sink: IntentSink
    capability_profile: CapabilityProfile

    def __post_init__(self) -> None:
        declared = _declared_sink_capability_profile(self.sink)
        if declared is not None:
            raise ValueError("a sink that already declares capabilities must not be wrapped")

    def __call__(self, intent: IntentV1, state: dict[str, object]) -> object:
        return self.sink(intent, state)

    def __getattr__(self, name: str) -> object:
        return getattr(self.sink, name)


@dataclass(frozen=True, slots=True)
class RelayLimits:
    intent_max_age_ms: int
    transport_event_max_age_ms: int
    future_clock_skew_ms: int
    telemetry_freshness_ms: int
    command_ttl_ms: int = 2_000
    audit_state_interval_ms: int = 10_000
    state_membership_history: int = 8

    def __post_init__(self) -> None:
        if (
            min(
                self.intent_max_age_ms,
                self.transport_event_max_age_ms,
                self.telemetry_freshness_ms,
                self.command_ttl_ms,
            )
            <= 0
        ):
            raise ValueError("freshness limits must be positive")
        if self.future_clock_skew_ms < 0:
            raise ValueError("future_clock_skew_ms must be non-negative")
        if (
            type(self.audit_state_interval_ms) is not int
            or not 1 <= self.audit_state_interval_ms <= MAX_AUDIT_STATE_INTERVAL_MS
        ):
            raise ValueError(
                "audit_state_interval_ms must be an integer from 1 through "
                f"{MAX_AUDIT_STATE_INTERVAL_MS}"
            )
        if (
            type(self.state_membership_history) is not int
            or not 1 <= self.state_membership_history <= MAX_MEMBERSHIP_HISTORY_LIMIT
        ):
            raise ValueError(
                "state_membership_history must be an integer from 1 through "
                f"{MAX_MEMBERSHIP_HISTORY_LIMIT}"
            )


@dataclass(slots=True)
class _IntentLedgerEntry:
    status: LifecycleStatus
    selection: tuple[int, ...]
    command_statuses: dict[str, LifecycleStatus]
    retryable: bool = True


@dataclass(slots=True)
class _IssuedCommand:
    """The immutable command identity plus bounded late-result bookkeeping."""

    intent_id: str
    roster_version: int
    drone_id: int
    connection_epoch: int
    operation: CommandOperation
    issued_at: int
    status: LifecycleStatus | None = None
    waiter_active: bool = True


@dataclass(slots=True)
class _PendingIntent:
    intent: IntentV1
    executing: bool = False
    operation_id: int | None = None
    events: list[dict[str, object]] | None = None
    acknowledgements: list[AdapterAcknowledgement] = field(default_factory=list)


@dataclass(slots=True)
class _ResumeWork:
    acknowledgement: AdapterAcknowledgement
    token: object
    operation_id: int
    blocked_ids: set[str]
    phased: bool


@dataclass(slots=True)
class _AuditSampling:
    """What state the audit last recorded; accepted telemetry is never sampled."""

    state_projection: str | None = None
    state_audited_at: int | None = None

    def copy(self) -> _AuditSampling:
        return _AuditSampling(
            state_projection=self.state_projection,
            state_audited_at=self.state_audited_at,
        )


class RelaySession:
    """Own one authenticated session's ordering, state, audit, and replay."""

    def __init__(
        self,
        *,
        session_id: str,
        audit_log: SessionAuditLog,
        limits: RelayLimits,
        clock: Clock | None = None,
        event_ids: EventIdFactory | None = None,
        intent_sink: IntentSink | None = None,
        leave_authorizer: LeaveAuthorizer | None = None,
        capability_profile: CapabilityProfile = C1_CAPABILITY_PROFILE,
        control_localization_projector: ControlLocalizationProjector | None = None,
        control_pose_signing_key: ControlPoseSigningKey | None = None,
        relay_clock_id: str = "unix_epoch_ms",
        media_evidence: MediaEvidenceProvider | None = None,
    ) -> None:
        if audit_log.session != session_id:
            raise ValueError("audit log belongs to another session")
        self.session_id = session_identifier(session_id)
        self.audit_log = audit_log
        self.limits = limits
        self.clock = clock or _epoch_ms
        self.event_ids = event_ids or (lambda: str(uuid.uuid4()))
        self.leave_authorizer = leave_authorizer
        if (
            control_localization_projector is not None
            and control_localization_projector.relay_clock_id != relay_clock_id
        ):
            raise ValueError("control localization and relay runtime use different clocks")
        self.control_localization_projector = control_localization_projector
        self.control_pose_signing_key = control_pose_signing_key
        self.relay_clock_id = relay_clock_id
        self._capability_profile = capability_profile
        self._intent_sink: IntentSink | None = None
        self.intent_sink = intent_sink
        # Bound exactly once by the relay transcript compiler.  A language
        # principal has no admission path while this remains absent.
        self._language_intent_authorizer: LanguageIntentAuthorizer | None = None
        self.registry = FleetRegistry(
            telemetry_freshness_ms=limits.telemetry_freshness_ms,
            capability_profile=capability_profile,
            media_evidence=media_evidence,
            membership_history_limit=limits.state_membership_history,
        )
        self._audit_sampling = _AuditSampling()
        # Values are the last instant when the exact signed event could still pass
        # the transport freshness check. This keeps replay protection bounded for
        # high-rate diagnostic producers without accepting a still-fresh replay.
        self._seen_transport_event_ids: dict[str, int] = {}
        self._last_transport_t: dict[tuple[str, int | None], int] = {}
        self._intents: dict[str, _IntentLedgerEntry] = {}
        self._command_seq: dict[tuple[int, int], int] = {}
        self._issued_commands: dict[str, _IssuedCommand] = {}
        self._command_waiters: dict[str, queue.SimpleQueue[AdapterAcknowledgement]] = {}
        self._captures = CaptureLedger()
        self._capture_readiness: dict[int, CaptureReadinessFrame] = {}
        self._control_pose: dict[int, ControlPose] = {}
        self._pending_intents: dict[str, _PendingIntent] = {}
        self._acknowledgements: dict[str, list[AdapterAcknowledgement]] = {}
        self._resuming_intents: set[str] = set()
        self._resume_continuations: list[_ResumeWork] = []
        self._execution_lock = Lock()
        self._metrics = {
            "accepted_intents": 0,
            "refused_intents": 0,
            "acknowledgements": 0,
            "membership_events": 0,
            "telemetry_events": 0,
            "node_events": 0,
            "commands_issued": 0,
            "safety_actions": 0,
        }
        self._mutation_usable = True
        self._projection_usable = True
        self._replay_usable = True
        self._audit_batch: list[dict[str, object]] | None = None
        self._audit_operation_id: int | None = None
        self._audit_undo: list[Callable[[], None]] | None = None
        self._lock = RLock()

    @property
    def capability_profile(self) -> CapabilityProfile:
        return self._capability_profile

    @property
    def intent_sink(self) -> IntentSink | None:
        return self._intent_sink

    @intent_sink.setter
    def intent_sink(self, sink: IntentSink | None) -> None:
        if sink is not None and _sink_capability_profile(sink) != self.capability_profile:
            raise ValueError("relay session and planner use different capability profiles")
        self._intent_sink = sink

    def bind_language_intent_authorizer(self, authorizer: LanguageIntentAuthorizer) -> None:
        """Install the one plan-binding gate used by the language principal.

        The compiler is composed after sessions are constructed in some tests,
        so this narrow one-time hook avoids a mutable policy surface while still
        keeping a language socket closed until an audited compiler owns it.
        """
        if not callable(authorizer):
            raise ValueError("language intent authorizer must be callable")
        with self._lock:
            existing = self._language_intent_authorizer
            if existing is not None and existing is not authorizer:
                raise ValueError("language intent authorizer is already bound")
            self._language_intent_authorizer = authorizer

    def record_operator_events(self, events: list[Mapping[str, object]]) -> None:
        if not events:
            return
        with self._lock, self._audit_operation():
            self._ensure_mutation_usable()
            for event in events:
                if event.get("session") != self.session_id:
                    raise ValueError("operator event belongs to another session")
                if event.get("type") not in {
                    "detection_frame",
                    "detection",
                    "detection_acknowledgement",
                }:
                    raise ValueError("operator event type is not permitted")
                self._append_audit(event)

    def process_frame(self, raw: object, principal: Principal) -> list[dict[str, object]]:
        """Route one post-authentication frame according to its bound principal."""
        frame_type = raw.get("type") if isinstance(raw, Mapping) else None
        if frame_type == "operator_presence":
            return self.process_operator_presence(raw, principal)
        if principal.source == "localization" and frame_type == "control_localization":
            return self.process_control_localization(raw, principal)
        if principal.source in REGISTERED_SOURCES and frame_type == "intent":
            return self.process_intent(raw, principal)
        if principal.source == "adapter":
            if frame_type == "membership":
                return self.process_membership(raw, principal)
            if frame_type == "telemetry":
                return self.process_telemetry(raw, principal)
            if frame_type == "acknowledgement":
                return self.process_acknowledgement(raw, principal)
            if frame_type in NODE_FRAME_TYPES:
                return self.process_node_frame(raw, principal)
        return [
            self.protocol_refusal(
                reason="frame_not_allowed",
                detail="frame type is not allowed for the authenticated source",
            )
        ]

    def process_operator_presence(
        self, raw: object, principal: Principal
    ) -> list[dict[str, object]]:
        if (
            principal.source != "console"
            or not isinstance(raw, Mapping)
            or set(raw) != {"v", "type"}
            or type(raw["v"]) is not int
            or raw["v"] != 1
        ):
            return [
                self.protocol_refusal(
                    reason="invalid_operator_presence",
                    detail="presence requires an authenticated console",
                )
            ]
        receive = getattr(self.intent_sink, "record_operator_presence", None)
        if not callable(receive):
            return []
        now = self.clock()
        with self._lock, self._audit_operation():
            self._ensure_mutation_usable()
            self._append_audit(
                {
                    "v": 1,
                    "t": now,
                    "type": "operator_presence",
                    "session": self.session_id,
                    "event_id": self.event_ids(),
                    "source": principal.source,
                }
            )
        receive(now)
        return []

    def protocol_refusal(self, *, reason: str, detail: str) -> dict[str, object]:
        with self._lock, self._audit_operation():
            self._ensure_mutation_usable()
            return self._protocol_refusal(reason=reason, detail=detail, now=self.clock())

    def process_intent(self, raw: object, principal: Principal) -> list[dict[str, object]]:
        now = self.clock()
        with self._lock, self._audit_operation():
            self._ensure_mutation_usable()
            public_id = _safe_string_field(raw, "intent_id")
            if public_id is not None and public_id.startswith("safety:"):
                return [
                    self._refuse_intent(
                        raw,
                        reason="reserved_intent_id",
                        detail="safety: intent IDs are reserved for controller-generated stops",
                        now=now,
                        add_to_ledger=False,
                    )
                ]
            if principal.source not in REGISTERED_SOURCES or principal.drone_id is not None:
                return [
                    self._refuse_intent(
                        raw,
                        reason="source_not_allowed",
                        detail="this authenticated connection cannot emit intents",
                        now=now,
                    )
                ]

            if self.intent_sink is not None and not self._sink_profile_agrees():
                return [
                    self._refuse_intent(
                        raw,
                        reason="capability_profile_mismatch",
                        detail="the relay and downstream planner capability profiles diverged",
                        now=now,
                    )
                ]

            result = validate_intent(raw, capability_profile=self.capability_profile)
            if isinstance(result, RejectedIntent):
                return [
                    self._refuse_intent(
                        raw,
                        reason=result.reason.value,
                        detail=result.detail,
                        now=now,
                    )
                ]
            assert isinstance(result, AcceptedIntent)
            intent = result.intent
            if intent.source != principal.source:
                return [
                    self._refuse_intent(
                        raw,
                        reason="source_mismatch",
                        detail="intent source does not match the authenticated connection",
                        now=now,
                        normalized=intent,
                    )
                ]
            if intent.session != self.session_id:
                return [
                    self._refuse_intent(
                        raw,
                        reason="session_mismatch",
                        detail="intent session does not match the WebSocket path",
                        now=now,
                        normalized=intent,
                    )
                ]
            age_error = self._timestamp_error(intent.t, now, self.limits.intent_max_age_ms)
            if age_error is not None:
                return [
                    self._refuse_intent(
                        raw,
                        reason=age_error,
                        detail="intent timestamp is outside the configured freshness window",
                        now=now,
                        normalized=intent,
                    )
                ]
            if intent.intent_id in self._intents:
                return [
                    self._refuse_intent(
                        raw,
                        reason="duplicate_intent",
                        detail="intent_id has already been observed in this session",
                        now=now,
                        normalized=intent,
                        add_to_ledger=False,
                    )
                ]
            if intent.retry_of is not None:
                if not self._can_retry(intent.retry_of):
                    return [
                        self._refuse_intent(
                            raw,
                            reason="invalid_retry",
                            detail=(
                                "retry_of must reference a failed terminal request in this session"
                            ),
                            now=now,
                            normalized=intent,
                        )
                    ]

            if self.intent_sink is None:
                return [
                    self._refuse_intent(
                        raw,
                        reason="downstream_unavailable",
                        detail="no planner/arbiter intent consumer is configured",
                        now=now,
                        normalized=intent,
                    )
                ]

            if intent.source == "language":
                authorizer = self._language_intent_authorizer
                refusal = (
                    (
                        "unbound_language_intent",
                        "language intents require an active audited compiler plan",
                    )
                    if authorizer is None
                    else authorizer(intent, self._state_event(now), now)
                )
                if refusal is not None:
                    reason, detail = refusal
                    return [
                        self._refuse_intent(
                            raw,
                            reason=reason,
                            detail=detail,
                            now=now,
                            normalized=intent,
                        )
                    ]

            self._remember_intent(intent.intent_id)
            self._intents[intent.intent_id] = _IntentLedgerEntry(
                status=LifecycleStatus.ACCEPTED,
                selection=intent.selection,
                command_statuses={},
            )
            self._log_intent(intent, outcome=LifecycleStatus.ACCEPTED, reason=None, now=now)
            self._pending_intents[intent.intent_id] = _PendingIntent(
                intent=intent,
            )
            event = acknowledgement_event(
                t=now,
                event_id=self.event_ids(),
                session=self.session_id,
                intent_id=intent.intent_id,
                status=LifecycleStatus.ACCEPTED,
                roster_version=self.registry.roster_version,
            )
            self._append_audit(event)
            admit = getattr(self.intent_sink, "admit_intent", None)
            if callable(admit):
                admit(intent)
            self._metrics["accepted_intents"] += 1
            self._metrics["acknowledgements"] += 1
            return [event]

    def execute_pending_intent(
        self, intent_id: str, *, defer_resume: bool = False
    ) -> list[dict[str, object]]:
        """Execute accepted work or return a group outcome already committed for it."""
        if self.intent_sink is not None and not self._sink_profile_agrees():
            return self.fail_pending_intent(
                intent_id,
                reason="capability_profile_mismatch",
                detail="the relay and downstream planner capability profiles diverged",
            )
        with self._lock:
            self._ensure_mutation_usable()
            pending = self._pending_intents.get(intent_id)
            if pending is None:
                return []
            if pending.events is not None:
                self._pending_intents.pop(intent_id)
                return pending.events
            if pending.executing:
                return []
            pending.executing = True
            sink = self.intent_sink
        assert sink is not None
        concurrent_intents = getattr(sink, "concurrent_intents", ())
        if pending.intent.name is IntentName.ESTOP or pending.intent.name in concurrent_intents:
            events = self._execute_pending(pending, sink)
        else:
            with self._execution_lock:
                events = self._execute_pending(pending, sink)
        if not defer_resume:
            events.extend(self._resume_acknowledgements(intent_id))
        with self._lock:
            self._pending_intents.pop(intent_id, None)
        return events

    def _sink_profile_agrees(self) -> bool:
        sink = self.intent_sink
        if sink is None:
            return True
        try:
            return _sink_capability_profile(sink) == self.capability_profile
        except ValueError:
            return False

    def execute_coordinated_group(
        self,
        delivered_intent_ids: tuple[str, ...],
        dispatch: Callable[[], dict[str, IntentSinkResult]],
    ) -> dict[str, IntentSinkResult]:
        """Commit all group outcomes; any dispatch or commit failure poisons the session."""
        with self._lock:
            pending = [self._pending_intents[intent_id] for intent_id in delivered_intent_ids]
            for item in pending:
                self._begin_pending_operation(item)
        try:
            results = dispatch()
            with self._lock:
                outcomes: dict[str, list[dict[str, object]]] = {}
                for intent_id, result in results.items():
                    item = self._pending_intents.get(intent_id)
                    if item is None and self._intents[intent_id].status is LifecycleStatus.REFUSED:
                        continue
                    if item is None:
                        raise RuntimeError("coordinated intent has no pending execution")
                    outcomes[intent_id] = self._complete_pending(item, result)
                for intent_id, own_events in outcomes.items():
                    item = self._pending_intents[intent_id]
                    item.events = [
                        event
                        for sibling_id, events in outcomes.items()
                        if sibling_id != intent_id
                        for event in events
                    ] + own_events
            return results
        except BaseException:
            with self._lock:
                self._mutation_usable = False
                self._projection_usable = False
                self._replay_usable = False
                for item in pending:
                    if item.operation_id is not None:
                        self.audit_log.abandon_operation(item.operation_id)
            raise

    def _begin_pending_operation(self, pending: _PendingIntent) -> None:
        with self._audit_operation():
            self._ensure_mutation_usable()
            if pending.events is None and pending.operation_id is None:
                pending.operation_id = self.audit_log.begin_operation()

    def mark_pending_intent_delivered(self, intent_id: str) -> None:
        with self._lock:
            self._ensure_mutation_usable()
            if intent_id not in self._pending_intents:
                return
            delivered = getattr(self.intent_sink, "intent_delivered", None)
            if callable(delivered):
                delivered(intent_id)

    def fail_pending_intent(
        self, intent_id: str, *, reason: str, detail: str
    ) -> list[dict[str, object]]:
        now = self.clock()
        with self._lock, self._audit_operation():
            self._ensure_mutation_usable()
            pending = self._pending_intents.get(intent_id)
            if pending is None:
                return []
            if pending.events is not None:
                self._pending_intents.pop(intent_id)
                return pending.events
            if pending.executing or pending.operation_id is not None:
                return []
            self._remember_intent(intent_id)
            self._pending_intents.pop(intent_id)
            self._acknowledgements.pop(intent_id, None)
            cancel = getattr(self.intent_sink, "cancel_intent", None)
            if callable(cancel):
                cancel(intent_id)
            self._intents[intent_id].status = LifecycleStatus.REFUSED
            self._log_intent(
                pending.intent,
                outcome=LifecycleStatus.REFUSED,
                reason=reason,
                now=now,
            )
            return [self._refusal(intent_id=intent_id, reason=reason, detail=detail, now=now)]

    def _execute_pending(
        self, pending: _PendingIntent, sink: IntentSink
    ) -> list[dict[str, object]]:
        now = self.clock()
        with self._lock:
            if pending.events is not None:
                return pending.events
            self._begin_pending_operation(pending)
        events: list[dict[str, object]] = []
        try:
            process = getattr(sink, "process_relay_intent", None)
            if callable(process):
                delivered = process(pending.intent, self.current_state(), self)
                sink_result = delivered.execution
                events.extend(delivered.relay_events)
            else:
                sink_result = sink(pending.intent, self.current_state())
        except BaseException as error:
            with self._lock, self._audit_operation(operation_id=pending.operation_id):
                self._ensure_mutation_usable()
                if not isinstance(error, Exception):
                    raise
                return [self._record_downstream_error(pending.intent, now)]
        with self._lock:
            return self._complete_pending(pending, sink_result, relay_events=events)

    def _complete_pending(
        self,
        pending: _PendingIntent,
        sink_result: object,
        *,
        relay_events: list[dict[str, object]] | None = None,
    ) -> list[dict[str, object]]:
        self._ensure_mutation_usable()
        if pending.events is not None:
            return pending.events
        self._begin_pending_operation(pending)
        with self._audit_operation(operation_id=pending.operation_id):
            events = [] if relay_events is None else list(relay_events)
            if sink_result is not None and not isinstance(sink_result, IntentSinkResult):
                events.extend(self._record_execution_result(pending.intent, sink_result))
            elif isinstance(sink_result, IntentSinkResult):
                self._log_sink_result(pending.intent, sink_result, now=self.clock())
                events.extend(dict(event) for event in sink_result.events)
                if any(
                    value is not None
                    for value in (
                        sink_result.selection_update,
                        sink_result.armed_update,
                        sink_result.estop_update,
                        sink_result.formation_update,
                        sink_result.spacing_update,
                    )
                ):
                    plan = sink_result.result.get("plan")
                    accepted_plan = dict(plan) if isinstance(plan, Mapping) else None
                    events.append(
                        self.update_control_projection(
                            selection=sink_result.selection_update,
                            accepted_plan=accepted_plan,
                            armed=sink_result.armed_update,
                            estop=sink_result.estop_update,
                            formation=sink_result.formation_update,
                            spacing=sink_result.spacing_update,
                        )
                    )
                events.append(
                    self.record_lifecycle(
                        intent_id=pending.intent.intent_id,
                        status=sink_result.status,
                        source=sink_result.source,
                        reason=sink_result.reason,
                        detail=sink_result.detail,
                    )
                )
        pending.events = events
        return events

    def _record_downstream_error(self, intent: IntentV1, now: int) -> dict[str, object]:
        """Record uncertain post-dispatch failure without authorizing duplicate I/O."""
        self._remember_intent(intent.intent_id)
        entry = self._intents[intent.intent_id]
        entry.retryable = False
        if entry.status is LifecycleStatus.ACCEPTED:
            entry.status = LifecycleStatus.REFUSED
        self._log_intent(intent, outcome=entry.status, reason="downstream_error", now=now)
        detail = "downstream execution is uncertain; this request cannot be retried"
        if entry.status is LifecycleStatus.REFUSED:
            return self._refusal(
                intent_id=intent.intent_id,
                reason="downstream_error",
                detail=detail,
                now=now,
            )
        event = acknowledgement_event(
            t=now,
            event_id=self.event_ids(),
            session=self.session_id,
            intent_id=intent.intent_id,
            status=entry.status,
            roster_version=self.registry.roster_version,
            reason="downstream_error",
            detail=detail,
        )
        self._append_audit(event)
        self._metrics["acknowledgements"] += 1
        return event

    def process_membership(self, raw: object, principal: Principal) -> list[dict[str, object]]:
        now = self.clock()
        with self._lock, self._audit_operation():
            self._ensure_mutation_usable()
            if principal.source != "adapter" or principal.drone_id is None:
                return [
                    self._protocol_refusal(
                        reason="source_not_allowed",
                        detail="only an authenticated adapter may send membership events",
                        now=now,
                    )
                ]
            try:
                request = parse_membership_request(raw)
                self._check_adapter_binding(request.drone_id, principal)
                if request.session != self.session_id:
                    raise ContractError(
                        "session_mismatch", "membership session does not match the WebSocket path"
                    )
                if not verify_event_signature(
                    request.unsigned_event(), request.signature, principal.signing_key
                ):
                    raise ContractError(
                        "invalid_signature", "membership signature was not accepted"
                    )
                self._claim_transport_event(request.event_id, request.t, principal, now)
                transition = self._apply_membership(request)
            except (ContractError, RegistryError) as error:
                return [
                    self._protocol_refusal(
                        reason=error.code,
                        detail=error.detail,
                        now=now,
                        drone_id=principal.drone_id,
                        connection_epoch=self.registry.connection_epoch(principal.drone_id),
                    )
                ]
            return self._record_transition_and_state(transition, now=now)

    def process_telemetry(self, raw: object, principal: Principal) -> list[dict[str, object]]:
        now = self.clock()
        with self._lock, self._audit_operation():
            self._ensure_mutation_usable()
            if principal.source != "adapter" or principal.drone_id is None:
                return [
                    self._protocol_refusal(
                        reason="source_not_allowed",
                        detail="only an authenticated adapter may send telemetry",
                        now=now,
                    )
                ]
            try:
                telemetry = parse_telemetry(raw)
                self._check_adapter_binding(telemetry.drone, principal)
                if telemetry.session != self.session_id:
                    raise ContractError(
                        "session_mismatch", "telemetry session does not match the WebSocket path"
                    )
                self._claim_transport_event(telemetry.event_id, telemetry.t, principal, now)
                transition = self.registry.apply_telemetry(
                    telemetry, transition_event_id=self.event_ids()
                )
            except (ContractError, RegistryError) as error:
                return [
                    self._protocol_refusal(
                        reason=error.code,
                        detail=error.detail,
                        now=now,
                        drone_id=principal.drone_id,
                        connection_epoch=self.registry.connection_epoch(principal.drone_id),
                    )
                ]

            # Sampling telemetry would lose the inputs behind later safety decisions.
            state = self._state_event(now)
            projection = _material_state_projection(state)
            state_changed = transition is not None or self._state_projection_changed(projection)
            telemetry_event = telemetry.to_event()
            self._append_audit(telemetry_event)
            self._metrics["telemetry_events"] += 1
            events: list[dict[str, object]] = [telemetry_event]
            if transition is not None:
                transition_event = transition.to_event(self.session_id)
                self._append_audit(transition_event)
                self._metrics["membership_events"] += 1
                events.append(transition_event)
            if state_changed or self._state_keepalive_due(now):
                self._record_state_audit(state, projection, now)
            events.append(state)
            if transition is not None:
                events.extend(self._reconcile_membership())
            reconcile = getattr(self.intent_sink, "reconcile_landing", None)
            if callable(reconcile):
                events.extend(reconcile(self))
            return events

    def process_acknowledgement(
        self, raw: object, principal: Principal, *, defer_resume: bool = False
    ) -> list[dict[str, object]]:
        now = self.clock()
        with self._lock, self._audit_operation():
            self._ensure_mutation_usable()
            if principal.source != "adapter" or principal.drone_id is None:
                return [
                    self._protocol_refusal(
                        reason="source_not_allowed",
                        detail="only an authenticated adapter may send adapter acknowledgements",
                        now=now,
                    )
                ]
            try:
                acknowledgement = parse_adapter_acknowledgement(raw)
                self._check_acknowledgement(acknowledgement, principal, now)
            except (ContractError, RegistryError) as error:
                return [
                    self._protocol_refusal(
                        reason=error.code,
                        detail=error.detail,
                        now=now,
                        drone_id=principal.drone_id,
                        connection_epoch=self.registry.connection_epoch(principal.drone_id),
                    )
                ]

            event = acknowledgement.to_event()
            self._record_adapter_ack_fact(acknowledgement)
            self._append_audit(event)
            self._metrics["acknowledgements"] += 1
            waiter = self._command_waiters.get(acknowledgement.command_id)
            if waiter is not None:
                waiter.put(acknowledgement)
            events = [event]
            if acknowledgement.status in {
                LifecycleStatus.COMPLETED,
                LifecycleStatus.FAILED,
                LifecycleStatus.INVALIDATED,
            }:
                resume = getattr(self.intent_sink, "resume_after_acknowledgement", None)
                if callable(resume):
                    pending = self._pending_intents.get(acknowledgement.intent_id)
                    queue = self._acknowledgements.setdefault(
                        acknowledgement.intent_id,
                        pending.acknowledgements if pending is not None else [],
                    )
                    queue.append(acknowledgement)
        if not defer_resume:
            events.extend(self._resume_acknowledgements(acknowledgement.intent_id))
        return events

    def _resume_acknowledgements(self, intent_id: str) -> list[dict[str, object]]:
        events = []
        while (work := self.prepare_resume(intent_id)) is not None:
            events.extend(self.commit_resume(work, self.resume_io(work)))
        return events

    def prepare_resume(self, intent_id: str | None = None) -> _ResumeWork | None:
        """Claim queued completion work while the runtime owns its session operation."""
        with self._lock, self._audit_operation():
            self._ensure_mutation_usable()
            for index, work in enumerate(self._resume_continuations):
                if intent_id is None or work.acknowledgement.intent_id == intent_id:
                    work.operation_id = self.audit_log.begin_operation()
                    return self._resume_continuations.pop(index)
            for queued_id, queue in tuple(self._acknowledgements.items()):
                if intent_id is not None and queued_id != intent_id:
                    continue
                pending = self._pending_intents.get(queued_id)
                if queued_id in self._resuming_intents or (
                    pending is not None and pending.events is None
                ):
                    continue
                while queue:
                    acknowledgement = queue[0]
                    prepare = getattr(self.intent_sink, "prepare_resume", None)
                    phased = callable(prepare)
                    token = prepare(self, acknowledgement) if phased else acknowledgement
                    if token is None:
                        queue.pop(0)
                        continue
                    operation_id = self.audit_log.begin_operation()
                    blocked_ids = {queued_id, getattr(token, "intent_id", queued_id)}
                    self._resuming_intents.update(blocked_ids)
                    return _ResumeWork(acknowledgement, token, operation_id, blocked_ids, phased)
                self._acknowledgements.pop(queued_id, None)
            return None

    def resume_io(self, work: _ResumeWork) -> object:
        """Run claimed adapter work without runtime or relay mutation locks."""
        try:
            if work.phased:
                return self.intent_sink.resume_io(work.token)
            return self.intent_sink.resume_after_acknowledgement(self, work.acknowledgement)
        except BaseException:
            with self._lock:
                self.audit_log.abandon_operation(work.operation_id)
                self._resuming_intents.difference_update(work.blocked_ids)
                self._mutation_usable = False
                self._projection_usable = False
                self._replay_usable = False
            raise

    def commit_resume(self, work: _ResumeWork, outcome: object) -> list[dict[str, object]]:
        """Commit owned results for publication in the same runtime operation."""
        with self._lock, self._audit_operation(operation_id=work.operation_id):
            self._ensure_mutation_usable()
            self._remember_intent(work.acknowledgement.intent_id)
            self._remember_resume(work)
            committed = (
                self.intent_sink.commit_resume(work.token, outcome) if work.phased else outcome
            )
            events = list(committed.relay_events) if committed is not None else []
            continuation = getattr(committed, "continuation", None)
            if continuation is not None:
                work.token = continuation
                work.blocked_ids.add(continuation.intent_id)
                self._resuming_intents.add(continuation.intent_id)
                self._resume_continuations.append(work)
            else:
                queue = self._acknowledgements.get(work.acknowledgement.intent_id)
                if queue and queue[0] is work.acknowledgement:
                    queue.pop(0)
                self._resuming_intents.difference_update(work.blocked_ids)
            return events

    def process_control_localization(
        self, raw: object, principal: Principal
    ) -> list[dict[str, object]]:
        """Authenticate, pin, audit, and re-sign one diagnostic localization pose.

        The incoming floating-point payload is never fanned out. The retained and
        returned ``control_pose`` is integer-only and explicitly not flight-approved.
        """
        now = self.clock()
        with self._lock, self._audit_operation():
            self._ensure_mutation_usable()
            try:
                if principal.source != "localization" or principal.drone_id is None:
                    raise ContractError("source_not_allowed", "localization producer required")
                projector = self.control_localization_projector
                if projector is None:
                    raise ContractError(
                        "localization_not_configured",
                        "host-owned localization pins are not configured",
                    )
                frame = ControlLocalizationFrame.parse(raw)
                self._check_adapter_binding(frame.wire.drone_id, principal)
                if frame.session != self.session_id:
                    raise ContractError("session_mismatch", "localization session differs")
                if not frame.signature_valid(principal.signing_key):
                    raise ContractError("invalid_signature", "localization signature rejected")
                self.registry.check_current(frame.wire.drone_id, frame.wire.connection_epoch)
                self._claim_transport_event(frame.event_id, frame.t, principal, now)
                signing_key = (
                    None
                    if self.control_pose_signing_key is None
                    else self.control_pose_signing_key(frame.wire.drone_id)
                )
                if type(signing_key) is not bytes or not 32 <= len(signing_key) <= 4_096:
                    raise ContractError(
                        "control_pose_signing_key_missing",
                        "no bounded aircraft-specific relay signing key is configured",
                    )
                drone_id = frame.wire.drone_id
                pose_event_id = self.event_ids()
                if pose_event_id in self._seen_transport_event_ids or any(
                    retained.event_id == pose_event_id for retained in self._control_pose.values()
                ):
                    raise ContractError(
                        "event_id_collision",
                        "relay event identity collides with retained session evidence",
                    )
                pose = projector.project(
                    frame.wire,
                    authenticated_drone_id=principal.drone_id,
                    authenticated_connection_epoch=frame.wire.connection_epoch,
                    now_ms=now,
                    event_id=pose_event_id,
                    session=self.session_id,
                    previous=self._control_pose.get(drone_id),
                )
            except (ContractError, RegistryError) as error:
                return [self._protocol_refusal(reason=error.code, detail=error.detail, now=now)]
            except LocalizationProjectionError as error:
                return [self._protocol_refusal(reason=error.code, detail=error.detail, now=now)]
            except (ValueError, TypeError, OverflowError):
                return [
                    self._protocol_refusal(
                        reason="invalid_payload",
                        detail="invalid localization frame",
                        now=now,
                    )
                ]
            previous = self._control_pose.get(drone_id)

            def undo() -> None:
                if previous is None:
                    self._control_pose.pop(drone_id, None)
                else:
                    self._control_pose[drone_id] = previous

            assert self._audit_undo is not None
            self._audit_undo.append(undo)
            self._control_pose[drone_id] = pose
            signed_pose = sign_control_pose(pose, signing_key)
            self._append_audit(
                {
                    **frame.unsigned_event(),
                    "signature_verified": True,
                    "projected_event_id": pose.event_id,
                }
            )
            self._append_audit({**pose.unsigned_event(), "signature_emitted": True})
            return [signed_pose]

    def control_pose(self, drone_id: int) -> ControlPose | None:
        with self._lock:
            pose = self._control_pose.get(drone_id)
            if pose is None:
                return None
            try:
                self.registry.check_current(drone_id, pose.connection_epoch)
            except RegistryError:
                return None
            return pose

    def record_navigation_packet(self, packet: Mapping[str, object]) -> dict[str, object]:
        drone_id = packet.get("drone_id")
        epoch = packet.get("connection_epoch")
        if type(drone_id) is not int or type(epoch) is not int:
            raise ValueError("navigation packet has no bounded aircraft identity")
        with self._lock, self._audit_operation():
            self._ensure_mutation_usable()
            self.registry.check_current(drone_id, epoch)
            self._append_audit({key: value for key, value in packet.items() if key != "signature"})
            return dict(packet)

    def process_node_frame(self, raw: object, principal: Principal) -> list[dict[str, object]]:
        """Accept a node-authored frame and project the ones that change state.

        Capabilities and node_status update the aircraft row; media files and capture
        bundles are audited and added to the bounded ``state.captures`` live projection
        without being fanned out themselves; capture readiness is fanned out unchanged.
        """
        now = self.clock()
        with self._lock, self._audit_operation():
            self._ensure_mutation_usable()
            if principal.source != "adapter" or principal.drone_id is None:
                return [
                    self._protocol_refusal(
                        reason="source_not_allowed",
                        detail="only an authenticated adapter may send node frames",
                        now=now,
                    )
                ]
            try:
                frame = _parse_node_frame(raw)
                drone_id, connection_epoch = _node_frame_identity(frame)
                self._check_adapter_binding(drone_id, principal)
                if frame.session != self.session_id:
                    raise ContractError(
                        "session_mismatch", "node frame session does not match the WebSocket path"
                    )
                self._claim_transport_event(frame.event_id, frame.t, principal, now)
                self.registry.check_current(drone_id, connection_epoch)
                if isinstance(frame, CapabilitiesFrame):
                    self.registry.apply_capabilities(frame)
                elif isinstance(frame, NodeStatusFrame):
                    self.registry.apply_node_status(frame)
                elif isinstance(frame, MediaFileFrame):
                    capture_key = (
                        frame.file.drone_id,
                        frame.file.connection_epoch,
                        frame.file.capture_id,
                    )
                    self._remember_capture(capture_key)
                    evicted = self._captures.eviction_candidate(capture_key)
                    if evicted is not None:
                        self._remember_capture(evicted)
                    self._captures.record_media(frame.file, t=frame.t)
                elif isinstance(frame, CaptureBundleFrame):
                    raise ContractError(
                        "source_not_allowed",
                        "a node cannot author capture_bundle because its command carries "
                        "no authoritative room or pattern; send media_file records only",
                    )
                elif isinstance(frame, CaptureReadinessFrame):
                    self._remember_capture_readiness(drone_id)
                    self._capture_readiness[drone_id] = frame
            except (ContractError, RegistryError, CaptureLedgerError) as error:
                return [
                    self._protocol_refusal(
                        reason=error.code,
                        detail=error.detail,
                        now=now,
                        drone_id=principal.drone_id,
                        connection_epoch=self.registry.connection_epoch(principal.drone_id),
                    )
                ]

            event = frame.to_event()
            self._append_audit(event)
            self._metrics["node_events"] += 1
            if isinstance(frame, MediaFileFrame):
                state = self._state_event(now)
                self._append_audit(state)
                return [state]
            events: list[dict[str, object]] = [event]
            if isinstance(frame, CapabilitiesFrame | NodeStatusFrame):
                state = self._state_event(now)
                self._sample_state_audit(state, now)
                events.append(state)
            return events

    def issue_command(
        self,
        *,
        command_id: str,
        intent_id: str,
        roster_version: int,
        drone_id: int,
        connection_epoch: int,
        operation: CommandOperation,
        args: Mapping[str, object],
        signing_key: bytes,
    ) -> dict[str, object]:
        """Sign, sequence, and audit one relay-authored command for a joined node.

        Commands originate in-process, so an intent the session has not seen (an
        autonomy-originated safety plan) is registered as executing so the node's
        acknowledgements correlate; a terminal intent cannot receive new commands.
        The audit record omits the signature; the returned frame carries it.
        """
        now = self.clock()
        with self._lock, self._audit_operation():
            self._ensure_mutation_usable()
            self._prune_command_ledger(now)
            registered = self._issued_commands.get(command_id)
            identity = (intent_id, roster_version, drone_id, connection_epoch, operation)
            if registered is not None and (
                (
                    registered.intent_id,
                    registered.roster_version,
                    registered.drone_id,
                    registered.connection_epoch,
                    registered.operation,
                )
                != identity
                or registered.waiter_active
                or registered.status is not None
            ):
                raise ValueError("command_id has already been issued in this session")
            if registered is None and len(self._issued_commands) >= _COMMAND_LEDGER_MAX:
                raise ValueError(
                    "command ledger is full of commands still awaiting a bounded terminal result"
                )
            self.registry.check_current(drone_id, connection_epoch)
            entry = self._intents.get(intent_id)
            if entry is not None and entry.status in _TERMINAL_STATUSES:
                raise ValueError("intent is terminal and cannot receive new commands")
            key = (drone_id, connection_epoch)
            seq = self._command_seq.get(key, 0) + 1
            event = command_event(
                t=now,
                event_id=self.event_ids(),
                session=self.session_id,
                command_id=command_id,
                intent_id=intent_id,
                roster_version=roster_version,
                drone_id=drone_id,
                connection_epoch=connection_epoch,
                seq=seq,
                issued_at=now,
                ttl_ms=self.limits.command_ttl_ms,
                operation=operation,
                args=args,
            )
            self._remember_command_sequence(key)
            self._remember_issued_command(command_id)
            if entry is None:
                self._remember_intent(intent_id)
                self._intents[intent_id] = _IntentLedgerEntry(
                    status=LifecycleStatus.EXECUTING,
                    selection=(drone_id,),
                    command_statuses={},
                )
            self._command_seq[key] = seq
            if registered is None:
                self._issued_commands[command_id] = _IssuedCommand(
                    intent_id=intent_id,
                    roster_version=roster_version,
                    drone_id=drone_id,
                    connection_epoch=connection_epoch,
                    operation=operation,
                    issued_at=now,
                )
            else:
                registered.issued_at = now
                registered.waiter_active = True
            self._command_waiters[command_id] = queue.SimpleQueue()
            self._append_audit(event)
            self._metrics["commands_issued"] += 1
            return {**event, "signature": sign_event(event, signing_key)}

    def register_dispatched_command(self, command: object) -> None:
        """Register an adapter-domain command at the immediate pre-I/O boundary."""
        command_id = getattr(command, "command_id", None)
        intent_id = getattr(command, "intent_id", None)
        roster_version = getattr(command, "roster_version", None)
        drone_id = getattr(command, "drone_id", None)
        connection_epoch = getattr(command, "connection_epoch", None)
        operation = getattr(command, "operation", None)
        if (
            not isinstance(command_id, str)
            or not command_id
            or not isinstance(intent_id, str)
            or not intent_id
            or not isinstance(roster_version, int)
            or isinstance(roster_version, bool)
            or not isinstance(drone_id, int)
            or isinstance(drone_id, bool)
            or not isinstance(connection_epoch, int)
            or isinstance(connection_epoch, bool)
            or not isinstance(operation, CommandOperation)
        ):
            raise ValueError("dispatched command violates the typed command boundary")
        with self._lock:
            self._ensure_mutation_usable()
            self.registry.check_current(drone_id, connection_epoch)
            entry = self._intents.get(intent_id)
            if entry is None or entry.status in _TERMINAL_STATUSES:
                raise ValueError("dispatched command does not belong to an active intent")
            issued = self._issued_commands.get(command_id)
            identity = (intent_id, roster_version, drone_id, connection_epoch, operation)
            if issued is not None:
                if (
                    issued.intent_id,
                    issued.roster_version,
                    issued.drone_id,
                    issued.connection_epoch,
                    issued.operation,
                ) != identity:
                    raise ValueError("command_id was already registered with another identity")
                return
            now = self.clock()
            self._prune_command_ledger(now)
            if len(self._issued_commands) >= _COMMAND_LEDGER_MAX:
                raise ValueError("command ledger is full")
            self._issued_commands[command_id] = _IssuedCommand(
                intent_id=intent_id,
                roster_version=roster_version,
                drone_id=drone_id,
                connection_epoch=connection_epoch,
                operation=operation,
                issued_at=now,
                waiter_active=False,
            )

    def await_command_acknowledgement(
        self, command_id: str, *, timeout_ms: int
    ) -> AdapterAcknowledgement | None:
        """Block outside the session lock until the node acknowledges or the wait expires.

        Each call returns the next acknowledgement for the command. The waiter is
        released after a terminal acknowledgement or a timeout; later acknowledgements
        remain audited facts but no longer wake a caller.
        """
        with self._lock:
            waiter = self._command_waiters.get(command_id)
        if waiter is None:
            return None
        try:
            acknowledgement = waiter.get(timeout=max(timeout_ms, 0) / 1000)
        except queue.Empty:
            with self._lock:
                self._command_waiters.pop(command_id, None)
                if issued := self._issued_commands.get(command_id):
                    issued.waiter_active = False
            return None
        if acknowledgement.status in _TERMINAL_STATUSES:
            with self._lock:
                self._command_waiters.pop(command_id, None)
                if issued := self._issued_commands.get(command_id):
                    issued.waiter_active = False
        return acknowledgement

    def discard_command_waiter(self, command_id: str) -> None:
        """Stop waking a caller for a command that could not reach the node."""
        with self._lock:
            self._command_waiters.pop(command_id, None)
            if issued := self._issued_commands.get(command_id):
                issued.waiter_active = False

    def capture_readiness(self, drone_id: int) -> CaptureReadinessFrame | None:
        """Return the node's latest capture_readiness frame for its current epoch."""
        with self._lock:
            frame = self._capture_readiness.get(drone_id)
            if frame is None:
                return None
            try:
                self.registry.check_current(drone_id, frame.connection_epoch)
            except RegistryError:
                return None
            return frame

    def media_files(self, drone_id: int, capture_id: str) -> tuple[MediaFileRecord, ...]:
        """Return media records the node reported for a capture in its current epoch."""
        with self._lock:
            epoch = self.registry.connection_epoch(drone_id)
            if epoch is None:
                return ()
            return self._captures.media_files(drone_id, epoch, capture_id)

    def captures(self) -> list[dict[str, object]]:
        """The retained captures exactly as ``state.captures`` projects them."""
        with self._lock:
            return self._captures.projection()

    def _remember_capture(self, key: CaptureKey) -> None:
        before: CaptureEntry | None = self._captures.snapshot_entry(key)

        def undo_captures() -> None:
            self._captures.restore_entry(key, before)

        assert self._audit_undo is not None
        self._audit_undo.append(undo_captures)

    def _record_capture_bundle(self, result: object, now: int) -> dict[str, object] | None:
        """Retain the bundle the dispatcher composed for a capture_room plan.

        The command wire carries only ``capture_id``, so the node cannot name the room or
        pattern; the composed bundle is the authoritative closing record. It is audited
        with the node frame's shape plus ``source: autonomy`` so replay tells it apart.
        """
        bundle = getattr(result, "capture_bundle", None)
        plan = getattr(result, "plan", None)
        intent_name = getattr(getattr(plan, "intent_name", None), "value", None)
        result_status = getattr(getattr(result, "status", None), "value", None)
        if bundle is None:
            if intent_name == "capture_room" and result_status == "completed":
                raise CaptureLedgerError(
                    "capture_bundle_missing",
                    "a completed capture_room result requires an authoritative capture bundle",
                )
            return None
        to_dict = getattr(bundle, "to_dict", None)
        if not callable(to_dict):
            raise CaptureLedgerError(
                "invalid_capture_bundle", "the composed capture bundle is not serializable"
            )
        payload = to_dict()
        if not isinstance(payload, Mapping):
            raise CaptureLedgerError(
                "invalid_capture_bundle", "the composed capture bundle must serialize to an object"
            )
        raw = {
            "v": 1,
            "t": now,
            "type": "capture_bundle",
            "event_id": self.event_ids(),
            "session": self.session_id,
            **payload,
        }
        if raw.get("detail") == "":
            raw["detail"] = None
        try:
            frame = parse_capture_bundle(raw)
        except ContractError as error:
            raise CaptureLedgerError(error.code, error.detail) from None
        self._validate_capture_bundle_result(result, frame)
        key = (frame.drone_id, frame.connection_epoch, frame.capture_id)
        self._remember_capture(key)
        evicted = self._captures.eviction_candidate(key)
        if evicted is not None:
            self._remember_capture(evicted)
        self._captures.record_bundle(frame, t=now)
        event = {**frame.to_event(), "source": LIFECYCLE_SOURCE_AUTONOMY}
        self._append_audit(event)
        return event

    @staticmethod
    def _validate_capture_bundle_result(result: object, frame: CaptureBundleFrame) -> None:
        """Bind dispatcher-composed evidence to the exact accepted capture plan."""
        plan = getattr(result, "plan", None)
        intent_name = getattr(getattr(plan, "intent_name", None), "value", None)
        if intent_name != "capture_room":
            raise CaptureLedgerError(
                "capture_bundle_mismatch", "only a capture_room plan may carry a capture bundle"
            )
        if getattr(plan, "intent_id", None) != getattr(result, "intent_id", None):
            raise CaptureLedgerError(
                "capture_bundle_mismatch", "capture bundle result does not match its plan intent"
            )
        metadata: set[tuple[object, ...]] = set()
        for command in getattr(plan, "commands", ()):
            operation = getattr(getattr(command, "operation", None), "value", None)
            if operation not in {"capture_panorama", "capture_photo"}:
                continue
            parameters = getattr(command, "parameters", None)
            if not isinstance(parameters, Mapping):
                raise CaptureLedgerError(
                    "capture_bundle_mismatch", "capture command parameters are unavailable"
                )
            metadata.add(
                (
                    parameters.get("room_id"),
                    parameters.get("capture_id"),
                    parameters.get("pattern"),
                    getattr(command, "drone_id", None),
                    getattr(command, "connection_epoch", None),
                )
            )
        actual = (
            frame.room_id,
            frame.capture_id,
            frame.pattern,
            frame.drone_id,
            frame.connection_epoch,
        )
        if metadata != {actual}:
            raise CaptureLedgerError(
                "capture_bundle_mismatch",
                "capture bundle room, capture, pattern, drone, or epoch differs from its plan",
            )
        lifecycle_status = getattr(getattr(result, "status", None), "value", None)
        if lifecycle_status == "completed" and frame.status != "completed":
            raise CaptureLedgerError(
                "capture_bundle_mismatch",
                "a completed capture_room lifecycle requires a completed capture bundle",
            )
        refusal = getattr(result, "refusal", None)
        refusal_reason = getattr(getattr(refusal, "reason", None), "value", None)
        if (
            frame.status != "completed"
            and refusal_reason is not None
            and frame.reason != refusal_reason
        ):
            raise CaptureLedgerError(
                "capture_bundle_mismatch",
                "capture bundle reason differs from the terminal intent refusal",
            )

    def _remember_capture_readiness(self, drone_id: int) -> None:
        present = drone_id in self._capture_readiness
        before = self._capture_readiness.get(drone_id)

        def undo_readiness() -> None:
            if present:
                assert before is not None
                self._capture_readiness[drone_id] = before
            else:
                self._capture_readiness.pop(drone_id, None)

        assert self._audit_undo is not None
        self._audit_undo.append(undo_readiness)

    def record_lifecycle(
        self,
        *,
        intent_id: str,
        status: LifecycleStatus,
        source: str,
        command_id: str | None = None,
        drone_id: int | None = None,
        connection_epoch: int | None = None,
        reason: str | None = None,
        detail: str | None = None,
    ) -> dict[str, object]:
        """Wire a planner/arbiter-owned lifecycle result without importing its types."""
        if status is LifecycleStatus.REFUSED:
            return self.record_refusal(
                intent_id=intent_id,
                source=source,
                command_id=command_id,
                drone_id=drone_id,
                connection_epoch=connection_epoch,
                reason=reason or "refused",
                detail=detail or "request refused",
            )
        now = self.clock()
        with self._lock, self._audit_operation():
            self._ensure_mutation_usable()
            entry = self._intents.get(intent_id)
            if entry is None:
                raise ValueError("unknown intent_id")
            event = acknowledgement_event(
                t=now,
                event_id=self.event_ids(),
                session=self.session_id,
                intent_id=intent_id,
                command_id=command_id,
                status=status,
                roster_version=self.registry.roster_version,
                source=source,
                drone_id=drone_id,
                connection_epoch=connection_epoch,
                reason=reason,
                detail=detail,
            )
            self._remember_intent(intent_id)
            self._transition_intent(entry, status)
            self._append_audit(event)
            self._metrics["acknowledgements"] += 1
            return event

    def record_refusal(
        self,
        *,
        intent_id: str | None,
        source: str,
        reason: str,
        detail: str,
        command_id: str | None = None,
        drone_id: int | None = None,
        connection_epoch: int | None = None,
    ) -> dict[str, object]:
        now = self.clock()
        with self._lock, self._audit_operation():
            self._ensure_mutation_usable()
            event = refusal_event(
                t=now,
                event_id=self.event_ids(),
                session=self.session_id,
                intent_id=intent_id,
                command_id=command_id,
                reason=reason,
                detail=detail,
                roster_version=self.registry.roster_version,
                source=source,
                drone_id=drone_id,
                connection_epoch=connection_epoch,
            )
            if intent_id is not None and intent_id in self._intents:
                self._remember_intent(intent_id)
                self._transition_intent(self._intents[intent_id], LifecycleStatus.REFUSED)
            self._append_audit(event)
            self._metrics["refused_intents"] += 1
            return event

    def admit_safety_stop(self, intent: IntentV1) -> dict[str, object]:
        """Register a controller-generated safety stop before adapter I/O."""
        with self._lock, self._audit_operation():
            self._ensure_mutation_usable()
            if (
                intent.name not in {IntentName.HOLD, IntentName.ESTOP}
                or intent.session != self.session_id
            ):
                raise ValueError("expected a safety stop for this session")
            if intent.intent_id in self._intents:
                raise ValueError("duplicate safety intent_id")
            self._remember_intent(intent.intent_id)
            self._intents[intent.intent_id] = _IntentLedgerEntry(
                status=LifecycleStatus.ACCEPTED,
                selection=intent.selection,
                command_statuses={},
            )
            now = self.clock()
            self._log_intent(intent, outcome=LifecycleStatus.ACCEPTED, reason=None, now=now)
            event = acknowledgement_event(
                t=now,
                event_id=self.event_ids(),
                session=self.session_id,
                intent_id=intent.intent_id,
                status=LifecycleStatus.ACCEPTED,
                roster_version=self.registry.roster_version,
            )
            self._append_audit(event)
            self._metrics["accepted_intents"] += 1
            self._metrics["acknowledgements"] += 1
            return event

    def record_safety_action(
        self,
        *,
        reason: str,
        action: str,
        operator_last_seen_ms: int,
    ) -> dict[str, object]:
        """Record a relay safety action before its corresponding stop is dispatched."""
        if reason != "operator_presence_expired" or action not in {"hold", "estop"}:
            raise ValueError("invalid relay safety action")
        if (
            not isinstance(operator_last_seen_ms, int)
            or isinstance(operator_last_seen_ms, bool)
            or operator_last_seen_ms < 0
        ):
            raise ValueError("operator_last_seen_ms must be non-negative")
        now = self.clock()
        with self._lock, self._audit_operation():
            self._ensure_mutation_usable()
            event = {
                "v": 1,
                "t": now,
                "type": "safety_action",
                "event_id": self.event_ids(),
                "session": self.session_id,
                "reason": reason,
                "action": action,
                "operator_last_seen_ms": operator_last_seen_ms,
            }
            self._append_audit(event)
            self._metrics["safety_actions"] += 1
            return event

    def record_execution_result(self, intent: IntentV1, result: object) -> list[dict[str, object]]:
        with self._lock:
            if intent.intent_id not in self._intents:
                raise ValueError("unknown intent_id")
            return self._record_execution_result(intent, result)

    def record_capture_bundle(self, result: object) -> list[dict[str, object]]:
        """Retain and audit the bundle a terminal capture_room result carries, then project it.

        Returns the state event that lists the closed capture, or nothing when the result
        carries no bundle. Invalid or conflicting evidence raises instead of allowing the
        intent lifecycle to report a false completion. The ``capture_bundle`` record itself
        is audited, not fanned out.
        """
        with self._lock, self._audit_operation():
            self._ensure_mutation_usable()
            now = self.clock()
            if self._record_capture_bundle(result, now) is None:
                return []
            state = self._state_event(now)
            self._append_audit(state)
            return [state]

    def _record_execution_result(self, intent: IntentV1, result: object) -> list[dict[str, object]]:
        raw_status = getattr(getattr(result, "status", None), "value", None)
        try:
            status = LifecycleStatus(raw_status)
        except (TypeError, ValueError):
            raise ValueError("intent sink returned an invalid execution status") from None
        if getattr(result, "intent_id", None) != intent.intent_id:
            raise ValueError("intent sink returned an execution for another intent")
        plan = getattr(result, "plan", None)
        if plan is None:
            raise ValueError("intent sink returned execution without its accepted plan")
        plan_dict = plan.to_dict()
        self._register_dispatched_commands(result, plan)
        events: list[dict[str, object]] = []
        terminal = {
            LifecycleStatus.COMPLETED,
            LifecycleStatus.REFUSED,
            LifecycleStatus.FAILED,
            LifecycleStatus.INVALIDATED,
        }
        if status in terminal:
            # Validate and retain capture evidence before clearing the accepted plan or
            # recording a terminal lifecycle. A malformed/conflicting bundle must fail
            # closed rather than leave a completed intent with no trustworthy evidence.
            events.extend(self.record_capture_bundle(result))
            completion_pending = getattr(self.intent_sink, "completion_pending", None)
            awaiting_landing = (
                status in {LifecycleStatus.COMPLETED, LifecycleStatus.INVALIDATED}
                and callable(completion_pending)
                and completion_pending(intent.intent_id)
            )
            active = self.current_state()["accepted_plan"]
            events.append(
                self.update_control_projection(
                    selection=(
                        getattr(plan, "selection_update", None)
                        if status is LifecycleStatus.COMPLETED
                        else None
                    ),
                    accepted_plan=(
                        plan_dict
                        if awaiting_landing
                        else (
                            None
                            if active is None or active.get("intent_id") == intent.intent_id
                            else _UNSET
                        )
                    ),
                    armed=(
                        getattr(plan, "armed_update", None)
                        if status is LifecycleStatus.COMPLETED
                        else None
                    ),
                    estop=(True if getattr(plan, "estop_update", None) is True else None),
                    formation=(
                        getattr(plan, "formation_update", None)
                        if status is LifecycleStatus.COMPLETED
                        else None
                    ),
                    spacing=(
                        getattr(plan, "spacing_update", None)
                        if status is LifecycleStatus.COMPLETED
                        else None
                    ),
                )
            )
        elif status is LifecycleStatus.EXECUTING:
            events.append(
                self.update_control_projection(
                    accepted_plan=plan_dict,
                    estop=True if getattr(plan, "estop_update", None) is True else None,
                )
            )
        refusal = getattr(result, "refusal", None)
        reason = getattr(getattr(refusal, "reason", None), "value", None)
        detail = getattr(refusal, "detail", None)
        events.append(
            self.record_lifecycle(
                intent_id=intent.intent_id,
                status=status,
                source="autonomy",
                reason=reason,
                detail=detail,
            )
        )
        return events

    def _register_dispatched_commands(self, result: object, plan: object) -> None:
        """Register exact adapter-domain commands that returned a nonterminal result.

        The remote wire normally registers a command in ``issue_command`` before it is
        sent. In-process adapters have no command WebSocket, so their typed executing
        result is the durable proof that the same planner command was dispatched. This
        keeps the authenticated late-ACK path exact without accepting arbitrary IDs.
        """
        if getattr(getattr(result, "status", None), "value", None) != "executing":
            return
        commands = {command.command_id: command for command in getattr(plan, "commands", ())}
        for acknowledgement in getattr(result, "acknowledgements", ()):
            if getattr(acknowledgement.status, "value", None) not in {"accepted", "executing"}:
                continue
            command = commands.get(acknowledgement.command_id)
            if command is None:
                raise ValueError("executing result references a command outside its plan")
            identity = (
                command.intent_id,
                command.roster_version,
                command.drone_id,
                command.connection_epoch,
            )
            if identity != (
                acknowledgement.intent_id,
                acknowledgement.roster_version,
                acknowledgement.drone_id,
                acknowledgement.connection_epoch,
            ):
                raise ValueError("executing acknowledgement does not match its plan command")
            issued = self._issued_commands.get(command.command_id)
            if issued is None:
                self._prune_command_ledger(self.clock())
                if len(self._issued_commands) >= _COMMAND_LEDGER_MAX:
                    raise ValueError("command ledger is full")
                self._issued_commands[command.command_id] = _IssuedCommand(
                    intent_id=command.intent_id,
                    roster_version=command.roster_version,
                    drone_id=command.drone_id,
                    connection_epoch=command.connection_epoch,
                    operation=command.operation,
                    issued_at=self.clock(),
                    status=LifecycleStatus(acknowledgement.status.value),
                    waiter_active=False,
                )
                continue
            if (
                issued.intent_id,
                issued.roster_version,
                issued.drone_id,
                issued.connection_epoch,
                issued.operation,
            ) != (*identity, command.operation):
                raise ValueError("wire command identity does not match its planner command")
            issued.status = LifecycleStatus(acknowledgement.status.value)

    def handle_adapter_disconnect(
        self, *, drone_id: int, connection_epoch: int | None
    ) -> list[dict[str, object]]:
        """Turn an authenticated socket loss into a relay-attested membership event."""
        now = self.clock()
        with self._lock, self._audit_operation():
            self._ensure_mutation_usable()
            transition = self.registry.disconnect(
                drone_id=drone_id,
                connection_epoch=connection_epoch,
                t=now,
                event_id=self.event_ids(),
            )
            if transition is None:
                return []
            return self._record_transition_and_state(transition, now=now)

    def periodic_events(self) -> list[dict[str, object]]:
        """Return the 10 Hz projection; audit it only on change or as a bounded keepalive."""
        now = self.clock()
        with self._lock, self._audit_operation():
            self._ensure_mutation_usable()
            possible_ids = [self.event_ids() for _ in range(4)]
            transitions = self.registry.expire_stale_telemetry(now_ms=now, event_ids=possible_ids)
            events: list[dict[str, object]] = []
            for transition in transitions:
                event = transition.to_event(self.session_id)
                self._append_audit(event)
                self._metrics["membership_events"] += 1
                events.append(event)
            state = self._state_event(now)
            if transitions:
                self._record_state_audit(state, _material_state_projection(state), now)
            else:
                self._sample_state_audit(state, now)
            events.append(state)
            if transitions:
                events.extend(self._reconcile_membership())
            return events

    def current_state(self) -> dict[str, object]:
        with self._lock:
            self._ensure_projection_usable()
            return self._state_event(self.clock())

    def current_state_if_available(self) -> dict[str, object] | None:
        """Return immediately so async callers can offload only contended reads."""
        if not self._lock.acquire(blocking=False):
            return None
        try:
            self._ensure_projection_usable()
            return self._state_event(self.clock())
        finally:
            self._lock.release()

    def update_control_projection(
        self,
        *,
        selection: tuple[int, ...] | None = None,
        accepted_plan: dict[str, object] | None | object = _UNSET,
        pending: dict[str, object] | None | object = _UNSET,
        armed: bool | None = None,
        estop: bool | None = None,
        formation: str | None = None,
        spacing: float | None = None,
    ) -> dict[str, object]:
        """Apply accepted control state; failure after the durable marker disables the session."""
        if accepted_plan is not _UNSET:
            accepted_plan = _bounded_control_projection_snapshot(accepted_plan, "accepted_plan")
        if pending is not _UNSET:
            pending = _bounded_control_projection_snapshot(pending, "pending")
        now = self.clock()
        with self._lock, self._audit_operation():
            self._ensure_mutation_usable()
            if self._audit_operation_id is None:
                self._audit_operation_id = self.audit_log.begin_operation()
            if selection is not None:
                self.registry.set_selection(selection)
            if accepted_plan is not _UNSET:
                assert accepted_plan is None or isinstance(accepted_plan, dict)
                self.registry.set_accepted_plan(accepted_plan)
            if pending is not _UNSET:
                assert pending is None or isinstance(pending, dict)
                self.registry.set_pending(pending)
            if armed is not None:
                self.registry.set_armed(armed)
            if estop is not None:
                self.registry.set_estop(estop)
            if formation is not None:
                self.registry.set_formation(formation)
            if spacing is not None:
                self.registry.set_spacing(spacing)
            state = self._state_event(now)
            self._record_state_audit(state, _material_state_projection(state), now)
            return state

    def replay(
        self, *, after_sequence: int = 0, deadline: float | None = None
    ) -> dict[str, object]:
        if deadline is None:
            deadline = time.monotonic() + LIVE_REPLAY_TIMEOUT_SECONDS
        with self._replay_lock(deadline):
            self._ensure_replay_usable()
        records, last_sequence = self.audit_log.replay_snapshot(
            after_sequence=after_sequence, deadline=deadline
        )
        with self._replay_lock(deadline):
            self._ensure_replay_usable()
            return {
                "v": 1,
                "t": self.clock(),
                "type": "replay",
                "event_id": self.event_ids(),
                "session": self.session_id,
                "after_sequence": after_sequence,
                "last_sequence": last_sequence,
                "events": records,
            }

    @contextmanager
    def _replay_lock(self, deadline: float) -> Iterator[None]:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not self._lock.acquire(timeout=remaining):
            raise AuditLogError("relay session replay exceeded the live replay deadline")
        try:
            yield
        finally:
            self._lock.release()

    def metrics(self) -> dict[str, int]:
        with self._lock:
            self._ensure_projection_usable()
            return {**self._metrics, "roster_version": self.registry.roster_version}

    def _apply_membership(self, request: MembershipRequest) -> MembershipTransition:
        if request.action is MembershipAction.JOIN:
            return self.registry.apply_join(request)
        if request.action is MembershipAction.READINESS:
            return self.registry.apply_readiness(request)
        if request.action is MembershipAction.GRACEFUL_LEAVE:
            assert request.connection_epoch is not None
            if self.leave_authorizer is None:
                raise RegistryError(
                    "graceful_leave_not_authorized",
                    "the autonomy safety path has not authorized graceful leave",
                )
            try:
                authorized = self.leave_authorizer(
                    request.drone_id,
                    request.connection_epoch,
                    self.current_state(),
                )
            except Exception:
                authorized = False
            if authorized is not True:
                raise RegistryError(
                    "graceful_leave_not_authorized",
                    "aircraft must be landed, disarmed, and task-free before leaving",
                )
            return self.registry.apply_graceful_leave(request)
        raise AssertionError("wire membership parser returned an internal action")

    def _record_transition_and_state(
        self, transition: MembershipTransition, *, now: int
    ) -> list[dict[str, object]]:
        event = transition.to_event(self.session_id)
        state = self._state_event(transition.t)
        if transition.invalidation_reason is not None:
            state.update(
                invalidated_intent_ids=list(transition.invalidated_intent_ids),
                invalidation_reason=transition.invalidation_reason,
                prior_roster_version=transition.prior_roster_version,
                cleared_control_fields=list(transition.cleared_control_fields),
            )
        self._append_audit(event)
        self._record_state_audit(state, _material_state_projection(state), now)
        self._metrics["membership_events"] += 1
        return [event, state, *self._reconcile_membership()]

    def _reconcile_membership(self) -> tuple[dict[str, object], ...]:
        reconcile = getattr(self.intent_sink, "reconcile_membership", None)
        return tuple(reconcile(self)) if callable(reconcile) else ()

    def _check_acknowledgement(
        self, acknowledgement: AdapterAcknowledgement, principal: Principal, now: int
    ) -> None:
        self._check_adapter_binding(acknowledgement.drone_id, principal)
        if acknowledgement.session != self.session_id:
            raise ContractError(
                "session_mismatch", "acknowledgement session does not match WebSocket path"
            )
        self._claim_transport_event(acknowledgement.event_id, acknowledgement.t, principal, now)
        current_epoch = self.registry.connection_epoch(acknowledgement.drone_id)
        if current_epoch is None:
            raise RegistryError("unknown_aircraft", "acknowledging aircraft has not joined")
        if acknowledgement.connection_epoch != current_epoch:
            raise RegistryError(
                "stale_connection_epoch", "acknowledgement carries a prior connection epoch"
            )
        issued = self._issued_commands.get(acknowledgement.command_id)
        if issued is None:
            raise ContractError(
                "unknown_command_id",
                "acknowledgement does not reference a command issued by this relay session",
            )
        if acknowledgement.intent_id != issued.intent_id:
            raise ContractError(
                "command_intent_mismatch",
                "acknowledgement intent does not match the issued command",
            )
        if acknowledgement.roster_version != issued.roster_version:
            raise ContractError(
                "command_roster_mismatch",
                "acknowledgement roster does not match the issued command",
            )
        if acknowledgement.drone_id != issued.drone_id:
            raise ContractError(
                "command_drone_mismatch",
                "acknowledgement aircraft does not match the issued command",
            )
        if acknowledgement.connection_epoch != issued.connection_epoch:
            raise ContractError(
                "command_epoch_mismatch",
                "acknowledgement epoch does not match the issued command",
            )
        if acknowledgement.intent_id not in self._intents:
            raise ContractError("unknown_intent_id", "acknowledgement references an unknown intent")

    def _record_adapter_ack_fact(self, acknowledgement: AdapterAcknowledgement) -> None:
        """Retain command facts; only the autonomy owner terminalizes an intent."""
        self._remember_intent(acknowledgement.intent_id)
        self._remember_issued_command(acknowledgement.command_id)
        entry = self._intents[acknowledgement.intent_id]
        entry.command_statuses[acknowledgement.command_id] = acknowledgement.status
        issued = self._issued_commands[acknowledgement.command_id]
        issued.status = acknowledgement.status

    def _prune_command_ledger(self, now: int) -> None:
        """Bound retained terminal/abandoned commands without evicting active waiters."""
        retention_ms = max(
            _COMMAND_RETENTION_MIN_MS,
            self.limits.command_ttl_ms * _COMMAND_RETENTION_MULTIPLIER,
        )
        expired = [
            command_id
            for command_id, issued in self._issued_commands.items()
            if not issued.waiter_active and now - issued.issued_at > retention_ms
        ]
        for command_id in expired:
            if self._audit_undo is not None:
                self._remember_issued_command(command_id)
            self._issued_commands.pop(command_id, None)
            self._command_waiters.pop(command_id, None)
        if len(self._issued_commands) < _COMMAND_LEDGER_MAX:
            return
        removable = [
            command_id
            for command_id, issued in self._issued_commands.items()
            if not issued.waiter_active and issued.status in _TERMINAL_STATUSES
        ]
        for command_id in removable:
            if len(self._issued_commands) < _COMMAND_LEDGER_MAX:
                break
            if self._audit_undo is not None:
                self._remember_issued_command(command_id)
            self._issued_commands.pop(command_id, None)
            self._command_waiters.pop(command_id, None)

    @staticmethod
    def _transition_intent(entry: _IntentLedgerEntry, status: LifecycleStatus) -> None:
        if entry.status in _TERMINAL_STATUSES and entry.status is not status:
            raise ValueError(
                f"cannot move terminal intent from {entry.status.value} to {status.value}"
            )
        if status is LifecycleStatus.ACCEPTED and entry.status is not LifecycleStatus.ACCEPTED:
            raise ValueError("accepted is only valid as the initial lifecycle status")
        entry.status = status

    def _claim_transport_event(
        self, event_id: str, timestamp: int, principal: Principal, now: int
    ) -> None:
        error = self._timestamp_error(timestamp, now, self.limits.transport_event_max_age_ms)
        if error is not None:
            raise ContractError(error, "transport event is outside the freshness window")
        pruned: dict[str, int] = {}
        if len(self._seen_transport_event_ids) >= _TRANSPORT_REPLAY_PRUNE_AT:
            pruned = {
                seen_id: expires_at
                for seen_id, expires_at in self._seen_transport_event_ids.items()
                if expires_at < now
            }
            for seen_id in pruned:
                self._seen_transport_event_ids.pop(seen_id)
            if pruned:
                assert self._audit_undo is not None
                self._audit_undo.append(lambda: self._seen_transport_event_ids.update(pruned))
        if event_id in self._seen_transport_event_ids:
            raise ContractError("replayed_event", "event_id has already been observed")
        if len(self._seen_transport_event_ids) >= _TRANSPORT_REPLAY_LEDGER_MAX:
            raise ContractError(
                "transport_replay_capacity",
                "fresh transport replay evidence exceeds the bounded session ledger",
            )
        key = (principal.source, principal.drone_id)
        previous_t = self._last_transport_t.get(key)
        if previous_t is not None and timestamp < previous_t:
            raise ContractError("out_of_order_event", "event timestamp precedes the prior event")

        def undo_claim() -> None:
            self._seen_transport_event_ids.pop(event_id, None)
            if previous_t is None:
                self._last_transport_t.pop(key, None)
            else:
                self._last_transport_t[key] = previous_t

        assert self._audit_undo is not None
        self._audit_undo.append(undo_claim)
        self._seen_transport_event_ids[event_id] = (
            timestamp + self.limits.transport_event_max_age_ms
        )
        self._last_transport_t[key] = timestamp

    def _timestamp_error(self, timestamp: int, now: int, max_age: int) -> str | None:
        if timestamp - now > self.limits.future_clock_skew_ms:
            return "future_timestamp"
        if now - timestamp > max_age:
            return "stale_timestamp"
        return None

    @staticmethod
    def _check_adapter_binding(drone_id: int, principal: Principal) -> None:
        if principal.drone_id != drone_id:
            raise ContractError(
                "drone_identity_mismatch",
                "frame drone ID does not match the authenticated adapter binding",
            )

    def _refuse_intent(
        self,
        raw: object,
        *,
        reason: str,
        detail: str,
        now: int,
        normalized: IntentV1 | None = None,
        add_to_ledger: bool = True,
    ) -> dict[str, object]:
        intent_id = _safe_string_field(raw, "intent_id")
        if normalized is not None:
            intent_id = normalized.intent_id
        self._log_refused_intent(
            raw,
            normalized=normalized,
            reason=reason,
            now=now,
        )
        if intent_id is not None and add_to_ledger and intent_id not in self._intents:
            retry_of = _safe_string_field(raw, "retry_of")
            self._remember_intent(intent_id)
            self._intents[intent_id] = _IntentLedgerEntry(
                status=LifecycleStatus.REFUSED,
                selection=() if normalized is None else normalized.selection,
                command_statuses={},
                retryable=reason != "invalid_retry"
                and (retry_of is None or self._can_retry(retry_of)),
            )
        return self._refusal(intent_id=intent_id, reason=reason, detail=detail, now=now)

    def _can_retry(self, intent_id: str) -> bool:
        entry = self._intents.get(intent_id)
        return (
            entry is not None
            and entry.retryable
            and entry.status
            in {LifecycleStatus.REFUSED, LifecycleStatus.FAILED, LifecycleStatus.INVALIDATED}
        )

    def _refusal(
        self, *, intent_id: str | None, reason: str, detail: str, now: int
    ) -> dict[str, object]:
        event = refusal_event(
            t=now,
            event_id=self.event_ids(),
            session=self.session_id,
            intent_id=intent_id,
            reason=reason,
            detail=detail,
            roster_version=self.registry.roster_version,
        )
        self._append_audit(event)
        self._metrics["refused_intents"] += 1
        return event

    def _protocol_refusal(
        self,
        *,
        reason: str,
        detail: str,
        now: int,
        drone_id: int | None = None,
        connection_epoch: int | None = None,
    ) -> dict[str, object]:
        event = refusal_event(
            t=now,
            event_id=self.event_ids(),
            session=self.session_id,
            intent_id=None,
            reason=reason,
            detail=detail,
            roster_version=self.registry.roster_version,
            source="relay",
            drone_id=drone_id,
            connection_epoch=connection_epoch,
        )
        self._append_audit(event)
        return event

    def _log_intent(
        self,
        intent: IntentV1,
        *,
        outcome: LifecycleStatus,
        reason: str | None,
        now: int,
    ) -> None:
        event = {
            "v": 1,
            "t": now,
            "type": "intent_record",
            "event_id": self.event_ids(),
            "session": self.session_id,
            "intent_id": intent.intent_id,
            "outcome": outcome.value,
            "reason": reason,
            "roster_version": self.registry.roster_version,
            "intent": _intent_to_dict(intent),
        }
        self._append_audit(event)

    def _log_sink_result(self, intent: IntentV1, result: IntentSinkResult, *, now: int) -> None:
        self._append_audit(
            {
                "v": 1,
                "t": now,
                "type": "autonomy_result",
                "event_id": self.event_ids(),
                "session": self.session_id,
                "intent_id": intent.intent_id,
                "status": result.status.value,
                "source": result.source,
                "result": dict(result.result),
            }
        )

    def _log_refused_intent(
        self,
        raw: object,
        *,
        normalized: IntentV1 | None,
        reason: str,
        now: int,
    ) -> None:
        if normalized is not None:
            self._log_intent(
                normalized,
                outcome=LifecycleStatus.REFUSED,
                reason=reason,
                now=now,
            )
            return
        event = {
            "v": 1,
            "t": now,
            "type": "intent_record",
            "event_id": self.event_ids(),
            "session": self.session_id,
            "intent_id": _safe_string_field(raw, "intent_id"),
            "outcome": LifecycleStatus.REFUSED.value,
            "reason": reason,
            "roster_version": self.registry.roster_version,
            "intent": None,
        }
        self._append_audit(event)

    @contextmanager
    def _audit_operation(self, *, operation_id: int | None = None) -> Iterator[None]:
        outermost = self._audit_batch is None
        if outermost:
            self._audit_batch = []
            self._audit_operation_id = operation_id
        registry_transaction = self.registry.transaction() if outermost else nullcontext()
        with registry_transaction, self._rollback_session_state(outermost):
            try:
                yield
                batch = self._audit_batch
                if outermost and (batch or self._audit_operation_id is not None):
                    assert batch is not None
                    self._commit_audit_batch(batch)
            except BaseException as error:
                if (
                    self._audit_batch
                    or self._audit_operation_id is not None
                    or isinstance(error, AuditLogError)
                ):
                    self._mutation_usable = False
                    self._projection_usable = False
                    self._replay_usable = False
                    if self._audit_operation_id is not None:
                        self.audit_log.abandon_operation(self._audit_operation_id)
                raise
            finally:
                if outermost:
                    self._audit_batch = None
                    self._audit_operation_id = None

    @contextmanager
    def _rollback_session_state(self, outermost: bool) -> Iterator[None]:
        if not outermost:
            yield
            return
        before_metrics = self._metrics.copy()
        self._audit_undo = []
        try:
            yield
        except BaseException:
            for undo in reversed(self._audit_undo):
                undo()
            self._metrics = before_metrics
            raise
        finally:
            self._audit_undo = None

    def _remember_intent(self, intent_id: str) -> None:
        entry = self._intents.get(intent_id)
        before = (
            None
            if entry is None
            else replace(entry, command_statuses=entry.command_statuses.copy())
        )
        pending = self._pending_intents.get(intent_id)
        acknowledgements = None if pending is None else pending.acknowledgements.copy()
        queue_for_intent = self._acknowledgements.get(intent_id)
        queued = None if queue_for_intent is None else queue_for_intent.copy()

        def undo_intent() -> None:
            if before is None:
                self._intents.pop(intent_id, None)
            else:
                self._intents[intent_id] = before
            if pending is None:
                self._pending_intents.pop(intent_id, None)
            else:
                assert acknowledgements is not None
                pending.acknowledgements[:] = acknowledgements
                self._pending_intents[intent_id] = pending
            if queue_for_intent is None:
                self._acknowledgements.pop(intent_id, None)
            else:
                assert queued is not None
                queue_for_intent[:] = queued
                self._acknowledgements[intent_id] = queue_for_intent

        assert self._audit_undo is not None
        self._audit_undo.append(undo_intent)

    def _remember_resume(self, work: _ResumeWork) -> None:
        token = work.token
        blocked_ids = work.blocked_ids.copy()
        resuming = self._resuming_intents.copy()
        continuations = self._resume_continuations.copy()

        def undo_resume() -> None:
            work.token = token
            work.blocked_ids.clear()
            work.blocked_ids.update(blocked_ids)
            self._resuming_intents.clear()
            self._resuming_intents.update(resuming)
            self._resume_continuations[:] = continuations

        assert self._audit_undo is not None
        self._audit_undo.append(undo_resume)

    def _remember_issued_command(self, command_id: str) -> None:
        issued = self._issued_commands.get(command_id)
        before = None if issued is None else replace(issued)
        waiter_present = command_id in self._command_waiters
        waiter = self._command_waiters.get(command_id)

        def undo_command() -> None:
            if before is None:
                self._issued_commands.pop(command_id, None)
            else:
                self._issued_commands[command_id] = before
            if waiter_present:
                assert waiter is not None
                self._command_waiters[command_id] = waiter
            else:
                self._command_waiters.pop(command_id, None)

        assert self._audit_undo is not None
        self._audit_undo.append(undo_command)

    def _remember_command_sequence(self, key: tuple[int, int]) -> None:
        present = key in self._command_seq
        before = self._command_seq.get(key)

        def undo_sequence() -> None:
            if present:
                assert before is not None
                self._command_seq[key] = before
            else:
                self._command_seq.pop(key, None)

        assert self._audit_undo is not None
        self._audit_undo.append(undo_sequence)

    def _append_audit(self, event: Mapping[str, object]) -> dict[str, object]:
        self._ensure_mutation_usable()
        if self._audit_batch is None:
            raise RuntimeError("audit append requires an active relay operation")
        buffered = dict(event)
        self._audit_batch.append(buffered)
        if self._audit_operation_id is None:
            self._audit_operation_id = self.audit_log.begin_operation()
        return buffered

    def _remember_audit_sampling(self) -> None:
        before = self._audit_sampling.copy()

        def undo_sampling() -> None:
            self._audit_sampling = before

        assert self._audit_undo is not None
        self._audit_undo.append(undo_sampling)

    def _state_projection_changed(self, projection: str) -> bool:
        return projection != self._audit_sampling.state_projection

    def _state_keepalive_due(self, now: int) -> bool:
        audited_at = self._audit_sampling.state_audited_at
        return (
            audited_at is None
            or now < audited_at
            or now - audited_at >= self.limits.audit_state_interval_ms
        )

    def _sample_state_audit(self, state: dict[str, object], now: int) -> bool:
        """Audit a snapshot only when its material projection changed or a keepalive is due."""
        projection = _material_state_projection(state)
        if not (self._state_projection_changed(projection) or self._state_keepalive_due(now)):
            return False
        self._record_state_audit(state, projection, now)
        return True

    def _record_state_audit(self, state: dict[str, object], projection: str, now: int) -> None:
        """Audit a snapshot unconditionally; decisions always log the state they saw."""
        self._remember_audit_sampling()
        self._append_audit(state)
        self._audit_sampling.state_projection = projection
        self._audit_sampling.state_audited_at = now

    def _commit_audit_batch(self, events: list[dict[str, object]]) -> None:
        if self._audit_operation_id is None:
            raise RuntimeError("audit commit requires a durable operation marker")
        try:
            self.audit_log.append_batch(events, operation_id=self._audit_operation_id)
        except AuditLogError:
            self._mutation_usable = False
            self._projection_usable = False
            self._replay_usable = False
            raise

    def _ensure_mutation_usable(self) -> None:
        if not self._mutation_usable:
            raise AuditLogError("relay session is unusable after an audit failure")

    def _ensure_projection_usable(self) -> None:
        if not self._projection_usable:
            raise AuditLogError("relay session is unusable after an audit failure")

    def _ensure_replay_usable(self) -> None:
        if not self._replay_usable:
            raise AuditLogError("relay session is unusable after an audit failure")

    def _state_event(self, now: int) -> dict[str, object]:
        event = self.registry.state_event(
            session=self.session_id,
            t=now,
            event_id=self.event_ids(),
        )
        event["captures"] = self._captures.projection()
        return event


_VOLATILE_STATE_KEYS = frozenset({"t", "event_id", "state_sequence"})
# These two planner-owned objects share the per-aircraft projection budget. Four
# maximum aircraft plus both maximum control objects still fit one 1 MiB record.
MAX_MATERIAL_CONTROL_PROJECTION_BYTES = 128 * 1024
_MATERIAL_STATE_PASSTHROUGH_KEYS = frozenset(
    {
        "v",
        "type",
        "session",
        "roster_version",
        "armed",
        "estop",
        "selection",
        "formation",
        "spacing",
        "mode",
        "capability_profile",
        "enabled_intent_names",
        "invalidated_intent_ids",
        "invalidation_reason",
        "prior_roster_version",
        "cleared_control_fields",
    }
)
_DRONE_STATE_KEYS = frozenset(
    {
        "drone_id",
        "connection_epoch",
        "membership",
        "readiness_reasons",
        "flight_state",
        "battery",
        "link",
        "pos_quality",
        "control_authority",
        "last_seen_at",
        "camera_patterns",
        "selectable",
        "adapter_id",
        "adapter_capabilities",
        "home_pose",
        "rc_safety_operator_present",
        "telemetry",
        "membership_history",
        "membership_history_truncated",
        "camera_capabilities",
        "node_status",
        "video",
    }
)
_VOLATILE_DRONE_KEYS = frozenset({"last_seen_at", "telemetry", "battery", "link", "pos_quality"})
_MAX_DRONE_PROJECTION_LIST_ITEMS = MAX_CAPABILITY_LIST_ITEMS
_BOUNDED_DRONE_LIST_FIELDS = frozenset(
    {"readiness_reasons", "camera_patterns", "adapter_capabilities", "membership_history"}
)
_TIMESTAMPED_DRONE_REPORTS = {
    "camera_capabilities": "t",
    "node_status": "t",
    "video": "last_frame_at",
}
_DRONE_REPORT_FIELDS = {
    "camera_capabilities": frozenset(
        {
            "v",
            "t",
            "type",
            "drone_id",
            "native_panorama_modes",
            "photo_capture",
            "gimbal_pitch_min_deg",
            "gimbal_pitch_max_deg",
            "horizontal_fov_deg",
            "storage_remaining_bytes",
            "media_retrieval",
            "aircraft_model",
            "aircraft_firmware",
            "rc_firmware",
            "phone_model",
            "android_version",
            "sdk_version",
            "measured_hfov_deg",
        }
    ),
    "node_status": frozenset(
        {
            "v",
            "t",
            "type",
            "drone_id",
            "virtual_stick_enabled",
            "control_authority",
            "authority_change_reason",
            "watchdog_state",
            "video_publish_state",
            "phone_battery_percent",
            "phone_thermal_state",
        }
    ),
    "video": frozenset({"status", "last_frame_at"}),
}
_MEMBERSHIP_HISTORY_FIELDS = frozenset(
    {
        "t",
        "action",
        "membership",
        "connection_epoch",
        "reason",
        "flight_state",
        "battery",
        "link",
        "pos_quality",
    }
)
_MAX_MATERIAL_DRONE_PROJECTION_BYTES = 128 * 1024
_MAX_MATERIAL_CAPTURE_PROJECTION_BYTES = 1024 * 1024


def _material_state_projection(state: Mapping[str, object]) -> str:
    """Exclude volatile fields already retained in source events from snapshot comparison."""
    # New collections need bounded projections to keep this 10 Hz comparison bounded.
    unknown = (
        set(state)
        - _VOLATILE_STATE_KEYS
        - _MATERIAL_STATE_PASSTHROUGH_KEYS
        - _MATERIAL_STATE_FIELD_PROJECTORS.keys()
    )
    if unknown:
        fields = ", ".join(sorted(str(key) for key in unknown))
        raise AuditLogError(f"state fields need bounded audit projectors: {fields}")
    projection = {
        key: value for key, value in state.items() if key in _MATERIAL_STATE_PASSTHROUGH_KEYS
    }
    if "drones" not in state:
        raise AuditLogError("state is missing the required drones projection")
    for key, projector in _MATERIAL_STATE_FIELD_PROJECTORS.items():
        if key in state:
            projection[key] = projector(state[key])
    try:
        return json.dumps(
            projection,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as error:
        raise AuditLogError(f"material state is not JSON-native: {error}") from None


def _material_drones_projection(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list) or len(value) > MAX_PHYSICAL_AIRCRAFT:
        raise AuditLogError("state drones require a bounded aircraft list")
    if not all(isinstance(drone, Mapping) for drone in value):
        raise AuditLogError("state drones must contain objects")
    return [_material_drone_projection(drone) for drone in value]


def _material_pending_projection(value: object) -> dict[str, object] | None:
    return _material_control_projection(value, "pending")


def _material_accepted_plan_projection(value: object) -> dict[str, object] | None:
    return _material_control_projection(value, "accepted_plan")


def _material_captures_projection(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list) or len(value) > MAX_CAPTURE_ENTRIES:
        raise AuditLogError("state captures require a bounded capture list")
    fields = {
        "capture_id",
        "drone_id",
        "connection_epoch",
        "room_id",
        "pattern",
        "coverage",
        "status",
        "reason",
        "detail",
        "files",
        "updated_at",
    }
    projected = []
    for capture in value:
        if not isinstance(capture, Mapping) or set(capture) != fields:
            raise AuditLogError("capture fields do not match the bounded projection")
        files = capture["files"]
        if not isinstance(files, list) or len(files) > MAX_MEDIA_FILES_PER_CAPTURE:
            raise AuditLogError("capture files require a bounded media list")
        entry = {key: item for key, item in capture.items() if key != "updated_at"}
        try:
            size = len(json.dumps(entry, allow_nan=False).encode())
        except (TypeError, ValueError) as error:
            raise AuditLogError(f"material capture is not JSON-native: {error}") from None
        if size > _MAX_MATERIAL_CAPTURE_PROJECTION_BYTES:
            raise AuditLogError("material capture exceeds the bounded audit projection")
        projected.append(entry)
    return projected


_MATERIAL_STATE_FIELD_PROJECTORS: dict[str, Callable[[object], object]] = {
    "drones": _material_drones_projection,
    "captures": _material_captures_projection,
    "pending": _material_pending_projection,
    "accepted_plan": _material_accepted_plan_projection,
}


def _material_control_projection(value: object, field: str) -> dict[str, object] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise AuditLogError(f"state {field} must be an object or null")
    try:
        encoded = json.dumps(
            value,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    except (TypeError, ValueError) as error:
        raise AuditLogError(f"state {field} is not JSON-native: {error}") from None
    if len(encoded) > MAX_MATERIAL_CONTROL_PROJECTION_BYTES:
        raise AuditLogError(f"state {field} exceeds {MAX_MATERIAL_CONTROL_PROJECTION_BYTES} bytes")
    return value


def _bounded_control_projection_snapshot(value: object, field: str) -> object:
    """Return the exact bounded snapshot that a later control transaction may retain."""
    if not isinstance(value, dict):
        return value
    try:
        encoded = json.dumps(
            value,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    except (TypeError, ValueError):
        # Preserve the existing fail-closed transaction behavior for malformed
        # internal objects; this preflight exists only for the explicit size bound.
        return value
    if len(encoded) > MAX_MATERIAL_CONTROL_PROJECTION_BYTES:
        raise ValueError(
            f"{field} exceeds the {MAX_MATERIAL_CONTROL_PROJECTION_BYTES}-byte state bound"
        )
    decoded = json.loads(encoded)
    assert isinstance(decoded, dict)
    return decoded


def _material_drone_projection(drone: Mapping[str, object]) -> dict[str, object]:
    missing = _DRONE_STATE_KEYS - set(drone)
    unknown = set(drone) - _DRONE_STATE_KEYS
    if missing or unknown:
        detail = []
        if missing:
            detail.append(f"missing {', '.join(sorted(missing))}")
        if unknown:
            detail.append(f"unknown {', '.join(sorted(str(key) for key in unknown))}")
        raise AuditLogError(
            f"drone state fields need bounded audit projectors: {'; '.join(detail)}"
        )
    for list_field in _BOUNDED_DRONE_LIST_FIELDS:
        value = drone[list_field]
        if not isinstance(value, list) or len(value) > _MAX_DRONE_PROJECTION_LIST_ITEMS:
            raise AuditLogError(
                f"drone state {list_field} must be a list of at most "
                f"{_MAX_DRONE_PROJECTION_LIST_ITEMS} items"
            )
    history = drone["membership_history"]
    if any(
        not isinstance(item, Mapping) or set(item) != _MEMBERSHIP_HISTORY_FIELDS for item in history
    ):
        raise AuditLogError("drone membership history fields do not match the bounded projection")
    home_pose = drone["home_pose"]
    if home_pose is not None and (
        not isinstance(home_pose, Mapping) or set(home_pose) != {"x", "y", "z"}
    ):
        raise AuditLogError("drone home_pose fields do not match the bounded projection")
    projection = {key: drone[key] for key in _DRONE_STATE_KEYS - _VOLATILE_DRONE_KEYS}
    for report, timestamp in _TIMESTAMPED_DRONE_REPORTS.items():
        value = projection.get(report)
        if value is None and report != "video":
            continue
        if not isinstance(value, Mapping) or set(value) != _DRONE_REPORT_FIELDS[report]:
            raise AuditLogError(f"drone {report} fields do not match the bounded projection")
        projection[report] = {key: item for key, item in value.items() if key != timestamp}
    camera = projection.get("camera_capabilities")
    if camera is not None:
        assert isinstance(camera, Mapping)
        modes = camera["native_panorama_modes"]
        if not isinstance(modes, list) or len(modes) > _MAX_DRONE_PROJECTION_LIST_ITEMS:
            raise AuditLogError(
                "drone camera_capabilities.native_panorama_modes must be a bounded list"
            )
    try:
        size = len(
            json.dumps(
                projection,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        )
    except (TypeError, ValueError) as error:
        raise AuditLogError(f"material drone state is not JSON-native: {error}") from None
    if size > _MAX_MATERIAL_DRONE_PROJECTION_BYTES:
        raise AuditLogError("material drone state exceeds the bounded audit projection")
    return projection


def _intent_to_dict(intent: IntentV1) -> dict[str, object]:
    return {
        "v": intent.v,
        "t": intent.t,
        "type": intent.type,
        "intent_id": intent.intent_id,
        "retry_of": intent.retry_of,
        "source": intent.source,
        "session": intent.session,
        "name": intent.name.value,
        "args": _thaw(intent.args),
        "selection": list(intent.selection),
        "mode": intent.mode.value,
        "confirm": intent.confirm,
    }


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _parse_node_frame(raw: object) -> NodeFrame:
    frame_type = raw.get("type") if isinstance(raw, Mapping) else None
    parser = _NODE_FRAME_PARSERS.get(frame_type) if isinstance(frame_type, str) else None
    if parser is None:
        raise ContractError("frame_not_allowed", "frame type is not a node-authored frame")
    return parser(raw)


def _node_frame_identity(frame: NodeFrame) -> tuple[int, int]:
    if isinstance(frame, MediaFileFrame):
        return frame.file.drone_id, frame.file.connection_epoch
    return frame.drone_id, frame.connection_epoch


def _safe_string_field(raw: object, field: str) -> str | None:
    if not isinstance(raw, Mapping):
        return None
    value = raw.get(field)
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_INTENT_IDENTIFIER_CHARS
        or value != value.strip()
        or not value.isprintable()
    ):
        return None
    return value


def _epoch_ms() -> int:
    return time.time_ns() // 1_000_000
