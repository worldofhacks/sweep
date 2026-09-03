"""Pure autonomy decisions applied around relay-owned roster transitions."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum

from planner.models import (
    FleetSnapshot,
    FlightState,
    JsonValue,
    LossBehavior,
    MembershipState,
    Plan,
    Refusal,
    RefusalReason,
)


class RosterChangeKind(StrEnum):
    JOIN = "join"
    READY = "ready"
    GRACEFUL_REMOVE = "graceful_remove"
    UNEXPECTED_LOSS = "unexpected_loss"
    REJOIN = "rejoin"


@dataclass(frozen=True, slots=True)
class RosterChange:
    kind: RosterChangeKind
    drone_id: int


@dataclass(frozen=True, slots=True)
class RemovalAuthorization:
    drone_id: int
    roster_version: int
    allowed: bool
    refusal: Refusal | None = None

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "drone_id": self.drone_id,
            "roster_version": self.roster_version,
            "allowed": self.allowed,
            "refusal": self.refusal.to_dict() if self.refusal is not None else None,
        }


@dataclass(frozen=True, slots=True)
class LossResponse:
    drone_id: int
    connection_epoch: int
    behavior: LossBehavior
    adapter_watchdog_required: bool
    physical_rc_preserved: bool

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "drone_id": self.drone_id,
            "connection_epoch": self.connection_epoch,
            "behavior": self.behavior.value,
            "adapter_watchdog_required": self.adapter_watchdog_required,
            "physical_rc_preserved": self.physical_rc_preserved,
        }


@dataclass(frozen=True, slots=True)
class RosterReconciliation:
    change: RosterChange
    accepted: bool
    invalidated_plan_ids: tuple[str, ...] = ()
    invalidated_confirmation_ids: tuple[str, ...] = ()
    loss_response: LossResponse | None = None
    refusal: Refusal | None = None

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "change": {"kind": self.change.kind.value, "drone_id": self.change.drone_id},
            "accepted": self.accepted,
            "invalidated_plan_ids": list(self.invalidated_plan_ids),
            "invalidated_confirmation_ids": list(self.invalidated_confirmation_ids),
            "loss_response": (
                self.loss_response.to_dict() if self.loss_response is not None else None
            ),
            "refusal": self.refusal.to_dict() if self.refusal is not None else None,
        }


def authorize_graceful_removal(snapshot: FleetSnapshot, drone_id: int) -> RemovalAuthorization:
    """Authorize a signed leave only when M1.2's physical-state gate passes."""
    aircraft = snapshot.aircraft.get(drone_id)
    if aircraft is None:
        refusal = _transition_refusal(
            snapshot,
            drone_id,
            RefusalReason.AIRCRAFT_NOT_REGISTERED,
            "graceful removal target is not registered",
        )
    elif aircraft.flight_state not in {FlightState.DISARMED, FlightState.LANDED}:
        refusal = _transition_refusal(
            snapshot,
            drone_id,
            RefusalReason.INVALID_STATE,
            "graceful removal requires a landed aircraft",
        )
    elif aircraft.armed:
        refusal = _transition_refusal(
            snapshot,
            drone_id,
            RefusalReason.INVALID_STATE,
            "graceful removal requires a disarmed aircraft",
        )
    elif aircraft.active_task_id is not None:
        refusal = _transition_refusal(
            snapshot,
            drone_id,
            RefusalReason.ACTIVE_TASK,
            "graceful removal requires no active task",
        )
    else:
        refusal = None
    return RemovalAuthorization(
        drone_id=drone_id,
        roster_version=snapshot.roster_version,
        allowed=refusal is None,
        refusal=refusal,
    )


def reconcile_roster_change(
    previous: FleetSnapshot,
    current: FleetSnapshot,
    change: RosterChange,
    *,
    accepted_plans: Iterable[Plan] = (),
    pending_confirmation_versions: Mapping[str, int] | None = None,
    loss_behavior: LossBehavior,
) -> RosterReconciliation:
    """Return autonomy invalidations/actions without taking ownership of relay state.

    ``invalidated_plan_ids`` are stable :attr:`Plan.plan_id` values. Keys in
    ``pending_confirmation_versions`` are stable pending-confirmation identifiers;
    the relay integration uses the originating ``intent_id`` as that key. The #14
    relay owns the atomic selection/pending/accepted-plan projection and emits its
    one-shot ``invalidated_intent_ids`` metadata after this module authorizes the
    physical leave and this function verifies the resulting snapshot.
    """
    pending_confirmation_versions = pending_confirmation_versions or {}
    if current.roster_version <= previous.roster_version:
        return _reconciliation_refusal(
            previous,
            change,
            "roster-changing events must increase roster_version",
        )

    if change.kind is RosterChangeKind.GRACEFUL_REMOVE:
        authorization = authorize_graceful_removal(previous, change.drone_id)
        if not authorization.allowed:
            return RosterReconciliation(
                change=change,
                accepted=False,
                refusal=authorization.refusal,
            )
        current_aircraft = current.aircraft.get(change.drone_id)
        if change.drone_id in current.selection or (
            current_aircraft is not None
            and current_aircraft.membership
            not in {MembershipState.LEAVING, MembershipState.DISCONNECTED}
        ):
            return _reconciliation_refusal(
                current,
                change,
                "graceful removal must atomically clear selection and active membership",
            )
        return RosterReconciliation(
            change=change,
            accepted=True,
            invalidated_plan_ids=tuple(
                sorted(
                    plan.plan_id
                    for plan in accepted_plans
                    if plan.roster_version != current.roster_version
                )
            ),
            invalidated_confirmation_ids=tuple(
                sorted(
                    confirmation_id
                    for confirmation_id, roster_version in pending_confirmation_versions.items()
                    if roster_version != current.roster_version
                )
            ),
        )

    if change.kind is RosterChangeKind.UNEXPECTED_LOSS:
        aircraft = current.aircraft.get(change.drone_id)
        if aircraft is None or aircraft.membership not in {
            MembershipState.DISCONNECTED,
            MembershipState.DEGRADED,
        }:
            return _reconciliation_refusal(
                current,
                change,
                "unexpected loss must remain visible as disconnected or degraded",
            )
        return RosterReconciliation(
            change=change,
            accepted=True,
            loss_response=LossResponse(
                drone_id=aircraft.drone_id,
                connection_epoch=aircraft.connection_epoch,
                behavior=loss_behavior,
                adapter_watchdog_required=True,
                physical_rc_preserved=aircraft.physical_rc_available,
            ),
        )

    if change.kind is RosterChangeKind.REJOIN:
        old = previous.aircraft.get(change.drone_id)
        new = current.aircraft.get(change.drone_id)
        if old is None or new is None or new.connection_epoch <= old.connection_epoch:
            return _reconciliation_refusal(
                current,
                change,
                "rejoin must increment the aircraft connection epoch",
            )
        return RosterReconciliation(change=change, accepted=True)

    if change.kind is RosterChangeKind.JOIN:
        if change.drone_id in previous.aircraft or change.drone_id not in current.aircraft:
            return _reconciliation_refusal(
                current,
                change,
                "join must add a new stable aircraft id",
            )
        if current.selection != previous.selection:
            return _reconciliation_refusal(
                current,
                change,
                "join must preserve the current selection",
            )
        return RosterReconciliation(change=change, accepted=True)

    if change.kind is RosterChangeKind.READY:
        aircraft = current.aircraft.get(change.drone_id)
        if aircraft is None or aircraft.membership is not MembershipState.READY:
            return _reconciliation_refusal(
                current,
                change,
                "ready transition must produce ready membership",
            )
        return RosterReconciliation(change=change, accepted=True)

    return _reconciliation_refusal(current, change, "unknown roster change")


def _transition_refusal(
    snapshot: FleetSnapshot,
    drone_id: int,
    reason: RefusalReason,
    detail: str,
) -> Refusal:
    aircraft = snapshot.aircraft.get(drone_id)
    return Refusal(
        intent_id=f"membership:{snapshot.roster_version}:{drone_id}",
        roster_version=snapshot.roster_version,
        drone_id=drone_id,
        connection_epoch=aircraft.connection_epoch if aircraft is not None else None,
        reason=reason,
        detail=detail,
    )


def _reconciliation_refusal(
    snapshot: FleetSnapshot, change: RosterChange, detail: str
) -> RosterReconciliation:
    return RosterReconciliation(
        change=change,
        accepted=False,
        refusal=_transition_refusal(
            snapshot,
            change.drone_id,
            RefusalReason.INVALID_ROSTER_TRANSITION,
            detail,
        ),
    )
