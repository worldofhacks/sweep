"""Production relay boundary for autonomy execution and simulator safety evidence."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from threading import Event, Lock

from adapters.protocols import NodeSafetyAction, WatchdogConfig
from planner.controller import AutonomyController
from planner.coordination import MOTION_INTENTS
from planner.models import (
    ExecutionResult,
    FleetSnapshot,
    RelaySnapshotEnrichment,
)
from planner.models import (
    LifecycleStatus as AutonomyStatus,
)
from relay.contracts import LifecycleStatus as RelayStatus
from relay.intent_v1 import IntentV1
from relay.session import IntentSinkResult, RelaySession

type EnrichmentProvider = Callable[[Mapping[str, object]], RelaySnapshotEnrichment]
type ConnectionEpochSynchronizer = Callable[[int, int], None]
type AdapterIngress = Callable[[], list[dict[str, object]]]
type NodeActivity = Callable[[int, int, int], None]
type NodeSafetyEvents = Callable[[], list[NodeSafetyAction]]


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
        self.concurrent_intents = MOTION_INTENTS
        self._execution_lock = Lock()
        self._motion_lock = Lock()
        self._waiting_motion: _MotionWaiter | None = None

    def __call__(self, intent: IntentV1, _relay_state: dict[str, object]) -> IntentSinkResult:
        if intent.name in MOTION_INTENTS:
            return self._coordinate_motion(intent)
        if intent.name.value == "estop":
            return self._execute_one(intent)
        with self._execution_lock:
            return self._execute_one(intent)

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

    def _coordinate_motion(self, intent: IntentV1) -> IntentSinkResult:
        waiter = _MotionWaiter(intent=intent)
        with self._motion_lock:
            peer = self._waiting_motion
            if peer is None:
                self._waiting_motion = waiter
            else:
                self._waiting_motion = None

        if peer is not None:
            try:
                with self._execution_lock:
                    results = self._execute_pair(peer.intent, intent)
            except Exception as error:
                peer.error = error
                peer.done.set()
                raise
            peer.result = results[peer.intent.intent_id]
            peer.done.set()
            return results[intent.intent_id]

        timeout_s = self.controller.arbiter.config.motion_conflict_window_ms / 1_000
        if waiter.done.wait(timeout=timeout_s):
            if waiter.error is not None:
                raise waiter.error
            assert waiter.result is not None
            return waiter.result

        with self._motion_lock:
            if self._waiting_motion is waiter:
                self._waiting_motion = None
                execute_single = True
            else:
                execute_single = False
        if execute_single:
            with self._execution_lock:
                waiter.result = self._execute_one(intent)
            waiter.done.set()
        else:
            waiter.done.wait()
        if waiter.error is not None:
            raise waiter.error
        assert waiter.result is not None
        return waiter.result

    def _execute_pair(self, first: IntentV1, second: IntentV1) -> dict[str, IntentSinkResult]:
        def snapshot() -> FleetSnapshot:
            current = self.session.current_state()
            return FleetSnapshot.from_relay_state(
                current,
                enrichment=self.enrichment(current),
            )

        pair = self.controller.execute_pair(
            first,
            second,
            snapshot(),
            current_snapshot=snapshot,
        )
        post_execution = self._post_execution_events()
        results = {}
        for index, execution in enumerate(pair.executions):
            results[execution.intent_id] = self._sink_result(
                execution,
                events=post_execution if index == 0 else (),
            )
        safety = None if pair.safety_execution is None else pair.safety_execution.to_dict()
        for index, refusal in enumerate(pair.resolution.refusals):
            results[refusal.intent_id] = IntentSinkResult(
                status=RelayStatus.REFUSED,
                source="autonomy",
                result={"safety": safety},
                events=post_execution if index == 0 else (),
                reason=refusal.reason.value,
                detail=refusal.detail,
            )
        for intent_id in pair.resolution.invalidated_intent_ids:
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
        self.node_activity(drone_id, connection_epoch, int(relay_state["t"]))
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
        if self.synchronize_connection_epoch is not None:
            self.synchronize_connection_epoch(drone_id, connection_epoch)
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
class _MotionWaiter:
    intent: IntentV1
    done: Event = field(default_factory=Event)
    result: IntentSinkResult | None = None
    error: Exception | None = None
