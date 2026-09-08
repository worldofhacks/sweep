"""Swarm UI's empty-target session ARM preserves the relay ground boundary."""

from relay.audit import SessionAuditLog
from relay.auth import Principal
from relay.contracts import NodeType
from relay.session import RelayLimits, RelaySession
from relay.tests.conftest import CONSOLE_KEY, SESSION, EventIds, MutableClock, profiled_sink
from relay.tests.test_ground_node_foundation import _ground_join


def test_session_arm_has_no_ground_targets_even_when_ground_is_selected(tmp_path):
    received = []
    clock = MutableClock()
    session = RelaySession(
        session_id=SESSION,
        audit_log=SessionAuditLog(tmp_path, SESSION),
        limits=RelayLimits(5000, 5000, 1000, 1000),
        clock=clock,
        event_ids=EventIds(),
        node_types={9: NodeType.GROUND},
        intent_sink=profiled_sink(lambda intent, state: received.append((intent, state))),
    )
    session.registry.apply_join(_ground_join(9, "ground-join"))
    session.registry.set_selection((9,))
    payload = {
        "v": 1,
        "t": clock(),
        "type": "intent",
        "intent_id": "global-arm",
        "retry_of": None,
        "source": "console",
        "session": SESSION,
        "name": "arm",
        "args": {},
        "selection": [],
        "mode": "indoor",
        "confirm": False,
    }
    principal = Principal("console", None, CONSOLE_KEY)
    accepted = session.process_frame(payload, principal)
    assert accepted[0]["status"] == "accepted"
    session.execute_pending_intent("global-arm")
    assert len(received) == 1
    assert received[0][0].selection == ()
    assert received[0][1]["selection"] == [9]
    refused = session.process_frame(
        {**payload, "intent_id": "targeted-arm", "selection": [9]}, principal
    )
    assert refused[0]["reason"] == "ground_intent_not_supported"
    assert len(received) == 1
