from dataclasses import replace

import pytest

from language.test_land_ownership import _ack, _compiled_command
from language.test_land_ownership import landing_session as landing_session
from planner.models import LifecycleStatus
from relay.auth import Principal
from relay.tests.test_router_delivery import _prepared_translation


def _stop(relay, timestamp):
    intent_id = "stop-before-resume"
    payload = {
        "v": 1,
        "t": timestamp,
        "type": "intent",
        "intent_id": intent_id,
        "retry_of": None,
        "source": "console",
        "session": relay.session_id,
        "name": "estop",
        "args": {},
        "selection": [],
        "mode": "indoor",
        "confirm": True,
    }
    principal = Principal(source="console", drone_id=None, signing_key=b"x" * 32)
    assert relay.process_intent(payload, principal)[0]["status"] == "accepted"
    relay.mark_pending_intent_delivered(intent_id)
    events = relay.execute_pending_intent(intent_id)
    assert any(event.get("status") == "completed" for event in events)


def test_estop_after_ack_validation_makes_resume_a_benign_noop(
    tmp_path, monkeypatch, console_principal
):
    relay, prepared, flight, payload = _prepared_translation(tmp_path)
    router = relay.intent_sink
    goto = flight.goto
    monkeypatch.setattr(
        flight, "goto", lambda *args: replace(goto(*args), status=LifecycleStatus.EXECUTING)
    )
    relay.process_intent(payload, console_principal)
    relay.mark_pending_intent_delivered(prepared.intent.intent_id)
    relay.execute_pending_intent(prepared.intent.intent_id)
    boundary = "resume_io" if hasattr(router, "resume_io") else "resume"
    resume_io = getattr(router, boundary)

    def stop_before_resume_io(*args, **kwargs):
        _stop(relay, prepared.intent.t)
        return resume_io(*args, **kwargs)

    monkeypatch.setattr(router, boundary, stop_before_resume_io)
    _ack(relay, [prepared.snapshot], prepared.plan.commands[0], "completed")
    assert [call.operation.value for call in flight.calls] == ["goto", "estop"]
    assert relay.current_state()["estop"] is True
    assert relay.current_state()["accepted_plan"] is None
    relay.replay()


@pytest.mark.parametrize("landing_session", [2], indirect=True)
def test_estop_before_landing_resume_io_cannot_restore_retired_fences(landing_session, monkeypatch):
    current, flight, router, relay = landing_session
    land = flight.land
    monkeypatch.setattr(
        flight,
        "land",
        lambda ids: tuple(replace(ack, status=LifecycleStatus.ACCEPTED) for ack in land(ids)),
    )
    prepared = _compiled_command(current, router, relay, "land_all", "landing-before-stop")

    boundary = "resume_io" if hasattr(router, "resume_io") else "resume"
    resume_io = getattr(router, boundary)
    captured = []

    def stop_before_resume_io(token, *args, **kwargs):
        captured.append(getattr(token, "intent_id", token))
        _stop(relay, current[0].now_ms)
        return resume_io(token, *args, **kwargs)

    monkeypatch.setattr(router, boundary, stop_before_resume_io)
    _ack(relay, current, prepared.execution.plan.commands[0], "completed")
    assert captured == ["landing-before-stop"]
    assert [call.operation.value for call in flight.calls] == ["land", "estop"]
    assert relay.current_state()["estop"] is True
    assert relay.current_state()["accepted_plan"] is None
    assert not router.completion_pending("landing-before-stop")
    assert "landing-before-stop" not in router._landing_ack_times
    relay.replay()
