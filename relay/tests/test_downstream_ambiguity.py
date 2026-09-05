from __future__ import annotations

import pytest

from relay.auth import Principal
from relay.contracts import LifecycleStatus
from relay.intent_v1 import IntentV1
from relay.session import RelaySession
from relay.tests.conftest import intent_payload


@pytest.mark.parametrize("refusal", ["invalid_payload", "stale_timestamp"])
def test_early_refusal_cannot_launder_ambiguous_retry_ancestry(
    relay_session: RelaySession, console_principal: Principal, refusal: str
) -> None:
    dispatched: list[str] = []

    def dispatch(intent: IntentV1, _state: dict[str, object]) -> None:
        dispatched.append(intent.intent_id)
        raise RuntimeError("adapter acknowledgement connection closed")

    relay_session.intent_sink = dispatch
    relay_session.process_intent(intent_payload(intent_id="ambiguous"), console_principal)
    relay_session.execute_pending_intent("ambiguous")
    alias = intent_payload(intent_id="alias", retry_of="ambiguous")
    if refusal == "invalid_payload":
        alias["args"] = {"bogus": 1}
    else:
        alias["t"] = 0
    refused = relay_session.process_intent(alias, console_principal)
    result = relay_session.process_intent(
        intent_payload(intent_id="retry", retry_of="alias"), console_principal
    )

    assert refused[0]["reason"] == refusal
    assert result[0]["reason"] == "invalid_retry"
    assert relay_session.execute_pending_intent(str(result[0]["intent_id"])) == []
    assert dispatched == ["ambiguous"]


@pytest.mark.parametrize("retry_parent", ["ambiguous", "retry-1"])
def test_post_dispatch_exception_cannot_duplicate_io_through_retry(
    relay_session: RelaySession,
    console_principal: Principal,
    retry_parent: str,
) -> None:
    dispatched: list[str] = []

    def dispatch_then_fail(intent: IntentV1, _state: dict[str, object]) -> None:
        dispatched.append(intent.intent_id)
        raise RuntimeError("adapter acknowledgement connection closed")

    relay_session.intent_sink = dispatch_then_fail
    relay_session.process_intent(intent_payload(intent_id="ambiguous"), console_principal)
    relay_session.execute_pending_intent("ambiguous")
    relay_session.process_intent(
        intent_payload(intent_id="retry-1", retry_of="ambiguous"), console_principal
    )
    result = relay_session.process_intent(
        intent_payload(intent_id="retry-2", retry_of=retry_parent), console_principal
    )

    assert dispatched == ["ambiguous"]
    assert result[0]["reason"] == "invalid_retry"
    assert relay_session.execute_pending_intent(str(result[0]["intent_id"])) == []


def test_ambiguous_dispatch_does_not_block_fresh_emergency_stop(
    relay_session: RelaySession, console_principal: Principal
) -> None:
    dispatched: list[str] = []

    def dispatch(intent: IntentV1, _state: dict[str, object]) -> None:
        dispatched.append(intent.intent_id)
        if intent.intent_id == "ambiguous":
            raise RuntimeError("adapter acknowledgement connection closed")

    relay_session.intent_sink = dispatch
    relay_session.process_intent(intent_payload(intent_id="ambiguous"), console_principal)
    relay_session.execute_pending_intent("ambiguous")
    estop = intent_payload(intent_id="emergency-stop")
    estop.update(name="estop", confirm=True)

    result = relay_session.process_intent(estop, console_principal)
    relay_session.execute_pending_intent("emergency-stop")

    assert dispatched == ["ambiguous", "emergency-stop"]
    assert result[0]["status"] == "accepted"


def test_pre_dispatch_refusal_stays_retryable_when_consumer_becomes_available(
    relay_session: RelaySession, console_principal: Principal
) -> None:
    dispatched: list[str] = []
    relay_session.intent_sink = None
    unavailable = relay_session.process_intent(
        intent_payload(intent_id="unavailable"), console_principal
    )

    def dispatch(intent: IntentV1, _state: dict[str, object]) -> None:
        dispatched.append(intent.intent_id)

    relay_session.intent_sink = dispatch
    result = relay_session.process_intent(
        intent_payload(intent_id="retry", retry_of="unavailable"), console_principal
    )
    relay_session.execute_pending_intent("retry")

    assert unavailable[0]["reason"] == "downstream_unavailable"
    assert dispatched == ["retry"]
    assert result[0]["status"] == "accepted"


@pytest.mark.parametrize("status", [LifecycleStatus.COMPLETED, LifecycleStatus.EXECUTING])
def test_dispatch_exception_preserves_already_recorded_execution(
    relay_session: RelaySession,
    console_principal: Principal,
    status: LifecycleStatus,
) -> None:
    dispatched: list[str] = []

    def dispatch(intent: IntentV1, _state: dict[str, object]) -> None:
        dispatched.append(intent.intent_id)
        relay_session.record_lifecycle(intent_id=intent.intent_id, status=status, source="planner")
        raise RuntimeError("response transport failed after lifecycle publication")

    relay_session.intent_sink = dispatch
    relay_session.process_intent(intent_payload(intent_id="ambiguous"), console_principal)
    relay_session.execute_pending_intent("ambiguous")
    result = relay_session.process_intent(
        intent_payload(intent_id="retry", retry_of="ambiguous"), console_principal
    )

    assert dispatched == ["ambiguous"]
    assert result[0]["reason"] == "invalid_retry"
    assert relay_session.execute_pending_intent(str(result[0]["intent_id"])) == []
    replay = relay_session.replay()["events"]
    lifecycle = [
        record["event"]["status"]
        for record in replay
        if record["event"]["type"] in {"acknowledgement", "refusal"}
        and record["event"]["intent_id"] == "ambiguous"
    ]
    assert "refused" not in lifecycle
    assert status.value in lifecycle
