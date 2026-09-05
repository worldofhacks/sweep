from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event

from language.test_land_ownership import _ack
from planner.models import LifecycleStatus
from relay.tests.test_resume_supersession import _stop
from relay.tests.test_router_delivery import _prepared_translation


def test_estop_prevents_resumed_command_launch_after_snapshot_read(
    tmp_path, monkeypatch, console_principal
):
    relay, prepared, flight, payload = _prepared_translation(tmp_path)
    entered_resumed_goto = Event()
    release_resumed_goto = Event()
    original_goto = flight.goto

    def pause_before_resumed_goto(*args):
        if flight.calls:
            entered_resumed_goto.set()
            assert release_resumed_goto.wait(timeout=2)
            return original_goto(*args)
        return replace(original_goto(*args), status=LifecycleStatus.EXECUTING)

    monkeypatch.setattr(flight, "goto", pause_before_resumed_goto)
    assert relay.process_intent(payload, console_principal)[0]["status"] == "accepted"
    relay.mark_pending_intent_delivered(prepared.intent.intent_id)
    relay.execute_pending_intent(prepared.intent.intent_id)

    command = prepared.plan.commands[0]
    with ThreadPoolExecutor(max_workers=1) as executor:
        resumed = executor.submit(_ack, relay, [prepared.snapshot], command, "completed")
        assert entered_resumed_goto.wait(timeout=2)
        _stop(relay, prepared.intent.t)
        release_resumed_goto.set()
        resumed.result(timeout=2)

    operations = [call.operation.value for call in flight.calls]
    assert operations == ["goto", "estop"]
