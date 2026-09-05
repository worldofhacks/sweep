import asyncio
from threading import Event

import pytest

from relay.app import RelayRuntime
from relay.auth import Principal
from relay.capabilities import C1_CAPABILITY_PROFILE
from relay.contracts import LifecycleStatus
from relay.session import CapabilityBoundIntentSink
from relay.settings import RelaySettings
from relay.tests.conftest import (
    ADAPTER_KEY,
    CONSOLE_KEY,
    SESSION,
    intent_payload,
    membership_payload,
)


@pytest.mark.parametrize("activate_after_stop", [False, True])
def test_resumed_batch_cannot_roll_back_existing_or_newly_activated_subscriber(
    tmp_path, clock, event_ids, activate_after_stop, monkeypatch
):
    async def exercise():
        runtime = RelayRuntime(
            RelaySettings(relay_token=CONSOLE_KEY, adapter_keys={1: ADAPTER_KEY}, log_dir=tmp_path),
            clock=clock,
            event_ids=event_ids,
            intent_sink_factory=lambda _session: CapabilityBoundIntentSink(
                lambda _intent, _state: None, C1_CAPABILITY_PROFILE
            ),
        )
        session = runtime.session(SESSION)
        console = Principal("console", None, CONSOLE_KEY)
        adapter = Principal("adapter", 1, ADAPTER_KEY)
        existing = await runtime.subscribe(SESSION, console)
        session.process_intent(intent_payload(), console)
        captured = Event()
        release = Event()

        prepared = False

        def receipt(_frame, _principal, *, defer_resume):
            assert defer_resume
            assert runtime._session_operations[SESSION].locked()
            events = session.process_frame(
                membership_payload(action="join", event_id="old-join"), adapter
            )
            events.append(
                session.record_lifecycle(
                    intent_id="intent-1",
                    command_id="command-1",
                    status=LifecycleStatus.COMPLETED,
                    source="adapter",
                )
            )
            return events

        def prepare():
            nonlocal prepared
            assert runtime._session_operations[SESSION].locked()
            if prepared:
                return None
            prepared = True
            return object()

        def io(work):
            assert not runtime._session_operations[SESSION].locked()
            captured.set()
            assert release.wait(3)
            return work

        def commit(work, outcome):
            assert runtime._session_operations[SESSION].locked()
            assert outcome is work
            return [
                session.current_state(),
                session.record_lifecycle(
                    intent_id="intent-1",
                    status=LifecycleStatus.COMPLETED,
                    source="autonomy",
                ),
            ]

        monkeypatch.setattr(session, "process_acknowledgement", receipt)
        monkeypatch.setattr(session, "prepare_resume", prepare, raising=False)
        monkeypatch.setattr(session, "resume_io", io, raising=False)
        monkeypatch.setattr(session, "commit_resume", commit, raising=False)
        old = asyncio.create_task(
            runtime.process_acknowledgement_and_publish(
                SESSION,
                session,
                {},
                adapter,
            )
        )
        try:
            assert await asyncio.to_thread(captured.wait, 2)
            newer = await runtime.process_and_publish(
                SESSION,
                lambda: [session.update_control_projection(estop=True)],
            )
            joined = await runtime.subscribe(SESSION, console)
        finally:
            release.set()
        old_events = await old
        for subscription in (joined if activate_after_stop else existing,):
            events = [
                subscription.queue.get_nowait().event for _ in range(subscription.queue.qsize())
            ]
            states = [subscription.initial_state] + [
                event for event in events if event["type"] == "state"
            ]
            sequences = [state["state_sequence"] for state in states]
            assert sequences == sorted(sequences), [
                (state["state_sequence"], state["estop"]) for state in states
            ]
            assert states[-1]["estop"] is True
            if not activate_after_stop:
                assert any(event.get("type") == "membership" for event in events)
            else:
                assert subscription.initial_state["roster_version"] == 1
            assert any(
                event.get("intent_id") == "intent-1" and event.get("status") == "completed"
                for event in events
            )
        assert old_events[1]["state_sequence"] < newer[0]["state_sequence"]

    asyncio.run(exercise())


@pytest.mark.parametrize("status", ["accepted", "executing", "malformed"])
def test_nonterminal_and_malformed_acknowledgements_do_not_prepare_resume(
    tmp_path, clock, event_ids, monkeypatch, status
):
    from relay.tests.conftest import acknowledgement_payload

    async def exercise():
        runtime = RelayRuntime(
            RelaySettings(relay_token=CONSOLE_KEY, adapter_keys={1: ADAPTER_KEY}, log_dir=tmp_path),
            clock=clock,
            event_ids=event_ids,
            intent_sink_factory=lambda _session: CapabilityBoundIntentSink(
                lambda _intent, _state: None, C1_CAPABILITY_PROFILE
            ),
        )
        session = runtime.session(SESSION)
        console = Principal("console", None, CONSOLE_KEY)
        adapter = Principal("adapter", 1, ADAPTER_KEY)
        session.process_frame(membership_payload(action="join", event_id="join"), adapter)
        session.process_intent(intent_payload(), console)
        raw = acknowledgement_payload(event_id="ack", status=status)
        receipt = session.process_acknowledgement

        def ordered_receipt(*args, **kwargs):
            assert runtime._session_operations[SESSION].locked()
            assert kwargs["defer_resume"] is True
            return receipt(*args, **kwargs)

        def unexpected_prepare():
            raise AssertionError("nonterminal or malformed ACK attempted resume")

        monkeypatch.setattr(session, "process_acknowledgement", ordered_receipt)
        monkeypatch.setattr(session, "prepare_resume", unexpected_prepare)
        events = await runtime.process_acknowledgement_and_publish(SESSION, session, raw, adapter)
        if status == "malformed":
            assert events[0]["reason"] == "invalid_acknowledgement"
        else:
            assert events[0]["status"] == status

    asyncio.run(exercise())
