"""Relay-session orchestration around pure contracts and fleet state."""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from threading import RLock

from relay.audit import SessionAuditLog
from relay.auth import Principal, verify_event_signature
from relay.contracts import (
    AdapterAcknowledgement,
    ContractError,
    LifecycleStatus,
    MembershipAction,
    MembershipRequest,
    acknowledgement_event,
    parse_adapter_acknowledgement,
    parse_membership_request,
    parse_telemetry,
    refusal_event,
)
from relay.intent_v1 import AcceptedIntent, IntentV1, RejectedIntent, validate_intent
from relay.state import FleetRegistry, MembershipTransition, RegistryError

Clock = Callable[[], int]
EventIdFactory = Callable[[], str]
IntentSink = Callable[[IntentV1, dict[str, object]], None]
LeaveAuthorizer = Callable[[int, int, dict[str, object]], bool]
_UNSET = object()


@dataclass(frozen=True, slots=True)
class RelayLimits:
    intent_max_age_ms: int
    transport_event_max_age_ms: int
    future_clock_skew_ms: int
    telemetry_freshness_ms: int

    def __post_init__(self) -> None:
        if (
            min(
                self.intent_max_age_ms,
                self.transport_event_max_age_ms,
                self.telemetry_freshness_ms,
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
        self._metrics = {
            "accepted_intents": 0,
            "refused_intents": 0,
            "acknowledgements": 0,
            "membership_events": 0,
            "telemetry_events": 0,
        }
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
        return [
            self.protocol_refusal(
                reason="frame_not_allowed",
                detail="frame type is not allowed for the authenticated source",
            )
        ]

    def protocol_refusal(self, *, reason: str, detail: str) -> dict[str, object]:
        with self._lock:
            return self._protocol_refusal(reason=reason, detail=detail, now=self.clock())

    def process_intent(self, raw: object, principal: Principal) -> list[dict[str, object]]:
        now = self.clock()
        with self._lock:
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
            self.audit_log.append(event)
            self._metrics["accepted_intents"] += 1
            self._metrics["acknowledgements"] += 1
            return [event]

    def process_membership(self, raw: object, principal: Principal) -> list[dict[str, object]]:
        now = self.clock()
        with self._lock:
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
        with self._lock:
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
            self.audit_log.append(telemetry_event)
            self._metrics["telemetry_events"] += 1
            events: list[dict[str, object]] = [telemetry_event]
            if transition is not None:
                transition_event = transition.to_event(self.session_id)
                self.audit_log.append(transition_event)
                self._metrics["membership_events"] += 1
                events.append(transition_event)
            state = self._state_event(now)
            self.audit_log.append(state)
            events.append(state)
            return events

    def process_acknowledgement(self, raw: object, principal: Principal) -> list[dict[str, object]]:
        now = self.clock()
        with self._lock:
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
            self.audit_log.append(event)
            self._metrics["acknowledgements"] += 1
            return [event]

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
        with self._lock:
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
            self.audit_log.append(event)
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
        with self._lock:
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
            self.audit_log.append(event)
            self._metrics["refused_intents"] += 1
            return event

    def handle_adapter_disconnect(
        self, *, drone_id: int, connection_epoch: int | None
    ) -> list[dict[str, object]]:
        """Turn an authenticated socket loss into a relay-attested membership event."""
        now = self.clock()
        with self._lock:
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
        with self._lock:
            possible_ids = [self.event_ids() for _ in range(4)]
            transitions = self.registry.expire_stale_telemetry(now_ms=now, event_ids=possible_ids)
            events: list[dict[str, object]] = []
            for transition in transitions:
                event = transition.to_event(self.session_id)
                self.audit_log.append(event)
                self._metrics["membership_events"] += 1
                events.append(event)
            state = self._state_event(now)
            if transitions:
                self.audit_log.append(state)
            events.append(state)
            return events

    def current_state(self) -> dict[str, object]:
        with self._lock:
            return self._state_event(self.clock())

    def update_control_projection(
        self,
        *,
        selection: tuple[int, ...] | None = None,
        accepted_plan: dict[str, object] | None | object = _UNSET,
        pending: dict[str, object] | None | object = _UNSET,
        armed: bool | None = None,
        estop: bool | None = None,
    ) -> dict[str, object]:
        """Apply state already accepted by the planner/arbiter and log its projection."""
        now = self.clock()
        with self._lock:
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
            self.audit_log.append(state)
            return state

    def replay(self, *, after_sequence: int = 0) -> dict[str, object]:
        records, last_sequence = self.audit_log.replay_snapshot(after_sequence=after_sequence)
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
        self.audit_log.append(event)
        self.audit_log.append(state)
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
        terminal = {
            LifecycleStatus.REFUSED,
            LifecycleStatus.COMPLETED,
            LifecycleStatus.FAILED,
            LifecycleStatus.INVALIDATED,
        }
        if entry.status in terminal and entry.status is not status:
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
        self.audit_log.append(event)
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
        self.audit_log.append(event)
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
        self.audit_log.append(event)

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
        self.audit_log.append(event)

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


def _safe_string_field(raw: object, field: str) -> str | None:
    if not isinstance(raw, Mapping):
        return None
    value = raw.get(field)
    if not isinstance(value, str) or not value or len(value) > 512:
        return None
    return value


def _epoch_ms() -> int:
    return time.time_ns() // 1_000_000
