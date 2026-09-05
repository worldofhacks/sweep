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

from relay.audit import AuditLogError, SessionAuditLog
from relay.auth import (
    AuthenticationError,
    CredentialResolver,
    Principal,
    authenticate,
)
from relay.intent_v1 import REGISTERED_SOURCES
from relay.session import Clock, EventIdFactory, IntentSink, LeaveAuthorizer, RelaySession
from relay.settings import RelaySettings, console_origins_from_env
from relay.voice import MAX_AUDIO_BYTES, MAX_AUDIO_DURATION_MS, TranscriptService, VoiceOutcome

IntentSinkFactory = Callable[[RelaySession], IntentSink | None]
LeaveAuthorizerFactory = Callable[[str], LeaveAuthorizer | None]
_LOGGER = logging.getLogger(__name__)
ShutdownCallback = Callable[[], None]
TranscriptServiceFactory = Callable[["RelayRuntime"], TranscriptService]
AuthoritativeRoomsFactory = Callable[[RelaySession], tuple[str, ...]]


@dataclass(eq=False, slots=True)
class _Subscription:
    connection_id: str
    principal: Principal
    initial_state: dict[str, object]
    roster_version: int
    queue: asyncio.Queue[_Outbound] = field(default_factory=asyncio.Queue)
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    sender_failed: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass(frozen=True, slots=True)
class _Outbound:
    event: dict[str, object]
    delivered: asyncio.Future[bool] | None = None


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
        leave_authorizer_factory: LeaveAuthorizerFactory | None = None,
        authoritative_rooms_factory: AuthoritativeRoomsFactory | None = None,
    ) -> None:
        self.settings = settings
        self.credential_resolver = credential_resolver or settings.credential_resolver()
        self.clock = clock or _epoch_ms
        self.event_ids = event_ids or (lambda: str(uuid.uuid4()))
        self.intent_sink_factory = intent_sink_factory
        self.leave_authorizer_factory = leave_authorizer_factory
        self.authoritative_rooms_factory = authoritative_rooms_factory
        self.sessions: dict[str, RelaySession] = {}
        self._subscriptions: dict[str, dict[str, _Subscription]] = {}
        self._adapter_connections: dict[tuple[str, int], str] = {}
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
                session = RelaySession(
                    session_id=session_id,
                    audit_log=audit_log,
                    limits=self.settings.limits(),
                    clock=self.clock,
                    event_ids=self.event_ids,
                    leave_authorizer=leave_authorizer,
                )
                if self.intent_sink_factory is not None:
                    session.intent_sink = self.intent_sink_factory(session)
                self.sessions[session_id] = session
            return session

    def replay(self, session_id: str, *, after_sequence: int = 0) -> dict[str, object]:
        """Read active or persisted history without reopening mutable live state."""
        _validate_session_id(session_id)
        with self._session_gate(session_id):
            session = self.sessions.get(session_id)
            if session is None:
                audit_log = SessionAuditLog(self.settings.log_dir, session_id)
                records, last_sequence = audit_log.replay_snapshot(after_sequence=after_sequence)
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
        return session.replay(after_sequence=after_sequence)

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
    def _session_gate(self, session_id: str) -> Iterator[None]:
        with self._session_gates_lock:
            gate = self._session_gates.setdefault(session_id, _SessionGate())
            gate.users += 1
        try:
            with gate.lock:
                yield
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
        self._fanout_task = asyncio.create_task(self._fanout_loop())

    async def stop(self) -> None:
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
            await asyncio.gather(*tuple(self._background_operations), return_exceptions=True)
        self._fanout_task = None

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
                    subscription.queue.put_nowait(_Outbound(event, delivered))
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
        except AuditLogError:
            self._fanout_failed_sessions.add(session_id)
            _LOGGER.exception("session fan-out stopped after audit failure session=%s", session_id)

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
    leave_authorizer_factory: LeaveAuthorizerFactory | None = None,
    authoritative_rooms_factory: AuthoritativeRoomsFactory | None = None,
    transcript_service_factory: TranscriptServiceFactory | None = None,
    shutdown_callback: ShutdownCallback | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI):
        active_settings = settings or RelaySettings.from_env()
        runtime = RelayRuntime(
            active_settings,
            credential_resolver=credential_resolver,
            clock=clock,
            event_ids=event_ids,
            intent_sink_factory=intent_sink_factory,
            leave_authorizer_factory=leave_authorizer_factory,
            authoritative_rooms_factory=authoritative_rooms_factory,
        )
        application.state.relay_runtime = runtime
        application.state.transcript_service = (
            TranscriptService()
            if transcript_service_factory is None
            else transcript_service_factory(runtime)
        )
        await runtime.start()
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
        allow_methods=["POST"],
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
            await websocket.send_json(
                _auth_refused(runtime, session_id=session_id, reason=code, detail=detail)
            )
            await websocket.close(code=1008)
            return
        except TimeoutError:
            await websocket.send_json(
                _auth_refused(
                    runtime,
                    session_id=session_id,
                    reason="auth_timeout",
                    detail="authentication frame was not received in time",
                )
            )
            await websocket.close(code=1008)
            return
        except WebSocketDisconnect:
            return
        except AuditLogError:
            with contextlib.suppress(WebSocketDisconnect, RuntimeError):
                await websocket.close(code=1011)
            return

        sender: asyncio.Task[None] | None = None
        executions: set[asyncio.Task[None]] = set()
        try:
            await websocket.send_json(accepted)
            await websocket.send_json(subscription.initial_state)
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
                            wait_for_connection_id=subscription.connection_id,
                        )
                    else:
                        events = await runtime.process_and_publish(
                            session_id,
                            lambda received=frame: runtime.process_frame(
                                session, received, principal
                            ),
                            wait_for_connection_id=subscription.connection_id,
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
        except AuditLogError:
            with contextlib.suppress(WebSocketDisconnect, RuntimeError):
                await websocket.close(code=1011)
        finally:
            if sender is not None:
                sender.cancel()
                with contextlib.suppress(asyncio.CancelledError, WebSocketDisconnect, RuntimeError):
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
            body = await _bounded_request_body(request)
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
        )
        status_code = 413 if outcome.reason == "audio_too_long" else 200
        return JSONResponse(
            outcome.to_dict(session_id=session_id, correlation_id=correlation_id or ""),
            status_code=status_code,
        )

    return application


async def _bounded_request_body(request: Request) -> bytes:
    body = bytearray()
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
    try:
        while True:
            outbound = await subscription.queue.get()
            sent = False
            try:
                async with subscription.send_lock:
                    await websocket.send_json(outbound.event)
                sent = True
            finally:
                if outbound.delivered is not None and not outbound.delivered.done():
                    outbound.delivered.set_result(sent)
    finally:
        subscription.sender_failed.set()
        while not subscription.queue.empty():
            outbound = subscription.queue.get_nowait()
            if outbound.delivered is not None and not outbound.delivered.done():
                outbound.delivered.set_result(False)


async def _receive_or_sender_failure(websocket: WebSocket, sender: asyncio.Task[None]) -> object:
    receive = asyncio.create_task(websocket.receive_json())
    done, _ = await asyncio.wait({receive, sender}, return_when=asyncio.FIRST_COMPLETED)
    if sender in done:
        receive.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await receive
        with contextlib.suppress(asyncio.CancelledError, WebSocketDisconnect, RuntimeError):
            await sender
        raise WebSocketDisconnect(code=1006)
    return await receive


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
        or any(ord(character) < 32 for character in session_id)
    ):
        raise ValueError("invalid session ID")


def _epoch_ms() -> int:
    return time.time_ns() // 1_000_000


app = create_app()
