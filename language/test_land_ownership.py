from dataclasses import replace

import pytest

from evals.language_corpus import StaticResponseTransport
from language.compiler import (
    ConfirmationError,
    ConfirmedPlan,
    InMemoryAuditSink,
    TranscriptCompiler,
)
from language.test_compiler import _hydrate_relay_from_snapshot
from planner.controller import PreparedExecutionRouter
from planner.models import LifecycleStatus
from relay.audit import SessionAuditLog
from relay.auth import Principal
from relay.session import RelayLimits, RelaySession
from tests.autonomy_fixtures import make_snapshot, make_stack, planning_config


@pytest.fixture
def landing_session(tmp_path):
    current = [make_snapshot(1)]
    controller, _, _, _, flight, _ = make_stack(current[0])
    router = PreparedExecutionRouter(controller, current_snapshot=lambda: current[0])
    relay = RelaySession(
        session_id="language-eval",
        audit_log=SessionAuditLog(tmp_path, "language-eval"),
        limits=RelayLimits(5000, 5000, 1000, 1000),
        clock=lambda: current[0].now_ms,
        intent_sink=router,
    )
    _hydrate_relay_from_snapshot(relay, current[0])
    return current, flight, router, relay


def _compiled_command(current, router, relay, name, intent_id):
    _, compiled = TranscriptCompiler(
        StaticResponseTransport(
            {
                "kind": "plan",
                "intents": [
                    {
                        "name": name,
                        "args": {"dx": 1, "dy": 0} if name == "translate" else {},
                        "selection": [1],
                        "mode": "indoor",
                    }
                ],
            }
        ),
        audit=InMemoryAuditSink(),
    ).compile(
        "Move right." if name == "translate" else "Land drone one.",
        relay.current_state(),
        capability_version="test",
        rooms=(),
        translation=planning_config().translation_grounding(current[0]),
        now_ms=current[0].now_ms,
    )
    assert compiled is not None
    pending = ConfirmedPlan(compiled, session=relay.session_id, audit=InMemoryAuditSink())
    prepared = pending.prepare_next(
        relay.current_state(),
        capability_version="test",
        rooms=(),
        now_ms=current[0].now_ms,
        intent_id=intent_id,
        router=router,
        snapshot=current[0],
    )
    pending.confirm_next(
        relay.current_state(),
        capability_version="test",
        rooms=(),
        now_ms=current[0].now_ms,
        intent_id=intent_id,
        emit=router.relay_emitter(
            relay, Principal(source="console", drone_id=None, signing_key=b"x" * 32)
        ),
        prepared=prepared,
    )
    return prepared


def _ack(relay, current, command, status):
    return relay.process_frame(
        {
            "v": 1,
            "t": current[0].now_ms,
            "type": "acknowledgement",
            "event_id": f"{command.command_id}-{status}",
            "session": relay.session_id,
            "intent_id": command.intent_id,
            "command_id": command.command_id,
            "status": status,
            "drone_id": command.drone_id,
            "connection_epoch": command.connection_epoch,
            "roster_version": command.roster_version,
            "reason": "adapter_failure" if status == "failed" else None,
            "detail": None,
        },
        Principal(source="adapter", drone_id=command.drone_id, signing_key=b"x" * 32),
    )


def _attempt_fresh_motion(current, router, relay, delay_ms=501):
    current[0] = replace(current[0], now_ms=current[0].now_ms + delay_ms)
    with pytest.raises(ConfirmationError, match="relay returned terminal status refused"):
        _compiled_command(current, router, relay, "translate", "independent-motion")


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("delay_ms", [1, 501])
def test_completed_land_owns_aircraft_until_landed_telemetry(
    landing_session, monkeypatch, asynchronous, delay_ms
):
    current, flight, router, relay = landing_session
    if asynchronous:
        land = flight.land
        monkeypatch.setattr(
            flight,
            "land",
            lambda ids: tuple(replace(ack, status=LifecycleStatus.ACCEPTED) for ack in land(ids)),
        )
    prepared = _compiled_command(current, router, relay, "land", "landing")
    if asynchronous:
        events = _ack(relay, current, prepared.execution.plan.commands[0], "completed")
        assert any(
            event.get("source") == "autonomy" and event["status"] == "completed" for event in events
        )
    assert router.completion_pending("landing")
    assert "landing" in router._running
    assert relay.current_state()["accepted_plan"]["intent_id"] == "landing"

    _attempt_fresh_motion(current, router, relay, delay_ms)

    assert [call.operation.value for call in flight.calls] == ["land"]
    assert "landing" in router._running
    assert relay.current_state()["accepted_plan"]["intent_id"] == "landing"
    telemetry = relay.current_state()["drones"][0]["telemetry"]
    events = relay.process_frame(
        {
            **telemetry,
            "v": 1,
            "t": current[0].now_ms,
            "type": "telemetry",
            "session": relay.session_id,
            "drone": 1,
            "connection_epoch": 1,
            "event_id": "landed-evidence",
            "state": "landed",
            "z": 0.0,
        },
        Principal(source="adapter", drone_id=1, signing_key=b"x" * 32),
    )
    assert not any(event["type"] == "refusal" for event in events)
    assert not router.completion_pending("landing")
    assert "landing" not in router._running
    assert relay.current_state()["accepted_plan"] is None


def test_failed_authenticated_land_keeps_registered_safety_hold_owned(landing_session, monkeypatch):
    current, flight, router, relay = landing_session
    land, hover = flight.land, flight.hover
    monkeypatch.setattr(
        flight,
        "land",
        lambda ids: tuple(replace(ack, status=LifecycleStatus.ACCEPTED) for ack in land(ids)),
    )
    monkeypatch.setattr(
        flight,
        "hover",
        lambda ids: tuple(replace(ack, status=LifecycleStatus.ACCEPTED) for ack in hover(ids)),
    )
    prepared = _compiled_command(current, router, relay, "land", "failed-landing")
    events = _ack(relay, current, prepared.execution.plan.commands[0], "failed")
    assert any(
        event.get("source") == "autonomy" and event["status"] == "failed" for event in events
    )
    assert [call.operation.value for call in flight.calls] == ["land", "hover"]
    safety_id = relay.current_state()["accepted_plan"]["intent_id"]
    assert safety_id != "failed-landing"
    assert safety_id in router._running

    _attempt_fresh_motion(current, router, relay)

    assert [call.operation.value for call in flight.calls] == ["land", "hover"]
    assert relay.current_state()["accepted_plan"]["intent_id"] == safety_id
    safety, _, _ = router._running[safety_id]
    events = _ack(relay, current, safety.plan.commands[0], "completed")
    assert not any(event.get("reason") == "unknown_intent_id" for event in events)


@pytest.mark.parametrize("asynchronous", [False, True])
def test_hold_retires_landing_that_is_waiting_for_telemetry(
    landing_session, monkeypatch, asynchronous
):
    current, flight, router, relay = landing_session
    if asynchronous:
        land = flight.land
        monkeypatch.setattr(
            flight,
            "land",
            lambda ids: tuple(replace(ack, status=LifecycleStatus.ACCEPTED) for ack in land(ids)),
        )
    prepared = _compiled_command(current, router, relay, "land", "landing")
    command = prepared.execution.plan.commands[0]
    if asynchronous:
        _ack(relay, current, command, "completed")
    assert router.completion_pending("landing")
    assert relay.current_state()["accepted_plan"]["intent_id"] == "landing"

    _compiled_command(current, router, relay, "hold", "superseding-hold")

    assert [call.operation.value for call in flight.calls] == ["land", "hover"]
    assert "landing" not in router._running
    assert not router.completion_pending("landing")
    assert relay.current_state()["accepted_plan"] is None
    current[0] = replace(current[0], now_ms=current[0].now_ms + 1)
    _ack(relay, current, command, "completed")
    assert [call.operation.value for call in flight.calls] == ["land", "hover"]
    assert "landing" not in router._running
    assert not router.completion_pending("landing")
    assert relay.current_state()["accepted_plan"] is None
