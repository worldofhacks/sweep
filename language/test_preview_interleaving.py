from dataclasses import replace

import pytest

from evals.language_corpus import StaticResponseTransport
from language.compiler import (
    ConfirmationError,
    ConfirmedPlan,
    InMemoryAuditSink,
    TranscriptCompiler,
)
from language.test_compiler import _case, _hydrate_relay_from_snapshot, _snapshot_at
from planner.controller import PreparedExecutionRouter
from planner.models import LifecycleStatus, Position
from relay.audit import SessionAuditLog
from relay.auth import Principal
from relay.session import RelayLimits, RelaySession
from tests.autonomy_fixtures import make_snapshot, make_stack, replace_aircraft


@pytest.mark.parametrize("position_drift", [False, True])
def test_relay_rechecks_preview_after_interleaved_telemetry(tmp_path, monkeypatch, position_drift):
    case = _case("translate-selected")
    snapshot = _snapshot_at(make_snapshot(1, roster_version=2), case.now_ms)
    controller, _, _, _, flight, _ = make_stack(snapshot)
    goto = flight.goto

    def accepted_goto(*args, **kwargs):
        return replace(goto(*args, **kwargs), status=LifecycleStatus.ACCEPTED)

    monkeypatch.setattr(flight, "goto", accepted_goto)
    router = PreparedExecutionRouter(controller, current_snapshot=lambda: snapshot)
    clock = [case.now_ms]
    relay = RelaySession(
        session_id="language-eval",
        audit_log=SessionAuditLog(tmp_path, "language-eval"),
        limits=RelayLimits(5_000, 5_000, 1_000, 1_000),
        clock=lambda: clock[0],
        intent_sink=router,
    )
    _hydrate_relay_from_snapshot(relay, snapshot)
    state = relay.current_state()
    _, plan = TranscriptCompiler(
        StaticResponseTransport(
            {
                "kind": "plan",
                "intents": [
                    {
                        "name": "translate",
                        "args": {"dx": 1, "dy": 0},
                        "selection": [1],
                        "mode": "indoor",
                    }
                ],
            }
        ),
        audit=InMemoryAuditSink(),
    ).compile(
        "Move right one step.",
        state,
        capability_version=case.capability_version,
        rooms=case.rooms,
        now_ms=case.now_ms,
        translation=controller.planner.config.translation_grounding(snapshot),
    )
    assert plan is not None
    pending = ConfirmedPlan(plan, session=relay.session_id, audit=InMemoryAuditSink())
    prepared = pending.prepare_next(
        state,
        capability_version=case.capability_version,
        rooms=case.rooms,
        now_ms=case.now_ms,
        intent_id="interleaved-translation",
        router=router,
        snapshot=snapshot,
    )
    process_intent = relay.process_intent
    emissions = []

    def interleaved_intent(raw, principal):
        clock[0] += 1
        telemetry = dict(relay.current_state()["drones"][0]["telemetry"])
        telemetry.update(
            v=1,
            t=clock[0],
            type="telemetry",
            event_id="interleaved-position",
            session=relay.session_id,
            drone=1,
            connection_epoch=1,
            x=snapshot.aircraft[1].pose.x + (0.2 if position_drift else 0.0),
        )
        events = relay.process_frame(
            telemetry, Principal(source="adapter", drone_id=1, signing_key=b"x" * 32)
        )
        assert not any(event["type"] == "refusal" for event in events)
        result = process_intent(raw, principal)
        emissions.extend(result)
        return result

    execute_pending_intent = relay.execute_pending_intent

    def record_execution(intent_id):
        result = execute_pending_intent(intent_id)
        emissions.extend(result)
        return result

    monkeypatch.setattr(relay, "execute_pending_intent", record_execution)
    monkeypatch.setattr(relay, "process_intent", interleaved_intent)
    emitter = router.relay_emitter(
        relay, Principal(source="console", drone_id=None, signing_key=b"x" * 32)
    )

    def confirm():
        return pending.confirm_next(
            state,
            capability_version=case.capability_version,
            rooms=case.rooms,
            now_ms=case.now_ms,
            intent_id="interleaved-translation",
            emit=emitter,
            prepared=prepared,
        )

    if position_drift:
        with pytest.raises(ConfirmationError):
            confirm()
        assert flight.calls == []
        assert any(event.get("reason") == "invalid_plan" for event in emissions)
    else:
        confirm()
        assert [call.operation.value for call in flight.calls] == ["goto"]


@pytest.mark.parametrize("unsafe_state", ["geofence", "battery"])
def test_unsafe_live_snapshot_has_no_confirmation_preview_or_adapter_io(tmp_path, unsafe_state):
    case = _case("translate-selected")
    snapshot = replace_aircraft(
        _snapshot_at(make_snapshot(1, roster_version=2), case.now_ms),
        1,
        pose=Position(9.75 if unsafe_state == "geofence" else 0.0, 0.0, 1.0),
    )
    controller, _, _, _, flight, _ = make_stack(snapshot)
    live_snapshot = (
        replace_aircraft(snapshot, 1, battery=0.215) if unsafe_state == "battery" else snapshot
    )
    router = PreparedExecutionRouter(controller, current_snapshot=lambda: live_snapshot)
    relay = RelaySession(
        session_id="language-eval",
        audit_log=SessionAuditLog(tmp_path, "language-eval"),
        limits=RelayLimits(5_000, 5_000, 1_000, 1_000),
        clock=lambda: case.now_ms,
        intent_sink=router,
    )
    _hydrate_relay_from_snapshot(relay, snapshot)
    state = relay.current_state()
    _, plan = TranscriptCompiler(
        StaticResponseTransport(
            {
                "kind": "plan",
                "intents": [
                    {
                        "name": "translate",
                        "args": {"dx": 2, "dy": 0},
                        "selection": [1],
                        "mode": "indoor",
                    }
                ],
            }
        ),
        audit=InMemoryAuditSink(),
    ).compile(
        "Move right two steps.",
        state,
        capability_version=case.capability_version,
        rooms=case.rooms,
        now_ms=case.now_ms,
        translation=controller.planner.config.translation_grounding(snapshot),
    )
    assert plan is not None
    pending = ConfirmedPlan(plan, session=relay.session_id, audit=InMemoryAuditSink())
    with pytest.raises(ConfirmationError, match="geofence|reserve"):
        pending.prepare_next(
            state,
            capability_version=case.capability_version,
            rooms=case.rooms,
            now_ms=case.now_ms,
            intent_id="outside-geofence",
            router=router,
            snapshot=snapshot,
        )
    assert flight.calls == []
    assert relay.current_state()["accepted_plan"] is None
