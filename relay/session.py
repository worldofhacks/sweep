"""Relay-session orchestration around pure contracts and fleet state."""

from __future__ import annotations

import queue
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from threading import RLock

from planner.models import CommandOperation
from relay.audit import AuditLogError, SessionAuditLog
from relay.auth import Principal, sign_event, verify_event_signature
from relay.contracts import (
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
from relay.intent_v1 import AcceptedIntent, IntentV1, RejectedIntent, validate_intent
from relay.state import FleetRegistry, MembershipTransition, RegistryError

Clock = Callable[[], int]
EventIdFactory = Callable[[], str]
IntentSink = Callable[[IntentV1, dict[str, object]], None]
LeaveAuthorizer = Callable[[int, int, dict[str, object]], bool]
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


@dataclass(frozen=True, slots=True)
class RelayLimits:
    intent_max_age_ms: int
    transport_event_max_age_ms: int
    future_clock_skew_ms: int
    telemetry_freshness_ms: int
    command_ttl_ms: int = 2_000

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


@dataclass(slots=True)
class _IntentLedgerEntry:
    status: LifecycleStatus
    selection: tuple[int, ...]
    command_statuses: dict[str, LifecycleStatus]


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
    ) -> None:
        if audit_log.session != session_id:
            raise ValueError("audit log belongs to another session")
        self.session_id = session_id
        self.audit_log = audit_log
        self.limits = limits
        self.clock = clock or _epoch_ms
        self.event_ids = event_ids or (lambda: str(uuid.uuid4()))
        self.intent_sink = intent_sink
        self.leave_authorizer = leave_authorizer
        self.registry = FleetRegistry(telemetry_freshness_ms=limits.telemetry_freshness_ms)
        self._seen_transport_event_ids: set[str] = set()
        self._last_transport_t: dict[tuple[str, int | None], int] = {}
        self._intents: dict[str, _IntentLedgerEntry] = {}
        self._command_seq: dict[tuple[int, int], int] = {}
        self._issued_command_ids: set[str] = set()
        self._command_waiters: dict[str, queue.SimpleQueue[AdapterAcknowledgement]] = {}
        self._media_files: dict[tuple[int, int, str], list[MediaFileRecord]] = {}
        self._capture_readiness: dict[int, CaptureReadinessFrame] = {}
        self._metrics = {
            "accepted_intents": 0,
            "refused_intents": 0,
            "acknowledgements": 0,
            "membership_events": 0,
            "telemetry_events": 0,
            "node_events": 0,
            "commands_issued": 0,
        }
        self._mutation_usable = True
        self._projection_usable = True
        self._replay_usable = True
        self._audit_batch: list[dict[str, object]] | None = None
        self._audit_operation_id: int | None = None
        self._lock = RLock()

    def process_frame(self, raw: object, principal: Principal) -> list[dict[str, object]]:
        """Route one post-authentication frame according to its bound principal."""
        frame_type = raw.get("type") if isinstance(raw, Mapping) else None
        if principal.source in {"console", "keyboard"} and frame_type == "intent":
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

    def protocol_refusal(self, *, reason: str, detail: str) -> dict[str, object]:
        with self._lock, self._audit_operation():
            self._ensure_mutation_usable()
            return self._protocol_refusal(reason=reason, detail=detail, now=self.clock())

    def process_intent(self, raw: object, principal: Principal) -> list[dict[str, object]]:
        now = self.clock()
        with self._lock, self._audit_operation():
            self._ensure_mutation_usable()
            if principal.source not in {"console", "keyboard"} or principal.drone_id is not None:
                return [
                    self._refuse_intent(
                        raw,
                        reason="source_not_allowed",
                        detail="this authenticated connection cannot emit intents",
                        now=now,
                    )
                ]

            result = validate_intent(raw)
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
                previous = self._intents.get(intent.retry_of)
                retryable = {
                    LifecycleStatus.REFUSED,
                    LifecycleStatus.FAILED,
                    LifecycleStatus.INVALIDATED,
                }
                if previous is None or previous.status not in retryable:
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

            self._intents[intent.intent_id] = _IntentLedgerEntry(
                status=LifecycleStatus.ACCEPTED,
                selection=intent.selection,
                command_statuses={},
            )
            self._log_intent(intent, outcome=LifecycleStatus.ACCEPTED, reason=None, now=now)
            try:
                self.intent_sink(intent, self.current_state())
            except Exception:
                self._intents[intent.intent_id].status = LifecycleStatus.REFUSED
                self._log_intent(
                    intent,
                    outcome=LifecycleStatus.REFUSED,
                    reason="downstream_error",
                    now=now,
                )
                return [
                    self._refusal(
                        intent_id=intent.intent_id,
                        reason="downstream_error",
                        detail="the downstream intent consumer did not accept the request",
                        now=now,
                    )
                ]
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
            return [event]

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
            return self._record_transition_and_state(transition)

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

            telemetry_event = telemetry.to_event()
            self._append_audit(telemetry_event)
            self._metrics["telemetry_events"] += 1
            events: list[dict[str, object]] = [telemetry_event]
            if transition is not None:
                transition_event = transition.to_event(self.session_id)
                self._append_audit(transition_event)
                self._metrics["membership_events"] += 1
                events.append(transition_event)
            state = self._state_event(now)
            self._append_audit(state)
            events.append(state)
            return events

    def process_acknowledgement(self, raw: object, principal: Principal) -> list[dict[str, object]]:
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
            return [event]

    def process_node_frame(self, raw: object, principal: Principal) -> list[dict[str, object]]:
        """Accept a node-authored frame; only capabilities and node_status change state.

        Media files and capture bundles are audited and retained for the command wire
        but not fanned out; capture readiness is fanned out unchanged.
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
                    self._retain_media(frame.file)
                elif isinstance(frame, CaptureBundleFrame):
                    for record in frame.media:
                        self._retain_media(record)
                elif isinstance(frame, CaptureReadinessFrame):
                    self._capture_readiness[drone_id] = frame
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

            event = frame.to_event()
            self._append_audit(event)
            self._metrics["node_events"] += 1
            if isinstance(frame, MediaFileFrame | CaptureBundleFrame):
                return []
            events: list[dict[str, object]] = [event]
            if isinstance(frame, CapabilitiesFrame | NodeStatusFrame):
                state = self._state_event(now)
                self._append_audit(state)
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
            if command_id in self._issued_command_ids:
                raise ValueError("command_id has already been issued in this session")
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
            if entry is None:
                self._intents[intent_id] = _IntentLedgerEntry(
                    status=LifecycleStatus.EXECUTING,
                    selection=(drone_id,),
                    command_statuses={},
                )
            self._command_seq[key] = seq
            self._issued_command_ids.add(command_id)
            self._command_waiters[command_id] = queue.SimpleQueue()
            self._append_audit(event)
            self._metrics["commands_issued"] += 1
            return {**event, "signature": sign_event(event, signing_key)}

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
            return None
        if acknowledgement.status in _TERMINAL_STATUSES:
            with self._lock:
                self._command_waiters.pop(command_id, None)
        return acknowledgement

    def discard_command_waiter(self, command_id: str) -> None:
        """Stop waking a caller for a command that could not reach the node."""
        with self._lock:
            self._command_waiters.pop(command_id, None)

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
            return tuple(self._media_files.get((drone_id, epoch, capture_id), ()))

    def _retain_media(self, record: MediaFileRecord) -> None:
        key = (record.drone_id, record.connection_epoch, record.capture_id)
        records = self._media_files.setdefault(key, [])
        for index, existing in enumerate(records):
            if existing.file_id == record.file_id:
                records[index] = record
                return
        records.append(record)

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
                self._transition_intent(self._intents[intent_id], LifecycleStatus.REFUSED)
            self._append_audit(event)
            self._metrics["refused_intents"] += 1
            return event

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
            return self._record_transition_and_state(transition)

    def periodic_events(self) -> list[dict[str, object]]:
        """Return the 10 Hz projection and log only actual staleness transitions."""
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
                self._append_audit(state)
            events.append(state)
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
    ) -> dict[str, object]:
        """Apply accepted control state; failure after the durable marker disables the session."""
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
            state = self._state_event(now)
            self._append_audit(state)
            return state

    def replay(self, *, after_sequence: int = 0) -> dict[str, object]:
        with self._lock:
            self._ensure_replay_usable()
        records, last_sequence = self.audit_log.replay_snapshot(after_sequence=after_sequence)
        with self._lock:
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
        self, transition: MembershipTransition
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
        self._append_audit(state)
        self._metrics["membership_events"] += 1
        return [event, state]

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
        if acknowledgement.intent_id not in self._intents:
            raise ContractError("unknown_intent_id", "acknowledgement references an unknown intent")

    def _record_adapter_ack_fact(self, acknowledgement: AdapterAcknowledgement) -> None:
        """Retain command facts; only the autonomy owner terminalizes an intent."""
        entry = self._intents[acknowledgement.intent_id]
        entry.command_statuses[acknowledgement.command_id] = acknowledgement.status

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
        if event_id in self._seen_transport_event_ids:
            raise ContractError("replayed_event", "event_id has already been observed")
        key = (principal.source, principal.drone_id)
        previous_t = self._last_transport_t.get(key)
        if previous_t is not None and timestamp < previous_t:
            raise ContractError("out_of_order_event", "event timestamp precedes the prior event")
        self._seen_transport_event_ids.add(event_id)
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
            self._intents[intent_id] = _IntentLedgerEntry(
                status=LifecycleStatus.REFUSED,
                selection=() if normalized is None else normalized.selection,
                command_statuses={},
            )
        return self._refusal(intent_id=intent_id, reason=reason, detail=detail, now=now)

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
    def _audit_operation(self) -> Iterator[None]:
        outermost = self._audit_batch is None
        if outermost:
            self._audit_batch = []
            self._audit_operation_id = None
        try:
            yield
            batch = self._audit_batch
            if outermost and batch:
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

    def _append_audit(self, event: Mapping[str, object]) -> dict[str, object]:
        self._ensure_mutation_usable()
        if self._audit_batch is None:
            raise RuntimeError("audit append requires an active relay operation")
        buffered = dict(event)
        self._audit_batch.append(buffered)
        if self._audit_operation_id is None:
            self._audit_operation_id = self.audit_log.begin_operation()
        return buffered

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
        return self.registry.state_event(
            session=self.session_id,
            t=now,
            event_id=self.event_ids(),
        )


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
    if not isinstance(value, str) or not value or len(value) > 512:
        return None
    return value


def _epoch_ms() -> int:
    return time.time_ns() // 1_000_000
