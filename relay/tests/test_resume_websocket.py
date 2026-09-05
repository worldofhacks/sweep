from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event

from fastapi.testclient import TestClient

from planner.models import LifecycleStatus
from relay.app import create_app
from relay.settings import RelaySettings
from relay.tests.conftest import CONSOLE_KEY
from relay.tests.test_router_delivery import _prepared_translation


def _receive_outcome(socket, intent_id, status, seen):
    for _ in range(100):
        event = socket.receive_json()
        seen.append(event)
        if event.get("intent_id") == intent_id and event.get("status") == status:
            return event
    raise AssertionError(f"missing {intent_id} {status}")


def test_estop_socket_dispatches_while_adapter_ack_resumes_blocked_motion(tmp_path, monkeypatch):
    relay, prepared, flight, payload = _prepared_translation(tmp_path)
    session_id = relay.session_id
    entered_resume = Event()
    release_resume = Event()
    stopped = Event()
    original_goto = flight.goto
    original_estop = flight.estop
    goto_count = 0

    def blocked_resumed_goto(*args):
        nonlocal goto_count
        goto_count += 1
        acknowledgement = original_goto(*args)
        if goto_count == 1:
            return replace(acknowledgement, status=LifecycleStatus.EXECUTING)
        entered_resume.set()
        assert release_resume.wait(5)
        return acknowledgement

    def observed_estop():
        acknowledgements = original_estop()
        stopped.set()
        return acknowledgements

    monkeypatch.setattr(flight, "goto", blocked_resumed_goto)
    monkeypatch.setattr(flight, "estop", observed_estop)
    command = prepared.plan.commands[0]
    adapter_key = b"x" * 32
    settings = RelaySettings(
        relay_token=CONSOLE_KEY, adapter_keys={command.drone_id: adapter_key}, log_dir=tmp_path
    )
    app = create_app(
        settings,
        clock=lambda: prepared.intent.t,
        intent_sink_factory=lambda _session: relay.intent_sink,
    )
    seen = []
    with TestClient(app) as client:
        app.state.relay_runtime.sessions[session_id] = relay
        with (
            client.websocket_connect(f"/ws/{session_id}") as console,
            client.websocket_connect(f"/ws/{session_id}") as adapter,
            ThreadPoolExecutor(max_workers=1) as executor,
        ):
            for socket, source, key in (
                (console, "console", CONSOLE_KEY),
                (adapter, "adapter", adapter_key),
            ):
                auth = {"v": 1, "type": "auth", "source": source, "token": key.decode()}
                if source == "adapter":
                    auth["drone_id"] = command.drone_id
                socket.send_json(auth)
                assert socket.receive_json()["type"] == "auth.accepted"
                assert socket.receive_json()["type"] == "state"
            console.send_json(payload)
            initial = executor.submit(
                _receive_outcome, console, prepared.intent.intent_id, "executing", seen
            )
            assert initial.result(timeout=3)["source"] == "autonomy"
            try:
                adapter.send_json(
                    {
                        "v": 1,
                        "t": prepared.intent.t,
                        "type": "acknowledgement",
                        "event_id": "resume-over-websocket",
                        "session": session_id,
                        "intent_id": prepared.intent.intent_id,
                        "command_id": command.command_id,
                        "status": "completed",
                        "drone_id": command.drone_id,
                        "connection_epoch": command.connection_epoch,
                        "roster_version": prepared.plan.roster_version,
                        "reason": None,
                        "detail": None,
                    }
                )
                assert entered_resume.wait(2), str(
                    [
                        {
                            key: record["event"].get(key)
                            for key in ("type", "status", "reason", "detail")
                        }
                        for record in relay.replay()["events"][-8:]
                    ]
                )
                stop = {
                    **payload,
                    "intent_id": "stop-during-websocket-resume",
                    "name": "estop",
                    "selection": [],
                    "args": {},
                }
                console.send_json(stop)
                accepted = executor.submit(
                    _receive_outcome, console, stop["intent_id"], "accepted", seen
                )
                assert accepted.result(timeout=2)["source"] == "relay"
                assert stopped.wait(2), "E-stop adapter I/O waited for the resumed GOTO"
                assert not release_resume.is_set()
                completed = executor.submit(
                    _receive_outcome, console, stop["intent_id"], "completed", seen
                )
                assert completed.result(timeout=2)["source"] == "autonomy"
            finally:
                release_resume.set()
                console.close()
                adapter.close()
    assert relay.current_state()["estop"] is True
    assert relay.current_state()["accepted_plan"] is None
    operations = [call.operation.value for call in flight.calls]
    assert operations == ["goto", "goto", "estop"]
    events = [record["event"] for record in relay.replay()["events"]]
    assert any(
        event.get("intent_id") == prepared.intent.intent_id
        and event.get("status") == "invalidated"
        and event.get("reason") == "conflicting_motion"
        for event in events
    )
    assert not any(
        event.get("intent_id") == prepared.intent.intent_id
        and event.get("status") == "completed"
        and event.get("source") == "autonomy"
        for event in events
    )
