import asyncio
from dataclasses import replace
from threading import Event

import pytest

from language.test_compiler import _hydrate_relay_from_snapshot
from planner.controller import PreparedExecutionRouter
from planner.models import LifecycleStatus, PreparedExecution
from relay.app import RelayRuntime
from relay.auth import Principal
from relay.intent_v1 import IntentName
from relay.settings import RelaySettings
from relay.tests.conftest import CONSOLE_KEY, acknowledgement_payload, intent_payload
from tests.autonomy_fixtures import make_intent, make_snapshot, make_stack


@pytest.mark.parametrize("interleave", ["receipt", "prepared", "between_calls", "before_commit"])
def test_estop_preserves_resume_ownership_and_publication_at_each_phase(
    tmp_path, monkeypatch, interleave
):
    async def exercise():
        snapshot = make_snapshot(3)
        controller, _, _, _, flight, _ = make_stack(snapshot)
        intent = make_intent(IntentName.TRANSLATE, selection=(1, 2, 3), args={"dx": 1, "dy": 0})
        prepared = controller.prepare(intent, snapshot)
        assert isinstance(prepared, PreparedExecution)
        router = PreparedExecutionRouter(controller, current_snapshot=lambda: snapshot)
        router.bind(prepared)
        runtime = RelayRuntime(
            RelaySettings(relay_token=CONSOLE_KEY, adapter_keys={}, log_dir=tmp_path),
            clock=lambda: snapshot.now_ms,
            intent_sink_factory=lambda _session: router,
        )
        session = runtime.session(intent.session)
        _hydrate_relay_from_snapshot(session, snapshot)
        principal = Principal("console", None, CONSOLE_KEY)
        subscription = await runtime.subscribe(intent.session, principal)
        original_goto = flight.goto
        entered = Event()
        release = Event()
        goto_count = 0

        def goto(*args):
            nonlocal goto_count
            goto_count += 1
            ack = original_goto(*args)
            if goto_count == 1:
                return replace(ack, status=LifecycleStatus.EXECUTING)
            if interleave == "between_calls" and goto_count == 2:
                entered.set()
                assert release.wait(3)
            return ack

        monkeypatch.setattr(flight, "goto", goto)
        payload = intent_payload(
            timestamp=intent.t,
            intent_id=intent.intent_id,
            session=intent.session,
        )
        payload.update(name="translate", args={"dx": 1, "dy": 0}, selection=[1, 2, 3])
        await runtime.process_and_publish(
            intent.session,
            lambda: session.process_intent(payload, principal),
        )
        session.mark_pending_intent_delivered(intent.intent_id)
        initial = session.execute_pending_intent(intent.intent_id, defer_resume=True)
        async with runtime._session_operation(intent.session):
            await runtime.publish(intent.session, initial)
        command = prepared.plan.commands[0]
        ack = acknowledgement_payload(
            event_id="first-completed",
            timestamp=intent.t,
            intent_id=intent.intent_id,
            command_id=command.command_id,
            drone_id=command.drone_id,
            connection_epoch=command.connection_epoch,
            roster_version=prepared.plan.roster_version,
            status="completed",
        )
        ack["session"] = intent.session
        adapter = Principal("adapter", command.drone_id, b"x" * 32)
        await runtime.process_and_publish(
            intent.session,
            lambda: session.process_acknowledgement(ack, adapter, defer_resume=True),
        )

        async def stop():
            stop_payload = {
                **payload,
                "intent_id": "phase-stop",
                "name": "estop",
                "args": {},
                "selection": [],
            }
            await runtime.process_and_publish(
                intent.session,
                lambda: session.process_intent(stop_payload, principal),
            )
            session.mark_pending_intent_delivered("phase-stop")
            outcome = await asyncio.to_thread(
                session.execute_pending_intent, "phase-stop", defer_resume=True
            )
            async with runtime._session_operation(intent.session):
                await runtime.publish(intent.session, outcome)
            assert any(event.get("status") == "completed" for event in outcome)

        work = None
        if interleave == "receipt":
            await stop()
        async with runtime._session_operation(intent.session):
            work = session.prepare_resume()
        if interleave == "receipt":
            assert work is None
        else:
            assert work is not None
            if interleave == "prepared":
                await stop()
            io = asyncio.create_task(asyncio.to_thread(session.resume_io, work))
            try:
                if interleave == "between_calls":
                    assert await asyncio.to_thread(entered.wait, 2)
                    await asyncio.wait_for(stop(), 2)
            finally:
                release.set()
            outcome = await io
            if interleave == "before_commit":
                await stop()
            async with runtime._session_operation(intent.session):
                committed = session.commit_resume(work, outcome)
                await runtime.publish(intent.session, committed)
                assert session.prepare_resume() is None

        operations = [call.operation.value for call in flight.calls]
        assert operations.count("estop") == 1
        assert operations[-1] == "estop", operations
        assert (
            operations.count("goto")
            == {
                "receipt": 1,
                "prepared": 1,
                "between_calls": 2,
                "before_commit": 3,
            }[interleave]
        )
        assert session.current_state()["estop"] is True
        assert session.current_state()["accepted_plan"] is None
        events = [subscription.queue.get_nowait().event for _ in range(subscription.queue.qsize())]
        states = [subscription.initial_state] + [
            event for event in events if event["type"] == "state"
        ]
        assert [state["state_sequence"] for state in states] == sorted(
            state["state_sequence"] for state in states
        )
        audit = [record["event"] for record in session.replay()["events"]]
        positions = {
            event["event_id"]: index for index, event in enumerate(audit) if "event_id" in event
        }
        delivered_positions = [
            positions[event["event_id"]] for event in events if event["event_id"] in positions
        ]
        assert delivered_positions == sorted(delivered_positions)
        assert any(
            event.get("intent_id") == intent.intent_id and event.get("status") == "invalidated"
            for event in audit
        )
        assert not any(
            event.get("intent_id") == intent.intent_id
            and event.get("source") == "autonomy"
            and event.get("status") == "completed"
            for event in audit
        )

    asyncio.run(exercise())
