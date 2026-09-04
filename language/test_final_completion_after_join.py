from dataclasses import replace

import pytest

from language.test_compiler import _hydrate_relay_from_snapshot
from planner.controller import PreparedExecutionRouter
from planner.models import LifecycleStatus, MembershipState, PreparedExecution
from relay.audit import SessionAuditLog
from relay.auth import Principal
from relay.intent_v1 import IntentName
from relay.session import RelayLimits, RelaySession
from relay.tests.conftest import membership_payload
from tests.autonomy_fixtures import (
    make_aircraft,
    make_intent,
    make_snapshot,
    make_stack,
    replace_aircraft,
)


@pytest.mark.parametrize("enrich_newcomer", [False, True])
@pytest.mark.parametrize("degraded_land", [False, True])
def test_newcomer_join_does_not_invalidate_final_completion(
    tmp_path, monkeypatch, enrich_newcomer, degraded_land
):
    snapshot = make_snapshot(2, selection=(1,), roster_version=4)
    if degraded_land:
        snapshot = replace_aircraft(snapshot, 1, membership=MembershipState.DEGRADED)
    controller, _, _, _, flight, _ = make_stack(snapshot)
    goto = flight.goto
    monkeypatch.setattr(
        flight, "goto", lambda *a, **kw: replace(goto(*a, **kw), status=LifecycleStatus.ACCEPTED)
    )
    if degraded_land:
        land = flight.land
        monkeypatch.setattr(
            flight,
            "land",
            lambda ids: tuple(replace(ack, status=LifecycleStatus.ACCEPTED) for ack in land(ids)),
        )
    operation = "land" if degraded_land else "goto"
    router = PreparedExecutionRouter(controller, current_snapshot=lambda: snapshot)
    relay = RelaySession(
        session_id="test-session",
        audit_log=SessionAuditLog(tmp_path, "test-session"),
        limits=RelayLimits(5_000, 5_000, 1_000, 1_000),
        clock=lambda: snapshot.now_ms,
        intent_sink=router,
    )
    _hydrate_relay_from_snapshot(relay, snapshot)
    intent = make_intent(
        IntentName.LAND if degraded_land else IntentName.TRANSLATE,
        selection=(1,),
        args={} if degraded_land else {"dx": 1, "dy": 0},
        confirm=degraded_land,
    )
    prepared = router.prepare(intent, snapshot)
    assert isinstance(prepared, PreparedExecution)
    router.bind(prepared)
    emitter = router.relay_emitter(
        relay, Principal(source="console", drone_id=None, signing_key=b"x" * 32)
    )
    assert emitter(intent)[-1]["status"] == "executing"
    before = relay.current_state()
    if enrich_newcomer:
        snapshot = replace(snapshot, aircraft={**snapshot.aircraft, 3: make_aircraft(3)})
    newcomer = Principal(source="adapter", drone_id=3, signing_key=b"x" * 32)
    events = relay.process_frame(
        membership_payload(
            action="join",
            event_id="benign-join",
            timestamp=snapshot.now_ms,
            drone_id=3,
            session=relay.session_id,
            key=newcomer.signing_key,
        ),
        newcomer,
    )
    after = relay.current_state()
    assert after["roster_version"] > before["roster_version"]
    assert after["accepted_plan"] == before["accepted_plan"]
    assert after["selection"] == before["selection"]
    assert [call.operation.value for call in flight.calls] == [operation]
    assert not any(event.get("status") == "invalidated" for event in events)
    first = prepared.plan.commands[0]
    events = relay.process_frame(
        {
            "v": 1,
            "type": "acknowledgement",
            "t": snapshot.now_ms,
            "event_id": "completion-after-join",
            "session": relay.session_id,
            "intent_id": intent.intent_id,
            "command_id": first.command_id,
            "status": "completed",
            "drone_id": first.drone_id,
            "connection_epoch": first.connection_epoch,
            "roster_version": first.roster_version,
            "reason": None,
            "detail": None,
        },
        Principal(source="adapter", drone_id=first.drone_id, signing_key=b"x" * 32),
    )
    assert any(
        event.get("intent_id") == intent.intent_id and event.get("status") == "completed"
        for event in events
    )
    assert (intent.intent_id in router._running) is degraded_land
    assert [call.operation.value for call in flight.calls] == [operation]
    assert not any(event.get("status") == "invalidated" for event in events)
    assert (relay.current_state()["accepted_plan"] is not None) is degraded_land
