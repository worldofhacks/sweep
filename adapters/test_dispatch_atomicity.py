from dataclasses import replace

import pytest

from adapters.test_dispatch import translate_plan
from planner.models import LifecycleStatus, RefusalReason
from tests.autonomy_fixtures import make_snapshot, make_stack


@pytest.mark.parametrize("resumed", [False, True])
@pytest.mark.parametrize("stop_status", [LifecycleStatus.COMPLETED, LifecycleStatus.ACCEPTED])
def test_sustained_enrichment_loss_after_goto_returns_stop_evidence(
    monkeypatch, resumed, stop_status
):
    snapshot = make_snapshot(2)
    plan = translate_plan(snapshot)
    _, _, _, dispatcher, flight, _ = make_stack(snapshot)
    goto = flight.goto
    hover = flight.hover
    unavailable = False
    calls = 0

    def send_goto(*args, **kwargs):
        nonlocal unavailable, calls
        calls += 1
        ack = goto(*args, **kwargs)
        if resumed and calls == 1:
            return replace(ack, status=LifecycleStatus.ACCEPTED)
        unavailable = True
        return ack

    def provider():
        if unavailable:
            raise RuntimeError("sustained enrichment outage")
        return snapshot

    monkeypatch.setattr(flight, "goto", send_goto)
    monkeypatch.setattr(
        flight,
        "hover",
        lambda *args, **kwargs: tuple(
            replace(ack, status=stop_status) for ack in hover(*args, **kwargs)
        ),
    )
    result = dispatcher.dispatch(plan, snapshot, current_snapshot=provider)
    if resumed:
        assert result.status is LifecycleStatus.EXECUTING
        result = dispatcher.resume_after_completion(
            plan,
            result,
            replace(result.acknowledgements[-1], status=LifecycleStatus.COMPLETED),
            snapshot,
            current_snapshot=provider,
        )
    assert result.status is LifecycleStatus.FAILED
    assert result.refusal.reason is RefusalReason.ADAPTER_FAILURE
    moving = {call.drone_ids[0] for call in flight.calls if call.operation.value == "goto"}
    stopped = {call.drone_ids[0] for call in flight.calls if call.operation.value == "hover"}
    assert moving <= stopped
    assert calls == (2 if resumed else 1)
    stop_acks = [ack for ack in result.acknowledgements if ack.command_id.endswith(":safety-hold")]
    assert {ack.drone_id for ack in stop_acks} == moving
    assert all(ack.status is stop_status for ack in stop_acks)


def test_provider_loss_after_accepted_ack_does_not_escape(monkeypatch):
    snapshot = make_snapshot(2)
    plan = translate_plan(snapshot)
    _, _, _, dispatcher, flight, _ = make_stack(snapshot)
    goto = flight.goto
    reads_after_io = 0
    sent = False

    def send_goto(*args, **kwargs):
        nonlocal sent
        sent = True
        return replace(goto(*args, **kwargs), status=LifecycleStatus.ACCEPTED)

    def provider():
        nonlocal reads_after_io
        if sent:
            reads_after_io += 1
            if reads_after_io > 1:
                raise RuntimeError("enrichment failed while retaining accepted command")
        return snapshot

    monkeypatch.setattr(flight, "goto", send_goto)
    result = dispatcher.dispatch(plan, snapshot, current_snapshot=provider)
    assert result.status is LifecycleStatus.FAILED
    assert [call.operation.value for call in flight.calls] == ["goto", "hover"]
    assert result.acknowledgements[-1].status is LifecycleStatus.COMPLETED


def test_enrichment_failure_during_hold_still_stops_every_target(monkeypatch):
    from relay.intent_v1 import IntentName
    from tests.autonomy_fixtures import make_intent

    snapshot = make_snapshot(2)
    controller, _, _, dispatcher, flight, _ = make_stack(snapshot)
    prepared = controller.prepare(make_intent(IntentName.HOLD), snapshot)
    hover = flight.hover
    unavailable = False

    def send_hover(*args, **kwargs):
        nonlocal unavailable
        acknowledgement = hover(*args, **kwargs)
        unavailable = True
        return acknowledgement

    def provider():
        if unavailable:
            raise RuntimeError("enrichment lost after HOLD I/O")
        return snapshot

    monkeypatch.setattr(flight, "hover", send_hover)
    result = dispatcher.dispatch(prepared.plan, snapshot, current_snapshot=provider)
    assert result.status is LifecycleStatus.FAILED
    assert {call.drone_ids[0] for call in flight.calls} == {1, 2}
    assert {ack.drone_id for ack in result.acknowledgements} == {1, 2}
    assert all(ack.status is LifecycleStatus.COMPLETED for ack in result.acknowledgements)
