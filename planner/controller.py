"""End-to-end autonomy orchestration without transport or relay coupling."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from threading import RLock
from weakref import WeakSet

from adapters.dispatch import AdapterDispatcher
from arbiter.safety import SafetyArbiter
from planner.coordination import ConflictResolution, resolve_intent_pair
from planner.models import (
    CommandAcknowledgement,
    ExecutionResult,
    FleetSnapshot,
    LifecycleStatus,
    Plan,
    PreparedExecution,
    Refusal,
    RefusalReason,
    RelayAircraftSafetyEnrichment,
    RelaySnapshotEnrichment,
)
from planner.planner import DeterministicPlanner
from relay.intent_v1 import IntentName, IntentV1

type SnapshotProvider = Callable[[], FleetSnapshot]
type IntentEmitter = Callable[[IntentV1], object]


@dataclass(frozen=True, slots=True)
class ConflictExecutionResult:
    resolution: ConflictResolution
    executions: tuple[ExecutionResult, ...]
    safety_execution: ExecutionResult | None = None


@dataclass(frozen=True, slots=True)
class PositioningLossResult:
    detected: bool
    action: str | None
    execution: ExecutionResult | None


@dataclass(frozen=True, slots=True)
class ResumedExecution:
    execution: ExecutionResult
    relay_events: tuple[dict[str, object], ...]


@dataclass(frozen=True, slots=True, eq=False, weakref_slot=True)
class PreparedIntentEmitter:
    router: PreparedExecutionRouter
    _send: IntentEmitter
    current_state: Callable[[], dict[str, object]]

    def __call__(self, intent: IntentV1) -> object:
        return self._send(intent)


class PreparedExecutionRouter:
    def __init__(
        self,
        controller: AutonomyController,
        *,
        current_snapshot: SnapshotProvider | None = None,
    ) -> None:
        self.controller = controller
        self.current_snapshot = current_snapshot
        self._prepared: dict[str, PreparedExecution] = {}
        self._running: dict[str, tuple[PreparedExecution, ExecutionResult, object]] = {}
        self._submitting_sessions: dict[str, object] = {}
        self._emitters: WeakSet[PreparedIntentEmitter] = WeakSet()
        self._lock = RLock()

    def prepare(
        self,
        intent: IntentV1,
        snapshot: FleetSnapshot,
    ) -> PreparedExecution | ExecutionResult:
        provider = self.current_snapshot or (lambda: snapshot)

        def at_confirmation_time() -> FleetSnapshot:
            current = provider()
            return replace(current, now_ms=max(intent.t, current.now_ms))

        return self.controller.prepare(
            intent,
            snapshot,
            current_snapshot=at_confirmation_time,
        )

    def bind(self, prepared: PreparedExecution) -> None:
        with self._lock:
            if prepared.intent.intent_id in self._prepared:
                raise ValueError("intent already has a prepared execution")
            self._prepared[prepared.intent.intent_id] = prepared

    def discard(self, intent_id: str) -> None:
        with self._lock:
            self._prepared.pop(intent_id, None)

    def relay_emitter(self, session: object, principal: object) -> PreparedIntentEmitter:
        from relay.auth import Principal
        from relay.session import RelaySession

        if (
            not isinstance(session, RelaySession)
            or not isinstance(principal, Principal)
            or session.intent_sink is not self
        ):
            raise ValueError("relay session is configured with a different intent sink")

        def send(intent: IntentV1) -> object:
            with self._lock:
                self._submitting_sessions[intent.intent_id] = session
            try:
                return session.process_intent(_intent_payload(intent), principal)
            finally:
                with self._lock:
                    self._submitting_sessions.pop(intent.intent_id, None)

        emitter = PreparedIntentEmitter(self, send, session.current_state)
        self._emitters.add(emitter)
        return emitter

    def owns_emitter(self, emitter: object) -> bool:
        return isinstance(emitter, PreparedIntentEmitter) and emitter in self._emitters

    def _relay_snapshot(self, session: object, state: object = None) -> FleetSnapshot:
        if self.current_snapshot is None:
            raise ValueError("relay execution requires live safety enrichment")
        raw = session.current_state() if state is None else state
        if not isinstance(raw, Mapping) or raw.get("session") != session.session_id:
            raise ValueError("execution state belongs to another relay session")
        enriched = self.current_snapshot()
        enrichment = RelaySnapshotEnrichment(
            operator_present=enriched.operator_present,
            operator_last_seen_ms=enriched.operator_last_seen_ms,
            aircraft={
                drone_id: RelayAircraftSafetyEnrichment(
                    drone_id=drone_id,
                    armed=aircraft.armed,
                    physical_rc_available=aircraft.physical_rc_available,
                    storage_remaining_bytes=aircraft.storage_remaining_bytes,
                    camera_ready=aircraft.camera_ready,
                    active_task_id=aircraft.active_task_id,
                    position_loss_since_ms=aircraft.position_loss_since_ms,
                    last_known_pose=aircraft.pose,
                    last_known_home=aircraft.home,
                    last_known_flight_state=aircraft.flight_state.value,
                    last_known_battery=aircraft.battery,
                    last_known_link_quality=aircraft.link_quality,
                    last_known_position_quality=aircraft.position_quality,
                    last_link_seen_ms=aircraft.link_last_seen_ms,
                    last_position_seen_ms=aircraft.position_last_seen_ms,
                )
                for drone_id, aircraft in enriched.aircraft.items()
            },
        )
        current = FleetSnapshot.from_relay_state(raw, enrichment=enrichment)
        if any(
            enriched.aircraft[drone_id].connection_epoch != aircraft.connection_epoch
            for drone_id, aircraft in current.aircraft.items()
        ):
            raise ValueError("safety enrichment belongs to another connection epoch")
        return current

    def __call__(self, intent: IntentV1, relay_state: object) -> ExecutionResult:
        with self._lock:
            prepared = self._prepared.pop(intent.intent_id, None)
            session = self._submitting_sessions.get(intent.intent_id)
        if session is None:
            raise RuntimeError("intent has no matching prepared execution")
        current = self._relay_snapshot(session, relay_state)
        if prepared is None and intent.name is IntentName.ESTOP:
            prepared = self.controller.prepare(intent, current)
            if isinstance(prepared, ExecutionResult):
                return prepared
        if prepared is None or prepared.intent != intent:
            raise RuntimeError("intent has no matching prepared execution")
        refusal = self.controller.arbiter.check_plan(prepared.plan, current)
        if refusal is None:
            refusal = self.controller.arbiter.check_intent(intent, current)
        if refusal is not None:
            return ExecutionResult(
                intent_id=intent.intent_id,
                roster_version=current.roster_version,
                status=LifecycleStatus.REFUSED,
                plan=prepared.plan,
                refusal=refusal,
            )
        result = self.controller.dispatch_prepared(
            prepared,
            current_snapshot=lambda: self._relay_snapshot(session),
        )
        if result.status is LifecycleStatus.EXECUTING:
            with self._lock:
                self._running[intent.intent_id] = (prepared, result, session)
        return result

    def process_relay_intent(
        self, intent: IntentV1, relay_state: object, session: object
    ) -> ExecutionResult:
        if intent.name is IntentName.ESTOP:
            session.update_control_projection(estop=True)
        with self._lock:
            self._submitting_sessions[intent.intent_id] = session
        try:
            return self(intent, relay_state)
        finally:
            with self._lock:
                self._submitting_sessions.pop(intent.intent_id, None)

    def resume(self, intent_id: str, terminal_ack: CommandAcknowledgement) -> ResumedExecution:
        with self._lock:
            running = self._running.pop(intent_id, None)
        if running is None:
            raise RuntimeError("intent has no executing prepared plan")
        prepared, pending, session = running
        result = self.controller.dispatcher.resume_after_completion(
            prepared.plan,
            pending,
            terminal_ack,
            prepared.snapshot,
            current_snapshot=(
                (lambda: self._relay_snapshot(session))
                if session is not None
                else self.current_snapshot
            ),
        )
        if result.status is LifecycleStatus.EXECUTING:
            with self._lock:
                self._running[intent_id] = (prepared, result, session)
        relay_events = ()
        if session is not None:
            relay_events = tuple(session.record_execution_result(prepared.intent, result))
        return ResumedExecution(execution=result, relay_events=relay_events)

    def resume_after_acknowledgement(
        self,
        session: object,
        acknowledgement: object,
    ) -> ResumedExecution | None:
        """Resume only the session-owned execution waiting for this terminal adapter ack."""
        status_value = getattr(getattr(acknowledgement, "status", None), "value", None)
        try:
            status = LifecycleStatus(status_value)
        except (TypeError, ValueError):
            return None
        if status not in {
            LifecycleStatus.COMPLETED,
            LifecycleStatus.FAILED,
            LifecycleStatus.INVALIDATED,
        }:
            return None
        intent_id = getattr(acknowledgement, "intent_id", None)
        command_id = getattr(acknowledgement, "command_id", None)
        if not isinstance(intent_id, str) or not isinstance(command_id, str):
            return None
        with self._lock:
            running = self._running.get(intent_id)
            if running is None:
                return None
            prepared, pending, retained_session = running
            if retained_session is not session:
                return None
            matching_pending = next(
                (
                    pending_ack
                    for pending_ack in pending.acknowledgements
                    if pending_ack.command_id == command_id
                    and pending_ack.status in {LifecycleStatus.ACCEPTED, LifecycleStatus.EXECUTING}
                ),
                None,
            )
            if matching_pending is None:
                return None
            command = next(
                (command for command in prepared.plan.commands if command.command_id == command_id),
                None,
            )
            if command is None:
                return None
            try:
                terminal_ack = CommandAcknowledgement(
                    command_id=command_id,
                    intent_id=intent_id,
                    roster_version=acknowledgement.roster_version,
                    drone_id=acknowledgement.drone_id,
                    connection_epoch=acknowledgement.connection_epoch,
                    status=status,
                    reason=_refusal_reason(getattr(acknowledgement, "reason", None)),
                    detail=getattr(acknowledgement, "detail", "") or "",
                )
            except (TypeError, ValueError):
                return None
            if (
                terminal_ack.intent_id != command.intent_id
                or terminal_ack.roster_version != command.roster_version
                or terminal_ack.drone_id != command.drone_id
                or terminal_ack.connection_epoch != command.connection_epoch
            ):
                return None
        return self.resume(intent_id, terminal_ack)


def _intent_payload(intent: IntentV1) -> dict[str, object]:
    args = dict(intent.args)
    if intent.name.value == "select":
        args["ids"] = list(intent.args["ids"])
    return {
        "v": intent.v,
        "t": intent.t,
        "type": intent.type,
        "intent_id": intent.intent_id,
        "retry_of": intent.retry_of,
        "source": intent.source,
        "session": intent.session,
        "name": intent.name.value,
        "args": args,
        "selection": list(intent.selection),
        "mode": intent.mode.value,
        "confirm": intent.confirm,
    }


def _refusal_reason(value: object) -> RefusalReason | None:
    try:
        return RefusalReason(value)
    except (TypeError, ValueError):
        return None


class AutonomyController:
    def __init__(
        self,
        *,
        planner: DeterministicPlanner,
        arbiter: SafetyArbiter,
        dispatcher: AdapterDispatcher,
    ) -> None:
        self.planner = planner
        self.arbiter = arbiter
        self.dispatcher = dispatcher

    def execute(
        self,
        intent: IntentV1,
        snapshot: FleetSnapshot,
        *,
        current_snapshot: SnapshotProvider | None = None,
    ) -> ExecutionResult:
        prepared = self.prepare(intent, snapshot, current_snapshot=current_snapshot)
        if isinstance(prepared, ExecutionResult):
            return prepared
        return self.dispatch_prepared(prepared, current_snapshot=current_snapshot)

    def prepare(
        self,
        intent: IntentV1,
        snapshot: FleetSnapshot,
        *,
        current_snapshot: SnapshotProvider | None = None,
    ) -> PreparedExecution | ExecutionResult:
        provider = current_snapshot or (lambda: snapshot)
        current = provider()
        if not self.planner.supports(intent):
            refusal = Refusal(
                intent_id=intent.intent_id,
                roster_version=current.roster_version,
                drone_id=None,
                connection_epoch=None,
                reason=RefusalReason.UNSUPPORTED,
                detail=f"{intent.name.value} has no earned M2.0 planner capability",
            )
            return ExecutionResult(
                intent_id=intent.intent_id,
                roster_version=current.roster_version,
                status=LifecycleStatus.REFUSED,
                refusal=refusal,
            )
        refusal = self.arbiter.check_intent(intent, current)
        if refusal is not None:
            return ExecutionResult(
                intent_id=intent.intent_id,
                roster_version=current.roster_version,
                status=LifecycleStatus.REFUSED,
                refusal=refusal,
            )
        try:
            planned = self.planner.plan(intent, current)
        except Exception as error:
            return self._planner_failure(intent, provider, error)
        if isinstance(planned, Refusal):
            return ExecutionResult(
                intent_id=intent.intent_id,
                roster_version=current.roster_version,
                status=LifecycleStatus.REFUSED,
                refusal=planned,
            )
        return PreparedExecution(intent=intent, plan=planned, snapshot=current)

    def dispatch_prepared(
        self,
        prepared: PreparedExecution,
        *,
        current_snapshot: SnapshotProvider | None = None,
    ) -> ExecutionResult:
        return self.dispatcher.dispatch(
            prepared.plan,
            prepared.snapshot,
            current_snapshot=current_snapshot,
        )

    def execute_pair(
        self,
        first: IntentV1,
        second: IntentV1,
        snapshot: FleetSnapshot,
        *,
        current_snapshot: SnapshotProvider | None = None,
    ) -> ConflictExecutionResult:
        provider = current_snapshot or (lambda: snapshot)
        resolution = resolve_intent_pair(
            first,
            second,
            provider(),
            conflict_window_ms=self.arbiter.config.motion_conflict_window_ms,
        )
        if resolution.hold_required:
            current = provider()
            hold_plan = self.planner.emergency_hold_plan(
                intent_id=f"safety:motion-conflict:{first.intent_id}:{second.intent_id}",
                snapshot=current,
            )
            safety = self.dispatcher.dispatch(
                hold_plan,
                current,
                current_snapshot=provider,
            )
            return ConflictExecutionResult(resolution, (), safety)
        executions = tuple(
            self.execute(intent, provider(), current_snapshot=provider)
            for intent in resolution.accepted
        )
        return ConflictExecutionResult(resolution, executions)

    def handle_positioning_loss(
        self,
        snapshot: FleetSnapshot,
        *,
        current_snapshot: SnapshotProvider | None = None,
    ) -> PositioningLossResult:
        provider = current_snapshot or (lambda: snapshot)
        current = provider()
        affected = tuple(
            aircraft
            for aircraft in current.aircraft.values()
            if aircraft.airborne
            and (
                aircraft.position_quality < self.arbiter.config.min_position_quality
                or self.arbiter.timestamp_exceeds_future_skew(
                    current, aircraft.position_last_seen_ms
                )
                or self.arbiter.timestamp_exceeds_future_skew(
                    current, aircraft.position_loss_since_ms
                )
                or current.now_ms - aircraft.position_last_seen_ms
                > self.arbiter.config.max_position_age_ms
            )
        )
        if not affected:
            return PositioningLossResult(False, None, None)
        invalid_future_time = any(
            self.arbiter.timestamp_exceeds_future_skew(current, aircraft.position_last_seen_ms)
            or self.arbiter.timestamp_exceeds_future_skew(current, aircraft.position_loss_since_ms)
            for aircraft in affected
        )
        loss_since = min(
            aircraft.position_loss_since_ms
            if aircraft.position_loss_since_ms is not None
            else aircraft.position_last_seen_ms
            for aircraft in affected
        )
        land = invalid_future_time or (
            current.now_ms - loss_since >= self.arbiter.config.positioning_loss_hold_ms
        )
        action = "land" if land else "hold"
        plan = self.planner.fleet_position_loss_plan(
            intent_id=f"safety:position-loss:{loss_since}:{action}",
            snapshot=current,
            land=land,
        )
        execution = self.dispatcher.dispatch(
            plan,
            current,
            current_snapshot=provider,
        )
        return PositioningLossResult(True, action, execution)

    def _planner_failure(
        self,
        intent: IntentV1,
        provider: SnapshotProvider,
        error: Exception,
    ) -> ExecutionResult:
        current = provider()
        hold_plan = self.planner.emergency_hold_plan(
            intent_id=f"safety:planner-failure:{intent.intent_id}",
            snapshot=current,
        )
        safety = self.dispatcher.dispatch(
            hold_plan,
            current,
            current_snapshot=provider,
        )
        refusal = Refusal(
            intent_id=intent.intent_id,
            roster_version=current.roster_version,
            drone_id=None,
            connection_epoch=None,
            reason=RefusalReason.PLANNER_FAILURE,
            detail=f"planner raised {type(error).__name__}; safety hold requested",
            status=LifecycleStatus.FAILED,
        )
        return ExecutionResult(
            intent_id=intent.intent_id,
            roster_version=current.roster_version,
            status=LifecycleStatus.FAILED,
            plan=hold_plan if isinstance(hold_plan, Plan) else None,
            acknowledgements=safety.acknowledgements,
            refusal=refusal,
            degraded_aircraft=safety.degraded_aircraft,
        )
