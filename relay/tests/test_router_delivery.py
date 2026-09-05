from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event

from fastapi.testclient import TestClient

from language.test_compiler import _hydrate_relay_from_snapshot
from planner.controller import PreparedExecutionRouter
from planner.models import LifecycleStatus, PreparedExecution
from relay.app import create_app
from relay.audit import SessionAuditLog
from relay.auth import Principal
from relay.capabilities import C1_CAPABILITY_PROFILE
from relay.intent_v1 import IntentName
from relay.session import CapabilityBoundIntentSink, RelayLimits, RelaySession
from relay.settings import RelaySettings
from relay.tests.conftest import CONSOLE_KEY, SESSION, intent_payload
from tests.autonomy_fixtures import make_intent, make_snapshot, make_stack


def _prepared_translation(tmp_path):
    snapshot = make_snapshot(2)
    controller, _, _, _, flight, _ = make_stack(snapshot)
    intent = make_intent(IntentName.TRANSLATE, args={"dx": 1, "dy": 0})
    prepared = controller.prepare(intent, snapshot)
    assert isinstance(prepared, PreparedExecution)
    router = PreparedExecutionRouter(controller, current_snapshot=lambda: snapshot)
    router.bind(prepared)
    relay = RelaySession(
        session_id=intent.session,
        audit_log=SessionAuditLog(tmp_path, intent.session),
        limits=RelayLimits(5_000, 5_000, 1_000, 1_000),
        clock=lambda: snapshot.now_ms,
        intent_sink=router,
    )
    _hydrate_relay_from_snapshot(relay, snapshot)
    payload = intent_payload(timestamp=intent.t, intent_id=intent.intent_id, session=intent.session)
    payload.update(name="translate", args={"dx": 1, "dy": 0}, selection=[1, 2])
    return relay, prepared, flight, payload


def test_prepared_router_admission_waits_for_acceptance_delivery(tmp_path, console_principal):
    relay, prepared, flight, payload = _prepared_translation(tmp_path)
    events = relay.process_intent(payload, console_principal)
    assert [event.get("status") for event in events] == ["accepted"]
    assert flight.calls == []
    assert relay.current_state()["accepted_plan"] is None

    relay.mark_pending_intent_delivered(prepared.intent.intent_id)
    assert flight.calls == []
    events = relay.execute_pending_intent(prepared.intent.intent_id)
    assert len(flight.calls) == 2
    assert events[-1]["status"] == "completed"
    assert relay.current_state()["accepted_plan"] is None
    assert relay.execute_pending_intent(prepared.intent.intent_id) == []


def test_failed_acceptance_delivery_never_dispatches_prepared_router(tmp_path, console_principal):
    relay, prepared, flight, payload = _prepared_translation(tmp_path)
    assert relay.process_intent(payload, console_principal)[0]["status"] == "accepted"
    events = relay.fail_pending_intent(
        prepared.intent.intent_id,
        reason="acceptance_delivery_failed",
        detail="requesting socket disconnected before acceptance",
    )
    assert events[-1]["reason"] == "acceptance_delivery_failed"
    assert relay.execute_pending_intent(prepared.intent.intent_id) == []
    assert flight.calls == []
    assert relay.current_state()["accepted_plan"] is None


def test_terminal_adapter_ack_during_initial_dispatch_resumes_owned_plan(
    tmp_path, monkeypatch, console_principal
):
    relay, prepared, flight, payload = _prepared_translation(tmp_path)
    entered_adapter = Event()
    release_adapter = Event()
    ack_started = Event()
    ack_finished = Event()
    original_goto = flight.goto

    def asynchronous_first_goto(*args):
        acknowledgement = original_goto(*args)
        if len(flight.calls) == 1:
            entered_adapter.set()
            assert release_adapter.wait(3)
            return replace(acknowledgement, status=LifecycleStatus.EXECUTING)
        return acknowledgement

    monkeypatch.setattr(flight, "goto", asynchronous_first_goto)
    relay.process_intent(payload, console_principal)
    relay.mark_pending_intent_delivered(prepared.intent.intent_id)
    command = prepared.plan.commands[0]

    def acknowledge():
        ack_started.set()
        try:
            return relay.process_acknowledgement(
                {
                    "v": 1,
                    "t": prepared.intent.t,
                    "type": "acknowledgement",
                    "event_id": "completed-before-dispatch-return",
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
        finally:
            ack_finished.set()

    with ThreadPoolExecutor(max_workers=2) as executor:
        execution = executor.submit(relay.execute_pending_intent, prepared.intent.intent_id)
        try:
            assert entered_adapter.wait(3)
            completion = executor.submit(acknowledge)
            assert ack_started.wait(3)
            # Both queuing the ACK and blocking reconciliation until ownership exists are valid.
            ack_finished.wait(0.1)
        finally:
            release_adapter.set()
        events = execution.result(timeout=3) + completion.result(timeout=3)

    assert len(flight.calls) == 2
    assert any(
        event.get("status") == "completed" and event.get("source") == "autonomy" for event in events
    )
    assert relay.current_state()["accepted_plan"] is None


def test_webcam_socket_executes_only_after_its_acceptance_is_delivered(tmp_path, clock):
    dispatched = Event()
    settings = RelaySettings(relay_token=CONSOLE_KEY, adapter_keys={}, log_dir=tmp_path)

    def sink(_intent, _state):
        dispatched.set()

    app = create_app(
        settings,
        clock=clock,
        intent_sink_factory=lambda _session: CapabilityBoundIntentSink(sink, C1_CAPABILITY_PROFILE),
    )
    with TestClient(app) as client, client.websocket_connect(f"/ws/{SESSION}") as socket:
        socket.send_json(
            {"v": 1, "type": "auth", "source": "webcam", "token": CONSOLE_KEY.decode()}
        )
        assert socket.receive_json()["type"] == "auth.accepted"
        assert socket.receive_json()["type"] == "state"
        assert not dispatched.is_set()
        socket.send_json(intent_payload(timestamp=clock(), source="webcam"))
        accepted = socket.receive_json()
        assert accepted["status"] == "accepted"
        assert dispatched.wait(1)


def test_estop_bypasses_a_router_dispatch_waiting_for_its_first_adapter_return(
    tmp_path, monkeypatch, console_principal
):
    relay, prepared, flight, payload = _prepared_translation(tmp_path)
    entered_adapter = Event()
    release_adapter = Event()
    original_goto = flight.goto

    def blocked_first_goto(*args):
        acknowledgement = original_goto(*args)
        entered_adapter.set()
        assert release_adapter.wait(3)
        return replace(acknowledgement, status=LifecycleStatus.EXECUTING)

    monkeypatch.setattr(flight, "goto", blocked_first_goto)
    relay.process_intent(payload, console_principal)
    relay.mark_pending_intent_delivered(prepared.intent.intent_id)
    stop = intent_payload(
        timestamp=prepared.intent.t,
        intent_id="stop-during-router-dispatch",
        session=prepared.intent.session,
    )
    stop.update(name="estop", selection=[], confirm=True)
    with ThreadPoolExecutor(max_workers=2) as executor:
        execution = executor.submit(relay.execute_pending_intent, prepared.intent.intent_id)
        try:
            assert entered_adapter.wait(3)
            assert relay.process_intent(stop, console_principal)[0]["status"] == "accepted"
            relay.mark_pending_intent_delivered(stop["intent_id"])
            stopped = executor.submit(relay.execute_pending_intent, stop["intent_id"])
            stop_events = stopped.result(timeout=1)
            assert any(event.get("status") == "completed" for event in stop_events)
            assert relay.current_state()["estop"] is True
            assert any(call.operation.value == "estop" for call in flight.calls)
        finally:
            release_adapter.set()
        execution.result(timeout=3)
    assert sum(call.operation.value == "goto" for call in flight.calls) == 1
