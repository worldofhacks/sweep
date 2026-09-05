"""Production relay boundary for autonomy execution and simulator safety evidence."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from threading import Condition, Lock, RLock
from time import monotonic

from adapters.protocols import NodeSafetyAction, WatchdogConfig
from planner.controller import AutonomyController
from planner.coordination import MOTION_INTENTS, ConflictResolution, resolve_intent_group
from planner.models import (
    ExecutionResult,
    FleetSnapshot,
    Refusal,
    RefusalReason,
    RelaySnapshotEnrichment,
)
from planner.models import (
    LifecycleStatus as AutonomyStatus,
)
from relay.contracts import LifecycleStatus as RelayStatus
from relay.intent_v1 import IntentName, IntentV1
from relay.session import IntentSinkResult, RelaySession

type EnrichmentProvider = Callable[[Mapping[str, object]], RelaySnapshotEnrichment]
type ConnectionEpochSynchronizer = Callable[[int, int], None]
type AdapterIngress = Callable[[], list[dict[str, object]]]
type NodeActivity = Callable[[int, int, int], None]
type NodeSafetyEvents = Callable[[], list[NodeSafetyAction]]
_COORDINATED_INTENTS = MOTION_INTENTS | {
    IntentName.ESTOP,
    IntentName.HOLD,
    IntentName.SELECT,
}


class AutonomyRelayBridge:
    """Execute validated relay intents and return relay-owned lifecycle evidence."""

    def __init__(
        self,
        *,
        session: RelaySession,
        controller: AutonomyController,
        enrichment: EnrichmentProvider,
        watchdog_config: WatchdogConfig,
        node_activity: NodeActivity,
        node_safety_events: NodeSafetyEvents,
        synchronize_connection_epoch: ConnectionEpochSynchronizer | None = None,
        ingress: AdapterIngress | None = None,
        post_execution_ingress: AdapterIngress | None = None,
    ) -> None:
        self.session = session
        self.controller = controller
        self.enrichment = enrichment
        self.watchdog_config = watchdog_config
        self.node_activity = node_activity
        self.node_safety_events = node_safety_events
        self.synchronize_connection_epoch = synchronize_connection_epoch
        self.ingress = ingress
        self.post_execution_ingress = post_execution_ingress
        self.concurrent_intents = _COORDINATED_INTENTS
        self._execution_lock = RLock()
        self._connection_epochs: dict[int, int] = {}
        self._coordination = Condition(Lock())
        self._admissions: dict[str, _CoordinatedIntent] = {}
        self._completed_ordering: list[_CompletedOrdering] = []
        self._coordinator_active = False

    def __call__(self, intent: IntentV1, _relay_state: dict[str, object]) -> IntentSinkResult:
        if intent.name in _COORDINATED_INTENTS:
            return self._coordinate_intent(intent)
        with self._execution_lock:
            return self._execute_one(intent)

    @contextmanager
    def execution_barrier(self) -> Iterator[None]:
        with self._execution_lock:
            yield

    def admit_intent(self, intent: IntentV1) -> None:
        if intent.name not in _COORDINATED_INTENTS:
            return
        with self._coordination:
            self._admissions.setdefault(
                intent.intent_id,
                _CoordinatedIntent(intent=intent, admitted_at=monotonic()),
            )
            self._coordination.notify_all()

    def intent_delivered(self, intent_id: str) -> None:
        with self._coordination:
            admission = self._admissions.get(intent_id)
            if admission is not None:
                admission.delivered = True
            self._coordination.notify_all()

    def cancel_intent(self, intent_id: str) -> None:
        with self._coordination:
            self._admissions.pop(intent_id, None)
            self._coordination.notify_all()

    def _execute_one(self, intent: IntentV1) -> IntentSinkResult:
        def snapshot() -> FleetSnapshot:
            current = self.session.current_state()
            return FleetSnapshot.from_relay_state(
                current,
                enrichment=self.enrichment(current),
            )

        execution = self.controller.execute(
            intent,
            snapshot(),
            current_snapshot=snapshot,
        )
        return self._sink_result(execution, events=self._post_execution_events())

    def _coordinate_intent(self, intent: IntentV1) -> IntentSinkResult:
        self.admit_intent(intent)
        while True:
            with self._coordination:
                admission = self._admissions[intent.intent_id]
                admission.delivered = True
                if (
                    self._coordinator_active
                    and intent.name is IntentName.ESTOP
                    and not admission.claimed
                ):
                    self._admissions.pop(intent.intent_id)
                    bypass = True
                else:
                    bypass = False
                if not bypass and (admission.result is not None or admission.error is not None):
                    break
                if bypass:
                    coordinator = False
                elif not self._coordinator_active:
                    self._coordinator_active = True
                    coordinator = True
                else:
                    coordinator = False
                    self._coordination.wait()
            if bypass:
                result = self._execute_one(intent)
                self._record_completed_ordering(
                    (_CoordinatedIntent(intent, monotonic(), delivered=True),),
                    {intent.intent_id: result},
                )
                return result
            if not coordinator:
                continue

            group, results, error = self._resolve_admission_group(admission)
            with self._coordination:
                for item in group:
                    current = self._admissions.get(item.intent.intent_id)
                    if current is None:
                        continue
                    if error is not None and item.delivered:
                        current.error = error
                    elif item.intent.intent_id in results:
                        current.result = results[item.intent.intent_id]
                self._coordinator_active = False
                self._coordination.notify_all()

        with self._coordination:
            self._admissions.pop(intent.intent_id, None)
            if admission.error is not None:
                raise admission.error
            assert admission.result is not None
            return admission.result

    def _resolve_admission_group(
        self, seed: _CoordinatedIntent
    ) -> tuple[
        tuple[_CoordinatedIntent, ...],
        dict[str, IntentSinkResult],
        Exception | None,
    ]:
        window_s = self.controller.arbiter.config.motion_conflict_window_ms / 1_000
        with self._coordination:
            while True:
                active = tuple(
                    admission
                    for admission in self._admissions.values()
                    if admission.result is None and admission.error is None
                )
                group = self._conflict_neighborhood(active, seed)
                safety = tuple(
                    admission
                    for admission in active
                    if admission.intent.name in {IntentName.ESTOP, IntentName.HOLD}
                    and any(
                        item.delivered
                        and abs(admission.intent.t - item.intent.t)
                        <= self.controller.arbiter.config.motion_conflict_window_ms
                        for item in group
                    )
                )
                if safety:
                    anchor = max(
                        safety,
                        key=lambda item: (
                            item.intent.name is IntentName.ESTOP,
                            item.delivered,
                            item.intent.t,
                            item.intent.intent_id,
                        ),
                    )
                    group = self._conflict_neighborhood(
                        tuple(
                            admission
                            for admission in active
                            if admission.intent.name in MOTION_INTENTS
                            or admission.intent.name in {IntentName.ESTOP, IntentName.HOLD}
                        ),
                        anchor,
                    )
                delivered_holds = tuple(
                    item for item in group if item.delivered and item.intent.name is IntentName.HOLD
                )
                if delivered_holds:
                    oldest_hold = max(delivered_holds, key=lambda item: item.intent.t)
                    group = tuple(
                        item
                        for item in active
                        if item in group
                        or (
                            item.intent.name in MOTION_INTENTS
                            and item.intent.t < oldest_hold.intent.t
                        )
                    )
                if any(item.delivered and item.intent.name is IntentName.ESTOP for item in group):
                    earliest_delivered = min(item.intent.t for item in group if item.delivered)
                    group = tuple(
                        item
                        for item in group
                        if item.delivered
                        or (
                            item.intent.name is not IntentName.ESTOP
                            and item.intent.t >= earliest_delivered
                        )
                    )
                deadline = max(item.admitted_at for item in group) + window_s
                remaining = deadline - monotonic()
                safety_present = any(
                    item.intent.name in {IntentName.ESTOP, IntentName.HOLD} for item in group
                )
                if remaining > 0 and not safety_present:
                    self._coordination.wait(timeout=remaining)
                    continue
                for item in group:
                    if item.delivered:
                        item.claimed = True
                group = tuple(replace(item) for item in group)
                break
        try:
            results = self._execute_group(group)
        except Exception as error:
            return group, {}, error
        return group, results, None

    def _conflict_neighborhood(
        self,
        admissions: tuple[_CoordinatedIntent, ...],
        seed: _CoordinatedIntent,
    ) -> tuple[_CoordinatedIntent, ...]:
        """Return the timestamp-connected component containing ``seed``."""
        ordered = tuple(sorted(admissions, key=lambda item: (item.intent.t, item.intent.intent_id)))
        index = ordered.index(seed)
        window_ms = self.controller.arbiter.config.motion_conflict_window_ms
        first = index
        while first and ordered[first].intent.t - ordered[first - 1].intent.t <= window_ms:
            first -= 1
        last = index
        while (
            last + 1 < len(ordered)
            and ordered[last + 1].intent.t - ordered[last].intent.t <= window_ms
        ):
            last += 1
        return ordered[first : last + 1]

    def _execute_group(
        self, admissions: tuple[_CoordinatedIntent, ...]
    ) -> dict[str, IntentSinkResult]:
        def snapshot() -> FleetSnapshot:
            current = self.session.current_state()
            return FleetSnapshot.from_relay_state(
                current,
                enrichment=self.enrichment(current),
            )

        intents = tuple(item.intent for item in admissions)
        delivered = {item.intent.intent_id for item in admissions if item.delivered}
        current = snapshot()
        resolution = resolve_intent_group(
            intents,
            current,
            conflict_window_ms=self.controller.arbiter.config.motion_conflict_window_ms,
        )
        resolution = self._preserve_delivered_safety_action(
            admissions,
            resolution,
            current,
            self.controller.arbiter.config.motion_conflict_window_ms,
        )
        resolution = self._retire_motion_preceding_delivered_hold(admissions, resolution)
        resolution = self._prioritize_delivered_estop(admissions, resolution)
        resolution = self._apply_completed_ordering(admissions, resolution, current)
        bypass_execution_lock = any(
            intent.name is IntentName.ESTOP and intent.intent_id in delivered
            for intent in resolution.accepted
        )

        def dispatch() -> dict[str, IntentSinkResult]:
            if bypass_execution_lock:
                return self._execute_resolution(
                    admissions, resolution, delivered, current, snapshot
                )
            with self._execution_lock:
                return self._execute_resolution(
                    admissions, resolution, delivered, current, snapshot
                )

        results = self.session.execute_coordinated_group(
            tuple(sorted(delivered)),
            dispatch,
        )
        self._record_completed_ordering(admissions, results)
        return results

    def _prune_completed_ordering(self) -> None:
        window_ms = self.controller.arbiter.config.motion_conflict_window_ms
        cutoff = (
            monotonic()
            - (
                self.session.limits.intent_max_age_ms
                + self.session.limits.future_clock_skew_ms
                + window_ms
            )
            / 1_000
        )
        pending = tuple(
            item.intent
            for item in self._admissions.values()
            if item.intent.name in MOTION_INTENTS and item.result is None and item.error is None
        )
        self._completed_ordering = [
            record
            for record in self._completed_ordering
            if record.completed_at >= cutoff
            or any(
                record.supersedes(intent, window_ms)
                or any(abs(intent.t - t) <= window_ms for t in record.motion_times)
                for intent in pending
            )
        ]

    def _apply_completed_ordering(
        self,
        admissions: tuple[_CoordinatedIntent, ...],
        resolution: ConflictResolution,
        snapshot: FleetSnapshot,
    ) -> ConflictResolution:
        motions = tuple(intent for intent in resolution.accepted if intent.name in MOTION_INTENTS)
        with self._coordination:
            self._prune_completed_ordering()
            records = tuple(self._completed_ordering)
        window_ms = self.controller.arbiter.config.motion_conflict_window_ms
        retired = {
            motion.intent_id
            for motion in motions
            if any(record.supersedes(motion, window_ms) for record in records)
        }
        conflicting = tuple(
            Refusal(
                intent_id=motion.intent_id,
                roster_version=snapshot.roster_version,
                drone_id=None,
                connection_epoch=None,
                reason=RefusalReason.CONFLICTING_MOTION,
                detail="another motion intent arrived inside the configured conflict window",
            )
            for motion in motions
            if motion.intent_id not in retired
            and any(
                abs(motion.t - t) <= window_ms for record in records for t in record.motion_times
            )
        )
        excluded = retired | {refusal.intent_id for refusal in conflicting}
        return replace(
            resolution,
            accepted=tuple(
                intent for intent in resolution.accepted if intent.intent_id not in excluded
            ),
            refusals=resolution.refusals + conflicting,
            invalidated_intent_ids=tuple(
                dict.fromkeys((*resolution.invalidated_intent_ids, *sorted(retired)))
            ),
            hold_required=resolution.hold_required or bool(conflicting),
        )

    def _record_completed_ordering(
        self,
        admissions: tuple[_CoordinatedIntent, ...],
        results: dict[str, IntentSinkResult],
    ) -> None:
        motion_times = tuple(
            item.intent.t
            for item in admissions
            if item.intent.name in MOTION_INTENTS
            and (result := results.get(item.intent.intent_id)) is not None
            and (result.status is RelayStatus.COMPLETED or result.reason == "conflicting_motion")
        )
        safety_times = tuple(
            item.intent.t
            for item in admissions
            if item.delivered
            and item.intent.name in {IntentName.HOLD, IntentName.ESTOP, IntentName.LAND_ALL}
            and (result := results.get(item.intent.intent_id)) is not None
            and result.status is RelayStatus.COMPLETED
        )
        with self._coordination:
            self._prune_completed_ordering()
            if motion_times or safety_times:
                self._completed_ordering.append(
                    _CompletedOrdering(monotonic(), motion_times, max(safety_times, default=None))
                )

    @staticmethod
    def _preserve_delivered_safety_action(
        admissions: tuple[_CoordinatedIntent, ...],
        resolution: ConflictResolution,
        snapshot: FleetSnapshot,
        conflict_window_ms: int,
    ) -> ConflictResolution:
        undelivered_safety = any(
            item.intent.name in {IntentName.ESTOP, IntentName.HOLD} and not item.delivered
            for item in admissions
        )
        if not undelivered_safety:
            return resolution
        fallback = resolve_intent_group(
            tuple(
                item.intent
                for item in admissions
                if item.delivered or item.intent.name not in {IntentName.ESTOP, IntentName.HOLD}
            ),
            snapshot,
            conflict_window_ms=conflict_window_ms,
        )
        fallback_accepted = {intent.intent_id for intent in fallback.accepted}
        protected = tuple(
            item.intent
            for item in admissions
            if item.delivered
            and item.intent.name
            in {IntentName.ESTOP, IntentName.HOLD, IntentName.LAND, IntentName.LAND_ALL}
            and item.intent.intent_id in resolution.invalidated_intent_ids
            and item.intent.intent_id in fallback_accepted
        )
        if not protected and not fallback.hold_required:
            return resolution
        protected_ids = {intent.intent_id for intent in protected}
        accepted_ids = {intent.intent_id for intent in resolution.accepted} | protected_ids
        reserved_ids = {
            item.intent.intent_id
            for item in admissions
            if not item.delivered and item.intent.name in {IntentName.ESTOP, IntentName.HOLD}
        }
        refusals = {refusal.intent_id: refusal for refusal in resolution.refusals}
        refusals.update((refusal.intent_id, refusal) for refusal in fallback.refusals)
        invalidated = dict.fromkeys(
            (*resolution.invalidated_intent_ids, *fallback.invalidated_intent_ids)
        )
        return ConflictResolution(
            accepted=tuple(
                intent for intent in fallback.accepted if intent.intent_id in accepted_ids
            ),
            refusals=tuple(refusals.values()),
            invalidated_intent_ids=tuple(
                intent_id
                for intent_id in invalidated
                if intent_id not in protected_ids | reserved_ids | refusals.keys()
            ),
            hold_required=resolution.hold_required or fallback.hold_required,
        )

    @staticmethod
    def _retire_motion_preceding_delivered_hold(
        admissions: tuple[_CoordinatedIntent, ...],
        resolution: ConflictResolution,
    ) -> ConflictResolution:
        delivered_holds = tuple(
            item.intent
            for item in admissions
            if item.delivered and item.intent.name is IntentName.HOLD
        )
        if not delivered_holds:
            return resolution
        hold_t = max(intent.t for intent in delivered_holds)
        retired_ids = {
            item.intent.intent_id
            for item in admissions
            if item.intent.name in MOTION_INTENTS and item.intent.t < hold_t
        }
        if not retired_ids:
            return resolution
        return replace(
            resolution,
            accepted=tuple(
                intent for intent in resolution.accepted if intent.intent_id not in retired_ids
            ),
            refusals=tuple(
                refusal for refusal in resolution.refusals if refusal.intent_id not in retired_ids
            ),
            invalidated_intent_ids=tuple(
                dict.fromkeys((*resolution.invalidated_intent_ids, *sorted(retired_ids)))
            ),
        )

    @staticmethod
    def _prioritize_delivered_estop(
        admissions: tuple[_CoordinatedIntent, ...],
        resolution: ConflictResolution,
    ) -> ConflictResolution:
        if not any(item.delivered and item.intent.name is IntentName.ESTOP for item in admissions):
            return resolution
        retired_ids = {
            item.intent.intent_id for item in admissions if item.intent.name in MOTION_INTENTS
        }
        return replace(
            resolution,
            accepted=tuple(
                intent for intent in resolution.accepted if intent.intent_id not in retired_ids
            ),
            refusals=tuple(
                refusal for refusal in resolution.refusals if refusal.intent_id not in retired_ids
            ),
            invalidated_intent_ids=tuple(
                dict.fromkeys((*resolution.invalidated_intent_ids, *sorted(retired_ids)))
            ),
        )

    def _execute_resolution(
        self,
        admissions: tuple[_CoordinatedIntent, ...],
        resolution: ConflictResolution,
        delivered: set[str],
        current: FleetSnapshot,
        snapshot: Callable[[], FleetSnapshot],
    ) -> dict[str, IntentSinkResult]:
        intents = tuple(item.intent for item in admissions)
        executions = tuple(
            self.controller.execute(intent, snapshot(), current_snapshot=snapshot)
            for intent in resolution.accepted
            if intent.intent_id in delivered
        )
        safety_execution = None
        if resolution.hold_required:
            hold_plan = self.controller.planner.emergency_hold_plan(
                intent_id="safety:motion-conflict:"
                + ":".join(intent.intent_id for intent in intents),
                snapshot=current,
            )
            safety_execution = self.controller.dispatcher.dispatch(
                hold_plan,
                current,
                current_snapshot=snapshot,
            )
        post_execution = self._post_execution_events()
        results = {}
        for index, execution in enumerate(executions):
            results[execution.intent_id] = self._sink_result(
                execution,
                events=post_execution if index == 0 else (),
            )
        safety = None if safety_execution is None else safety_execution.to_dict()
        for index, refusal in enumerate(resolution.refusals):
            results[refusal.intent_id] = IntentSinkResult(
                status=RelayStatus.REFUSED,
                source="autonomy",
                result={"safety": safety},
                events=post_execution if index == 0 else (),
                reason=refusal.reason.value,
                detail=refusal.detail,
            )
        for intent_id in resolution.invalidated_intent_ids:
            results[intent_id] = IntentSinkResult(
                status=RelayStatus.INVALIDATED,
                source="autonomy",
                result={"safety": safety},
                reason="superseded",
                detail="a higher-priority concurrent intent superseded this request",
            )
        return results

    def _post_execution_events(self) -> tuple[Mapping[str, object], ...]:
        if self.post_execution_ingress is None:
            return ()
        return tuple(self.post_execution_ingress())

    @staticmethod
    def _sink_result(
        execution: ExecutionResult,
        *,
        events: tuple[Mapping[str, object], ...] = (),
    ) -> IntentSinkResult:
        plan = execution.plan
        completed = execution.status is AutonomyStatus.COMPLETED
        refusal = execution.refusal
        return IntentSinkResult(
            status=RelayStatus(execution.status.value),
            source="autonomy",
            result=execution.to_dict(),
            events=events,
            selection_update=(plan.selection_update if completed and plan is not None else None),
            armed_update=(plan.armed_update if completed and plan is not None else None),
            estop_update=(plan.estop_update if plan is not None else None),
            reason=refusal.reason.value if refusal is not None else None,
            detail=refusal.detail if refusal is not None else None,
        )

    def adapter_disconnected(
        self,
        *,
        drone_id: int,
        connection_epoch: int,
        relay_state: Mapping[str, object],
    ) -> list[dict[str, object]]:
        return []

    def adapter_activity(
        self,
        *,
        drone_id: int,
        relay_state: Mapping[str, object],
    ) -> list[dict[str, object]]:
        drone = next(
            (
                item
                for item in relay_state.get("drones", [])
                if isinstance(item, Mapping) and item.get("drone_id") == drone_id
            ),
            None,
        )
        if drone is None:
            return []
        connection_epoch = drone.get("connection_epoch")
        membership = drone.get("membership")
        if (
            not isinstance(connection_epoch, int)
            or isinstance(connection_epoch, bool)
            or membership in {"disconnected", "leaving"}
        ):
            return []
        if (
            self.synchronize_connection_epoch is not None
            and self._connection_epochs.get(drone_id) != connection_epoch
        ):
            with self._execution_lock:
                self.synchronize_connection_epoch(drone_id, connection_epoch)
                self._connection_epochs[drone_id] = connection_epoch
        self.node_activity(drone_id, connection_epoch, int(relay_state["t"]))
        return []

    def periodic_events(self, relay_state: Mapping[str, object]) -> list[dict[str, object]]:
        events: list[dict[str, object]] = []
        for safety in self.node_safety_events():
            event: dict[str, object] = {
                "v": 1,
                "t": safety.t_ms,
                "type": "safety_action",
                "event_id": self.session.event_ids(),
                "session": self.session.session_id,
                "drone_id": safety.drone_id,
                "connection_epoch": safety.connection_epoch,
                "reason": "link_loss",
                "action": safety.action.value,
                "loss_behavior": self.watchdog_config.loss_behavior.value,
            }
            self.session.audit_log.append(event)
            events.append(event)
        return events

    def periodic_ingress(self) -> list[dict[str, object]]:
        return [] if self.ingress is None else self.ingress()


@dataclass(slots=True)
class _CoordinatedIntent:
    intent: IntentV1
    admitted_at: float
    delivered: bool = False
    claimed: bool = False
    result: IntentSinkResult | None = None
    error: Exception | None = None


@dataclass(frozen=True, slots=True)
class _CompletedOrdering:
    completed_at: float
    motion_times: tuple[int, ...]
    safety_t: int | None

    def supersedes(self, intent: IntentV1, conflict_window_ms: int) -> bool:
        if self.safety_t is None:
            return False
        recovery = intent.name in {IntentName.COME_HOME, IntentName.LAND, IntentName.LAND_ALL}
        cutoff = self.safety_t + (0 if recovery else conflict_window_ms)
        return intent.t <= cutoff
