from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from relay.audit import SessionAuditLog
from relay.auth import Principal, sign_event
from relay.session import RelayLimits, RelaySession

SESSION = "session-test"
CONSOLE_KEY = b"console-key-that-is-at-least-32-bytes"
ADAPTER_KEY = b"adapter-one-key-that-is-at-least-32"


@dataclass(slots=True)
class MutableClock:
    value: int = 1_756_700_000_000

    def __call__(self) -> int:
        return self.value

    def advance(self, milliseconds: int) -> None:
        self.value += milliseconds


class EventIds:
    def __init__(self) -> None:
        self.value = 0

    def __call__(self) -> str:
        self.value += 1
        return f"server-event-{self.value}"


@pytest.fixture
def clock() -> MutableClock:
    return MutableClock()


@pytest.fixture
def event_ids() -> EventIds:
    return EventIds()


@pytest.fixture
def relay_session(tmp_path: Path, clock: MutableClock, event_ids: EventIds) -> RelaySession:
    return RelaySession(
        session_id=SESSION,
        audit_log=SessionAuditLog(tmp_path, SESSION),
        limits=RelayLimits(
            intent_max_age_ms=5_000,
            transport_event_max_age_ms=5_000,
            future_clock_skew_ms=1_000,
            telemetry_freshness_ms=1_000,
        ),
        clock=clock,
        event_ids=event_ids,
        intent_sink=lambda _intent, _state: None,
    )


@pytest.fixture
def console_principal() -> Principal:
    return Principal(source="console", drone_id=None, signing_key=CONSOLE_KEY)


@pytest.fixture
def keyboard_principal() -> Principal:
    return Principal(source="keyboard", drone_id=None, signing_key=CONSOLE_KEY)


@pytest.fixture
def adapter_principal() -> Principal:
    return Principal(source="adapter", drone_id=1, signing_key=ADAPTER_KEY)


def intent_payload(
    *,
    timestamp: int = 1_756_700_000_000,
    intent_id: str = "intent-1",
    source: str = "console",
    session: str = SESSION,
    retry_of: str | None = None,
) -> dict[str, object]:
    return {
        "v": 1,
        "t": timestamp,
        "type": "intent",
        "intent_id": intent_id,
        "retry_of": retry_of,
        "source": source,
        "session": session,
        "name": "hold",
        "args": {},
        "selection": [1],
        "mode": "indoor",
        "confirm": False,
    }


def membership_payload(
    *,
    action: str,
    event_id: str,
    timestamp: int = 1_756_700_000_000,
    drone_id: int = 1,
    session: str = SESSION,
    connection_epoch: int = 1,
    key: bytes = ADAPTER_KEY,
    **overrides: object,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "v": 1,
        "t": timestamp,
        "type": "membership",
        "event_id": event_id,
        "session": session,
        "drone_id": drone_id,
        "action": action,
    }
    if action == "join":
        payload.update(adapter_id=f"adapter-{drone_id}", capabilities=["flight", "pano_360"])
    elif action == "readiness":
        payload.update(
            connection_epoch=connection_epoch,
            home_pose_confirmed=True,
            control_authority=True,
            rc_safety_operator_present=True,
        )
    elif action == "graceful_leave":
        payload["connection_epoch"] = connection_epoch
    payload.update(overrides)
    payload["signature"] = sign_event(payload, key)
    return payload


def telemetry_payload(
    *,
    event_id: str,
    timestamp: int = 1_756_700_000_000,
    drone_id: int = 1,
    session: str = SESSION,
    connection_epoch: int = 1,
    state: str = "hovering",
) -> dict[str, object]:
    return {
        "v": 1,
        "t": timestamp,
        "type": "telemetry",
        "event_id": event_id,
        "session": session,
        "drone": drone_id,
        "connection_epoch": connection_epoch,
        "x": 1.0,
        "y": 2.0,
        "z": 0.5,
        "vx": 0.0,
        "vy": 0.0,
        "vz": 0.0,
        "heading_deg": 0.0,
        "battery": 0.8,
        "state": state,
        "link": 0.9,
        "pos_quality": 0.95,
    }


def acknowledgement_payload(
    *,
    event_id: str,
    timestamp: int = 1_756_700_000_000,
    intent_id: str = "intent-1",
    command_id: str | None = "command-1",
    drone_id: int = 1,
    connection_epoch: int = 1,
    roster_version: int = 1,
    status: str = "executing",
    reason: str | None = None,
    detail: str | None = None,
) -> dict[str, object]:
    return {
        "v": 1,
        "t": timestamp,
        "type": "acknowledgement",
        "event_id": event_id,
        "session": SESSION,
        "intent_id": intent_id,
        "command_id": command_id,
        "status": status,
        "drone_id": drone_id,
        "connection_epoch": connection_epoch,
        "roster_version": roster_version,
        "reason": reason,
        "detail": detail,
    }
