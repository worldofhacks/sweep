from __future__ import annotations

import json
from pathlib import Path

from relay.auth import Principal
from relay.session import RelaySession
from relay.tests.conftest import membership_payload, node_status_payload

FIXTURE = (
    Path(__file__).parents[2]
    / "console"
    / "src"
    / "relay"
    / "python-node-status-projection.fixture.json"
)


def _projected_states(session: RelaySession, principal: Principal) -> dict[str, object]:
    session.process_membership(
        membership_payload(action="join", event_id="projection-join"), principal
    )
    absent = session.process_frame(node_status_payload(event_id="projection-absent"), principal)[1]
    present = session.process_frame(
        node_status_payload(
            event_id="projection-present",
            local_height={"z_m": 0.4, "source": "flight_controller_altitude", "age_ms": 12},
        ),
        principal,
    )[1]
    return {"absent": absent, "present": present}


def test_console_node_status_projection_fixture_is_produced_by_relay(
    relay_session: RelaySession, adapter_principal: Principal
) -> None:
    assert json.loads(FIXTURE.read_text()) == _projected_states(relay_session, adapter_principal)
