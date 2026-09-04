from __future__ import annotations

import asyncio
from pathlib import Path
from threading import Event

import anyio
import pytest

from relay.app import RelayRuntime
from relay.auth import Principal
from relay.session import RelaySession
from relay.settings import RelaySettings
from relay.tests.conftest import (
    ADAPTER_KEY,
    CONSOLE_KEY,
    SESSION,
    EventIds,
    MutableClock,
    membership_payload,
)


@pytest.fixture
def app_settings(tmp_path: Path) -> RelaySettings:
    return RelaySettings(
        relay_token=CONSOLE_KEY,
        adapter_keys={1: ADAPTER_KEY},
        log_dir=tmp_path,
        intent_max_age_ms=5_000,
        transport_event_max_age_ms=5_000,
        future_clock_skew_ms=1_000,
        telemetry_freshness_ms=1_000,
    )


def test_cancelled_worker_finishes_mutation_and_publish_before_next_operation(
    app_settings: RelaySettings, clock: MutableClock, event_ids: EventIds
) -> None:
    worker_started = Event()
    release_worker = Event()

    async def exercise() -> list[bool]:
        runtime = RelayRuntime(app_settings, clock=clock, event_ids=event_ids)
        session = runtime.session(SESSION)
        subscription = await runtime.subscribe(
            SESSION, Principal(source="console", drone_id=None, signing_key=CONSOLE_KEY)
        )

        def delayed_first_operation() -> list[dict[str, object]]:
            worker_started.set()
            assert release_worker.wait(timeout=2)
            return [session.update_control_projection(estop=True)]

        first = asyncio.create_task(runtime.process_and_publish(SESSION, delayed_first_operation))
        assert await asyncio.to_thread(worker_started.wait, 2)
        first.cancel()
        second = asyncio.create_task(
            runtime.process_and_publish(
                SESSION, lambda: [session.update_control_projection(estop=False)]
            )
        )
        await asyncio.sleep(0)
        assert not second.done()
        release_worker.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        await second
        return [
            bool(subscription.queue.get_nowait()["estop"]),
            bool(subscription.queue.get_nowait()["estop"]),
        ]

    assert asyncio.run(exercise()) == [True, False]


def test_cancelled_publish_waiting_for_connection_lock_keeps_session_order(
    app_settings: RelaySettings, clock: MutableClock, event_ids: EventIds
) -> None:
    mutation_finished = Event()

    async def exercise() -> list[bool]:
        runtime = RelayRuntime(app_settings, clock=clock, event_ids=event_ids)
        session = runtime.session(SESSION)
        subscription = await runtime.subscribe(
            SESSION, Principal(source="console", drone_id=None, signing_key=CONSOLE_KEY)
        )

        def first_operation() -> list[dict[str, object]]:
            event = session.update_control_projection(estop=True)
            mutation_finished.set()
            return [event]

        async with runtime._connection_lock:
            first = asyncio.create_task(runtime.process_and_publish(SESSION, first_operation))
            assert await asyncio.to_thread(mutation_finished.wait, 2)
            first.cancel()
            second = asyncio.create_task(
                runtime.process_and_publish(
                    SESSION, lambda: [session.update_control_projection(estop=False)]
                )
            )
            await asyncio.sleep(0)
            assert not second.done()

        with pytest.raises(asyncio.CancelledError):
            await first
        await second
        return [
            bool(subscription.queue.get_nowait()["estop"]),
            bool(subscription.queue.get_nowait()["estop"]),
        ]

    assert asyncio.run(exercise()) == [True, False]


def test_cancelled_cleanup_eventually_releases_adapter_binding(
    app_settings: RelaySettings,
    clock: MutableClock,
    event_ids: EventIds,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    disconnect_started = Event()
    release_disconnect = Event()

    async def exercise() -> None:
        runtime = RelayRuntime(app_settings, clock=clock, event_ids=event_ids)
        session = runtime.session(SESSION)
        principal = Principal(source="adapter", drone_id=1, signing_key=ADAPTER_KEY)
        subscription = await runtime.subscribe(SESSION, principal)
        await runtime.process_and_publish(
            SESSION,
            lambda: session.process_frame(
                membership_payload(action="join", event_id="joined-before-cancel"), principal
            ),
        )
        original_disconnect = RelaySession.handle_adapter_disconnect

        def delayed_disconnect(
            target: RelaySession, *, drone_id: int, connection_epoch: int
        ) -> list[dict[str, object]]:
            disconnect_started.set()
            assert release_disconnect.wait(timeout=2)
            return original_disconnect(target, drone_id=drone_id, connection_epoch=connection_epoch)

        monkeypatch.setattr(RelaySession, "handle_adapter_disconnect", delayed_disconnect)
        cleanup = asyncio.create_task(
            runtime.cleanup_connection(SESSION, session, principal, subscription)
        )
        assert await asyncio.to_thread(disconnect_started.wait, 2)
        cleanup.cancel()
        release_disconnect.set()
        with pytest.raises(asyncio.CancelledError):
            await cleanup

        for _ in range(100):
            if (SESSION, 1) not in runtime._adapter_connections:
                break
            await asyncio.sleep(0.01)
        assert (SESSION, 1) not in runtime._adapter_connections
        assert session.current_state()["drones"][0]["membership"] == "disconnected"
        assert any(
            record["event"].get("action") == "unexpected_loss"
            for record in session.replay()["events"]
        )
        retry = await runtime.subscribe(SESSION, principal)
        await runtime.unsubscribe(SESSION, retry)

    asyncio.run(exercise())


def test_anyio_cancelled_cleanup_releases_binding_after_contended_unsubscribe(
    app_settings: RelaySettings,
    clock: MutableClock,
    event_ids: EventIds,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unsubscribe_started = asyncio.Event()

    async def exercise() -> None:
        runtime = RelayRuntime(app_settings, clock=clock, event_ids=event_ids)
        session = runtime.session(SESSION)
        principal = Principal(source="adapter", drone_id=1, signing_key=ADAPTER_KEY)
        subscription = await runtime.subscribe(SESSION, principal)
        await runtime.process_and_publish(
            SESSION,
            lambda: session.process_frame(
                membership_payload(action="join", event_id="joined-before-anyio-cancel"), principal
            ),
        )
        original_unsubscribe = runtime.unsubscribe

        async def skip_publish(_session_id: str, _events: list[dict[str, object]]) -> None:
            return None

        async def observed_unsubscribe(session_id: str, target: object) -> None:
            unsubscribe_started.set()
            await original_unsubscribe(session_id, target)

        monkeypatch.setattr(runtime, "unsubscribe", observed_unsubscribe)
        monkeypatch.setattr(runtime, "publish", skip_publish)
        scope_ready = asyncio.Event()
        scope: anyio.CancelScope | None = None

        async def cleanup_caller() -> None:
            nonlocal scope
            with anyio.CancelScope() as local_scope:
                scope = local_scope
                scope_ready.set()
                await runtime.cleanup_connection(SESSION, session, principal, subscription)

        async with anyio.create_task_group() as task_group:
            async with runtime._connection_lock:
                task_group.start_soon(cleanup_caller)
                await scope_ready.wait()
                await asyncio.wait_for(unsubscribe_started.wait(), timeout=2)
                assert scope is not None
                scope.cancel()

        for _ in range(100):
            if (SESSION, 1) not in runtime._adapter_connections:
                break
            await asyncio.sleep(0.01)
        assert (SESSION, 1) not in runtime._adapter_connections
        assert session.current_state()["drones"][0]["membership"] == "disconnected"

    asyncio.run(exercise())


def test_stop_waits_for_cancelled_background_operation_to_publish(
    app_settings: RelaySettings, clock: MutableClock, event_ids: EventIds
) -> None:
    worker_started = Event()
    release_worker = Event()

    async def exercise() -> None:
        runtime = RelayRuntime(app_settings, clock=clock, event_ids=event_ids)
        session = runtime.session(SESSION)
        subscription = await runtime.subscribe(
            SESSION, Principal(source="console", drone_id=None, signing_key=CONSOLE_KEY)
        )

        def delayed_operation() -> list[dict[str, object]]:
            worker_started.set()
            assert release_worker.wait(timeout=2)
            return [session.update_control_projection(estop=True)]

        operation = asyncio.create_task(runtime.process_and_publish(SESSION, delayed_operation))
        assert await asyncio.to_thread(worker_started.wait, 2)
        operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await operation
        stopping = asyncio.create_task(runtime.stop())
        await asyncio.sleep(0)
        assert not stopping.done()
        release_worker.set()
        await stopping
        assert subscription.queue.get_nowait()["estop"] is True

    asyncio.run(exercise())


def test_blocked_session_fanout_does_not_block_another_session(
    app_settings: RelaySettings,
    clock: MutableClock,
    event_ids: EventIds,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blocked_started = Event()
    release_blocked = Event()

    async def exercise() -> None:
        runtime = RelayRuntime(app_settings, clock=clock, event_ids=event_ids)
        blocked = runtime.session("session-a")
        available = runtime.session("session-b")
        original_periodic = type(blocked).periodic_events

        def periodic_events(session: RelaySession) -> list[dict[str, object]]:
            if session is blocked:
                blocked_started.set()
                assert release_blocked.wait(timeout=2)
            return original_periodic(session)

        monkeypatch.setattr(type(blocked), "periodic_events", periodic_events)
        subscription = await runtime.subscribe(
            available.session_id,
            Principal(source="console", drone_id=None, signing_key=CONSOLE_KEY),
        )
        await runtime.start()
        try:
            assert await asyncio.to_thread(blocked_started.wait, 2)
            event = await asyncio.wait_for(subscription.queue.get(), timeout=0.5)
            assert event["type"] == "state"
        finally:
            release_blocked.set()
            await runtime.stop()

    asyncio.run(exercise())
