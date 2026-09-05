"""End-to-end autonomy orchestration without transport or relay coupling."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import nullcontext
from dataclasses import dataclass, replace
from threading import RLock
from weakref import WeakSet

from adapters.dispatch import AdapterDispatcher
from arbiter.safety import SafetyArbiter
from planner.coordination import MOTION_INTENTS, ConflictResolution, resolve_intent_pair
from planner.models import (
    CommandAcknowledgement,
    ExecutionResult,
    FleetSnapshot,
    LifecycleStatus,
    MembershipState,
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
class RelayExecution:
    execution: ExecutionResult
    relay_events: tuple[dict[str, object], ...]
    continuation: ResumeToken | RecoveryToken | None = None


@dataclass(frozen=True, slots=True, eq=False)
class ResumeToken:
    intent_id: str
    running: tuple[PreparedExecution, ExecutionResult, object]
    acknowledgement: CommandAcknowledgement
    stop_generation: int
    completed_at_ms: int | None = None
    landing: tuple[int, frozenset[int]] | None = None


@dataclass(frozen=True, slots=True, eq=False)
class RecoveryToken:
    intent_id: str
    original: ResumeToken
    prepared: PreparedExecution
    original_result: ExecutionResult
    commit_original_after: bool


@dataclass(frozen=True, slots=True)
class ResumeOutcome:
    execution: ExecutionResult
    recovery_id: str | None = None
    recovery_before_result: bool = False


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
        self._recoveries: dict[str, RecoveryToken] = {}
        self._stop_generations: dict[object, int] = {}
        self._running: dict[str, tuple[PreparedExecution, ExecutionResult, object]] = {}
        self._submitting_sessions: dict[str, object] = {}
        self._pending_landings: dict[str, tuple[int, frozenset[int]]] = {}
        self._landing_ack_times: dict[str, int] = {}
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

    def cancel_intent(self, intent_id: str) -> None:
        """Release an undispatched preparation without disturbing execution ownership."""
        with self._lock:
            if intent_id in self._running or self._prepared.pop(intent_id, None) is None:
                return
            self._submitting_sessions.pop(intent_id, None)

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
                events = session.process_intent(_intent_payload(intent), principal)
                if any(
                    event.get("type") == "acknowledgement"
                    and event.get("intent_id") == intent.intent_id
                    and event.get("status") == "accepted"
                    for event in events
                ):
                    session.mark_pending_intent_delivered(intent.intent_id)
                    events.extend(session.execute_pending_intent(intent.intent_id))
                return events
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
        return replace(
            current,
            aircraft={
                drone_id: replace(
                    aircraft,
                    heading_deg=(
                        aircraft.heading_deg
                        if aircraft.heading_deg is not None
                        else enriched.aircraft[drone_id].heading_deg
                    ),
                )
                for drone_id, aircraft in current.aircraft.items()
            },
        )

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
        if refusal is None:
            current_plan = self.controller.planner.plan(intent, current)
            if isinstance(current_plan, Refusal):
                refusal = current_plan
            elif current_plan != prepared.plan:
                refusal = Refusal(
                    intent_id=intent.intent_id,
                    roster_version=current.roster_version,
                    drone_id=None,
                    connection_epoch=None,
                    reason=RefusalReason.INVALID_PLAN,
                    detail="authoritative state changed the confirmed plan; preview again",
                )
        if refusal is not None:
            return ExecutionResult(
                intent_id=intent.intent_id,
                roster_version=current.roster_version,
                status=LifecycleStatus.REFUSED,
                plan=prepared.plan,
                refusal=refusal,
            )
        with self.controller.dispatcher.observe_commands(session.register_dispatched_command):
            try:
                result = self.controller.dispatch_prepared(
                    prepared,
                    current_snapshot=lambda: self._relay_snapshot(session),
                )
            except Exception:
                if intent.name is not IntentName.HOLD:
                    raise
                # Complete the stop with the last validated state; never retry motion this way.
                result = self.controller.dispatcher.dispatch(prepared.plan, current)
        if result.status in {
            LifecycleStatus.EXECUTING,
            LifecycleStatus.COMPLETED,
        } and intent.name in {
            IntentName.LAND,
            IntentName.LAND_ALL,
        }:
            self._pending_landings[intent.intent_id] = (
                session.clock(),
                frozenset(command.drone_id for command in prepared.plan.commands),
            )
        if result.status is LifecycleStatus.EXECUTING or intent.intent_id in self._pending_landings:
            with self._lock:
                self._running[intent.intent_id] = (prepared, result, session)
        return result

    def process_relay_intent(
        self, intent: IntentV1, relay_state: object, session: object
    ) -> RelayExecution:
        if intent.name is IntentName.ESTOP:
            session.update_control_projection(estop=True)
        reconciled = self.reconcile_landing(session)
        blocked = self._gate_active_execution(intent, relay_state, session)
        if blocked is not None:
            return replace(blocked, relay_events=reconciled + blocked.relay_events)
        with self._lock:
            self._submitting_sessions[intent.intent_id] = session
        try:
            prepared = self._prepared.get(intent.intent_id)
            result = self(intent, relay_state)
            events = reconciled + self._retire_held_motion(intent, result, session)
            if prepared is not None:
                events += self._retain_ambiguous_stop(intent, result, session, prepared.snapshot)
            return RelayExecution(result, events)
        finally:
            with self._lock:
                self._submitting_sessions.pop(intent.intent_id, None)

    def _gate_active_execution(
        self, intent: IntentV1, relay_state: object, session: object
    ) -> RelayExecution | None:
        if intent.name is IntentName.ESTOP:
            return None
        with self._lock:
            candidate = self._prepared.get(intent.intent_id)
            active = [
                (prepared, pending)
                for prepared, pending, owner in self._running.values()
                if owner is session
            ]
            if candidate is None or not active:
                return None
            if intent.name is IntentName.HOLD and not any(
                prepared.intent.name is IntentName.ESTOP for prepared, _ in active
            ):
                return None
            if intent.name is IntentName.SELECT and all(
                pending.status is not LifecycleStatus.EXECUTING
                and prepared.intent.name is not IntentName.ESTOP
                for prepared, pending in active
            ):
                return None
            if not candidate.plan.commands and intent.name is not IntentName.SELECT:
                return None
            conflicting = [
                (prepared, pending)
                for prepared, pending in active
                if intent.name in MOTION_INTENTS
                and prepared.intent.name in MOTION_INTENTS
                and pending.status is LifecycleStatus.EXECUTING
                and abs(intent.t - prepared.intent.t)
                <= self.controller.arbiter.config.motion_conflict_window_ms
            ]
            self._prepared.pop(intent.intent_id)
        events = []
        acknowledgements = ()
        detail = "another execution is still active; wait for its terminal result"
        if conflicting:
            detail = "conflicting motion invalidated the active execution; safety HOLD requested"
            for prepared, pending in conflicting:
                invalidated = replace(
                    pending,
                    status=LifecycleStatus.INVALIDATED,
                    refusal=Refusal(
                        intent_id=prepared.intent.intent_id,
                        roster_version=pending.roster_version,
                        drone_id=None,
                        connection_epoch=None,
                        reason=RefusalReason.CONFLICTING_MOTION,
                        detail=detail,
                        status=LifecycleStatus.INVALIDATED,
                    ),
                )
                events.extend(self._record_retirement(prepared, invalidated, session))
                with self._lock:
                    self._running[prepared.intent.intent_id] = (prepared, invalidated, session)
                self._pending_landings.pop(prepared.intent.intent_id, None)
                self._landing_ack_times.pop(prepared.intent.intent_id, None)
            try:
                current = self._safety_snapshot(session, candidate.snapshot)
                events.extend(
                    self._dispatch_safety_hold(
                        intent,
                        current,
                        session,
                        intent_id=f"safety:motion-conflict:{intent.intent_id}",
                    )
                )
            except Exception:
                detail += "; safety HOLD could not complete"
        result = ExecutionResult(
            intent_id=intent.intent_id,
            roster_version=candidate.plan.roster_version,
            status=LifecycleStatus.REFUSED,
            plan=candidate.plan,
            acknowledgements=acknowledgements,
            refusal=Refusal(
                intent_id=intent.intent_id,
                roster_version=candidate.plan.roster_version,
                drone_id=None,
                connection_epoch=None,
                reason=(
                    RefusalReason.CONFLICTING_MOTION if conflicting else RefusalReason.ACTIVE_TASK
                ),
                detail=detail,
            ),
        )
        return RelayExecution(result, tuple(events))

    def _dispatch_safety_hold(
        self,
        intent: IntentV1,
        current: FleetSnapshot,
        session: object,
        *,
        intent_id: str,
        estop: bool = False,
    ) -> tuple[dict[str, object], ...]:
        safety_intent = replace(
            intent,
            intent_id=intent_id,
            name=IntentName.ESTOP if estop else IntentName.HOLD,
            args={},
            retry_of=None,
            confirm=True,
        )
        if estop:
            hold = self.controller.planner.plan(safety_intent, current)
            if isinstance(hold, Refusal):
                raise ValueError(hold.detail)
        else:
            hold = self.controller.planner.emergency_hold_plan(
                intent_id=intent_id, snapshot=current
            )
        safety_intent = replace(safety_intent, selection=hold.selection)
        events = [session.admit_safety_stop(safety_intent)]
        with self.controller.dispatcher.observe_commands(session.register_dispatched_command):
            try:
                safety = self.controller.dispatcher.dispatch(
                    hold, current, current_snapshot=lambda: self._relay_snapshot(session)
                )
            except Exception:
                # Repeating a safety stop is safe when enrichment fails after possible I/O.
                safety = self.controller.dispatcher.dispatch(hold, current)
        if safety.status in {
            LifecycleStatus.EXECUTING,
            LifecycleStatus.FAILED,
            LifecycleStatus.INVALIDATED,
        }:
            with self._lock:
                self._running[hold.intent_id] = (
                    PreparedExecution(safety_intent, hold, current),
                    safety,
                    session,
                )
        events.extend(session.record_execution_result(safety_intent, safety))
        if not estop and safety.status in {LifecycleStatus.EXECUTING, LifecycleStatus.COMPLETED}:
            # The registered recovery owns every target even while its suffix is pending.
            events.extend(
                self._retire_held_motion(
                    safety_intent
                    if intent.name is not IntentName.HOLD
                    else replace(intent, name=IntentName.HOLD),
                    replace(safety, status=LifecycleStatus.EXECUTING),
                    session,
                )
            )
        return tuple(events)

    def _safety_snapshot(self, session: object, fallback: FleetSnapshot) -> FleetSnapshot:
        try:
            return self._relay_snapshot(session)
        except Exception:
            return fallback

    @staticmethod
    def _needs_ambiguous_stop(intent: IntentV1, result: ExecutionResult) -> bool:
        if intent.name is IntentName.ESTOP:
            return False
        if result.status not in {LifecycleStatus.FAILED, LifecycleStatus.INVALIDATED}:
            return False
        command_ids = (
            {command.command_id for command in result.plan.commands} if result.plan else set()
        )
        if intent.name not in {IntentName.HOLD, IntentName.LAND, IntentName.LAND_ALL} and not any(
            ack.command_id not in command_ids
            and ack.status
            in {LifecycleStatus.ACCEPTED, LifecycleStatus.EXECUTING, LifecycleStatus.FAILED}
            for ack in result.acknowledgements
        ):
            return False
        return True

    def _retain_ambiguous_stop(
        self, intent: IntentV1, result: ExecutionResult, session: object, fallback: FleetSnapshot
    ) -> tuple[dict[str, object], ...]:
        if not self._needs_ambiguous_stop(intent, result):
            return ()
        return self._dispatch_safety_hold(
            intent,
            self._safety_snapshot(session, fallback),
            session,
            intent_id=f"safety:ambiguous:{intent.intent_id}",
        )

    def _record_retirement(
        self,
        prepared: PreparedExecution,
        invalidated: ExecutionResult,
        session: object,
        *,
        retain_landing: bool = False,
    ) -> tuple[dict[str, object], ...]:
        if not retain_landing:
            self._pending_landings.pop(prepared.intent.intent_id, None)
        running = self._running.get(prepared.intent.intent_id)
        if running is not None and running[1].status is not LifecycleStatus.EXECUTING:
            active = session.current_state()["accepted_plan"]
            if active is not None and active["intent_id"] == prepared.intent.intent_id:
                return (session.update_control_projection(accepted_plan=None),)
            return ()
        return tuple(session.record_execution_result(prepared.intent, invalidated))

    def _retire_held_motion(
        self, intent: IntentV1, result: ExecutionResult, session: object
    ) -> tuple[dict[str, object], ...]:
        if intent.name not in {IntentName.HOLD, IntentName.ESTOP}:
            return ()
        if intent.name is IntentName.ESTOP:
            with self._lock:
                self._stop_generations[session] = self._stop_generations.get(session, 0) + 1
        held_aircraft = {
            ack.drone_id
            for ack in result.acknowledgements
            if ack.status
            in {LifecycleStatus.ACCEPTED, LifecycleStatus.EXECUTING, LifecycleStatus.COMPLETED}
        }
        if result.status is LifecycleStatus.EXECUTING and result.plan is not None:
            held_aircraft.update(command.drone_id for command in result.plan.commands)
        if not held_aircraft and intent.name is not IntentName.ESTOP:
            return ()
        drones = {drone["drone_id"]: drone for drone in session.current_state()["drones"]}
        retired = []
        retained = set()
        with self._lock:
            for intent_id, (prepared, pending, owner) in tuple(self._running.items()):
                if (
                    owner is session
                    and intent_id not in {intent.intent_id, result.intent_id}
                    and intent.name is IntentName.HOLD
                    and intent_id in self._pending_landings
                ):
                    completed_at, targets = self._pending_landings[intent_id]
                    remaining = targets - held_aircraft
                    completed_targets = {
                        ack.drone_id
                        for ack in pending.acknowledgements
                        if ack.status is LifecycleStatus.COMPLETED
                    }
                    cancels_suffix = pending.status is LifecycleStatus.EXECUTING and bool(
                        (targets - completed_targets) & held_aircraft
                    )
                    if cancels_suffix:
                        remaining &= frozenset(
                            ack.drone_id
                            for ack in pending.acknowledgements
                            if ack.status
                            in {
                                LifecycleStatus.COMPLETED,
                                LifecycleStatus.ACCEPTED,
                                LifecycleStatus.EXECUTING,
                            }
                        )
                    if remaining:
                        self._pending_landings[intent_id] = (completed_at, remaining)
                        if not cancels_suffix:
                            continue
                        retained.add(intent_id)
                if (
                    owner is session
                    and intent_id not in {intent.intent_id, result.intent_id}
                    and (
                        intent.name is IntentName.ESTOP
                        or (
                            prepared.intent.intent_id in self._pending_landings
                            and held_aircraft.intersection(
                                command.drone_id for command in prepared.plan.commands
                            )
                        )
                        or (
                            prepared.intent.name is not IntentName.ESTOP
                            and pending.status
                            in {LifecycleStatus.FAILED, LifecycleStatus.INVALIDATED}
                            and all(
                                command.drone_id in held_aircraft
                                or (
                                    (drone := drones.get(command.drone_id)) is not None
                                    and drone["connection_epoch"] == command.connection_epoch
                                    and (telemetry := drone.get("telemetry")) is not None
                                    and telemetry["t"] > prepared.snapshot.now_ms
                                    and telemetry["state"] in {"landed", "disarmed"}
                                )
                                for command in prepared.plan.commands
                            )
                        )
                        or (
                            pending.status is LifecycleStatus.EXECUTING
                            and held_aircraft.intersection(
                                ack.drone_id
                                for ack in pending.acknowledgements
                                if ack.status
                                in {LifecycleStatus.ACCEPTED, LifecycleStatus.EXECUTING}
                            )
                        )
                    )
                    and not (
                        intent.name is IntentName.HOLD and prepared.intent.name is IntentName.ESTOP
                    )
                ):
                    retired.append((prepared, pending))
        events = []
        for prepared, pending in retired:
            intent_id = prepared.intent.intent_id
            with session._lock, self._lock:
                current = self._running.get(intent_id)
                if current is None:
                    continue
                prepared, pending, _owner = current
                invalidated = replace(
                    pending,
                    status=LifecycleStatus.INVALIDATED,
                    refusal=Refusal(
                        intent_id=prepared.intent.intent_id,
                        roster_version=pending.roster_version,
                        drone_id=None,
                        connection_epoch=None,
                        reason=RefusalReason.CONFLICTING_MOTION,
                        detail=f"execution superseded by {intent.name.value} {intent.intent_id}",
                        status=LifecycleStatus.INVALIDATED,
                    ),
                )
                if intent_id not in retained:
                    self._pending_landings.pop(intent_id, None)
                events.extend(
                    self._record_retirement(
                        prepared, invalidated, session, retain_landing=intent_id in retained
                    )
                )
                if intent_id in retained:
                    self._running[intent_id] = (prepared, invalidated, session)
                else:
                    self._running.pop(intent_id, None)
                self._landing_ack_times.pop(intent_id, None)
        return tuple(events)

    def _owns_resume(self, token: ResumeToken | RecoveryToken) -> bool:
        with self._lock:
            if isinstance(token, RecoveryToken):
                return self._recoveries.get(token.intent_id) is token and self._owns_resume(
                    token.original
                )
            return (
                self._running.get(token.intent_id) is token.running
                and self._stop_generations.get(token.running[2], 0) == token.stop_generation
            )

    def resume(
        self,
        intent_id: str,
        terminal_ack: CommandAcknowledgement,
        *,
        completed_at_ms: int | None = None,
    ) -> RelayExecution | None:
        """Run all phases synchronously for callers outside the relay runtime."""
        with self._lock:
            running = self._running.get(intent_id)
            if running is None or running[1].status is not LifecycleStatus.EXECUTING:
                return None
            token = ResumeToken(
                intent_id,
                running,
                terminal_ack,
                self._stop_generations.get(running[2], 0),
                completed_at_ms,
            )
        return self._drive_resume(token)

    def _drive_resume(self, token: ResumeToken | RecoveryToken) -> RelayExecution | None:
        events = ()
        while True:
            outcome = self.resume_io(token)
            committed = self.commit_resume(token, outcome)
            if committed is None:
                return None
            events += committed.relay_events
            if committed.continuation is None:
                return replace(committed, relay_events=events)
            token = committed.continuation

    def resume_io(
        self,
        token: ResumeToken | RecoveryToken,
        current_snapshot: SnapshotProvider | None = None,
    ) -> ResumeOutcome:
        """Perform adapter work without changing router ownership or relay projections."""
        if isinstance(token, RecoveryToken):
            prepared = token.prepared
            session = token.original.running[2]
            with self.controller.dispatcher.observe_commands(session.register_dispatched_command):
                try:
                    result = self.controller.dispatcher.dispatch(
                        prepared.plan,
                        prepared.snapshot,
                        current_snapshot=current_snapshot
                        or (lambda: self._relay_snapshot(session)),
                        owner_still_valid=lambda: self._owns_resume(token),
                    )
                except Exception:
                    result = self.controller.dispatcher.dispatch(
                        prepared.plan,
                        prepared.snapshot,
                        owner_still_valid=lambda: self._owns_resume(token),
                    )
            return ResumeOutcome(result)
        prepared, pending, session = token.running
        intent_id = token.intent_id
        terminal_ack = token.acknowledgement
        if pending.status is not LifecycleStatus.EXECUTING:
            recovery = (
                f"safety:ambiguous:{intent_id}"
                if terminal_ack.status is not LifecycleStatus.COMPLETED
                and self._needs_ambiguous_stop(prepared.intent, pending)
                else None
            )
            return ResumeOutcome(pending, recovery)

        class ResumeSnapshotUnavailable(Exception):
            pass

        def live_snapshot() -> FleetSnapshot:
            try:
                if session is not None:
                    return self._relay_snapshot(session)
                return self.current_snapshot() if self.current_snapshot else prepared.snapshot
            except Exception as error:
                if (
                    session is not None
                    and prepared.plan.intent_name is not IntentName.ESTOP
                    and terminal_ack.status is LifecycleStatus.COMPLETED
                    and prepared.plan.commands
                    and terminal_ack.command_id == prepared.plan.commands[-1].command_id
                    and len(pending.acknowledgements) == len(prepared.plan.commands)
                ):
                    state = session.current_state()
                    members = {member["drone_id"]: member for member in state["drones"]}
                    if state["roster_version"] != prepared.plan.roster_version and all(
                        (member := members.get(command.drone_id)) is not None
                        and member["connection_epoch"] == command.connection_epoch
                        and (
                            member["membership"] == "ready"
                            or (
                                prepared.plan.intent_name
                                in {IntentName.HOLD, IntentName.LAND, IntentName.LAND_ALL}
                                and member["membership"] == "degraded"
                                and prepared.snapshot.aircraft[command.drone_id].membership
                                is MembershipState.DEGRADED
                            )
                        )
                        for command in prepared.plan.commands
                    ):
                        return replace(
                            prepared.snapshot,
                            roster_version=state["roster_version"],
                            aircraft={
                                drone_id: replace(
                                    aircraft,
                                    connection_epoch=members[drone_id]["connection_epoch"],
                                    membership=MembershipState(members[drone_id]["membership"]),
                                )
                                for drone_id, aircraft in prepared.snapshot.aircraft.items()
                                if drone_id in members
                            },
                            now_ms=state["t"],
                        )
                raise ResumeSnapshotUnavailable from error

        try:
            with self.controller.dispatcher.observe_commands(session.register_dispatched_command):
                result = self.controller.dispatcher.resume_after_completion(
                    prepared.plan,
                    pending,
                    terminal_ack,
                    prepared.snapshot,
                    current_snapshot=current_snapshot or live_snapshot,
                    owner_still_valid=lambda: self._owns_resume(token),
                )
        except Exception as error:
            status = (
                terminal_ack.status
                if terminal_ack.status in {LifecycleStatus.FAILED, LifecycleStatus.INVALIDATED}
                else LifecycleStatus.INVALIDATED
            )
            result = ExecutionResult(
                intent_id=intent_id,
                roster_version=pending.roster_version,
                status=status,
                plan=prepared.plan,
                acknowledgements=tuple(
                    terminal_ack if ack.command_id == terminal_ack.command_id else ack
                    for ack in pending.acknowledgements
                ),
                refusal=Refusal(
                    intent_id=intent_id,
                    roster_version=pending.roster_version,
                    drone_id=terminal_ack.drone_id,
                    connection_epoch=terminal_ack.connection_epoch,
                    reason=terminal_ack.reason or RefusalReason.INVALID_PLAN,
                    detail=(
                        "live safety state unavailable during resume; remaining dispatch cancelled"
                        if isinstance(error, ResumeSnapshotUnavailable)
                        else "resume raised after possible adapter I/O; dispatch cancelled"
                    ),
                    status=status,
                ),
            )
            return ResumeOutcome(result, f"safety:resume:{intent_id}", True)
        return ResumeOutcome(
            result,
            f"safety:ambiguous:{intent_id}"
            if self._needs_ambiguous_stop(prepared.intent, result)
            else None,
        )

    def _commit_resumed_result(
        self, token: ResumeToken, result: ExecutionResult, *, retain: bool = False
    ) -> tuple[ResumeToken, tuple[dict[str, object], ...]]:
        prepared, _pending, session = token.running
        intent_id = token.intent_id
        if (
            prepared.intent.name in {IntentName.LAND, IntentName.LAND_ALL}
            and token.acknowledgement.status is LifecycleStatus.COMPLETED
        ):
            acknowledged_at = token.completed_at_ms or (
                session.clock() if session is not None else prepared.snapshot.now_ms
            )
            self._landing_ack_times[intent_id] = max(
                self._landing_ack_times.get(intent_id, acknowledged_at), acknowledged_at
            )
            landing = self._pending_landings.get(intent_id)
            if landing is not None:
                self._pending_landings[intent_id] = (max(landing[0], acknowledged_at), landing[1])
        if (
            session is not None
            and result.status is LifecycleStatus.COMPLETED
            and prepared.intent.name in {IntentName.LAND, IntentName.LAND_ALL}
        ):
            landing = self._pending_landings.get(intent_id)
            if landing is not None:
                acknowledged_at = token.completed_at_ms or session.clock()
                self._pending_landings[intent_id] = (
                    max(
                        session.clock(),
                        self._landing_ack_times.get(intent_id, acknowledged_at),
                        acknowledged_at,
                    ),
                    landing[1],
                )
        elif result.status is not LifecycleStatus.EXECUTING:
            self._pending_landings.pop(intent_id, None)
        running = (prepared, result, session)
        self._running[intent_id] = running
        events = (
            tuple(session.record_execution_result(prepared.intent, result))
            if session is not None
            else ()
        )
        if result.status is not LifecycleStatus.EXECUTING and not retain:
            if intent_id not in self._pending_landings:
                self._running.pop(intent_id, None)
            self._landing_ack_times.pop(intent_id, None)
        return replace(token, running=running), events

    def commit_resume(
        self, token: ResumeToken | RecoveryToken, outcome: ResumeOutcome
    ) -> RelayExecution | None:
        """Commit an owned result; a recovery continuation must run outside the operation lock."""
        original = token.original if isinstance(token, RecoveryToken) else token
        prepared, pending, session = original.running
        with session._lock if session is not None else nullcontext(), self._lock:
            if isinstance(token, RecoveryToken):
                return self._commit_recovery(token, outcome)
            if not self._owns_resume(token):
                return None
            if pending.status is not LifecycleStatus.EXECUTING:
                if token.acknowledgement.status is LifecycleStatus.COMPLETED:
                    landing = self._pending_landings.get(token.intent_id)
                    if landing is not None:
                        self._pending_landings[token.intent_id] = (
                            max(landing[0], token.completed_at_ms or landing[0]),
                            landing[1],
                        )
                    return RelayExecution(pending, ())
            if outcome.recovery_id is None:
                _updated, events = self._commit_resumed_result(token, outcome.execution)
                if session is not None:
                    events += self._restore_active_projection(session)
                return RelayExecution(outcome.execution, events)
            events = ()
            if not outcome.recovery_before_result and pending.status is LifecycleStatus.EXECUTING:
                token, events = self._commit_resumed_result(token, outcome.execution, retain=True)
            current = self._safety_snapshot(session, prepared.snapshot)
            safety_intent = replace(
                prepared.intent,
                intent_id=outcome.recovery_id,
                name=(
                    IntentName.ESTOP
                    if prepared.intent.name is IntentName.ESTOP
                    else IntentName.HOLD
                ),
                args={},
                retry_of=None,
                confirm=True,
            )
            if safety_intent.name is IntentName.ESTOP:
                plan = self.controller.planner.plan(safety_intent, current)
                if isinstance(plan, Refusal):
                    raise ValueError(plan.detail)
            else:
                plan = self.controller.planner.emergency_hold_plan(
                    intent_id=safety_intent.intent_id, snapshot=current
                )
            safety_intent = replace(safety_intent, selection=plan.selection)
            recovery = RecoveryToken(
                safety_intent.intent_id,
                token,
                PreparedExecution(safety_intent, plan, current),
                outcome.execution,
                outcome.recovery_before_result,
            )
            events += (session.admit_safety_stop(safety_intent),)
            self._recoveries[recovery.intent_id] = recovery
            return RelayExecution(outcome.execution, events, recovery)

    def _commit_recovery(
        self, token: RecoveryToken, outcome: ResumeOutcome
    ) -> RelayExecution | None:
        if self._recoveries.get(token.intent_id) is not token:
            return None
        owns_original = self._owns_resume(token)
        self._recoveries.pop(token.intent_id)
        original_prepared, _pending, session = token.original.running
        safety = outcome.execution
        if not owns_original:
            safety = replace(safety, status=LifecycleStatus.INVALIDATED)
            events = tuple(session.record_execution_result(token.prepared.intent, safety))
            return RelayExecution(token.original_result, events)
        if safety.status in {
            LifecycleStatus.EXECUTING,
            LifecycleStatus.FAILED,
            LifecycleStatus.INVALIDATED,
        }:
            self._running[token.intent_id] = (token.prepared, safety, session)
        events = tuple(session.record_execution_result(token.prepared.intent, safety))
        if token.prepared.intent.name is not IntentName.ESTOP and safety.status in {
            LifecycleStatus.EXECUTING,
            LifecycleStatus.COMPLETED,
        }:
            retirement_intent = (
                original_prepared.intent
                if original_prepared.intent.name is IntentName.HOLD
                else token.prepared.intent
            )
            events += self._retire_held_motion(
                retirement_intent, replace(safety, status=LifecycleStatus.EXECUTING), session
            )
        if self._running.get(token.original.intent_id) is token.original.running:
            if token.commit_original_after:
                _updated, result_events = self._commit_resumed_result(
                    token.original, token.original_result
                )
                events += result_events
            elif token.original.landing is not None:
                updated = replace(
                    token.original_result,
                    acknowledgements=tuple(
                        token.original.acknowledgement
                        if ack.command_id == token.original.acknowledgement.command_id
                        else ack
                        for ack in token.original_result.acknowledgements
                    ),
                )
                self._running[token.original.intent_id] = (original_prepared, updated, session)
                landing = self._pending_landings.get(token.original.intent_id)
                if landing is not None:
                    self._pending_landings[token.original.intent_id] = (
                        max(landing[0], token.original.completed_at_ms or landing[0]),
                        landing[1],
                    )
            elif token.original_result.status is not LifecycleStatus.EXECUTING:
                if token.original.intent_id not in self._pending_landings:
                    self._running.pop(token.original.intent_id, None)
                self._landing_ack_times.pop(token.original.intent_id, None)
        events += self._restore_active_projection(session)
        return RelayExecution(token.original_result, events)

    def _restore_active_projection(self, session: object) -> tuple[dict[str, object], ...]:
        active = session.current_state()["accepted_plan"]
        owned = {
            intent_id: prepared.plan
            for intent_id, (prepared, pending, owner) in self._running.items()
            if owner is session
            and (pending.status is LifecycleStatus.EXECUTING or intent_id in self._pending_landings)
        }
        if active is not None and active["intent_id"] in owned:
            return ()
        replacement = next(iter(owned.values()), None)
        if active is None and replacement is None:
            return ()
        return (
            session.update_control_projection(
                accepted_plan=None if replacement is None else replacement.to_dict()
            ),
        )

    def completion_pending(self, intent_id: str) -> bool:
        return intent_id in self._pending_landings

    def reconcile_landing(self, session: object) -> tuple[dict[str, object], ...]:
        state = session.current_state()
        drones = {drone["drone_id"]: drone for drone in state["drones"]}
        events = []
        for intent_id, (completed_at, targets) in tuple(self._pending_landings.items()):
            running = self._running.get(intent_id)
            if running is None:
                self._pending_landings.pop(intent_id, None)
                continue
            prepared, pending, owner = running
            if owner is not session or pending.status is LifecycleStatus.EXECUTING:
                continue
            if not all(
                (drone := drones.get(command.drone_id)) is not None
                and drone["connection_epoch"] == command.connection_epoch
                and (telemetry := drone.get("telemetry")) is not None
                and telemetry["t"] > completed_at
                and telemetry["state"] in {"landed", "disarmed"}
                for command in prepared.plan.commands
                if command.drone_id in targets
            ):
                continue
            active = session.current_state()["accepted_plan"]
            if active is not None and active["intent_id"] == intent_id:
                events.append(session.update_control_projection(accepted_plan=None))
            self._pending_landings.pop(intent_id, None)
            self._running.pop(intent_id, None)
        return tuple(events)

    def reconcile_membership(self, session: object) -> tuple[dict[str, object], ...]:
        """Retire plans whose roster can no longer authenticate their waiting commands."""
        state = session.current_state()
        roster_version = state["roster_version"]
        members = {drone["drone_id"]: drone for drone in state["drones"]}
        with self._lock:
            stale = [
                (prepared, pending)
                for prepared, pending, owner in self._running.values()
                if owner is session
                and any(
                    (member := members.get(command.drone_id)) is None
                    or member["connection_epoch"] != command.connection_epoch
                    or (
                        member["membership"] in {"leaving", "disconnected", "degraded"}
                        and member["membership"]
                        != prepared.snapshot.aircraft[command.drone_id].membership.value
                    )
                    for command in prepared.plan.commands
                    if prepared.intent.intent_id not in self._pending_landings
                    or command.drone_id in self._pending_landings[prepared.intent.intent_id][1]
                )
            ]
        events: list[dict[str, object]] = []
        for prepared, pending in stale:
            try:
                current = self._relay_snapshot(session)
            except Exception:
                # Only safety dispatch may use last-known flight facts with the current roster.
                aircraft = {}
                for drone in state["drones"]:
                    previous = prepared.snapshot.aircraft.get(drone["drone_id"])
                    if previous is None:
                        continue
                    aircraft[previous.drone_id] = replace(
                        previous,
                        connection_epoch=drone["connection_epoch"],
                        membership=MembershipState(drone["membership"]),
                        control_authority=drone["control_authority"],
                        rc_safety_operator_present=drone["rc_safety_operator_present"],
                    )
                current = replace(
                    prepared.snapshot,
                    roster_version=roster_version,
                    aircraft=aircraft,
                    selection=tuple(
                        drone_id for drone_id in state["selection"] if drone_id in aircraft
                    ),
                    armed=state["armed"],
                    estop_active=state["estop"],
                    now_ms=state["t"],
                )
            events.extend(
                self._dispatch_safety_hold(
                    prepared.intent,
                    current,
                    session,
                    intent_id=f"safety:membership:{roster_version}:{prepared.intent.intent_id}",
                    estop=prepared.intent.name is IntentName.ESTOP,
                )
            )
            invalidated = ExecutionResult(
                intent_id=prepared.intent.intent_id,
                roster_version=roster_version,
                status=LifecycleStatus.INVALIDATED,
                plan=prepared.plan,
                acknowledgements=pending.acknowledgements,
                refusal=Refusal(
                    intent_id=prepared.intent.intent_id,
                    roster_version=roster_version,
                    drone_id=None,
                    connection_epoch=None,
                    reason=RefusalReason.STALE_ROSTER,
                    detail="membership changed; remaining dispatch cancelled",
                    status=LifecycleStatus.INVALIDATED,
                ),
            )
            events.extend(self._record_retirement(prepared, invalidated, session))
            with self._lock:
                self._running.pop(prepared.intent.intent_id, None)
                self._pending_landings.pop(prepared.intent.intent_id, None)
                self._landing_ack_times.pop(prepared.intent.intent_id, None)
        return tuple(events)

    def prepare_resume(
        self,
        session: object,
        acknowledgement: object,
    ) -> ResumeToken | None:
        """Capture a validated acknowledgement and its exact execution owner without dispatching."""
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
            landing = None
            if pending.status is not LifecycleStatus.EXECUTING:
                landing = self._pending_landings.get(intent_id)
                if landing is None or terminal_ack.drone_id not in landing[1]:
                    return None
            return ResumeToken(
                intent_id,
                running,
                terminal_ack,
                self._stop_generations.get(session, 0),
                acknowledgement.t,
                landing,
            )

    def resume_after_acknowledgement(
        self, session: object, acknowledgement: object
    ) -> RelayExecution | None:
        token = self.prepare_resume(session, acknowledgement)
        return self._drive_resume(token) if token is not None else None


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
