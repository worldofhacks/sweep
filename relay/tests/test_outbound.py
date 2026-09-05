from __future__ import annotations

import asyncio

import pytest

import relay.app as app_module
from relay.app import RelayRuntime, create_app
from relay.auth import Principal
from relay.tests.conftest import (
    ADAPTER_KEY,
    CONSOLE_KEY,
    SESSION,
    intent_payload,
    membership_payload,
)
from relay.tests.test_app import app_settings as app_settings


@pytest.mark.parametrize("send_failure", [RuntimeError, OSError, None])
@pytest.mark.parametrize("stall_close", [False, True])
def test_later_send_failure_releases_receiver_and_adapter(
    app_settings, clock, event_ids, send_failure, stall_close, monkeypatch
):
    monkeypatch.setattr(app_module, "_CLOSE_TIMEOUT_SECONDS", 0.01, raising=False)

    async def exercise():
        runtime = RelayRuntime(app_settings, clock=clock, event_ids=event_ids)
        session = runtime.session(SESSION)
        principal = Principal(source="adapter", drone_id=1, signing_key=ADAPTER_KEY)
        session.process_membership(
            membership_payload(action="join", event_id="join-before-failure"), principal
        )
        application = create_app(app_settings)
        application.state.relay_runtime = runtime
        route = next(
            r for r in application.routes if getattr(r, "path", None) == "/ws/{session_id}"
        )
        failed = asyncio.Event()
        receiving = asyncio.Event()
        receiver_cancelled = asyncio.Event()

        class Socket:
            app = application
            sends = 0
            receives = 0
            closed = []

            async def accept(self):
                pass

            async def close(self, code):
                self.closed.append(code)
                if stall_close:
                    await asyncio.Event().wait()

            async def receive_json(self):
                self.receives += 1
                if self.receives == 1:
                    return {
                        "v": 1,
                        "type": "auth",
                        "source": "adapter",
                        "drone_id": 1,
                        "token": ADAPTER_KEY.decode(),
                    }
                receiving.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    receiver_cancelled.set()

            async def send_json(self, data):
                self.sends += 1
                if self.sends == 3:
                    failed.set()
                    if send_failure is not None:
                        raise send_failure("injected later transport failure")
                    await asyncio.Event().wait()

        socket = Socket()
        endpoint = asyncio.create_task(route.endpoint(socket, SESSION))
        await asyncio.wait_for(receiving.wait(), 1)
        await runtime.publish(SESSION, [{"type": "state", "roster_version": 1}])
        await asyncio.wait_for(failed.wait(), 1)
        if send_failure is None:
            subscription = next(iter(runtime._subscriptions[SESSION].values()))
            for i in range(1000):
                await runtime.publish(SESSION, [{"type": "acknowledgement", "event_id": str(i)}])
            assert subscription.queue.qsize() <= 128
        done, _ = await asyncio.wait({endpoint}, timeout=1)
        try:
            assert endpoint in done, "dead sender left receive loop running"
            await endpoint
            assert receiver_cancelled.is_set()
            assert socket.closed == ([1013] if send_failure is None else [1011])
            assert runtime.connection_count() == 0
            assert runtime._adapter_connections == {}
            assert session.current_state()["drones"][0]["membership"] == "disconnected"
        finally:
            endpoint.cancel()
            await asyncio.gather(endpoint, return_exceptions=True)
            await runtime.stop()

    asyncio.run(exercise())


def test_pending_states_conflate_without_losing_ordered_events(app_settings, clock, event_ids):
    async def exercise():
        runtime = RelayRuntime(app_settings, clock=clock, event_ids=event_ids)
        runtime.session(SESSION)
        subscription = await runtime.subscribe(
            SESSION, Principal(source="adapter", drone_id=1, signing_key=ADAPTER_KEY)
        )
        for i in range(1000):
            await runtime.publish(SESSION, [{"type": "state", "roster_version": 0, "t": i}])
        await runtime.publish(SESSION, [{"type": "acknowledgement", "event_id": "one-shot"}])
        await runtime.publish(SESSION, [{"type": "state", "roster_version": 0, "t": 1000}])
        assert subscription.queue.qsize() == 2
        assert subscription.queue.get_nowait().event["event_id"] == "one-shot"
        assert subscription.queue.get_nowait().event["t"] == 1000

    asyncio.run(exercise())


def test_stalled_acceptance_send_times_out_and_releases_receipt_and_connection(
    app_settings, clock, event_ids, monkeypatch
):
    monkeypatch.setattr(app_module, "_SEND_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(app_module, "_CLOSE_TIMEOUT_SECONDS", 0.01)

    async def exercise():
        runtime = RelayRuntime(app_settings, clock=clock, event_ids=event_ids)
        session = runtime.session(SESSION)
        session.intent_sink = lambda _intent, _state: None
        application = create_app(app_settings)
        application.state.relay_runtime = runtime
        route = next(
            r for r in application.routes if getattr(r, "path", None) == "/ws/{session_id}"
        )
        stalled = asyncio.Event()

        class Socket:
            app = application
            sends = 0
            receives = 0
            closed = []

            async def accept(self):
                pass

            async def close(self, code):
                self.closed.append(code)

            async def receive_json(self):
                self.receives += 1
                if self.receives == 1:
                    return {
                        "v": 1,
                        "type": "auth",
                        "source": "console",
                        "token": CONSOLE_KEY.decode(),
                    }
                if self.receives == 2:
                    return intent_payload()
                await asyncio.Event().wait()

            async def send_json(self, data):
                self.sends += 1
                if self.sends == 3:
                    stalled.set()
                    await asyncio.Event().wait()

        socket = Socket()
        endpoint = asyncio.create_task(route.endpoint(socket, SESSION))
        await asyncio.wait_for(stalled.wait(), 1)
        await asyncio.wait_for(endpoint, 1)

        assert socket.closed == [1013]
        assert runtime.connection_count() == 0
        assert session._pending_intents == {}
        assert session._intents["intent-1"].status.value == "refused"
        replay = session.replay()["events"]
        assert any(item["event"].get("reason") == "acceptance_delivery_failed" for item in replay)

    asyncio.run(exercise())


def test_state_only_stalled_sender_times_out_through_real_receive_loop(
    app_settings, clock, event_ids, monkeypatch
):
    monkeypatch.setattr(app_module, "_SEND_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(app_module, "_CLOSE_TIMEOUT_SECONDS", 0.01)

    async def exercise():
        runtime = RelayRuntime(app_settings, clock=clock, event_ids=event_ids)
        session = runtime.session(SESSION)
        principal = Principal(source="adapter", drone_id=1, signing_key=ADAPTER_KEY)
        session.process_membership(
            membership_payload(action="join", event_id="join-before-state-stall"), principal
        )
        application = create_app(app_settings)
        application.state.relay_runtime = runtime
        route = next(
            r for r in application.routes if getattr(r, "path", None) == "/ws/{session_id}"
        )
        stalled = asyncio.Event()
        receiver_cancelled = asyncio.Event()

        class Socket:
            app = application
            sends = 0
            receives = 0
            closed = []

            async def accept(self):
                pass

            async def close(self, code):
                self.closed.append(code)

            async def receive_json(self):
                self.receives += 1
                if self.receives == 1:
                    return {
                        "v": 1,
                        "type": "auth",
                        "source": "adapter",
                        "drone_id": 1,
                        "token": ADAPTER_KEY.decode(),
                    }
                try:
                    await asyncio.Event().wait()
                finally:
                    receiver_cancelled.set()

            async def send_json(self, data):
                self.sends += 1
                if self.sends == 3:
                    assert data["type"] == "state"
                    stalled.set()
                    await asyncio.Event().wait()

        socket = Socket()
        endpoint = asyncio.create_task(route.endpoint(socket, SESSION))
        while socket.sends < 2:
            await asyncio.sleep(0)
        await runtime.publish(SESSION, [{"type": "state", "roster_version": 1}])
        await asyncio.wait_for(stalled.wait(), 1)
        await asyncio.wait_for(endpoint, 1)

        assert socket.closed == [1013]
        assert receiver_cancelled.is_set()
        assert runtime.connection_count() == 0
        assert runtime._adapter_connections == {}
        assert session.current_state()["drones"][0]["membership"] == "disconnected"

    asyncio.run(exercise())


def test_overflow_resolves_pending_delivery_receipts(app_settings, clock, event_ids):
    from starlette.websockets import WebSocketDisconnect

    from relay.app import _send_events

    async def exercise():
        runtime = RelayRuntime(app_settings, clock=clock, event_ids=event_ids)
        runtime.session(SESSION)
        subscription = await runtime.subscribe(
            SESSION, Principal(source="adapter", drone_id=1, signing_key=ADAPTER_KEY)
        )
        started = asyncio.Event()
        closed = []

        class SlowSocket:
            async def send_json(self, data):
                started.set()
                await asyncio.Event().wait()

            async def close(self, code):
                closed.append(code)

        receipts = []
        sender = asyncio.create_task(_send_events(SlowSocket(), subscription))
        await runtime.publish(
            SESSION,
            [{"type": "acknowledgement", "status": "accepted"}],
            wait_for_connection_id=subscription.connection_id,
            deferred_deliveries=receipts,
        )
        await asyncio.wait_for(started.wait(), 1)
        await runtime.publish(
            SESSION,
            [{"type": "acknowledgement", "event_id": str(i)} for i in range(200)],
            wait_for_connection_id=subscription.connection_id,
            deferred_deliveries=receipts,
        )
        with pytest.raises(WebSocketDisconnect):
            await asyncio.wait_for(sender, 1)
        assert len(receipts) == 201
        assert all(receipt.done() and receipt.result() is False for receipt in receipts)
        assert subscription.queue.empty()
        assert closed == [1013]

    asyncio.run(exercise())


def test_periodic_state_keeps_graceful_leave_invalidation_metadata(app_settings, clock, event_ids):
    async def exercise():
        runtime = RelayRuntime(
            app_settings,
            clock=clock,
            event_ids=event_ids,
            leave_authorizer_factory=lambda _session: lambda _drone, _epoch, _state: True,
        )
        session = runtime.session(SESSION)
        principal = Principal(source="adapter", drone_id=1, signing_key=ADAPTER_KEY)
        session.process_membership(
            membership_payload(action="join", event_id="join-before-leave"), principal
        )
        session.update_control_projection(
            selection=(1,),
            pending={"intent_id": "pending-intent", "name": "takeoff"},
            accepted_plan={"intent_id": "plan-intent", "plan_id": "plan-1"},
        )
        subscription = await runtime.subscribe(SESSION, principal)
        await runtime.process_and_publish(
            SESSION,
            lambda: session.process_membership(
                membership_payload(action="graceful_leave", event_id="leave-approved"), principal
            ),
        )
        for _ in range(10):
            await runtime.process_and_publish(SESSION, session.periodic_events)
        queued = [subscription.queue.get_nowait().event for _ in range(subscription.queue.qsize())]
        assert [event["type"] for event in queued] == ["membership", "state", "state"]
        assert queued[1]["invalidation_reason"] == "graceful_leave_roster_change"
        assert queued[1]["invalidated_intent_ids"] == ["pending-intent", "plan-intent"]
        assert queued[1]["cleared_control_fields"] == ["selection", "pending", "accepted_plan"]
        assert "invalidation_reason" not in queued[2]

    asyncio.run(exercise())
