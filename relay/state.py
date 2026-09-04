"""Pure authoritative fleet-registry transitions and state projection."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from threading import RLock

from relay.contracts import (
    CapabilitiesFrame,
    Membership,
    MembershipAction,
    MembershipRequest,
    NodeStatusFrame,
    TelemetryV1,
)

MAX_PHYSICAL_AIRCRAFT = 4
_CAMERA_PATTERNS = frozenset({"pano_360", "reconstruct_8"})


class RegistryError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class MembershipTransition:
    t: int
    event_id: str
    action: MembershipAction
    drone_id: int
    connection_epoch: int
    membership: Membership
    roster_version: int
    reason: str | None
    readiness_reasons: tuple[str, ...]
    adapter_id: str | None
    capabilities: tuple[str, ...]
    provenance: str
    invalidated_intent_ids: tuple[str, ...] = ()
    invalidation_reason: str | None = None
    prior_roster_version: int | None = None
    cleared_control_fields: tuple[str, ...] = ()

    def to_event(self, session: str) -> dict[str, object]:
        return {
            "v": 1,
            "t": self.t,
            "type": "membership",
            "event_id": self.event_id,
            "session": session,
            "action": self.action.value,
            "drone_id": self.drone_id,
            "connection_epoch": self.connection_epoch,
            "membership": self.membership.value,
            "roster_version": self.roster_version,
            "reason": self.reason,
            "readiness_reasons": list(self.readiness_reasons),
            "adapter_id": self.adapter_id,
            "capabilities": list(self.capabilities),
            "provenance": self.provenance,
        }


@dataclass(slots=True)
class _AircraftRecord:
    drone_id: int
    adapter_id: str
    capabilities: tuple[str, ...]
    connection_epoch: int
    membership: Membership
    joined_at: int
    updated_at: int
    identity_verified: bool = True
    readiness_declared: bool = False
    home_pose: dict[str, float] | None = None
    control_authority: bool = False
    rc_safety_operator_present: bool = False
    telemetry: TelemetryV1 | None = None
    disconnected_at: int | None = None
    history: list[dict[str, object]] = field(default_factory=list)
    camera_capabilities: CapabilitiesFrame | None = None
    node_status: NodeStatusFrame | None = None


class FleetRegistry:
    """One-session fleet state; transport authentication happens before entry."""

    def __init__(self, *, telemetry_freshness_ms: int) -> None:
        if telemetry_freshness_ms <= 0:
            raise ValueError("telemetry_freshness_ms must be positive")
        self.telemetry_freshness_ms = telemetry_freshness_ms
        self._aircraft: dict[int, _AircraftRecord] = {}
        self._roster_version = 0
        self._selection: tuple[int, ...] = ()
        self._armed = False
        self._estop = False
        self._formation = "none"
        self._spacing = 0.8
        self._mode = "indoor"
        self._pending: dict[str, object] | None = None
        self._accepted_plan: dict[str, object] | None = None
        self._lock = RLock()

    @property
    def roster_version(self) -> int:
        with self._lock:
            return self._roster_version

    def connection_epoch(self, drone_id: int) -> int | None:
        with self._lock:
            record = self._aircraft.get(drone_id)
            return None if record is None else record.connection_epoch

    def apply_join(self, request: MembershipRequest) -> MembershipTransition:
        if request.action is not MembershipAction.JOIN:
            raise ValueError("apply_join requires a join request")
        assert request.adapter_id is not None
        with self._lock:
            record = self._aircraft.get(request.drone_id)
            rejoining = record is not None
            if record is None:
                if len(self._aircraft) >= MAX_PHYSICAL_AIRCRAFT:
                    raise RegistryError(
                        "fleet_capacity",
                        f"session already contains {MAX_PHYSICAL_AIRCRAFT} stable aircraft IDs",
                    )
                record = _AircraftRecord(
                    drone_id=request.drone_id,
                    adapter_id=request.adapter_id,
                    capabilities=request.capabilities,
                    connection_epoch=1,
                    membership=Membership.REGISTERED,
                    joined_at=request.t,
                    updated_at=request.t,
                )
                self._aircraft[request.drone_id] = record
            else:
                if record.membership is not Membership.DISCONNECTED:
                    raise RegistryError(
                        "already_connected", "aircraft must be disconnected before rejoining"
                    )
                record.adapter_id = request.adapter_id
                record.capabilities = request.capabilities
                record.connection_epoch += 1
                record.membership = Membership.REGISTERED
                record.updated_at = request.t
                record.identity_verified = True
                record.readiness_declared = False
                record.home_pose = None
                record.control_authority = False
                record.rc_safety_operator_present = False
                record.telemetry = None
                record.disconnected_at = None
                record.camera_capabilities = None
                record.node_status = None

            self._roster_version += 1
            self._remember(
                record,
                t=request.t,
                action=MembershipAction.JOIN,
                reason="authenticated_rejoin" if rejoining else "authenticated_join",
            )
            return self._transition(
                record,
                t=request.t,
                event_id=request.event_id,
                action=MembershipAction.JOIN,
                reason="authenticated_rejoin" if rejoining else "authenticated_join",
                provenance="adapter_signature",
            )

    def apply_readiness(self, request: MembershipRequest) -> MembershipTransition:
        if request.action is not MembershipAction.READINESS:
            raise ValueError("apply_readiness requires a readiness request")
        assert request.connection_epoch is not None
        assert request.home_pose_confirmed is not None
        assert request.control_authority is not None
        assert request.rc_safety_operator_present is not None
        with self._lock:
            record = self._require_current(request.drone_id, request.connection_epoch)
            if record.membership in {Membership.DISCONNECTED, Membership.LEAVING}:
                raise RegistryError(
                    "invalid_membership_transition",
                    f"cannot declare readiness while {record.membership.value}",
                )
            record.readiness_declared = True
            record.control_authority = request.control_authority
            record.rc_safety_operator_present = request.rc_safety_operator_present
            if request.home_pose_confirmed and self._has_current_telemetry(record):
                assert record.telemetry is not None
                record.home_pose = {
                    "x": record.telemetry.x,
                    "y": record.telemetry.y,
                    "z": record.telemetry.z,
                }
            elif not request.home_pose_confirmed:
                record.home_pose = None
            reasons = self._readiness_reasons(record, request.t)
            record.membership = Membership.READY if not reasons else Membership.DEGRADED
            record.updated_at = request.t
            self._roster_version += 1
            self._remember(
                record,
                t=request.t,
                action=MembershipAction.READINESS,
                reason=None if not reasons else "readiness_gate_failed",
            )
            return self._transition(
                record,
                t=request.t,
                event_id=request.event_id,
                action=MembershipAction.READINESS,
                reason=None if not reasons else "readiness_gate_failed",
                provenance="adapter_signature",
            )

    def apply_graceful_leave(self, request: MembershipRequest) -> MembershipTransition:
        if request.action is not MembershipAction.GRACEFUL_LEAVE:
            raise ValueError("apply_graceful_leave requires a graceful_leave request")
        assert request.connection_epoch is not None
        with self._lock:
            record = self._require_current(request.drone_id, request.connection_epoch)
            if record.membership in {Membership.DISCONNECTED, Membership.LEAVING}:
                raise RegistryError(
                    "invalid_membership_transition",
                    f"cannot begin graceful leave while {record.membership.value}",
                )
            prior_roster_version = self._roster_version
            invalidated_intent_ids = _intent_ids(self._pending, self._accepted_plan)
            cleared_control_fields: list[str] = []
            if request.drone_id in self._selection:
                self._selection = tuple(
                    drone_id for drone_id in self._selection if drone_id != request.drone_id
                )
                cleared_control_fields.append("selection")
            if self._pending is not None:
                self._pending = None
                cleared_control_fields.append("pending")
            if self._accepted_plan is not None:
                self._accepted_plan = None
                cleared_control_fields.append("accepted_plan")
            record.membership = Membership.LEAVING
            record.updated_at = request.t
            self._roster_version += 1
            self._remember(
                record,
                t=request.t,
                action=MembershipAction.GRACEFUL_LEAVE,
                reason="graceful_leave_requested",
            )
            return self._transition(
                record,
                t=request.t,
                event_id=request.event_id,
                action=MembershipAction.GRACEFUL_LEAVE,
                reason="graceful_leave_requested",
                provenance="adapter_signature",
                invalidated_intent_ids=invalidated_intent_ids,
                invalidation_reason="graceful_leave_roster_change",
                prior_roster_version=prior_roster_version,
                cleared_control_fields=tuple(cleared_control_fields),
            )

    def apply_telemetry(
        self, telemetry: TelemetryV1, *, transition_event_id: str
    ) -> MembershipTransition | None:
        with self._lock:
            record = self._require_current(telemetry.drone, telemetry.connection_epoch)
            if record.membership in {Membership.DISCONNECTED, Membership.LEAVING}:
                raise RegistryError(
                    "invalid_membership_transition",
                    f"telemetry is not current while {record.membership.value}",
                )
            if record.telemetry is not None and telemetry.t < record.telemetry.t:
                raise RegistryError(
                    "out_of_order_telemetry",
                    "telemetry timestamp precedes the current canonical frame",
                )
            prior_membership = record.membership
            record.telemetry = telemetry
            record.updated_at = telemetry.t
            if not record.readiness_declared:
                return None
            reasons = self._readiness_reasons(record, telemetry.t)
            record.membership = Membership.READY if not reasons else Membership.DEGRADED
            if record.membership is prior_membership:
                return None
            self._roster_version += 1
            action = (
                MembershipAction.TELEMETRY_RECOVERED
                if record.membership is Membership.READY
                else MembershipAction.TELEMETRY_STALE
            )
            reason = "telemetry_recovered" if not reasons else "readiness_gate_failed"
            self._remember(record, t=telemetry.t, action=action, reason=reason)
            return self._transition(
                record,
                t=telemetry.t,
                event_id=transition_event_id,
                action=action,
                reason=reason,
                provenance="authenticated_adapter_telemetry",
            )

    def expire_stale_telemetry(
        self, *, now_ms: int, event_ids: list[str]
    ) -> list[MembershipTransition]:
        with self._lock:
            ready = [
                record
                for record in self._aircraft.values()
                if record.membership is Membership.READY
            ]
            if len(event_ids) < len(ready):
                raise ValueError("one event ID is required for each potentially stale aircraft")
            transitions: list[MembershipTransition] = []
            for record, event_id in zip(ready, event_ids, strict=False):
                reasons = self._readiness_reasons(record, now_ms)
                if "telemetry_stale" not in reasons:
                    continue
                record.membership = Membership.DEGRADED
                record.updated_at = now_ms
                self._roster_version += 1
                self._remember(
                    record,
                    t=now_ms,
                    action=MembershipAction.TELEMETRY_STALE,
                    reason="telemetry_stale",
                )
                transitions.append(
                    self._transition(
                        record,
                        t=now_ms,
                        event_id=event_id,
                        action=MembershipAction.TELEMETRY_STALE,
                        reason="telemetry_stale",
                        provenance="relay_freshness_attestation",
                    )
                )
            return transitions

    def disconnect(
        self, *, drone_id: int, t: int, event_id: str, connection_epoch: int | None = None
    ) -> MembershipTransition | None:
        with self._lock:
            record = self._aircraft.get(drone_id)
            if record is None or record.membership is Membership.DISCONNECTED:
                return None
            if connection_epoch is not None and record.connection_epoch != connection_epoch:
                return None
            graceful = record.membership is Membership.LEAVING
            action = (
                MembershipAction.GRACEFUL_LEAVE_COMPLETED
                if graceful
                else MembershipAction.UNEXPECTED_LOSS
            )
            reason = "graceful_leave_completed" if graceful else "adapter_connection_lost"
            record.membership = Membership.DISCONNECTED
            record.updated_at = t
            record.disconnected_at = t
            self._roster_version += 1
            self._remember(record, t=t, action=action, reason=reason)
            return self._transition(
                record,
                t=t,
                event_id=event_id,
                action=action,
                reason=reason,
                provenance="relay_transport_attestation",
            )

    def check_current(self, drone_id: int, connection_epoch: int) -> None:
        """Raise unless the aircraft joined in this epoch and is neither leaving nor lost."""
        with self._lock:
            record = self._require_current(drone_id, connection_epoch)
            if record.membership in {Membership.DISCONNECTED, Membership.LEAVING}:
                raise RegistryError(
                    "invalid_membership_transition",
                    f"node frames are not current while {record.membership.value}",
                )

    def apply_capabilities(self, frame: CapabilitiesFrame) -> None:
        """Retain the node's latest camera capabilities; readiness gates are unchanged."""
        with self._lock:
            self.check_current(frame.drone_id, frame.connection_epoch)
            self._aircraft[frame.drone_id].camera_capabilities = frame

    def apply_node_status(self, frame: NodeStatusFrame) -> None:
        """Retain the node's latest bridge health; only signed readiness changes authority."""
        with self._lock:
            self.check_current(frame.drone_id, frame.connection_epoch)
            self._aircraft[frame.drone_id].node_status = frame

    def camera_capabilities(self, drone_id: int) -> CapabilitiesFrame | None:
        with self._lock:
            record = self._aircraft.get(drone_id)
            return None if record is None else record.camera_capabilities

    def node_status(self, drone_id: int) -> NodeStatusFrame | None:
        with self._lock:
            record = self._aircraft.get(drone_id)
            return None if record is None else record.node_status

    def set_selection(self, drone_ids: tuple[int, ...]) -> None:
        if len(set(drone_ids)) != len(drone_ids) or any(item <= 0 for item in drone_ids):
            raise ValueError("selection must contain unique positive drone IDs")
        with self._lock:
            self._selection = tuple(drone_ids)

    def set_accepted_plan(self, plan: dict[str, object] | None) -> None:
        with self._lock:
            self._accepted_plan = _json_copy(plan)

    def set_pending(self, pending: dict[str, object] | None) -> None:
        with self._lock:
            self._pending = _json_copy(pending)

    def set_estop(self, value: bool) -> None:
        with self._lock:
            self._estop = value

    def set_armed(self, value: bool) -> None:
        with self._lock:
            self._armed = value

    def state_event(self, *, session: str, t: int, event_id: str) -> dict[str, object]:
        with self._lock:
            drones = [self._aircraft_state(record, t) for record in self._aircraft.values()]
            drones.sort(key=lambda drone: drone["drone_id"])
            return {
                "v": 1,
                "t": t,
                "type": "state",
                "event_id": event_id,
                "session": session,
                "roster_version": self._roster_version,
                "armed": self._armed,
                "estop": self._estop,
                "selection": list(self._selection),
                "formation": self._formation,
                "spacing": self._spacing,
                "mode": self._mode,
                "pending": _json_copy(self._pending),
                "accepted_plan": _json_copy(self._accepted_plan),
                "drones": drones,
            }

    def _require_current(self, drone_id: int, epoch: int) -> _AircraftRecord:
        record = self._aircraft.get(drone_id)
        if record is None:
            raise RegistryError("unknown_aircraft", f"drone {drone_id} has not joined")
        if record.connection_epoch != epoch:
            raise RegistryError(
                "stale_connection_epoch",
                f"epoch {epoch} is not current for drone {drone_id}",
            )
        return record

    def _readiness_reasons(self, record: _AircraftRecord, now_ms: int) -> tuple[str, ...]:
        reasons: list[str] = []
        if not record.identity_verified:
            reasons.append("identity_unverified")
        if not record.capabilities:
            reasons.append("adapter_capabilities_missing")
        elif "flight" not in record.capabilities:
            reasons.append("flight_capability_missing")
        if record.telemetry is None or record.telemetry.connection_epoch != record.connection_epoch:
            reasons.append("telemetry_missing")
        elif now_ms - record.telemetry.t > self.telemetry_freshness_ms:
            reasons.append("telemetry_stale")
        if record.home_pose is None:
            reasons.append("home_pose_missing")
        if not record.control_authority:
            reasons.append("control_authority_missing")
        if not record.rc_safety_operator_present:
            reasons.append("rc_safety_operator_missing")
        return tuple(reasons)

    @staticmethod
    def _has_current_telemetry(record: _AircraftRecord) -> bool:
        return (
            record.telemetry is not None
            and record.telemetry.connection_epoch == record.connection_epoch
        )

    def _transition(
        self,
        record: _AircraftRecord,
        *,
        t: int,
        event_id: str,
        action: MembershipAction,
        reason: str | None,
        provenance: str,
        invalidated_intent_ids: tuple[str, ...] = (),
        invalidation_reason: str | None = None,
        prior_roster_version: int | None = None,
        cleared_control_fields: tuple[str, ...] = (),
    ) -> MembershipTransition:
        readiness_reasons = self._readiness_reasons(record, t)
        if record.membership is Membership.DISCONNECTED:
            readiness_reasons = ("disconnected",)
        elif record.membership is Membership.LEAVING:
            readiness_reasons = ("leaving",)
        return MembershipTransition(
            t=t,
            event_id=event_id,
            action=action,
            drone_id=record.drone_id,
            connection_epoch=record.connection_epoch,
            membership=record.membership,
            roster_version=self._roster_version,
            reason=reason,
            readiness_reasons=readiness_reasons,
            adapter_id=record.adapter_id,
            capabilities=record.capabilities,
            provenance=provenance,
            invalidated_intent_ids=invalidated_intent_ids,
            invalidation_reason=invalidation_reason,
            prior_roster_version=prior_roster_version,
            cleared_control_fields=cleared_control_fields,
        )

    def _aircraft_state(self, record: _AircraftRecord, now_ms: int) -> dict[str, object]:
        telemetry = None if record.telemetry is None else record.telemetry.state_payload()
        camera_patterns = sorted(
            {
                capability.split(":", 1)[-1]
                for capability in record.capabilities
                if capability in _CAMERA_PATTERNS
                or (
                    capability.startswith("camera:")
                    and capability.split(":", 1)[-1] in _CAMERA_PATTERNS
                )
            }
        )
        reasons = self._readiness_reasons(record, now_ms)
        if record.membership is Membership.DISCONNECTED:
            reasons = ("disconnected",)
        elif record.membership is Membership.LEAVING:
            reasons = ("leaving",)
        flight_state = None if telemetry is None else telemetry["state"]
        battery = None if telemetry is None else telemetry["battery"]
        link = None if telemetry is None else telemetry["link"]
        pos_quality = None if telemetry is None else telemetry["pos_quality"]
        return {
            "drone_id": record.drone_id,
            "connection_epoch": record.connection_epoch,
            "membership": record.membership.value,
            "readiness_reasons": list(reasons),
            "flight_state": flight_state,
            "battery": battery,
            "link": link,
            "pos_quality": pos_quality,
            "control_authority": record.control_authority,
            "last_seen_at": None if record.telemetry is None else record.telemetry.t,
            "camera_patterns": camera_patterns,
            "selectable": record.membership is Membership.READY and not reasons,
            "adapter_id": record.adapter_id,
            "adapter_capabilities": list(record.capabilities),
            "home_pose": _json_copy(record.home_pose),
            "rc_safety_operator_present": record.rc_safety_operator_present,
            "telemetry": telemetry,
            "membership_history": _json_copy(record.history),
            "camera_capabilities": (
                None
                if record.camera_capabilities is None
                else record.camera_capabilities.state_payload()
            ),
            "node_status": (
                None if record.node_status is None else record.node_status.state_payload()
            ),
        }

    @staticmethod
    def _remember(
        record: _AircraftRecord,
        *,
        t: int,
        action: MembershipAction,
        reason: str | None,
    ) -> None:
        telemetry = record.telemetry
        record.history.append(
            {
                "t": t,
                "action": action.value,
                "membership": record.membership.value,
                "connection_epoch": record.connection_epoch,
                "reason": reason,
                "flight_state": None if telemetry is None else telemetry.state,
                "battery": None if telemetry is None else telemetry.battery,
                "link": None if telemetry is None else telemetry.link,
                "pos_quality": None if telemetry is None else telemetry.pos_quality,
            }
        )


def _json_copy(value: object) -> object:
    if value is None:
        return None
    return json.loads(json.dumps(value, allow_nan=False))


def _intent_ids(*values: dict[str, object] | None) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        intent_id = None if value is None else value.get("intent_id")
        if isinstance(intent_id, str) and intent_id and intent_id not in result:
            result.append(intent_id)
    return tuple(result)
