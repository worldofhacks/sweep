"""Executable relay wire contracts.

The autonomy layer owns plans, commands, and safety decisions.  This module owns
only the transport envelopes that carry their outcomes through the relay.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal


class ContractError(ValueError):
    """A typed failure while decoding an untrusted relay frame."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


class Membership(StrEnum):
    REGISTERED = "registered"
    READY = "ready"
    LEAVING = "leaving"
    DISCONNECTED = "disconnected"
    DEGRADED = "degraded"


class MembershipAction(StrEnum):
    JOIN = "join"
    READINESS = "readiness"
    GRACEFUL_LEAVE = "graceful_leave"
    GRACEFUL_LEAVE_COMPLETED = "graceful_leave_completed"
    UNEXPECTED_LOSS = "unexpected_loss"
    TELEMETRY_STALE = "telemetry_stale"
    TELEMETRY_RECOVERED = "telemetry_recovered"


class LifecycleStatus(StrEnum):
    ACCEPTED = "accepted"
    REFUSED = "refused"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"
    INVALIDATED = "invalidated"


WIRE_MEMBERSHIP_ACTIONS = frozenset(
    {
        MembershipAction.JOIN,
        MembershipAction.READINESS,
        MembershipAction.GRACEFUL_LEAVE,
    }
)


@dataclass(frozen=True, slots=True)
class MembershipRequest:
    v: Literal[1]
    t: int
    type: Literal["membership"]
    event_id: str
    session: str
    drone_id: int
    action: MembershipAction
    signature: str
    connection_epoch: int | None = None
    adapter_id: str | None = None
    capabilities: tuple[str, ...] = ()
    home_pose_confirmed: bool | None = None
    control_authority: bool | None = None
    rc_safety_operator_present: bool | None = None

    def unsigned_event(self) -> dict[str, object]:
        event: dict[str, object] = {
            "v": self.v,
            "t": self.t,
            "type": self.type,
            "event_id": self.event_id,
            "session": self.session,
            "drone_id": self.drone_id,
            "action": self.action.value,
        }
        if self.action is MembershipAction.JOIN:
            event.update(
                adapter_id=self.adapter_id,
                capabilities=list(self.capabilities),
            )
        elif self.action is MembershipAction.READINESS:
            event.update(
                connection_epoch=self.connection_epoch,
                home_pose_confirmed=self.home_pose_confirmed,
                control_authority=self.control_authority,
                rc_safety_operator_present=self.rc_safety_operator_present,
            )
        elif self.action is MembershipAction.GRACEFUL_LEAVE:
            event["connection_epoch"] = self.connection_epoch
        return event


@dataclass(frozen=True, slots=True)
class TelemetryV1:
    v: Literal[1]
    t: int
    type: Literal["telemetry"]
    event_id: str
    session: str
    drone: int
    connection_epoch: int
    x: float
    y: float
    z: float
    vx: float
    vy: float
    vz: float
    heading_deg: float
    battery: float
    state: str
    link: float
    pos_quality: float

    def to_event(self) -> dict[str, object]:
        return {
            "v": self.v,
            "t": self.t,
            "type": self.type,
            "event_id": self.event_id,
            "session": self.session,
            "drone": self.drone,
            "connection_epoch": self.connection_epoch,
            "x": self.x,
            "y": self.y,
            "z": self.z,
            "vx": self.vx,
            "vy": self.vy,
            "vz": self.vz,
            "heading_deg": self.heading_deg,
            "battery": self.battery,
            "state": self.state,
            "link": self.link,
            "pos_quality": self.pos_quality,
        }

    def state_payload(self) -> dict[str, object]:
        """Return Appendix B telemetry without transport-only fields."""
        return {
            "v": self.v,
            "t": self.t,
            "type": self.type,
            "drone": self.drone,
            "x": self.x,
            "y": self.y,
            "z": self.z,
            "vx": self.vx,
            "vy": self.vy,
            "vz": self.vz,
            "heading_deg": self.heading_deg,
            "battery": self.battery,
            "state": self.state,
            "link": self.link,
            "pos_quality": self.pos_quality,
        }


@dataclass(frozen=True, slots=True)
class AdapterAcknowledgement:
    v: Literal[1]
    t: int
    type: Literal["acknowledgement"]
    event_id: str
    session: str
    intent_id: str
    command_id: str
    status: LifecycleStatus
    drone_id: int
    connection_epoch: int
    roster_version: int
    reason: str | None
    detail: str | None

    def to_event(self) -> dict[str, object]:
        return {
            "v": self.v,
            "t": self.t,
            "type": self.type,
            "event_id": self.event_id,
            "session": self.session,
            "intent_id": self.intent_id,
            "command_id": self.command_id,
            "status": self.status.value,
            "source": "adapter",
            "drone_id": self.drone_id,
            "connection_epoch": self.connection_epoch,
            "roster_version": self.roster_version,
            "reason": self.reason,
            "detail": self.detail,
        }


def parse_membership_request(raw: object) -> MembershipRequest:
    value = _mapping(raw, "invalid_membership", "membership frame must be an object")
    action_value = value.get("action")
    try:
        action = MembershipAction(action_value)
    except (TypeError, ValueError):
        raise ContractError("invalid_membership", "unknown membership action") from None
    if action not in WIRE_MEMBERSHIP_ACTIONS:
        raise ContractError("invalid_membership", "membership action is relay-internal")

    common = {
        "v",
        "t",
        "type",
        "event_id",
        "session",
        "drone_id",
        "action",
        "signature",
    }
    action_fields = {
        MembershipAction.JOIN: {"adapter_id", "capabilities"},
        MembershipAction.READINESS: {
            "connection_epoch",
            "home_pose_confirmed",
            "control_authority",
            "rc_safety_operator_present",
        },
        MembershipAction.GRACEFUL_LEAVE: {"connection_epoch"},
    }
    _exact_fields(value, common | action_fields[action], "invalid_membership")
    _common_envelope(value, expected_type="membership", code="invalid_membership")

    drone_id = _positive_int(value["drone_id"], "drone_id", "invalid_membership")
    signature = _nonempty_string(value["signature"], "signature", "invalid_signature")

    if action is MembershipAction.JOIN:
        adapter_id = _nonempty_string(value["adapter_id"], "adapter_id", "invalid_membership")
        capabilities = _string_list(value["capabilities"], "capabilities", allow_empty=False)
        return MembershipRequest(
            1,
            value["t"],
            "membership",
            value["event_id"],
            value["session"],
            drone_id,
            action,
            signature,
            adapter_id=adapter_id,
            capabilities=capabilities,
        )

    connection_epoch = _positive_int(
        value["connection_epoch"], "connection_epoch", "invalid_membership"
    )
    if action is MembershipAction.READINESS:
        for field in (
            "home_pose_confirmed",
            "control_authority",
            "rc_safety_operator_present",
        ):
            if not isinstance(value[field], bool):
                raise ContractError("invalid_membership", f"{field} must be a boolean")
        return MembershipRequest(
            1,
            value["t"],
            "membership",
            value["event_id"],
            value["session"],
            drone_id,
            action,
            signature,
            connection_epoch=connection_epoch,
            home_pose_confirmed=value["home_pose_confirmed"],
            control_authority=value["control_authority"],
            rc_safety_operator_present=value["rc_safety_operator_present"],
        )

    return MembershipRequest(
        1,
        value["t"],
        "membership",
        value["event_id"],
        value["session"],
        drone_id,
        action,
        signature,
        connection_epoch=connection_epoch,
    )


def parse_telemetry(raw: object) -> TelemetryV1:
    value = _mapping(raw, "invalid_telemetry", "telemetry frame must be an object")
    fields = {
        "v",
        "t",
        "type",
        "event_id",
        "session",
        "drone",
        "connection_epoch",
        "x",
        "y",
        "z",
        "vx",
        "vy",
        "vz",
        "heading_deg",
        "battery",
        "state",
        "link",
        "pos_quality",
    }
    _exact_fields(value, fields, "invalid_telemetry")
    _common_envelope(value, expected_type="telemetry", code="invalid_telemetry")
    values = {
        field: _finite_number(value[field], field, "invalid_telemetry")
        for field in (
            "x",
            "y",
            "z",
            "vx",
            "vy",
            "vz",
            "heading_deg",
            "battery",
            "link",
            "pos_quality",
        )
    }
    if not 0 <= values["heading_deg"] < 360:
        raise ContractError("invalid_telemetry", "heading_deg must be in [0, 360)")
    for field in ("battery", "link", "pos_quality"):
        if not 0 <= values[field] <= 1:
            raise ContractError("invalid_telemetry", f"{field} must be between 0 and 1")
    return TelemetryV1(
        1,
        value["t"],
        "telemetry",
        value["event_id"],
        value["session"],
        _positive_int(value["drone"], "drone", "invalid_telemetry"),
        _positive_int(value["connection_epoch"], "connection_epoch", "invalid_telemetry"),
        values["x"],
        values["y"],
        values["z"],
        values["vx"],
        values["vy"],
        values["vz"],
        values["heading_deg"],
        values["battery"],
        _nonempty_string(value["state"], "state", "invalid_telemetry"),
        values["link"],
        values["pos_quality"],
    )


def parse_adapter_acknowledgement(raw: object) -> AdapterAcknowledgement:
    value = _mapping(raw, "invalid_acknowledgement", "acknowledgement must be an object")
    fields = {
        "v",
        "t",
        "type",
        "event_id",
        "session",
        "intent_id",
        "command_id",
        "status",
        "drone_id",
        "connection_epoch",
        "roster_version",
        "reason",
        "detail",
    }
    _exact_fields(value, fields, "invalid_acknowledgement")
    _common_envelope(value, expected_type="acknowledgement", code="invalid_acknowledgement")
    try:
        status = LifecycleStatus(value["status"])
    except (TypeError, ValueError):
        raise ContractError("invalid_acknowledgement", "unknown lifecycle status") from None
    if status is LifecycleStatus.REFUSED:
        raise ContractError("invalid_acknowledgement", "refused outcomes use the refusal envelope")
    reason = _nullable_string(value["reason"], "reason", machine_readable=True)
    detail = _nullable_string(value["detail"], "detail")
    command_id = _nonempty_string(value["command_id"], "command_id", "invalid_acknowledgement")
    if status in {LifecycleStatus.FAILED, LifecycleStatus.INVALIDATED} and reason is None:
        raise ContractError("invalid_acknowledgement", "terminal failure requires a reason")
    return AdapterAcknowledgement(
        1,
        value["t"],
        "acknowledgement",
        value["event_id"],
        value["session"],
        _nonempty_string(value["intent_id"], "intent_id", "invalid_acknowledgement"),
        command_id,
        status,
        _positive_int(value["drone_id"], "drone_id", "invalid_acknowledgement"),
        _positive_int(value["connection_epoch"], "connection_epoch", "invalid_acknowledgement"),
        _nonnegative_int(value["roster_version"], "roster_version", "invalid_acknowledgement"),
        reason,
        detail,
    )


def acknowledgement_event(
    *,
    t: int,
    event_id: str,
    session: str,
    intent_id: str,
    command_id: str | None = None,
    status: LifecycleStatus,
    roster_version: int,
    source: str = "relay",
    drone_id: int | None = None,
    connection_epoch: int | None = None,
    reason: str | None = None,
    detail: str | None = None,
) -> dict[str, object]:
    if status is LifecycleStatus.REFUSED:
        raise ValueError("refused outcomes use refusal_event")
    if status in {LifecycleStatus.FAILED, LifecycleStatus.INVALIDATED} and reason is None:
        raise ValueError("failed or invalidated acknowledgements require a reason")
    if reason is not None and not _is_machine_code(reason):
        raise ValueError("acknowledgement reason must be snake_case")
    return {
        "v": 1,
        "t": t,
        "type": "acknowledgement",
        "event_id": event_id,
        "session": session,
        "intent_id": intent_id,
        "command_id": command_id,
        "status": status.value,
        "source": source,
        "drone_id": drone_id,
        "connection_epoch": connection_epoch,
        "roster_version": roster_version,
        "reason": reason,
        "detail": detail,
    }


def refusal_event(
    *,
    t: int,
    event_id: str,
    session: str,
    intent_id: str | None,
    command_id: str | None = None,
    reason: str,
    detail: str,
    roster_version: int,
    source: str = "relay",
    drone_id: int | None = None,
    connection_epoch: int | None = None,
) -> dict[str, object]:
    if not _is_machine_code(reason):
        raise ValueError("refusal reason must be snake_case")
    return {
        "v": 1,
        "t": t,
        "type": "refusal",
        "event_id": event_id,
        "session": session,
        "intent_id": intent_id,
        "command_id": command_id,
        "status": LifecycleStatus.REFUSED.value,
        "source": source,
        "drone_id": drone_id,
        "connection_epoch": connection_epoch,
        "roster_version": roster_version,
        "reason": reason,
        "detail": detail,
    }


def _mapping(raw: object, code: str, detail: str) -> Mapping[str, object]:
    if not isinstance(raw, Mapping) or not all(isinstance(key, str) for key in raw):
        raise ContractError(code, detail)
    return raw


def _exact_fields(value: Mapping[str, object], fields: set[str], code: str) -> None:
    if set(value) != fields:
        raise ContractError(code, "frame fields do not match the v1 contract")


def _common_envelope(value: Mapping[str, object], *, expected_type: str, code: str) -> None:
    if value["v"] != 1 or isinstance(value["v"], bool) or not isinstance(value["v"], int):
        raise ContractError(code, "v must be integer 1")
    _nonnegative_int(value["t"], "t", code)
    if value["type"] != expected_type:
        raise ContractError(code, f"type must be {expected_type}")
    _nonempty_string(value["event_id"], "event_id", code)
    _nonempty_string(value["session"], "session", code)


def _nonempty_string(value: object, field: str, code: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise ContractError(code, f"{field} must be a non-empty string of at most 512 chars")
    return value


def _nullable_string(value: object, field: str, *, machine_readable: bool = False) -> str | None:
    if value is None:
        return None
    result = _nonempty_string(value, field, "invalid_acknowledgement")
    if machine_readable and not _is_machine_code(result):
        raise ContractError("invalid_acknowledgement", f"{field} must be snake_case")
    return result


def _nonnegative_int(value: object, field: str, code: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ContractError(code, f"{field} must be a non-negative integer")
    return value


def _positive_int(value: object, field: str, code: str) -> int:
    result = _nonnegative_int(value, field, code)
    if result == 0:
        raise ContractError(code, f"{field} must be a positive integer")
    return result


def _finite_number(value: object, field: str, code: str) -> float:
    if not isinstance(value, int | float) or isinstance(value, bool) or not math.isfinite(value):
        raise ContractError(code, f"{field} must be a finite number")
    return float(value)


def _string_list(value: object, field: str, *, allow_empty: bool) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise ContractError("invalid_membership", f"{field} must be a list")
    result = tuple(_nonempty_string(item, field, "invalid_membership") for item in value)
    if not result and not allow_empty:
        raise ContractError("invalid_membership", f"{field} may not be empty")
    if len(set(result)) != len(result):
        raise ContractError("invalid_membership", f"{field} may not contain duplicates")
    return result


def _is_machine_code(value: str) -> bool:
    return bool(value) and all(
        "a" <= char <= "z" or "0" <= char <= "9" or char == "_" for char in value
    )
