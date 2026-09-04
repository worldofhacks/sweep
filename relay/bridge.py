"""Relay-side node link: sign, sequence, deliver, and await over the node's socket."""

from __future__ import annotations

import asyncio
from concurrent.futures import TimeoutError as DeliveryTimeout

from adapters.dji_mini3.remote import CommandRequest
from adapters.protocols import AdapterError
from relay.app import RelayRuntime
from relay.contracts import AdapterAcknowledgement, CapabilitiesFrame, MediaFileRecord
from relay.session import RelaySession


class RelayNodeLink:
    """``NodeLink`` for one session over the live relay runtime.

    Drive it from a worker thread, never from the relay event loop: delivery is
    scheduled onto the loop and awaited synchronously, and acknowledgements arrive
    through the session as the node's socket frames are processed.
    """

    def __init__(self, runtime: RelayRuntime, session_id: str, *, delivery_timeout_ms: int) -> None:
        session = runtime.sessions.get(session_id)
        if session is None:
            raise ValueError(f"session {session_id!r} is not active on this relay")
        if (
            not isinstance(delivery_timeout_ms, int)
            or isinstance(delivery_timeout_ms, bool)
            or delivery_timeout_ms <= 0
        ):
            raise ValueError("delivery_timeout_ms must be a positive integer")
        self._runtime = runtime
        self._session_id = session_id
        self._session: RelaySession = session
        self._delivery_timeout_s = delivery_timeout_ms / 1000

    def connection_epoch(self, drone_id: int) -> int | None:
        return self._session.registry.connection_epoch(drone_id)

    def send(self, request: CommandRequest) -> None:
        loop = self._runtime.loop
        if loop is None:
            raise AdapterError("relay runtime is not started")
        _refuse_loop_thread(loop)
        key = self._runtime.credential_resolver.resolve("adapter", request.drone_id)
        if key is None:
            raise AdapterError(
                f"no adapter credential is configured for aircraft {request.drone_id}"
            )
        if not self._runtime.node_connected(self._session_id, request.drone_id):
            raise AdapterError(f"aircraft {request.drone_id} has no authenticated node socket")
        try:
            frame = self._session.issue_command(
                command_id=request.command_id,
                intent_id=request.intent_id,
                roster_version=request.roster_version,
                drone_id=request.drone_id,
                connection_epoch=request.connection_epoch,
                operation=request.operation,
                args=request.args,
                signing_key=key,
            )
        except ValueError as error:
            raise AdapterError(str(error)) from error
        future = asyncio.run_coroutine_threadsafe(
            self._runtime.deliver_to_node(self._session_id, request.drone_id, frame), loop
        )
        try:
            delivered = future.result(timeout=self._delivery_timeout_s)
        except DeliveryTimeout:
            future.cancel()
            delivered = False
        if not delivered:
            self._session.discard_command_waiter(request.command_id)
            raise AdapterError(
                f"command {request.command_id} could not be delivered to aircraft "
                f"{request.drone_id}"
            )

    def await_acknowledgement(
        self, command_id: str, *, timeout_ms: int
    ) -> AdapterAcknowledgement | None:
        return self._session.await_command_acknowledgement(command_id, timeout_ms=timeout_ms)

    def camera_capabilities(self, drone_id: int) -> CapabilitiesFrame | None:
        return self._session.registry.camera_capabilities(drone_id)

    def media_files(self, drone_id: int, capture_id: str) -> tuple[MediaFileRecord, ...]:
        return self._session.media_files(drone_id, capture_id)


def _refuse_loop_thread(loop: asyncio.AbstractEventLoop) -> None:
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        return
    if running is loop:
        raise AdapterError("RelayNodeLink must be driven from a worker thread, not the relay loop")
