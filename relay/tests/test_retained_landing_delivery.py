from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event

import pytest

from language.test_land_ownership import _ack, _compiled_command
from language.test_land_ownership import landing_session as landing_session
from planner.models import LifecycleStatus
from relay.auth import Principal


@pytest.mark.parametrize("landing_session", [3], indirect=True)
def test_estop_bypasses_retained_landing_failure_recovery(landing_session, monkeypatch):
    current, flight, router, relay = landing_session
    original_land = flight.land
    monkeypatch.setattr(
        flight,
        "land",
        lambda ids: tuple(
            replace(ack, status=LifecycleStatus.ACCEPTED) for ack in original_land(ids)
        ),
    )
    prepared = _compiled_command(current, router, relay, "land_all", "retained-before-stop")
    first, second, _ = prepared.execution.plan.commands
    _ack(relay, current, first, "completed")
    current[0] = replace(current[0], selection=(3,))
    relay.update_control_projection(selection=(3,))
    _compiled_command(current, router, relay, "hold", "retire-landing-suffix")
    entered_hover, release_hover = Event(), Event()
    original_hover = flight.hover

    def blocked_hover(ids):
        acknowledgements = original_hover(ids)
        entered_hover.set()
        assert release_hover.wait(3)
        return acknowledgements

    monkeypatch.setattr(flight, "hover", blocked_hover)

    def stop():
        payload = {
            "v": 1,
            "t": current[0].now_ms,
            "type": "intent",
            "intent_id": "stop-during-landing-recovery",
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
        relay.mark_pending_intent_delivered(payload["intent_id"])
        return relay.execute_pending_intent(payload["intent_id"])

    with ThreadPoolExecutor(max_workers=2) as executor:
        completion = executor.submit(_ack, relay, current, second, "failed")
        try:
            assert entered_hover.wait(2)
            stopped = executor.submit(stop)
            events = stopped.result(timeout=1)
            assert any(event.get("status") == "completed" for event in events)
            assert relay.current_state()["estop"] is True
        finally:
            release_hover.set()
        completion.result(timeout=3)
    assert "retained-before-stop" not in router._running
    assert not router.completion_pending("retained-before-stop")
    assert relay.current_state()["accepted_plan"] is None
