from __future__ import annotations

from copy import deepcopy

import pytest

from relay.audit import AuditLogError
from relay.auth import Principal
from relay.contracts import LifecycleStatus
from relay.session import RelaySession
from relay.tests.conftest import (
    MutableClock,
    acknowledgement_payload,
    intent_payload,
    membership_payload,
    telemetry_payload,
)


def snapshot(session: RelaySession) -> object:
    state = session.registry.state_event(session=session.session_id, t=0, event_id="snapshot")
    state.pop("state_sequence")
    return deepcopy(
        (
            state,
            session._intents,
            session._seen_transport_event_ids,
            session._last_transport_t,
            session._metrics,
            tuple(session._pending_intents),
        )
    )


@pytest.mark.parametrize("failure_at", ["begin_operation", "append_batch"])
@pytest.mark.parametrize(
    "operation",
    [
        "join",
        "rejoin",
        "readiness",
        "telemetry",
        "disconnect",
        "stale",
        "leave",
        "control",
        "intent",
        "lifecycle",
        "refusal",
        "ack",
        "invalid_intent",
        "pending_refusal",
    ],
)
def test_disk_full_restores_all_relay_state(
    relay_session: RelaySession,
    adapter_principal: Principal,
    console_principal: Principal,
    clock: MutableClock,
    monkeypatch: pytest.MonkeyPatch,
    failure_at: str,
    operation: str,
) -> None:
    session = relay_session
    if operation != "join":
        session.process_membership(
            membership_payload(action="join", event_id="join"), adapter_principal
        )
        session.process_telemetry(
            telemetry_payload(event_id="initial-telemetry", state="landed"), adapter_principal
        )
        session.process_membership(
            membership_payload(action="readiness", event_id="ready"), adapter_principal
        )
        session.process_intent(intent_payload(), console_principal)
        session.update_control_projection(selection=(1,), accepted_plan={"intent_id": "intent-1"})
    if operation == "rejoin":
        session.handle_adapter_disconnect(drone_id=1, connection_epoch=1)
    if operation == "stale":
        clock.advance(1_001)
    session.leave_authorizer = lambda *_: True
    before = snapshot(session)
    sequence_before = session.registry._state_sequence

    def disk_full(*args: object, **kwargs: object) -> None:
        raise AuditLogError("disk full")

    monkeypatch.setattr(session.audit_log, failure_at, disk_full)
    with pytest.raises(AuditLogError, match="disk full"):
        if operation in {"join", "rejoin", "readiness", "leave"}:
            action = {"rejoin": "join", "leave": "graceful_leave"}.get(operation, operation)
            session.process_membership(
                membership_payload(action=action, event_id="mutation"), adapter_principal
            )
        elif operation == "telemetry":
            session.process_telemetry(telemetry_payload(event_id="mutation"), adapter_principal)
        elif operation == "disconnect":
            session.handle_adapter_disconnect(drone_id=1, connection_epoch=1)
        elif operation == "stale":
            session.periodic_events()
        elif operation == "control":
            session.update_control_projection(
                selection=(2,),
                armed=True,
                estop=True,
                pending={"intent_id": "pending"},
                accepted_plan=None,
            )
        elif operation in {"intent", "invalid_intent"}:
            raw = intent_payload(intent_id="mutation")
            if operation == "invalid_intent":
                raw["name"] = "bogus"
            session.process_intent(raw, console_principal)
        elif operation == "lifecycle":
            session.record_lifecycle(
                intent_id="intent-1", status=LifecycleStatus.COMPLETED, source="planner"
            )
        elif operation == "refusal":
            session.record_refusal(
                intent_id="intent-1", source="planner", reason="unsafe", detail="unsafe"
            )
        elif operation == "pending_refusal":
            session.fail_pending_intent(
                "intent-1", reason="delivery_failed", detail="socket failed"
            )
        elif operation == "ack":
            session.process_acknowledgement(
                acknowledgement_payload(event_id="mutation"), adapter_principal
            )

    assert session.registry._state_sequence == sequence_before
    assert snapshot(session) == before
    with pytest.raises(AuditLogError, match="session is unusable"):
        session.current_state()


def test_execution_result_audit_failure_restores_projection_without_permitting_retry(
    relay_session: RelaySession,
    console_principal: Principal,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from relay.session import IntentSinkResult

    calls = []

    def sink(intent: object, state: object) -> IntentSinkResult:
        calls.append("adapter-io")
        return IntentSinkResult(
            status=LifecycleStatus.COMPLETED,
            source="planner",
            selection_update=(2,),
            armed_update=True,
        )

    relay_session.intent_sink = sink
    relay_session.process_intent(intent_payload(), console_principal)
    before = snapshot(relay_session)

    def disk_full(*args: object, **kwargs: object) -> None:
        raise AuditLogError("disk full")

    monkeypatch.setattr(relay_session.audit_log, "append_batch", disk_full)
    with pytest.raises(AuditLogError, match="disk full"):
        relay_session.execute_pending_intent("intent-1")

    assert calls == ["adapter-io"]
    assert snapshot(relay_session) == before
    with pytest.raises(AuditLogError, match="session is unusable"):
        relay_session.process_intent(
            intent_payload(intent_id="retry", retry_of="intent-1"), console_principal
        )
    assert calls == ["adapter-io"]


@pytest.mark.parametrize("failure_at", ["begin_operation", "append_batch"])
@pytest.mark.parametrize("stop_name", ["hold", "estop"])
def test_disk_full_does_not_leave_unaudited_safety_stop_ownership(
    relay_session: RelaySession,
    monkeypatch: pytest.MonkeyPatch,
    failure_at: str,
    stop_name: str,
) -> None:
    from relay.intent_v1 import AcceptedIntent, validate_intent

    raw = intent_payload(intent_id="safety:controller-stop")
    raw.update(name=stop_name, selection=[1] if stop_name == "hold" else [])
    validated = validate_intent(raw)
    assert isinstance(validated, AcceptedIntent)
    before = snapshot(relay_session)

    def disk_full(*args: object, **kwargs: object) -> None:
        raise AuditLogError("disk full")

    monkeypatch.setattr(relay_session.audit_log, failure_at, disk_full)
    with pytest.raises(AuditLogError, match="disk full"):
        relay_session.admit_safety_stop(validated.intent)

    assert snapshot(relay_session) == before
    with pytest.raises(AuditLogError, match="session is unusable"):
        relay_session.admit_safety_stop(validated.intent)
