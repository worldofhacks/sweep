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
    if conflict_window_ms < 0:
        raise ValueError("conflict_window_ms cannot be negative")
    earlier, later = sorted((first, second), key=lambda intent: intent.t)
    if later.t - earlier.t > conflict_window_ms:
        return ConflictResolution((first, second), (), (), False)

    if first.name is IntentName.ESTOP or second.name is IntentName.ESTOP:
        winner = first if first.name is IntentName.ESTOP else second
        loser = second if winner is first else first
        return ConflictResolution((winner,), (), (loser.intent_id,), False)
    if first.name is IntentName.HOLD or second.name is IntentName.HOLD:
        winner = first if first.name is IntentName.HOLD else second
        loser = second if winner is first else first
        return ConflictResolution((winner,), (), (loser.intent_id,), False)
    if first.name is IntentName.SELECT and second.name is IntentName.SELECT:
        return ConflictResolution((later,), (), (earlier.intent_id,), False)
    if first.name in MOTION_INTENTS and second.name in MOTION_INTENTS:
        refusals = tuple(_motion_refusal(intent, snapshot) for intent in (first, second))
        return ConflictResolution((), refusals, (), True)
    return ConflictResolution((first, second), (), (), False)


def _motion_refusal(intent: IntentV1, snapshot: FleetSnapshot) -> Refusal:
    return Refusal(
        intent_id=intent.intent_id,
        roster_version=snapshot.roster_version,
        drone_id=None,
        connection_epoch=None,
        reason=RefusalReason.CONFLICTING_MOTION,
        detail="another motion intent arrived inside the configured conflict window",
    )
