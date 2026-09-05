from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from adapters.dispatch import AdapterDispatcher
from adapters.dji_mini3.remote import CommandRequest, RemoteBridgeAdapter
from adapters.protocols import AdapterError
from adapters.sim.camera import SimCamera
from adapters.sim.flight import SimFlightAdapter
from arbiter.safety import SafetyArbiter
from planner.models import CommandOperation
from relay.app import RelayRuntime
from relay.bridge import RelayNodeLink, build_adapters, build_dispatcher
from relay.settings import AdapterBackend, RelaySettings
from relay.tests.conftest import ADAPTER_KEY, CONSOLE_KEY, SESSION, EventIds, MutableClock
from tests.autonomy_fixtures import camera_config, make_snapshot, safety_config


def _settings(log_dir: Path, backend: AdapterBackend = AdapterBackend.SIM) -> RelaySettings:
    return RelaySettings(
        relay_token=CONSOLE_KEY,
        adapter_keys={1: ADAPTER_KEY},
        log_dir=log_dir,
        adapter_backend=backend,
    )


def _hover_request() -> CommandRequest:
    return CommandRequest(
        command_id="command-1",
        intent_id="intent-1",
        roster_version=1,
        drone_id=1,
        connection_epoch=1,
        operation=CommandOperation.HOVER,
        args={},
    )


def test_node_link_refuses_the_relay_loop_thread_for_send_and_await(
    tmp_path: Path, clock: MutableClock, event_ids: EventIds
) -> None:
    async def exercise() -> RelayNodeLink:
        runtime = RelayRuntime(_settings(tmp_path), clock=clock, event_ids=event_ids)
        await runtime.start()
        try:
            await runtime.activate_session(SESSION)
            link = RelayNodeLink(runtime, SESSION, delivery_timeout_ms=100)
            with pytest.raises(AdapterError, match="worker thread"):
                link.send(_hover_request())
            with pytest.raises(AdapterError, match="worker thread"):
                link.await_acknowledgement("command-1", timeout_ms=10)
            from_worker = await asyncio.to_thread(
                link.await_acknowledgement, "command-1", timeout_ms=10
            )
            assert from_worker is None
        finally:
            await runtime.stop()
        return link

    link = asyncio.run(exercise())

    with pytest.raises(AdapterError, match="not started"):
        link.send(_hover_request())
    with pytest.raises(AdapterError, match="not started"):
        link.await_acknowledgement("command-1", timeout_ms=10)


def test_build_adapters_selects_the_configured_backend(
    tmp_path: Path, clock: MutableClock, event_ids: EventIds
) -> None:
    snapshot = make_snapshot(1, selection=(1,))
    sim_runtime = RelayRuntime(_settings(tmp_path / "sim"), clock=clock, event_ids=event_ids)
    sim_runtime.session(SESSION)
    remote_runtime = RelayRuntime(
        _settings(tmp_path / "remote", AdapterBackend.REMOTE), clock=clock, event_ids=event_ids
    )
    remote_runtime.session(SESSION)

    sim = build_adapters(sim_runtime, SESSION, snapshot, sim_camera_config=camera_config())
    remote = build_adapters(remote_runtime, SESSION, snapshot)

    assert isinstance(sim.flight, SimFlightAdapter)
    assert isinstance(sim.camera, SimCamera)
    assert isinstance(remote.flight, RemoteBridgeAdapter)
    assert remote.camera is remote.flight
    with pytest.raises(ValueError, match="SimCameraConfig"):
        build_adapters(sim_runtime, SESSION, snapshot)
    with pytest.raises(ValueError, match="not active"):
        build_adapters(remote_runtime, "session-missing", snapshot)


def test_build_dispatcher_wires_the_backend_adapters_and_arbiter(
    tmp_path: Path, clock: MutableClock, event_ids: EventIds
) -> None:
    snapshot = make_snapshot(1, selection=(1,))
    runtime = RelayRuntime(
        _settings(tmp_path, AdapterBackend.REMOTE), clock=clock, event_ids=event_ids
    )
    runtime.session(SESSION)
    arbiter = SafetyArbiter(safety_config())

    dispatcher = build_dispatcher(runtime, SESSION, snapshot, arbiter=arbiter)

    assert isinstance(dispatcher, AdapterDispatcher)
    assert dispatcher.arbiter is arbiter
    assert isinstance(dispatcher.flight, RemoteBridgeAdapter)
    assert dispatcher.camera is dispatcher.flight


def test_build_adapters_applies_the_link_wrapper_on_the_remote_backend_only(
    tmp_path: Path, clock: MutableClock, event_ids: EventIds
) -> None:
    snapshot = make_snapshot(1, selection=(1,))
    wrapped: list[object] = []

    def wrapper(link: RelayNodeLink) -> RelayNodeLink:
        wrapped.append(link)
        return link

    sim_runtime = RelayRuntime(_settings(tmp_path / "sim"), clock=clock, event_ids=event_ids)
    sim_runtime.session(SESSION)
    remote_runtime = RelayRuntime(
        _settings(tmp_path / "remote", AdapterBackend.REMOTE), clock=clock, event_ids=event_ids
    )
    remote_runtime.session(SESSION)

    build_adapters(
        sim_runtime, SESSION, snapshot, sim_camera_config=camera_config(), link_wrapper=wrapper
    )
    assert wrapped == []
    dispatcher = build_dispatcher(
        remote_runtime,
        SESSION,
        snapshot,
        arbiter=SafetyArbiter(safety_config()),
        link_wrapper=wrapper,
    )

    assert len(wrapped) == 1
    assert isinstance(wrapped[0], RelayNodeLink)
    assert isinstance(dispatcher.flight, RemoteBridgeAdapter)
