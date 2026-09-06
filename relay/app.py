"""FastAPI WebSocket relay, authenticated fan-out, metrics, and replay."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from threading import Lock, RLock

from anyio import CancelScope
from fastapi import FastAPI, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from perception.detection_contracts import PerceptionEvent
from relay.audit import LIVE_REPLAY_TIMEOUT_SECONDS, AuditLogError, SessionAuditLog
from relay.auth import (
    AuthenticationError,
    CredentialResolver,
    Principal,
    authenticate,
    sign_event,
)
from relay.capabilities import C1_CAPABILITY_PROFILE, CapabilityProfile
from relay.control_localization import ControlLocalizationProjector
from relay.detection_attention import DetectionAttention
from relay.intent_v1 import REGISTERED_SOURCES
from relay.media import MediaEvidence, MediaMonitor, MediaMtxClient
from relay.session import (
    Clock,
    ControlPoseSigningKey,
    EventIdFactory,
    IntentSink,
    LeaveAuthorizer,
    RelaySession,
)
from relay.settings import RelaySettings, console_origins_from_env
from relay.voice import (
    MAX_AUDIO_BYTES,
    MAX_AUDIO_DURATION_MS,
    TranscriptService,
    VoiceOutcome,
    configured_transcription,
)

IntentSinkFactory = Callable[[RelaySession], IntentSink | None]
LeaveAuthorizerFactory = Callable[[str], LeaveAuthorizer | None]
_LOGGER = logging.getLogger(__name__)
ShutdownCallback = Callable[[], None]
StartupCallback = Callable[[], None]
_OUTBOUND_LIMIT = 128
_SEND_TIMEOUT_SECONDS = 5.0
_CLOSE_TIMEOUT_SECONDS = 1.0
_CONTROL_HEARTBEAT_MAX_INTERVAL_SECONDS = 1.0
TranscriptServiceFactory = Callable[["RelayRuntime"], TranscriptService]
AuthoritativeRoomsFactory = Callable[[RelaySession], tuple[str, ...]]
ControlLocalizationFactory = Callable[[str], ControlLocalizationProjector | None]
MediaMonitorFactory = Callable[[RelaySettings, Clock], MediaMonitor | None]
RecordedFrameProcessor = Callable[[str, int, str], tuple[PerceptionEvent, ...]]


def default_media_monitor(settings: RelaySettings, clock: Clock) -> MediaMonitor | None:
    """The MediaMTX poller behind the video projection, or ``None`` without an API URL."""
    if settings.media_api_url is None or settings.media_api_password is None:
        return None
    client = MediaMtxClient(
        settings.media_api_url,
        username=settings.media_api_username,
        password=settings.media_api_password,
        timeout_s=settings.media_api_timeout_ms / 1_000,
    )
    return MediaMonitor(
        client,
        clock=clock,
        poll_interval_ms=settings.media_poll_interval_ms,
        stale_after_ms=settings.media_stale_after_ms,
    )


@dataclass(eq=False, slots=True)
class _Subscription:
    connection_id: str
    principal: Principal
    initial_state: dict[str, object]
    roster_version: int
    queue: asyncio.Queue[_Outbound] = field(
        default_factory=lambda: asyncio.Queue(maxsize=_OUTBOUND_LIMIT)
    )
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    sender_failed: asyncio.Event = field(default_factory=asyncio.Event)
    overflowed: asyncio.Event = field(default_factory=asyncio.Event)

    def enqueue(self, outbound: _Outbound) -> bool:
        """Queue ordered events, conflating state and dominated pose diagnostics."""
        if self.overflowed.is_set() or self.sender_failed.is_set():
            _resolve_delivery(outbound, False)
            return False
        if outbound.event.get("type") in {"state", "control_pose", "navigation_pose"}:
            retained: list[_Outbound] = []
            while not self.queue.empty():
                pending = self.queue.get_nowait()
                self.queue.task_done()
                replace_state = (
                    outbound.event.get("type") == "state"
                    and pending.event.get("type") == "state"
                    and pending.delivered is None
                    and pending.event.get("invalidation_reason") is None
                    and _same_state_projection(pending.event, outbound.event)
                )
                replace_control_pose = _supersedes_control_pose(outbound, pending)
                if not replace_state and not replace_control_pose:
                    retained.append(pending)
            for pending in retained:
                self.queue.put_nowait(pending)
        try:
            self.queue.put_nowait(outbound)
        except asyncio.QueueFull:
            self.overflowed.set()
            _resolve_delivery(outbound, False)
            return False
        return True


@dataclass(frozen=True, slots=True)
class _Outbound:
    event: dict[str, object]
    delivered: asyncio.Future[bool] | None = None


def _supersedes_control_pose(new: _Outbound, pending: _Outbound) -> bool:
    """Conflate a drone's diagnostics without hiding a queued safer transition."""
    if (
        new.event.get("type") not in {"control_pose", "navigation_pose"}
        or pending.event.get("type") != new.event.get("type")
        or pending.delivered is not None
        or pending.event.get("drone_id") != new.event.get("drone_id")
    ):
        return False
    new_status = new.event.get("status")
    if new_status == "ready":
        superseded = ("ready",)
    elif new_status == "hold":
        superseded = ("ready", "hold")
    elif new_status == "land":
        superseded = ("ready", "hold", "land")
    else:
        return False
    return pending.event.get("status") in superseded


@dataclass(slots=True)
class _SessionGate:
    lock: RLock = field(default_factory=RLock)
    users: int = 0


class RelayRuntime:
    def __init__(
        self,
        settings: RelaySettings,
        *,
        credential_resolver: CredentialResolver | None = None,
        clock: Clock | None = None,
        event_ids: EventIdFactory | None = None,
        intent_sink_factory: IntentSinkFactory | None = None,
        capability_profile: CapabilityProfile = C1_CAPABILITY_PROFILE,
        leave_authorizer_factory: LeaveAuthorizerFactory | None = None,
        authoritative_rooms_factory: AuthoritativeRoomsFactory | None = None,
        control_localization_factory: ControlLocalizationFactory | None = None,
        control_pose_signing_key: ControlPoseSigningKey | None = None,
        media_monitor: MediaMonitor | None = None,
        detection_attention: DetectionAttention | None = None,
        recorded_frame_processor: RecordedFrameProcessor | None = None,
    ) -> None:
        self.settings = settings
        self.media_monitor = media_monitor
        self.detection_attention = DetectionAttention() if detection_attention is None else detection_attention
        self.recorded_frame_processor = recorded_frame_processor
        self.credential_resolver = credential_resolver or settings.credential_resolver()
        self.clock = clock or _epoch_ms
        self.event_ids = event_ids or (lambda: str(uuid.uuid4()))
        declared_profile = getattr(intent_sink_factory, "capability_profile", None)
        if declared_profile is None:
            factory_owner = getattr(intent_sink_factory, "__self__", None)
            declared_profile = getattr(factory_owner, "capability_profile", None)
        if declared_profile is not None and declared_profile != capability_profile:
            raise ValueError("intent sink factory and relay runtime use different profiles")
        self.intent_sink_factory = intent_sink_factory
        self.capability_profile = capability_profile
        self.leave_authorizer_factory = leave_authorizer_factory
        self.authoritative_rooms_factory = authoritative_rooms_factory
        self.control_localization_factory = control_localization_factory
        self.control_pose_signing_key = (
            settings.adapter_keys.get
            if control_pose_signing_key is None
            else control_pose_signing_key
        )
        self.sessions: dict[str, RelaySession] = {}
        self._subscriptions: dict[str, dict[str, _Subscription]] = {}
        self._adapter_connections: dict[tuple[str, int], str] = {}
        self._localization_connections: dict[tuple[str, int], str] = {}
        self._session_gates: dict[str, _SessionGate] = {}
        self._session_gates_lock = Lock()
        self._activation_tasks: dict[str, asyncio.Task[RelaySession]] = {}
        self._activation_tasks_lock = Lock()
        self._session_operations: dict[str, asyncio.Lock] = {}
        self._background_operations: set[asyncio.Task[object]] = set()
        self._connection_lock = asyncio.Lock()
        self._fanout_failed_sessions: set[str] = set()
        self._fanout_task: asyncio.Task[None] | None = None
        self._fanout_session_tasks: dict[str, asyncio.Task[None]] = {}
        self._control_heartbeat_last: dict[str, float] = {}
        self._control_heartbeat_sequence: dict[str, int] = {}
        self.loop: asyncio.AbstractEventLoop | None = None

    def session(self, session_id: str) -> RelaySession:
        _validate_session_id(session_id)
        with self._session_gate(session_id):
            session = self.sessions.get(session_id)
            if session is None:
                audit_log = SessionAuditLog(self.settings.log_dir, session_id)
                if audit_log.had_persisted_log:
                    raise AuthenticationError(
                        "session_closed",
                        "persisted sessions are replay-only after a relay process restart; "
                        "use a new session ID",
                    )
                leave_authorizer = (
                    None
                    if self.leave_authorizer_factory is None
                    else self.leave_authorizer_factory(session_id)
                )
                projector = (
                    None
                    if self.control_localization_factory is None
                    else self.control_localization_factory(session_id)
                )
                session = RelaySession(
                    session_id=session_id,
                    audit_log=audit_log,
                    limits=self.settings.limits(),
                    clock=self.clock,
                    event_ids=self.event_ids,
                    leave_authorizer=leave_authorizer,
                    capability_profile=self.capability_profile,
                    control_localization_projector=projector,
                    control_pose_signing_key=self.control_pose_signing_key,
                    media_evidence=self.media_evidence,
                )
                if self.intent_sink_factory is not None:
                    session.intent_sink = self.intent_sink_factory(session)
                self.sessions[session_id] = session
            return session

    def media_evidence(self, drone_id: int, now_ms: int) -> MediaEvidence | None:
        """The monitor's last completed MediaMTX read for one aircraft; never blocks."""
        if self.media_monitor is None:
            return None
        return self.media_monitor.evidence(drone_id, now_ms)

    def replay(self, session_id: str, *, after_sequence: int = 0) -> dict[str, object]:
        """Read active or persisted history without reopening mutable live state."""
        _validate_session_id(session_id)
        deadline = time.monotonic() + LIVE_REPLAY_TIMEOUT_SECONDS
        with self._session_gate(session_id, deadline=deadline):
            session = self.sessions.get(session_id)
            if session is None:
                audit_log = SessionAuditLog(self.settings.log_dir, session_id)
                records, last_sequence = audit_log.replay_snapshot(
                    after_sequence=after_sequence, deadline=deadline
                )
                return {
                    "v": 1,
                    "t": self.clock(),
                    "type": "replay",
                    "event_id": self.event_ids(),
                    "session": session_id,
                    "after_sequence": after_sequence,
                    "last_sequence": last_sequence,
                    "events": records,
                }
        return session.replay(after_sequence=after_sequence, deadline=deadline)

    async def activate_session(self, session_id: str) -> RelaySession:
        with self._activation_tasks_lock:
            task = self._activation_tasks.get(session_id)
            if task is None:
                task = asyncio.create_task(asyncio.to_thread(self.session, session_id))
                self._activation_tasks[session_id] = task
                task.add_done_callback(
                    lambda completed, session=session_id: self._clear_activation_task(
                        session, completed
                    )
                )
        return await asyncio.shield(task)

    def authoritative_rooms(self, session: RelaySession) -> tuple[str, ...]:
        if self.authoritative_rooms_factory is None:
            return ()
        return self.authoritative_rooms_factory(session)

    @contextlib.contextmanager
    def _session_gate(self, session_id: str, *, deadline: float | None = None) -> Iterator[None]:
        with self._session_gates_lock:
            gate = self._session_gates.setdefault(session_id, _SessionGate())
            gate.users += 1
        try:
            if deadline is None:
                acquired = gate.lock.acquire()
            else:
                remaining = deadline - time.monotonic()
                acquired = remaining > 0 and gate.lock.acquire(timeout=remaining)
            if not acquired:
                raise AuditLogError("relay replay exceeded the live replay deadline")
            try:
                yield
            finally:
                gate.lock.release()
        finally:
            with self._session_gates_lock:
                gate.users -= 1
                if gate.users == 0:
                    del self._session_gates[session_id]

    def _clear_activation_task(
        self, session_id: str, completed: asyncio.Task[RelaySession]
    ) -> None:
        with self._activation_tasks_lock:
            if self._activation_tasks.get(session_id) is completed:
                del self._activation_tasks[session_id]

    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        self._fanout_task = asyncio.create_task(self._fanout_loop())
        if self.media_monitor is not None:
            await self.media_monitor.start()

    async def stop(self) -> None:
        if self.media_monitor is not None:
            await self.media_monitor.stop()
        if self._fanout_task is not None:
            self._fanout_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._fanout_task
        tasks = tuple(self._fanout_session_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        while self._background_operations:
            pending = tuple(task for task in self._background_operations if not task.done())
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            # gather completes synchronously for finished tasks, so yield once so their
            # queued done callbacks can run, then drop whatever has already finished.
            await asyncio.sleep(0)
            self._background_operations.difference_update(
                task for task in tuple(self._background_operations) if task.done()
            )
        self._fanout_task = None
        self.loop = None

    def node_connected(self, session_id: str, drone_id: int) -> bool:
        return (session_id, drone_id) in self._adapter_connections

    async def deliver_to_node(
        self, session_id: str, drone_id: int, frame: dict[str, object]
    ) -> bool:
        """Queue a relay-authored frame for the one socket bound to this aircraft."""
        async with self._connection_lock:
            connection_id = self._adapter_connections.get((session_id, drone_id))
            if connection_id is None:
                return False
            subscription = self._subscriptions.get(session_id, {}).get(connection_id)
            if subscription is None:
                return False
            return subscription.enqueue(_Outbound(frame))

    async def subscribe(self, session_id: str, principal: Principal) -> _Subscription:
        """Freeze the initial snapshot at the same linearization point as activation."""
        async with self._session_operation(session_id):
            async with self._connection_lock:
                if principal.source == "adapter":
                    assert principal.drone_id is not None
                    key = (session_id, principal.drone_id)
                    if key in self._adapter_connections:
                        raise AuthenticationError(
                            "adapter_already_connected",
                            "an authenticated connection is already bound to this drone",
                        )
                if principal.source == "localization":
                    assert principal.drone_id is not None
                    key = (session_id, principal.drone_id)
                    if key in self._localization_connections:
                        raise AuthenticationError(
                            "localization_already_connected",
                            "a localization producer is already bound to this drone",
                        )
                session = self.sessions[session_id]
                initial_state = session.current_state_if_available()
                if initial_state is None:
                    initial_state = await asyncio.to_thread(session.current_state)
                subscription = _Subscription(
                    connection_id=self.event_ids(),
                    principal=principal,
                    initial_state=initial_state,
                    roster_version=int(initial_state["roster_version"]),
                )
                if principal.source == "adapter":
                    assert principal.drone_id is not None
                    key = (session_id, principal.drone_id)
                    self._adapter_connections[key] = subscription.connection_id
                elif principal.source == "localization":
                    assert principal.drone_id is not None
                    key = (session_id, principal.drone_id)
                    self._localization_connections[key] = subscription.connection_id
                self._subscriptions.setdefault(session_id, {})[subscription.connection_id] = (
                    subscription
                )
        return subscription

    @asynccontextmanager
    async def _session_operation(self, session_id: str):
        lock = self._session_operations.setdefault(session_id, asyncio.Lock())
        async with lock:
            yield

    async def process_and_publish(
        self,
        session_id: str,
        operation: Callable[[], list[dict[str, object]]],
        *,
        wait_for_connection_id: str | None = None,
    ) -> list[dict[str, object]]:
        """Caller cancellation leaves the ordered mutation and publication running."""
        task = asyncio.create_task(
            self._process_and_publish(
                session_id,
                operation,
                wait_for_connection_id=wait_for_connection_id,
            )
        )
        self._track_background_operation(task)
        return await asyncio.shield(task)

    async def record_detection_events(
        self, session_id: str, drone_id: int, events: tuple[PerceptionEvent, ...]
    ) -> list[dict[str, object]]:
        session = self.sessions.get(session_id)
        if session is None:
            raise ValueError("session is unavailable for detection events")
        return await self.process_and_publish(
            session_id,
            lambda: self.detection_attention.record(session, drone_id, events),
        )

    async def acknowledge_detection(
        self, session_id: str, detection_id: str, principal: Principal
    ) -> list[dict[str, object]]:
        if principal.source != "console":
            raise ValueError("only the console operator can acknowledge a detection")
        session = self.sessions.get(session_id)
        if session is None:
            raise ValueError("session is unavailable for detection acknowledgement")
        return await self.process_and_publish(
            session_id,
            lambda: self.detection_attention.acknowledge(session, detection_id, principal.source),
        )

    async def _process_and_publish(
        self,
        session_id: str,
        operation: Callable[[], list[dict[str, object]]],
        *,
        wait_for_connection_id: str | None = None,
    ) -> list[dict[str, object]]:
        deliveries: list[asyncio.Future[bool]] = []
        async with self._session_operation(session_id):
            events = await asyncio.to_thread(operation)
            await self.publish(
                session_id,
                events,
                wait_for_connection_id=wait_for_connection_id,
                deferred_deliveries=deliveries,
            )
        delivered = (
            all(await asyncio.gather(*deliveries)) if deliveries else wait_for_connection_id is None
        )
        if not delivered:
            if len(events) == 1 and events[0].get("status") == "accepted":
                intent_id = events[0].get("intent_id")
                if isinstance(intent_id, str):
                    failed = await asyncio.to_thread(
                        self.sessions[session_id].fail_pending_intent,
                        intent_id,
                        reason="acceptance_delivery_failed",
                        detail="the accepting connection did not receive the acknowledgement",
                    )
                    await self.publish(session_id, failed)
            raise WebSocketDisconnect(code=1006)
        return events

    async def process_acknowledgement_and_publish(
        self,
        session_id: str,
        session: RelaySession,
        frame: object,
        principal: Principal,
        *,
        wait_for_connection_id: str | None = None,
    ) -> list[dict[str, object]]:
        """Keep acknowledgement commits ordered while adapter I/O yields to safety work."""
        task = asyncio.create_task(
            self._process_acknowledgement_and_publish(
                session_id,
                session,
                frame,
                principal,
                wait_for_connection_id=wait_for_connection_id,
            )
        )
        self._track_background_operation(task)
        return await asyncio.shield(task)

    async def _process_acknowledgement_and_publish(
        self,
        session_id: str,
        session: RelaySession,
        frame: object,
        principal: Principal,
        *,
        wait_for_connection_id: str | None,
    ) -> list[dict[str, object]]:
        deliveries: list[asyncio.Future[bool]] = []
        async with self._session_operation(session_id):
            events = await asyncio.to_thread(
                session.process_acknowledgement,
                frame,
                principal,
                defer_resume=True,
            )
            terminal = any(
                event.get("type") == "acknowledgement"
                and event.get("command_id") is not None
                and event.get("status") in {"completed", "failed", "invalidated"}
                for event in events
            )
            if not any(event.get("type") == "refusal" for event in events):
                assert principal.drone_id is not None
                events.extend(
                    await asyncio.to_thread(
                        self.adapter_activity,
                        session,
                        drone_id=principal.drone_id,
                    )
                )
            await self.publish(
                session_id,
                events,
                wait_for_connection_id=wait_for_connection_id,
                deferred_deliveries=deliveries,
            )
            work = await asyncio.to_thread(session.prepare_resume) if terminal else None
        events.extend(
            await self._resume_and_publish(
                session_id,
                session,
                work,
                wait_for_connection_id=wait_for_connection_id,
                deliveries=deliveries,
            )
        )
        delivered = (
            all(await asyncio.gather(*deliveries)) if deliveries else wait_for_connection_id is None
        )
        if not delivered:
            raise WebSocketDisconnect(code=1006)
        return events

    async def _resume_and_publish(
        self,
        session_id: str,
        session: RelaySession,
        work: object,
        *,
        wait_for_connection_id: str | None = None,
        deliveries: list[asyncio.Future[bool]] | None = None,
    ) -> list[dict[str, object]]:
        events = []
        while work is not None:
            outcome = await asyncio.to_thread(session.resume_io, work)
            async with self._session_operation(session_id):
                committed = await asyncio.to_thread(session.commit_resume, work, outcome)
                events.extend(committed)
                await self.publish(
                    session_id,
                    committed,
                    wait_for_connection_id=wait_for_connection_id,
                    deferred_deliveries=deliveries,
                )
                work = await asyncio.to_thread(session.prepare_resume)
        return events

    def _track_background_operation(self, task: asyncio.Task[object]) -> None:
        self._background_operations.add(task)
        task.add_done_callback(self._background_operation_done)

    def _background_operation_done(self, task: asyncio.Task[object]) -> None:
        self._background_operations.discard(task)
        _log_background_failure(task)

    async def unsubscribe(self, session_id: str, subscription: _Subscription) -> None:
        async with self._connection_lock:
            subscriptions = self._subscriptions.get(session_id)
            if subscriptions is not None:
                subscriptions.pop(subscription.connection_id, None)
                if not subscriptions:
                    self._subscriptions.pop(session_id, None)
            principal = subscription.principal
            if principal.source == "adapter":
                assert principal.drone_id is not None
                key = (session_id, principal.drone_id)
                if self._adapter_connections.get(key) == subscription.connection_id:
                    self._adapter_connections.pop(key, None)
            elif principal.source == "localization":
                assert principal.drone_id is not None
                key = (session_id, principal.drone_id)
                if self._localization_connections.get(key) == subscription.connection_id:
                    self._localization_connections.pop(key, None)
            self._control_heartbeat_last.pop(subscription.connection_id, None)
            self._control_heartbeat_sequence.pop(subscription.connection_id, None)

    async def cleanup_connection(
        self,
        session_id: str,
        session: RelaySession,
        principal: Principal,
        subscription: _Subscription,
    ) -> None:
        """Complete disconnect and unbinding even when the socket task is cancelled."""
        task = asyncio.create_task(
            self._cleanup_connection(session_id, session, principal, subscription)
        )
        self._track_background_operation(task)
        with CancelScope(shield=True):
            await asyncio.shield(task)

    async def _cleanup_connection(
        self,
        session_id: str,
        session: RelaySession,
        principal: Principal,
        subscription: _Subscription,
    ) -> None:
        with CancelScope(shield=True):
            async with self._session_operation(session_id):
                try:
                    if principal.source == "adapter":
                        assert principal.drone_id is not None
                        try:
                            connection_epoch = session.registry.connection_epoch(principal.drone_id)
                            events = await asyncio.to_thread(
                                session.handle_adapter_disconnect,
                                drone_id=principal.drone_id,
                                connection_epoch=connection_epoch,
                            )
                            events.extend(
                                await asyncio.to_thread(
                                    self.adapter_disconnected,
                                    session,
                                    drone_id=principal.drone_id,
                                    connection_epoch=connection_epoch,
                                )
                            )
                            await self.publish(session_id, events)
                        except AuditLogError:
                            _LOGGER.exception(
                                "adapter disconnect audit failed session=%s drone=%s",
                                session_id,
                                principal.drone_id,
                            )
                finally:
                    await self.unsubscribe(session_id, subscription)

    async def publish(
        self,
        session_id: str,
        events: list[dict[str, object]],
        *,
        wait_for_connection_id: str | None = None,
        deferred_deliveries: list[asyncio.Future[bool]] | None = None,
    ) -> bool:
        """Queue an event batch atomically with respect to subscription activation."""
        deliveries: list[asyncio.Future[bool]] = []
        async with self._connection_lock:
            subscriptions = tuple(self._subscriptions.get(session_id, {}).values())
            for subscription in subscriptions:
                if subscription.sender_failed.is_set():
                    continue
                for event in events:
                    if event.get("type") in {
                        "control_pose",
                        "navigation_pose",
                        "navigation_route_authorization",
                    } and (
                        subscription.principal.source != "adapter"
                        or subscription.principal.drone_id != event.get("drone_id")
                    ):
                        continue
                    roster_version = event.get("roster_version")
                    if (
                        event.get("type") in {"membership", "state"}
                        and isinstance(roster_version, int)
                        and not isinstance(roster_version, bool)
                    ):
                        if roster_version < subscription.roster_version:
                            continue
                        subscription.roster_version = roster_version
                    delivered = (
                        asyncio.get_running_loop().create_future()
                        if subscription.connection_id == wait_for_connection_id
                        else None
                    )
                    if delivered is not None:
                        deliveries.append(delivered)
                    subscription.enqueue(_Outbound(event, delivered))
        if deferred_deliveries is not None:
            deferred_deliveries.extend(deliveries)
            return bool(deliveries) or wait_for_connection_id is None
        if not deliveries:
            return wait_for_connection_id is None
        return all(await asyncio.gather(*deliveries))

    def connection_count(self) -> int:
        return sum(len(subscriptions) for subscriptions in self._subscriptions.values())

    async def _fanout_loop(self) -> None:
        delay = 1 / self.settings.fanout_hz
        while True:
            await asyncio.sleep(delay)
            for session_id, session in tuple(self.sessions.items()):
                if (
                    session_id in self._fanout_failed_sessions
                    or session_id in self._fanout_session_tasks
                ):
                    continue
                task = asyncio.create_task(self._fanout_session(session_id, session))
                self._fanout_session_tasks[session_id] = task
                task.add_done_callback(
                    lambda completed, active_session=session_id: self._fanout_session_done(
                        active_session, completed
                    )
                )

    async def _fanout_session(self, session_id: str, session: RelaySession) -> None:
        try:
            await self.process_and_publish(session_id, lambda: self.periodic_events(session))
            await self._publish_control_heartbeats(session_id, session)
        except AuditLogError:
            self._fanout_failed_sessions.add(session_id)
            _LOGGER.exception("session fan-out stopped after audit failure session=%s", session_id)

    async def _publish_control_heartbeats(self, session_id: str, session: RelaySession) -> None:
        """Send each joined adapter its own signed, non-replayable control lease.

        Heartbeats are transport control frames, not fleet-history events: they are
        deliberately routed only to the authenticated adapter and are not appended
        to the audit log or broadcast to consoles.  The node accepts one only when
        its signature and current session/drone/epoch/roster identity all match.
        """
        now = time.monotonic()
        # Default to 1 Hz, but keep at least two lease opportunities inside a
        # shorter configured hold window (the fan-out loop remains the upper rate).
        interval = min(
            _CONTROL_HEARTBEAT_MAX_INTERVAL_SECONDS,
            self.settings.node_watchdog_hold_ms / 2_000,
        )
        # Match the session -> connection lock order used by mutations and fan-out.
        # That keeps the joined identity and the bound socket at one linearization
        # point, so an authenticated replacement cannot inherit the prior lease.
        async with self._session_operation(session_id):
            async with self._connection_lock:
                subscriptions = tuple(self._subscriptions.get(session_id, {}).values())
                for subscription in subscriptions:
                    principal = subscription.principal
                    if principal.source != "adapter" or principal.drone_id is None:
                        continue
                    last = self._control_heartbeat_last.get(subscription.connection_id)
                    if last is not None and now - last < interval:
                        continue
                    identity = session.registry.active_connection_identity(principal.drone_id)
                    if identity is None:
                        continue
                    connection_epoch, roster_version = identity
                    sequence = (
                        self._control_heartbeat_sequence.get(subscription.connection_id, 0) + 1
                    )
                    unsigned: dict[str, object] = {
                        "v": 1,
                        "t": self.clock(),
                        "type": "control_heartbeat",
                        "event_id": self.event_ids(),
                        "session": session_id,
                        "source": "relay",
                        "drone_id": principal.drone_id,
                        "connection_epoch": connection_epoch,
                        "roster_version": roster_version,
                        "seq": sequence,
                    }
                    event = {
                        **unsigned,
                        "signature": sign_event(unsigned, principal.signing_key),
                    }
                    if subscription.enqueue(_Outbound(event)):
                        self._control_heartbeat_last[subscription.connection_id] = now
                        self._control_heartbeat_sequence[subscription.connection_id] = sequence

    def _fanout_session_done(self, session_id: str, task: asyncio.Task[None]) -> None:
        if self._fanout_session_tasks.get(session_id) is task:
            del self._fanout_session_tasks[session_id]
        if not task.cancelled() and (error := task.exception()) is not None:
            _LOGGER.error(
                "session fan-out task failed session=%s",
                session_id,
                exc_info=(type(error), error, error.__traceback__),
            )

    def adapter_disconnected(
        self,
        session: RelaySession,
        *,
        drone_id: int,
        connection_epoch: int | None,
    ) -> list[dict[str, object]]:
        sink = session.intent_sink
        disconnected = getattr(sink, "adapter_disconnected", None)
        if not callable(disconnected) or connection_epoch is None:
            return []
        try:
            return disconnected(
                drone_id=drone_id,
                connection_epoch=connection_epoch,
                relay_state=session.current_state(),
            )
        except AuditLogError:
            raise
        except Exception:
            return [
                session.protocol_refusal(
                    reason="safety_runtime_error",
                    detail="the configured safety runtime failed closed",
                )
            ]

    def periodic_events(self, session: RelaySession) -> list[dict[str, object]]:
        sink = session.intent_sink
        ingress = getattr(sink, "periodic_ingress", None)
        if callable(ingress):
            try:
                events = ingress()
            except AuditLogError:
                raise
            except Exception:
                events = [
                    session.protocol_refusal(
                        reason="safety_runtime_error",
                        detail="the configured safety runtime failed closed",
                    )
                ]
        else:
            events = []
        events.extend(session.periodic_events())
        periodic = getattr(sink, "periodic_events", None)
        if callable(periodic):
            try:
                events.extend(periodic(events[-1]))
            except AuditLogError:
                raise
            except Exception:
                events.append(
                    session.protocol_refusal(
                        reason="safety_runtime_error",
                        detail="the configured safety runtime failed closed",
                    )
                )
        return events

    def process_frame(
        self,
        session: RelaySession,
        frame: object,
        principal: Principal,
    ) -> list[dict[str, object]]:
        events = session.process_frame(frame, principal)
        if (
            principal.source == "adapter"
            and principal.drone_id is not None
            and not any(event.get("type") == "refusal" for event in events)
        ):
            events.extend(self.adapter_activity(session, drone_id=principal.drone_id))
        return events

    def adapter_activity(
        self,
        session: RelaySession,
        *,
        drone_id: int,
    ) -> list[dict[str, object]]:
        sink = session.intent_sink
        activity = getattr(sink, "adapter_activity", None)
        if not callable(activity):
            return []
        try:
            return activity(drone_id=drone_id, relay_state=session.current_state())
        except AuditLogError:
            raise
        except Exception:
            return [
                session.protocol_refusal(
                    reason="safety_runtime_error",
                    detail="the configured safety runtime failed closed",
                )
            ]


def create_app(
    settings: RelaySettings | None = None,
    *,
    credential_resolver: CredentialResolver | None = None,
    clock: Clock | None = None,
    event_ids: EventIdFactory | None = None,
    intent_sink_factory: IntentSinkFactory | None = None,
    capability_profile: CapabilityProfile = C1_CAPABILITY_PROFILE,
    leave_authorizer_factory: LeaveAuthorizerFactory | None = None,
    authoritative_rooms_factory: AuthoritativeRoomsFactory | None = None,
    control_localization_factory: ControlLocalizationFactory | None = None,
    control_pose_signing_key: ControlPoseSigningKey | None = None,
    transcript_service_factory: TranscriptServiceFactory | None = None,
    startup_callback: StartupCallback | None = None,
    shutdown_callback: ShutdownCallback | None = None,
    media_monitor_factory: MediaMonitorFactory | None = None,
    recorded_frame_processor: RecordedFrameProcessor | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI):
        active_settings = settings or RelaySettings.from_env()
        active_clock = clock or _epoch_ms
        build_monitor = (
            default_media_monitor if media_monitor_factory is None else media_monitor_factory
        )
        runtime = RelayRuntime(
            active_settings,
            credential_resolver=credential_resolver,
            clock=active_clock,
            event_ids=event_ids,
            intent_sink_factory=intent_sink_factory,
            capability_profile=capability_profile,
            leave_authorizer_factory=leave_authorizer_factory,
            authoritative_rooms_factory=authoritative_rooms_factory,
            control_localization_factory=control_localization_factory,
            control_pose_signing_key=control_pose_signing_key,
            media_monitor=build_monitor(active_settings, active_clock),
            recorded_frame_processor=recorded_frame_processor,
        )
        application.state.relay_runtime = runtime
        application.state.transcript_service = (
            TranscriptService(
                transcription=configured_transcription(
                    provider=runtime.settings.transcription_provider
                )
            )
            if transcript_service_factory is None
            else transcript_service_factory(runtime)
        )
        await runtime.start()
        if startup_callback is not None:
            startup_callback()
        try:
            yield
        finally:
            await runtime.stop()
            if shutdown_callback is not None:
                shutdown_callback()

    application = FastAPI(title="Sweep relay", version="1", lifespan=lifespan)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=list(
            settings.console_origins if settings is not None else console_origins_from_env()
        ),
        allow_methods=["GET", "POST"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "X-Sweep-Correlation-Id",
            "X-Sweep-Audio-Duration-Ms",
        ],
    )

    @application.websocket("/ws/{session_id}")
    async def websocket_relay(websocket: WebSocket, session_id: str) -> None:
        runtime: RelayRuntime = websocket.app.state.relay_runtime
        await websocket.accept()
        try:
            _validate_session_id(session_id)
            raw_auth = await asyncio.wait_for(websocket.receive_json(), timeout=5)
            principal = authenticate(raw_auth, runtime.credential_resolver)
            session = await runtime.activate_session(session_id)
            accepted = _auth_accepted(runtime, session_id, principal)
            subscription = await runtime.subscribe(session_id, principal)
        except (AuthenticationError, ValueError, json.JSONDecodeError) as error:
            code = getattr(error, "code", "invalid_auth")
            detail = getattr(error, "detail", "authentication frame was not accepted")
            with contextlib.suppress(TimeoutError, WebSocketDisconnect, RuntimeError, OSError):
                await _send_json(
                    websocket,
                    _auth_refused(runtime, session_id=session_id, reason=code, detail=detail),
                )
            await _close_failed_socket(websocket, code=1008)
            return
        except TimeoutError:
            with contextlib.suppress(TimeoutError, WebSocketDisconnect, RuntimeError, OSError):
                await _send_json(
                    websocket,
                    _auth_refused(
                        runtime,
                        session_id=session_id,
                        reason="auth_timeout",
                        detail="authentication frame was not received in time",
                    ),
                )
            await _close_failed_socket(websocket, code=1008)
            return
        except WebSocketDisconnect:
            return
        except AuditLogError:
            await _close_failed_socket(websocket, code=1011)
            return

        sender: asyncio.Task[None] | None = None
        executions: set[asyncio.Task[None]] = set()
        try:
            await _send_json(websocket, accepted)
            await _send_json(websocket, subscription.initial_state)
            sender = asyncio.create_task(_send_events(websocket, subscription))
            while True:
                try:
                    assert sender is not None
                    frame = await _receive_or_sender_failure(websocket, sender)
                except json.JSONDecodeError:
                    await runtime.process_and_publish(
                        session_id,
                        lambda: [
                            session.protocol_refusal(
                                reason="invalid_json",
                                detail="frame must contain valid JSON",
                            )
                        ],
                    )
                else:
                    if (
                        principal.source == "adapter"
                        and isinstance(frame, Mapping)
                        and frame.get("type") == "acknowledgement"
                    ):
                        events = await runtime.process_acknowledgement_and_publish(
                            session_id,
                            session,
                            frame,
                            principal,
                            wait_for_connection_id=None,
                        )
                    elif (
                        isinstance(frame, Mapping)
                        and frame.get("type") == "detection_acknowledgement"
                    ):
                        detection_id = frame.get("detection_id")
                        if (
                            frame.get("v") != 1
                            or set(frame) != {"v", "type", "detection_id"}
                            or not isinstance(detection_id, str)
                            or not detection_id
                            or len(detection_id) > 512
                        ):
                            events = await runtime.process_and_publish(
                                session_id,
                                lambda: [
                                    session.protocol_refusal(
                                        reason="invalid_detection_acknowledgement",
                                        detail="detection acknowledgement must name one detection",
                                    )
                                ],
                            )
                        elif principal.source != "console":
                            events = await runtime.process_and_publish(
                                session_id,
                                lambda: [
                                    session.protocol_refusal(
                                        reason="detection_acknowledgement_forbidden",
                                        detail="only the console operator can acknowledge a detection",
                                    )
                                ],
                            )
                        else:
                            try:
                                events = await runtime.acknowledge_detection(
                                    session_id, detection_id, principal
                                )
                            except ValueError as error:
                                events = await runtime.process_and_publish(
                                    session_id,
                                    lambda: [
                                        session.protocol_refusal(
                                            reason="invalid_detection_acknowledgement",
                                            detail=str(error),
                                        )
                                    ],
                                )
                    else:
                        events = await runtime.process_and_publish(
                            session_id,
                            lambda received=frame: runtime.process_frame(
                                session, received, principal
                            ),
                            wait_for_connection_id=(
                                subscription.connection_id
                                if (
                                    principal.source in REGISTERED_SOURCES
                                    and isinstance(frame, Mapping)
                                    and frame.get("type") == "intent"
                                )
                                else None
                            ),
                        )
                    if (
                        principal.source in REGISTERED_SOURCES
                        and isinstance(frame, Mapping)
                        and frame.get("type") == "intent"
                        and isinstance(frame.get("intent_id"), str)
                        and len(events) == 1
                        and events[0].get("type") == "acknowledgement"
                        and events[0].get("status") == "accepted"
                    ):
                        session.mark_pending_intent_delivered(frame["intent_id"])
                        execution = asyncio.create_task(
                            _execute_and_publish(runtime, session_id, session, frame["intent_id"])
                        )
                        executions.add(execution)
                        execution.add_done_callback(executions.discard)
                        runtime._track_background_operation(execution)
        except WebSocketDisconnect:
            pass
        except TimeoutError:
            await _close_failed_socket(websocket, code=1013)
        except (RuntimeError, OSError):
            await _close_failed_socket(websocket, code=1011)
        except AuditLogError:
            await _close_failed_socket(websocket, code=1011)
        finally:
            if sender is not None:
                sender.cancel()
                with contextlib.suppress(
                    asyncio.CancelledError, WebSocketDisconnect, RuntimeError, OSError
                ):
                    await sender
            await runtime.cleanup_connection(session_id, session, principal, subscription)

    def authorized_runtime(authorization: str | None = Header(default=None)) -> RelayRuntime:
        runtime: RelayRuntime = application.state.relay_runtime
        expected = runtime.credential_resolver.resolve("console", None)
        supplied = _bearer_token(authorization)
        if expected is None or supplied is None or not hmac.compare_digest(supplied, expected):
            raise HTTPException(status_code=401, detail="authentication required")
        return runtime

    @application.get("/metrics")
    def metrics(authorization: str | None = Header(default=None)) -> dict[str, object]:
        runtime = authorized_runtime(authorization)
        return {
            "v": 1,
            "t": runtime.clock(),
            "type": "metrics",
            "event_id": runtime.event_ids(),
            "session_count": len(runtime.sessions),
            "connection_count": runtime.connection_count(),
            "sessions": {
                session_id: session.metrics()
                for session_id, session in sorted(runtime.sessions.items())
            },
        }

    @application.get("/runtime-config.json")
    def runtime_config(authorization: str | None = Header(default=None)) -> JSONResponse:
        """The console's media bootstrap, the same shape the dev server serves at this path."""
        runtime = authorized_runtime(authorization)
        media = runtime.settings.media_runtime_config()
        headers = {"Cache-Control": "no-store"}
        if media is None:
            return JSONResponse({"media": None}, status_code=503, headers=headers)
        return JSONResponse({"media": media}, headers=headers)

    @application.get("/session/{session_id}")
    def replay(
        session_id: str,
        after_sequence: int = Query(default=0, ge=0),
        authorization: str | None = Header(default=None),
    ) -> dict[str, object]:
        runtime = authorized_runtime(authorization)
        try:
            return runtime.replay(session_id, after_sequence=after_sequence)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from None
        except AuditLogError as error:
            raise HTTPException(status_code=503, detail=str(error)) from None

    @application.post("/api/sessions/{session_id}/transcripts", response_model=None)
    async def transcript(
        session_id: str,
        request: Request,
        authorization: str | None = Header(default=None),
        correlation_id: str | None = Header(default=None, alias="X-Sweep-Correlation-Id"),
    ) -> dict[str, object] | JSONResponse:
        runtime = authorized_runtime(authorization)
        try:
            _validate_session_id(session_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid session ID") from None
        session = runtime.sessions.get(session_id)
        if session is None:
            outcome = VoiceOutcome("refused", "template", "session_unavailable", None)
            return JSONResponse(
                outcome.to_dict(session_id=session_id, correlation_id=correlation_id or ""),
                status_code=409,
            )
        content_length = _content_length(request.headers.get("content-length"))
        if content_length is not None and content_length > MAX_AUDIO_BYTES:
            outcome = VoiceOutcome("refused", "template", "upload_too_large", None)
            return JSONResponse(
                outcome.to_dict(session_id=session_id, correlation_id=correlation_id or ""),
                status_code=413,
            )
        declared_duration_ms = _nonnegative_header(request.headers.get("x-sweep-audio-duration-ms"))
        if declared_duration_ms is not None and declared_duration_ms > MAX_AUDIO_DURATION_MS:
            outcome = VoiceOutcome("refused", "template", "audio_too_long", None)
            return JSONResponse(
                outcome.to_dict(session_id=session_id, correlation_id=correlation_id or ""),
                status_code=413,
            )
        try:
            body = await _bounded_request_body(
                request, timeout_s=runtime.settings.transcript_upload_timeout_ms / 1_000
            )
        except TimeoutError:
            outcome = VoiceOutcome("refused", "template", "upload_timeout", None)
            return JSONResponse(
                outcome.to_dict(session_id=session_id, correlation_id=correlation_id or ""),
                status_code=408,
            )
        except ValueError as error:
            outcome = VoiceOutcome("refused", "template", str(error), None)
            return JSONResponse(
                outcome.to_dict(session_id=session_id, correlation_id=correlation_id or ""),
                status_code=413,
            )
        outcome = await asyncio.to_thread(
            application.state.transcript_service.process,
            session_id=session_id,
            correlation_id=correlation_id or "",
            content_type=request.headers.get("content-type"),
            body=body,
            relay_state=session.current_state(),
            rooms=runtime.authoritative_rooms(session),
            now_ms=runtime.clock(),
            # Transcription takes seconds; the compiler grounds on a state event read
            # after it so its maximum state age is measured against the plan, not the
            # upload.
            refresh_state=lambda: (session.current_state(), runtime.clock()),
        )
        status_code = 413 if outcome.reason == "audio_too_long" else 200
        return JSONResponse(
            outcome.to_dict(session_id=session_id, correlation_id=correlation_id or ""),
            status_code=status_code,
        )

    @application.post("/api/sessions/{session_id}/detections/recorded-frame")
    async def recorded_detection_frame(
        session_id: str,
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> dict[str, object]:
        runtime = authorized_runtime(authorization)
        processor = runtime.recorded_frame_processor
        if processor is None:
            raise HTTPException(status_code=503, detail="recorded frame detector is unavailable")
        try:
            _validate_session_id(session_id)
            body = await request.json()
            if not isinstance(body, Mapping) or set(body) != {"recording_id", "drone_id"}:
                raise ValueError("recorded frame request has unexpected fields")
            recording_id = body["recording_id"]
            drone_id = body["drone_id"]
            if (
                not isinstance(recording_id, str)
                or not recording_id
                or recording_id != recording_id.strip()
                or len(recording_id) > 256
                or type(drone_id) is not int
                or drone_id <= 0
            ):
                raise ValueError("recorded frame request is invalid")
            events = await asyncio.to_thread(processor, session_id, drone_id, recording_id)
            published = await runtime.record_detection_events(session_id, drone_id, events)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from None
        return {"events": published}

    return application


async def _send_json(websocket: WebSocket, event: dict[str, object]) -> None:
    await asyncio.wait_for(websocket.send_json(event), timeout=_SEND_TIMEOUT_SECONDS)


async def _close_failed_socket(websocket: WebSocket, *, code: int) -> None:
    with contextlib.suppress(TimeoutError, WebSocketDisconnect, RuntimeError, OSError):
        await asyncio.wait_for(websocket.close(code=code), timeout=_CLOSE_TIMEOUT_SECONDS)


def _resolve_delivery(outbound: _Outbound, delivered: bool) -> None:
    if outbound.delivered is not None and not outbound.delivered.done():
        outbound.delivered.set_result(delivered)


def _same_state_projection(left: Mapping[str, object], right: Mapping[str, object]) -> bool:
    volatile = {"t", "event_id", "state_sequence"}
    return {key: value for key, value in left.items() if key not in volatile} == {
        key: value for key, value in right.items() if key not in volatile
    }


async def _bounded_request_body(request: Request, *, timeout_s: float = 15.0) -> bytes:
    body = bytearray()
    async with asyncio.timeout(timeout_s):
        async for chunk in request.stream():
            if len(chunk) > MAX_AUDIO_BYTES - len(body):
                raise ValueError("upload_too_large")
            body.extend(chunk)
    return bytes(body)


def _content_length(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


def _nonnegative_header(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


async def _send_events(websocket: WebSocket, subscription: _Subscription) -> None:
    worker = asyncio.create_task(_send_outbound(websocket, subscription))
    overflow = asyncio.create_task(subscription.overflowed.wait())
    try:
        done, _ = await asyncio.wait({worker, overflow}, return_when=asyncio.FIRST_COMPLETED)
        if overflow in done:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
            await _close_failed_socket(websocket, code=1013)
            raise WebSocketDisconnect(code=1013)
        try:
            await worker
        except TimeoutError:
            await _close_failed_socket(websocket, code=1013)
            raise WebSocketDisconnect(code=1013) from None
    finally:
        worker.cancel()
        overflow.cancel()
        await asyncio.gather(worker, overflow, return_exceptions=True)
        subscription.sender_failed.set()
        while not subscription.queue.empty():
            outbound = subscription.queue.get_nowait()
            subscription.queue.task_done()
            _resolve_delivery(outbound, False)


async def _send_outbound(websocket: WebSocket, subscription: _Subscription) -> None:
    while True:
        outbound = await subscription.queue.get()
        sent = False
        try:
            async with subscription.send_lock:
                await _send_json(websocket, outbound.event)
            sent = True
        finally:
            subscription.queue.task_done()
            _resolve_delivery(outbound, sent)


async def _receive_or_sender_failure(websocket: WebSocket, sender: asyncio.Task[None]) -> object:
    receive = asyncio.create_task(websocket.receive_json())
    try:
        done, _ = await asyncio.wait({receive, sender}, return_when=asyncio.FIRST_COMPLETED)
        if sender in done:
            await sender
            raise WebSocketDisconnect(code=1006)
        return await receive
    finally:
        receive.cancel()
        await asyncio.gather(receive, return_exceptions=True)


def _log_background_failure(task: asyncio.Task[object]) -> None:
    if task.cancelled():
        return
    if (error := task.exception()) is not None:
        _LOGGER.error(
            "cancellation-safe relay operation failed",
            exc_info=(type(error), error, error.__traceback__),
        )


async def _execute_and_publish(
    runtime: RelayRuntime,
    session_id: str,
    session: RelaySession,
    intent_id: str,
) -> None:
    outcome = await asyncio.to_thread(session.execute_pending_intent, intent_id, defer_resume=True)
    async with runtime._session_operation(session_id):
        await runtime.publish(session_id, outcome)
        work = await asyncio.to_thread(session.prepare_resume)
    await runtime._resume_and_publish(session_id, session, work)


def _auth_accepted(
    runtime: RelayRuntime, session_id: str, principal: Principal
) -> dict[str, object]:
    return {
        "v": 1,
        "t": runtime.clock(),
        "type": "auth.accepted",
        "event_id": runtime.event_ids(),
        "session": session_id,
        "source": principal.source,
        "drone_id": principal.drone_id,
        "node": runtime.settings.node_settings() if principal.source == "adapter" else None,
    }


def _auth_refused(
    runtime: RelayRuntime, *, session_id: str, reason: str, detail: str
) -> dict[str, object]:
    return {
        "v": 1,
        "t": runtime.clock(),
        "type": "auth.refused",
        "event_id": runtime.event_ids(),
        "session": session_id,
        "status": "refused",
        "reason": reason,
        "detail": detail,
    }


def _bearer_token(authorization: str | None) -> bytes | None:
    if authorization is None or not authorization.startswith("Bearer "):
        return None
    token = authorization.removeprefix("Bearer ")
    return token.encode() if token else None


def _validate_session_id(session_id: str) -> None:
    if (
        not session_id
        or len(session_id) > 512
        or session_id != session_id.strip()
        or not session_id.isprintable()
    ):
        raise ValueError("invalid session ID")


def _epoch_ms() -> int:
    return time.time_ns() // 1_000_000


app = create_app()
