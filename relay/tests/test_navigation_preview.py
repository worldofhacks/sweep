from dataclasses import replace

import pytest

from planner.models import LifecycleStatus
from planner.test_navigation_runtime import stack
from relay.audit import SessionAuditLog
from relay.auth import Principal
from relay.autonomy import AutonomyComposition, AutonomyConfig
from relay.session import RelayLimits, RelaySession
from relay.tests.conftest import (
    ADAPTER_KEY,
    CONSOLE_KEY,
    SESSION,
    EventIds,
    MutableClock,
    membership_payload,
    telemetry_payload,
)
from relay.tests.test_autonomy import _intent
from tests.autonomy_fixtures import planning_config, safety_config


@pytest.fixture
def preview_session(tmp_path):
    controller, dispatcher, flight, snapshot, current, maps, _ = stack()
    runtime = controller.planner.navigation
    composition = AutonomyComposition(
        AutonomyConfig(
            planning=planning_config(),
            safety=safety_config(),
            navigation=runtime,
        )
    )
    clock = MutableClock()
    owner = composition.session(SESSION)
    session = RelaySession(
        session_id=SESSION,
        limits=RelayLimits(5000, 5000, 1000, 1000),
        audit_log=SessionAuditLog(tmp_path, SESSION),
        clock=clock,
        event_ids=EventIds(),
        intent_sink=owner,
        capability_profile=composition.capability_profile,
    )
    principal = Principal(source="adapter", drone_id=1, signing_key=ADAPTER_KEY)
    session.process_membership(membership_payload(action="join", event_id="join"), principal)
    session.process_telemetry(
        {**telemetry_payload(event_id="pose", state="hovering"), "x": 0.5, "y": 1.5, "z": 1.0},
        principal,
    )
    session.process_membership(membership_payload(action="readiness", event_id="ready"), principal)
    session.update_control_projection(selection=(1,), armed=True)
    console = Principal(source="console", drone_id=None, signing_key=CONSOLE_KEY)
    yield session, owner, console, clock, maps, controller, flight, current
    composition.close()


def draft():
    return _intent("navigate", intent_id="nav-1", selection=[1], args={"zone_id": "atrium"})


def preview(session, console, intent=None):
    return session.process_frame(
        {
            "v": 1,
            "type": "navigation_preview_request",
            "intent": draft() if intent is None else intent,
        },
        console,
    )[0]


def test_preview_has_real_route_and_emits_no_command(preview_session):
    session, owner, console, _, _, _, flight, _ = preview_session
    response = preview(session, console)
    assert response["type"] == "navigation_preview", response
    assert response["plan"]["navigation"]["route"]["routes"]
    assert response["expires_at_ms"] > response["t"]
    assert not flight.calls
    assert session.current_state()["navigation"]["zones"][0]["arrival_slots"] == ["atrium-1"]
    events = [record["event"] for record in session.replay()["events"]]
    assert not [event for event in events if event["type"] == "command"]
    assert "nav-1" in owner._navigation_previews


@pytest.mark.parametrize("change", ["no_preview", "expired", "destination", "selection", "map"])
def test_confirmation_drift_refuses_before_acceptance(preview_session, change):
    session, _, console, clock, maps, _, flight, _ = preview_session
    if change != "no_preview":
        assert preview(session, console)["type"] == "navigation_preview"
    intent = {**draft(), "confirm": True}
    if change == "expired":
        clock.advance(15_001)
        intent["t"] = clock.value
    elif change == "destination":
        intent["args"] = {"zone_id": "lobby"}
    elif change == "selection":
        session.update_control_projection(selection=())
    elif change == "map":
        maps[0] = replace(maps[0], map_pin=replace(maps[0].map_pin, version="changed"))
    result = session.process_frame(intent, console)[0]
    assert result["type"] == "refusal", result
    assert result["reason"] == "navigation_preview_required"
    assert not flight.calls


def test_confirmation_accepts_the_same_cached_plan(preview_session):
    session, owner, console, _, _, controller, flight, current = preview_session
    response = preview(session, console)
    result = session.process_frame({**draft(), "confirm": True}, console)[0]
    assert result["status"] == "accepted", result
    prepared = owner._navigation_previews["nav-1"][1]
    assert prepared.plan.to_dict() == response["plan"]

    # Align the independent simulator's roster and clock with the authenticated session.
    def live():
        value = current()
        return replace(
            value,
            now_ms=prepared.snapshot.now_ms + value.now_ms,
            roster_version=prepared.snapshot.roster_version,
            aircraft={
                key: replace(
                    aircraft,
                    position_last_seen_ms=prepared.snapshot.now_ms + value.now_ms,
                    link_last_seen_ms=prepared.snapshot.now_ms + value.now_ms,
                )
                for key, aircraft in value.aircraft.items()
            },
            operator_last_seen_ms=prepared.snapshot.now_ms + value.now_ms,
        )

    result = controller.dispatch_prepared(prepared, current_snapshot=live)
    assert result.status is LifecycleStatus.COMPLETED, result.refusal
    assert result.plan.to_dict() == response["plan"]
    assert flight.aircraft[1].pose.x == 6.5


def test_adapter_cannot_request_preview(preview_session):
    session, _, _, _, _, _, _, _ = preview_session
    adapter = Principal(source="adapter", drone_id=1, signing_key=ADAPTER_KEY)
    result = preview(session, adapter)
    assert result["type"] == "refusal"
    assert result["reason"] == "frame_not_allowed"


def test_unconfirmed_draft_can_preview_but_cannot_dispatch(preview_session):
    session, owner, console, _, _, _, _, _ = preview_session
    unconfirmed = {**draft(), "confirm": False}
    response = preview(session, console, unconfirmed)
    assert response["type"] == "navigation_preview", response
    refused = session.process_frame(unconfirmed, console)
    assert any(event.get("type") == "refusal" for event in refused)
    assert "confirmation" in str(refused)
    assert "nav-1" in owner._navigation_previews
