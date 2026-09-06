"""Relay-side node link: sign, sequence, deliver, and await over the node's socket."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import TimeoutError as DeliveryTimeout
from dataclasses import dataclass
from typing import TYPE_CHECKING

from adapters.dispatch import AdapterDispatcher
from adapters.dji_mini3.remote import CommandRequest, NodeLink, RemoteBridgeAdapter
from adapters.protocols import AdapterError, CameraCapture, SwarmAdapter
from adapters.sim.camera import SimCamera, SimCameraConfig
from adapters.sim.flight import SimFlightAdapter
from arbiter.safety import SafetyArbiter
from planner.models import FleetSnapshot
from relay.app import RelayRuntime
from relay.contracts import AdapterAcknowledgement, CapabilitiesFrame, MediaFileRecord
from relay.session import RelaySession

if TYPE_CHECKING:
    from planner.models import Command, Plan
    from relay.navigation_control import NavigationControl
from relay.settings import AdapterBackend


class RelayNodeLink:
    """``NodeLink`` for one session over the live relay runtime.

    Drive it from a worker thread, never from the relay event loop: delivery is
    scheduled onto the loop and awaited synchronously, and acknowledgements arrive
    through the session as the node's socket frames are processed. Both ``send`` and
    ``await_acknowledgement`` block the calling thread and refuse the loop thread.
    """

    def __init__(
        self,
        runtime: RelayRuntime,
        session_id: str,
        *,
        delivery_timeout_ms: int,
        navigation_control: NavigationControl | None = None,
    ) -> None:
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
        self._navigation_control = navigation_control

    def connection_epoch(self, drone_id: int) -> int | None:
        return self._session.registry.connection_epoch(drone_id)

    def send(self, request: CommandRequest) -> None:
        loop = self._worker_loop()
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

    def authorize_navigation(self, plan: Plan, command: Command, snapshot: FleetSnapshot) -> None:
        if self._navigation_control is None:
            raise AdapterError("mapped navigation has no approved phone control")
        packet = self._navigation_control.authorize(plan, command, snapshot, self._session_id)
        initial_pose = self._navigation_control.initial_pose(
            command.drone_id, self._session, snapshot.now_ms
        )
        loop = self._worker_loop()
        for frame in (packet, initial_pose):
            self._session.record_navigation_packet(frame)
            future = asyncio.run_coroutine_threadsafe(
                self._runtime.deliver_to_node(self._session_id, command.drone_id, frame), loop
            )
            try:
                delivered = future.result(timeout=self._delivery_timeout_s)
            except DeliveryTimeout:
                future.cancel()
                delivered = False
            if not delivered:
                raise AdapterError("navigation authorization could not be delivered")

    def await_acknowledgement(
        self, command_id: str, *, timeout_ms: int
    ) -> AdapterAcknowledgement | None:
        self._worker_loop()
        return self._session.await_command_acknowledgement(command_id, timeout_ms=timeout_ms)

    def camera_capabilities(self, drone_id: int) -> CapabilitiesFrame | None:
        return self._session.registry.camera_capabilities(drone_id)

    def media_files(self, drone_id: int, capture_id: str) -> tuple[MediaFileRecord, ...]:
        return self._session.media_files(drone_id, capture_id)

    def _worker_loop(self) -> asyncio.AbstractEventLoop:
        """Return the started relay loop after proving this thread is not running it."""
        loop = self._runtime.loop
        if loop is None:
            raise AdapterError("relay runtime is not started")
        _refuse_loop_thread(loop)
        return loop


LinkWrapper = Callable[["RelayNodeLink"], NodeLink]


@dataclass(frozen=True, slots=True)
class AdapterPair:
    """The flight and camera adapters one session dispatches through."""

    flight: SwarmAdapter
    camera: CameraCapture


def build_adapters(
    runtime: RelayRuntime,
    session_id: str,
    snapshot: FleetSnapshot,
    *,
    sim_camera_config: SimCameraConfig | None = None,
    link_wrapper: LinkWrapper | None = None,
    navigation_control: NavigationControl | None = None,
) -> AdapterPair:
    """Construct the adapters ``SWEEP_ADAPTER_BACKEND`` selects for one session.

    ``sim`` builds the deterministic simulator from the snapshot and requires an
    explicit ``SimCameraConfig`` because the relay carries no camera fixture values.
    ``remote`` builds one ``RemoteBridgeAdapter`` serving as both flight and camera over
    a ``RelayNodeLink``; delivery and acknowledgement waits are bounded by the command
    TTL the relay stamps on every wire command and one command's total wait by
    ``SWEEP_COMMAND_DEADLINE_MS``. ``link_wrapper`` may decorate that
    link, for example to gate sends behind a preemption flag; ``sim`` ignores it.
    """
    settings = runtime.settings
    backend = settings.adapter_backend
    if backend is AdapterBackend.SIM:
        if sim_camera_config is None:
            raise ValueError("the sim backend requires an explicit SimCameraConfig")
        flight = SimFlightAdapter.from_snapshot(snapshot)
        camera = SimCamera(
            drone_epochs={
                drone_id: aircraft.connection_epoch
                for drone_id, aircraft in snapshot.aircraft.items()
            },
            pose_provider=flight.camera_pose,
            config=sim_camera_config,
        )
        return AdapterPair(flight=flight, camera=camera)
    if backend is AdapterBackend.REMOTE:
        node_link = RelayNodeLink(
            runtime,
            session_id,
            delivery_timeout_ms=settings.command_ttl_ms,
            navigation_control=navigation_control,
        )
        link: NodeLink = node_link if link_wrapper is None else link_wrapper(node_link)
        remote = RemoteBridgeAdapter.from_snapshot(
            link,
            snapshot,
            acknowledgement_timeout_ms=settings.command_ttl_ms,
            command_deadline_ms=settings.command_deadline_ms,
        )
        return AdapterPair(flight=remote, camera=remote)
    raise ValueError(f"unknown adapter backend {backend!r}")


def build_dispatcher(
    runtime: RelayRuntime,
    session_id: str,
    snapshot: FleetSnapshot,
    *,
    arbiter: SafetyArbiter,
    sim_camera_config: SimCameraConfig | None = None,
    link_wrapper: LinkWrapper | None = None,
    navigation_control: NavigationControl | None = None,
) -> AdapterDispatcher:
    """Construct a session's ``AdapterDispatcher`` on the configured backend."""
    adapters = build_adapters(
        runtime,
        session_id,
        snapshot,
        sim_camera_config=sim_camera_config,
        link_wrapper=link_wrapper,
        navigation_control=navigation_control,
    )
    return AdapterDispatcher(flight=adapters.flight, camera=adapters.camera, arbiter=arbiter)


def _refuse_loop_thread(loop: asyncio.AbstractEventLoop) -> None:
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        return
    if running is loop:
        raise AdapterError("RelayNodeLink must be driven from a worker thread, not the relay loop")
