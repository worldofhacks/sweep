from __future__ import annotations

import json
from pathlib import Path

from relay.audit import SessionAuditLog
from relay.auth import Principal
from relay.session import CapabilityBoundIntentSink, RelayLimits, RelaySession
from relay.supervised_vertical import SUPERVISED_VERTICAL_PROFILE
from relay.tests.conftest import (
    ADAPTER_KEY,
    SESSION,
    EventIds,
    MutableClock,
    membership_payload,
    telemetry_payload,
)

FIXTURE = (
    Path(__file__).parents[2]
    / "console"
    / "src"
    / "relay"
    / "python-supervised-vertical-readiness.fixture.json"
)


def _state(tmp_path: Path) -> dict[str, object]:
    clock = MutableClock()
    session = RelaySession(
        session_id=SESSION,
        audit_log=SessionAuditLog(tmp_path, SESSION),
        limits=RelayLimits(5_000, 5_000, 1_000, 1_000),
        clock=clock,
        event_ids=EventIds(),
        capability_profile=SUPERVISED_VERTICAL_PROFILE,
        intent_sink=CapabilityBoundIntentSink(
            lambda _intent, _state: None, SUPERVISED_VERTICAL_PROFILE
        ),
    )
    principal = Principal(source="adapter", drone_id=1, signing_key=ADAPTER_KEY)
    session.process_membership(
        membership_payload(action="join", event_id="vertical-join"), principal
    )
    telemetry = telemetry_payload(event_id="vertical-telemetry", state="landed")
    telemetry["pos_quality"] = 0.0
    session.process_telemetry(telemetry, principal)
    session.process_membership(
        membership_payload(
            action="readiness",
            event_id="vertical-readiness",
            home_pose_confirmed=False,
        ),
        principal,
    )
    return session.update_control_projection(selection=(1,), armed=True)


def test_console_supervised_vertical_readiness_fixture_is_produced_by_relay(
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)

    assert state["capability_profile"] == "supervised_vertical"
    assert state["drones"][0]["membership"] == "ready"
    assert state["drones"][0]["selectable"] is True
    assert state["drones"][0]["home_pose"] is None
    assert state["drones"][0]["node_status"] is None
    assert json.loads(FIXTURE.read_text()) == state
