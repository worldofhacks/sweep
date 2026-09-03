"""Production relay boundary for autonomy execution and simulator safety evidence."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from adapters.protocols import NodeWatchdogState, WatchdogConfig
from planner.controller import AutonomyController
from planner.models import (
    FleetSnapshot,
    LossBehavior,
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


class AutonomyRelayBridge:
    """Execute validated relay intents and return relay-owned lifecycle evidence."""

    def __init__(
        self,
        *,
        session: RelaySession,
        controller: AutonomyController,
        enrichment: EnrichmentProvider,
        watchdog_config: WatchdogConfig,
        synchronize_connection_epoch: ConnectionEpochSynchronizer | None = None,
        ingress: AdapterIngress | None = None,
    ) -> None:
        self.session = session
        self.controller = controller
        self.enrichment = enrichment
        self.watchdog_config = watchdog_config
        self.synchronize_connection_epoch = synchronize_connection_epoch
        self.ingress = ingress
        self._watchdogs: dict[int, _WatchdogProgress] = {}

    def __call__(self, intent: IntentV1, _relay_state: dict[str, object]) -> IntentSinkResult:
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
        plan = execution.plan
        completed = execution.status is AutonomyStatus.COMPLETED
        refusal = execution.refusal
        return IntentSinkResult(
            status=RelayStatus(execution.status.value),
            source="autonomy",
            result=execution.to_dict(),
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
        progress = self._watchdogs.get(drone_id)
        if progress is None or progress.state.connection_epoch != connection_epoch:
            self._watchdogs[drone_id] = _WatchdogProgress(
                state=NodeWatchdogState(
                    drone_id=drone_id,
                    connection_epoch=connection_epoch,
                    last_activity_ms=int(relay_state["t"]),
                )
            )
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
        self._watchdogs[drone_id] = _WatchdogProgress(
            state=NodeWatchdogState(
                drone_id=drone_id,
                connection_epoch=connection_epoch,
                last_activity_ms=int(relay_state["t"]),
            )
        )
        return []

    def periodic_events(self, relay_state: Mapping[str, object]) -> list[dict[str, object]]:
        now_ms = int(relay_state["t"])
        drones = {
            int(item["drone_id"]): item
            for item in relay_state.get("drones", [])
            if isinstance(item, Mapping) and isinstance(item.get("drone_id"), int)
        }
        events: list[dict[str, object]] = []
        for drone_id, progress in tuple(self._watchdogs.items()):
            drone = drones.get(drone_id)
            if drone is None or drone.get("connection_epoch") != progress.state.connection_epoch:
                self._watchdogs.pop(drone_id, None)
                continue
            event = self._apply_node_watchdog(progress, now_ms=now_ms)
            if event is not None:
                events.append(event)
        return events

    def periodic_ingress(self) -> list[dict[str, object]]:
        return [] if self.ingress is None else self.ingress()

    def _apply_node_watchdog(
        self, progress: _WatchdogProgress, *, now_ms: int
    ) -> dict[str, object] | None:
        action = self.controller.dispatcher.flight.apply_node_watchdog(
            progress.state,
            now_ms=now_ms,
            config=self.watchdog_config,
        )
        if action is None or action is progress.action:
            return None
        progress.action = action
        event: dict[str, object] = {
            "v": 1,
            "t": now_ms,
            "type": "safety_action",
            "event_id": self.session.event_ids(),
            "session": self.session.session_id,
            "drone_id": progress.state.drone_id,
            "connection_epoch": progress.state.connection_epoch,
            "reason": "link_loss",
            "action": action.value,
            "loss_behavior": self.watchdog_config.loss_behavior.value,
        }
        self.session.audit_log.append(event)
        return event


@dataclass(slots=True)
class _WatchdogProgress:
    state: NodeWatchdogState
    action: LossBehavior | None = None
