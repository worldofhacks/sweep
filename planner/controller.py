"""End-to-end autonomy orchestration without transport or relay coupling."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from adapters.dispatch import AdapterDispatcher
from arbiter.safety import SafetyArbiter
from planner.coordination import ConflictResolution, resolve_intent_pair
from planner.models import (
    ExecutionResult,
    FleetSnapshot,
    LifecycleStatus,
    Plan,
    Refusal,
    RefusalReason,
)
from planner.planner import DeterministicPlanner
from relay.intent_v1 import IntentV1

type SnapshotProvider = Callable[[], FleetSnapshot]


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
        return self.dispatcher.dispatch(
            planned,
            current,
            current_snapshot=provider,
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
