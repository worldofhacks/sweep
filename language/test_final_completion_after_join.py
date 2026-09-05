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
@pytest.mark.parametrize(
    "intent_name", [IntentName.TRANSLATE, IntentName.LAND, IntentName.LAND_ALL, IntentName.HOLD]
)
def test_newcomer_join_does_not_invalidate_final_completion(
    tmp_path, monkeypatch, enrich_newcomer, intent_name
):
    degraded_land = intent_name in {IntentName.LAND, IntentName.LAND_ALL}
    degraded_target = degraded_land or intent_name is IntentName.HOLD
    snapshot = make_snapshot(
        1 if intent_name is IntentName.LAND_ALL else 2, selection=(1,), roster_version=4
    )
    if degraded_target:
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
    if intent_name is IntentName.HOLD:
        hover = flight.hover
        monkeypatch.setattr(
            flight,
            "hover",
            lambda ids: tuple(replace(ack, status=LifecycleStatus.ACCEPTED) for ack in hover(ids)),
        )
    operation = "land" if degraded_land else "hover" if intent_name is IntentName.HOLD else "goto"
    router = PreparedExecutionRouter(controller, current_snapshot=lambda: snapshot)
    relay = RelaySession(
        session_id="test-session",
        audit_log=SessionAuditLog(tmp_path, "test-session"),
        limits=RelayLimits(5_000, 5_000, 1_000, 1_000),
        clock=lambda: snapshot.now_ms,
        intent_sink=router,
    )
    _hydrate_relay_from_snapshot(relay, snapshot)
    if degraded_target:
        relay.process_membership(
            membership_payload(
                action="readiness",
                event_id="degraded-target",
                timestamp=snapshot.now_ms,
                drone_id=1,
                session=relay.session_id,
                key=b"x" * 32,
                home_pose_confirmed=False,
            ),
            Principal(source="adapter", drone_id=1, signing_key=b"x" * 32),
        )
        assert relay.current_state()["drones"][0]["membership"] == "degraded"
        snapshot = replace(snapshot, roster_version=relay.registry.roster_version)
    intent = make_intent(
        intent_name,
        selection=(1,),
        args={"dx": 1, "dy": 0} if intent_name is IntentName.TRANSLATE else {},
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
    if degraded_target:
        assert after["drones"][0]["membership"] == "degraded"
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
