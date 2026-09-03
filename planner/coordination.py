"""Deterministic coordination for temporally conflicting intents."""

from __future__ import annotations

from dataclasses import dataclass

from planner.models import FleetSnapshot, Refusal, RefusalReason
from relay.intent_v1 import IntentName, IntentV1

MOTION_INTENTS = frozenset(
    {
        IntentName.TAKEOFF,
        IntentName.TRANSLATE,
        IntentName.COME_HOME,
        IntentName.LAND,
        IntentName.LAND_ALL,
        IntentName.CAPTURE_ROOM,
    }
)


@dataclass(frozen=True, slots=True)
class ConflictResolution:
    accepted: tuple[IntentV1, ...]
    refusals: tuple[Refusal, ...]
    invalidated_intent_ids: tuple[str, ...]
    hold_required: bool


def resolve_intent_pair(
    first: IntentV1,
    second: IntentV1,
    snapshot: FleetSnapshot,
    *,
    conflict_window_ms: int,
) -> ConflictResolution:
    """Apply PRD 7.1 ordering before either intent reaches the adapter.

    Two motion requests in the conflict window are both refused and require a
    safety hold.  Two selection changes keep only the later event.  Stop and hold
    always win over a simultaneous non-safety request.
    """
    return resolve_intent_group(
        (first, second),
        snapshot,
        conflict_window_ms=conflict_window_ms,
    )


def resolve_intent_group(
    intents: tuple[IntentV1, ...],
    snapshot: FleetSnapshot,
    *,
    conflict_window_ms: int,
) -> ConflictResolution:
    if not intents:
        raise ValueError("intent group cannot be empty")
    if conflict_window_ms < 0:
        raise ValueError("conflict_window_ms cannot be negative")
    ordered = tuple(sorted(intents, key=lambda intent: (intent.t, intent.intent_id)))
    earlier, later = ordered[0], ordered[-1]
    if later.t - earlier.t > conflict_window_ms:
        return ConflictResolution(intents, (), (), False)

    estops = tuple(intent for intent in ordered if intent.name is IntentName.ESTOP)
    if estops:
        winner = estops[-1]
        return ConflictResolution(
            (winner,),
            (),
            tuple(intent.intent_id for intent in ordered if intent is not winner),
            False,
        )
    holds = tuple(intent for intent in ordered if intent.name is IntentName.HOLD)
    if holds:
        winner = holds[-1]
        return ConflictResolution(
            (winner,),
            (),
            tuple(intent.intent_id for intent in ordered if intent is not winner),
            False,
        )
    if all(intent.name is IntentName.SELECT for intent in ordered):
        return ConflictResolution(
            (later,), (), tuple(intent.intent_id for intent in ordered[:-1]), False
        )
    motions = tuple(intent for intent in ordered if intent.name in MOTION_INTENTS)
    if len(motions) > 1:
        refusals = tuple(_motion_refusal(intent, snapshot) for intent in motions)
        return ConflictResolution((), refusals, (), True)
    return ConflictResolution(intents, (), (), False)


def _motion_refusal(intent: IntentV1, snapshot: FleetSnapshot) -> Refusal:
    return Refusal(
        intent_id=intent.intent_id,
        roster_version=snapshot.roster_version,
        drone_id=None,
        connection_epoch=None,
        reason=RefusalReason.CONFLICTING_MOTION,
        detail="another motion intent arrived inside the configured conflict window",
    )
