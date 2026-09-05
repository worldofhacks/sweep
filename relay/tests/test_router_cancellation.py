from dataclasses import replace

from planner.models import LifecycleStatus
from relay.auth import Principal
from relay.tests.test_router_delivery import _prepared_translation


def test_failed_delivery_releases_only_its_prepared_router_entry(tmp_path, console_principal):
    relay, prepared, flight, payload = _prepared_translation(tmp_path)
    router = relay.intent_sink
    other = replace(prepared, intent=replace(prepared.intent, intent_id="other-prepared"))
    router.bind(other)
    router._submitting_sessions[prepared.intent.intent_id] = relay
    router._submitting_sessions[other.intent.intent_id] = relay
    assert relay.process_intent(payload, console_principal)[0]["status"] == "accepted"

    events = relay.fail_pending_intent(
        prepared.intent.intent_id,
        reason="acceptance_delivery_failed",
        detail="requesting socket disconnected",
    )

    assert events[-1]["reason"] == "acceptance_delivery_failed"
    assert prepared.intent.intent_id not in router._prepared
    assert prepared.intent.intent_id not in router._submitting_sessions
    assert router._prepared[other.intent.intent_id] is other
    assert router._submitting_sessions[other.intent.intent_id] is relay
    assert relay.execute_pending_intent(prepared.intent.intent_id) == []
    assert flight.calls == []
    router.cancel_intent(prepared.intent.intent_id)


def test_cancellation_preserves_router_ownership_and_terminal_ack_resume(
    tmp_path, monkeypatch, console_principal
):
    relay, prepared, flight, payload = _prepared_translation(tmp_path)
    router = relay.intent_sink
    original_goto = flight.goto

    def first_command_pending(*args):
        acknowledgement = original_goto(*args)
        return (
            replace(acknowledgement, status=LifecycleStatus.EXECUTING)
            if len(flight.calls) == 1
            else acknowledgement
        )

    monkeypatch.setattr(flight, "goto", first_command_pending)
    relay.process_intent(payload, console_principal)
    relay.mark_pending_intent_delivered(prepared.intent.intent_id)
    assert relay.execute_pending_intent(prepared.intent.intent_id)[-1]["status"] == "executing"
    router.cancel_intent(prepared.intent.intent_id)
    assert relay.current_state()["accepted_plan"] == prepared.plan.to_dict()
    command = prepared.plan.commands[0]
    events = relay.process_acknowledgement(
        {
            "v": 1,
            "t": prepared.intent.t,
            "type": "acknowledgement",
            "event_id": "completion-after-late-cancel",
            "session": prepared.intent.session,
            "intent_id": prepared.intent.intent_id,
            "command_id": command.command_id,
            "status": "completed",
            "drone_id": command.drone_id,
            "connection_epoch": command.connection_epoch,
            "roster_version": prepared.plan.roster_version,
            "reason": None,
            "detail": None,
        },
        Principal(source="adapter", drone_id=command.drone_id, signing_key=b"x" * 32),
    )
    assert len(flight.calls) == 2
    assert any(
        event.get("status") == "completed" and event.get("source") == "autonomy" for event in events
    )
    assert relay.current_state()["accepted_plan"] is None


def test_cancellation_during_dispatch_preserves_its_submitting_session(
    tmp_path, monkeypatch, console_principal
):
    relay, prepared, flight, payload = _prepared_translation(tmp_path)
    router = relay.intent_sink
    original_goto = flight.goto

    def cancel_after_io(*args):
        acknowledgement = original_goto(*args)
        router.cancel_intent(prepared.intent.intent_id)
        assert router._submitting_sessions[prepared.intent.intent_id] is relay
        return acknowledgement

    monkeypatch.setattr(flight, "goto", cancel_after_io)
    relay.process_intent(payload, console_principal)
    relay.mark_pending_intent_delivered(prepared.intent.intent_id)
    events = relay.execute_pending_intent(prepared.intent.intent_id)
    assert events[-1]["status"] == "completed"
    assert len(flight.calls) == 2
    assert prepared.intent.intent_id not in router._submitting_sessions
