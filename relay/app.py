"""FastAPI WebSocket relay, authenticated fan-out, metrics, and replay."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import time
import uuid
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from fastapi import FastAPI, Header, HTTPException, Query, WebSocket, WebSocketDisconnect

from relay.audit import SessionAuditLog
from relay.auth import (
    AuthenticationError,
    CredentialResolver,
    Principal,
    authenticate,
)
from relay.session import Clock, EventIdFactory, IntentSink, LeaveAuthorizer, RelaySession
from relay.settings import RelaySettings

IntentSinkFactory = Callable[[str], IntentSink | None]
LeaveAuthorizerFactory = Callable[[str], LeaveAuthorizer | None]


@dataclass(eq=False, slots=True)
class _Subscription:
    connection_id: str
    principal: Principal
    initial_state: dict[str, object]
    queue: asyncio.Queue[dict[str, object]] = field(default_factory=asyncio.Queue)


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
    ) -> None:
        self.settings = settings
        self.credential_resolver = credential_resolver or settings.credential_resolver()
        self.clock = clock or _epoch_ms
        self.event_ids = event_ids or (lambda: str(uuid.uuid4()))
        self.intent_sink_factory = intent_sink_factory
        self.leave_authorizer_factory = leave_authorizer_factory
        self.sessions: dict[str, RelaySession] = {}
        self._subscriptions: dict[str, dict[str, _Subscription]] = {}
        self._adapter_connections: dict[tuple[str, int], str] = {}
        self._connection_lock = asyncio.Lock()
        self._fanout_task: asyncio.Task[None] | None = None

    def session(self, session_id: str) -> RelaySession:
        _validate_session_id(session_id)
        session = self.sessions.get(session_id)
        if session is None:
            audit_log = SessionAuditLog(self.settings.log_dir, session_id)
            if audit_log.had_persisted_log:
                raise AuthenticationError(
                    "session_closed",
                    "persisted sessions are replay-only after a relay process restart; "
                    "use a new session ID",
                )
            sink = (
                None if self.intent_sink_factory is None else self.intent_sink_factory(session_id)
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
                intent_sink=sink,
                leave_authorizer=leave_authorizer,
            )
            self.sessions[session_id] = session
        return session

    def replay(self, session_id: str, *, after_sequence: int = 0) -> dict[str, object]:
        """Read active or persisted history without reopening mutable live state."""
        _validate_session_id(session_id)
        session = self.sessions.get(session_id)
        if session is not None:
            return session.replay(after_sequence=after_sequence)

        audit_log = SessionAuditLog(self.settings.log_dir, session_id)
        records = audit_log.replay(after_sequence=after_sequence)
        return {
            "v": 1,
            "t": self.clock(),
            "type": "replay",
            "event_id": self.event_ids(),
            "session": session_id,
            "after_sequence": after_sequence,
            "last_sequence": audit_log.last_sequence,
            "events": records,
        }

    async def start(self) -> None:
        self._fanout_task = asyncio.create_task(self._fanout_loop())

    async def stop(self) -> None:
        if self._fanout_task is None:
            return
        self._fanout_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._fanout_task
        self._fanout_task = None

    async def subscribe(self, session_id: str, principal: Principal) -> _Subscription:
        """Freeze the initial snapshot at the same linearization point as activation."""
        async with self._connection_lock:
            if principal.source == "adapter":
                assert principal.drone_id is not None
                key = (session_id, principal.drone_id)
                if key in self._adapter_connections:
                    raise AuthenticationError(
                        "adapter_already_connected",
                        "an authenticated connection is already bound to this drone",
                    )
            subscription = _Subscription(
                connection_id=self.event_ids(),
                principal=principal,
                initial_state=self.sessions[session_id].current_state(),
            )
            if principal.source == "adapter":
                assert principal.drone_id is not None
                key = (session_id, principal.drone_id)
                self._adapter_connections[key] = subscription.connection_id
            self._subscriptions.setdefault(session_id, {})[subscription.connection_id] = (
                subscription
            )
        return subscription

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

    async def publish(self, session_id: str, events: list[dict[str, object]]) -> None:
        """Queue an event batch atomically with respect to subscription activation."""
        async with self._connection_lock:
            subscriptions = tuple(self._subscriptions.get(session_id, {}).values())
            for subscription in subscriptions:
                for event in events:
                    subscription.queue.put_nowait(event)

    def connection_count(self) -> int:
        return sum(len(subscriptions) for subscriptions in self._subscriptions.values())

    async def _fanout_loop(self) -> None:
        delay = 1 / self.settings.fanout_hz
        while True:
            await asyncio.sleep(delay)
            for session_id, session in tuple(self.sessions.items()):
                await self.publish(session_id, session.periodic_events())


def create_app(
    settings: RelaySettings | None = None,
    *,
    credential_resolver: CredentialResolver | None = None,
    clock: Clock | None = None,
    event_ids: EventIdFactory | None = None,
    intent_sink_factory: IntentSinkFactory | None = None,
    leave_authorizer_factory: LeaveAuthorizerFactory | None = None,
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
        )
        application.state.relay_runtime = runtime
        await runtime.start()
        try:
            yield
        finally:
            await runtime.stop()

    application = FastAPI(title="Sweep relay", version="1", lifespan=lifespan)

    @application.websocket("/ws/{session_id}")
    async def websocket_relay(websocket: WebSocket, session_id: str) -> None:
        runtime: RelayRuntime = websocket.app.state.relay_runtime
        await websocket.accept()
        try:
            _validate_session_id(session_id)
            raw_auth = await asyncio.wait_for(websocket.receive_json(), timeout=5)
            principal = authenticate(raw_auth, runtime.credential_resolver)
            session = runtime.session(session_id)
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

        sender: asyncio.Task[None] | None = None
        try:
            await websocket.send_json(accepted)
            await websocket.send_json(subscription.initial_state)
            sender = asyncio.create_task(_send_events(websocket, subscription))
            while True:
                try:
                    frame = await websocket.receive_json()
                except json.JSONDecodeError:
                    events = [
                        session.protocol_refusal(
                            reason="invalid_json", detail="frame must contain valid JSON"
                        )
                    ]
                else:
                    events = session.process_frame(frame, principal)
                await runtime.publish(session_id, events)
        except WebSocketDisconnect:
            pass
        finally:
            if sender is not None:
                sender.cancel()
                with contextlib.suppress(asyncio.CancelledError, WebSocketDisconnect, RuntimeError):
                    await sender
            try:
                if principal.source == "adapter":
                    assert principal.drone_id is not None
                    events = session.handle_adapter_disconnect(
                        drone_id=principal.drone_id,
                        connection_epoch=session.registry.connection_epoch(principal.drone_id),
                    )
                    await runtime.publish(session_id, events)
            finally:
                await runtime.unsubscribe(session_id, subscription)

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

    return application


async def _send_events(websocket: WebSocket, subscription: _Subscription) -> None:
    while True:
        event = await subscription.queue.get()
        await websocket.send_json(event)


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
