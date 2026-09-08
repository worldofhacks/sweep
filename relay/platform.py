"""Authenticated platform APIs for real map evidence and frozen destination reviews."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Callable, Mapping
from contextlib import ExitStack
from typing import TYPE_CHECKING

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from planner.navigation import ArtifactPin
from relay.auth import AuthenticationError, authenticate
from relay.map_authoring import MapAuthoringError, MapAuthoringStore
from relay.navigation_service import NavigationError, NavigationService
from relay.observations import Observation, ObservationError, ObservationSubmission
from relay.platform_observations import WorldObservationError, WorldObservationService
from relay.settings import SettingsError

if TYPE_CHECKING:
    from relay.app import RelayRuntime

_LOGGER = logging.getLogger(__name__)
MAX_REQUEST_BYTES = 16 * 1024 * 1024
MAP_OPERATIONS = ("list", "load", "save", "validate", "approve", "compare", "activate")


class _FlightExecutionAdapter:
    def __init__(self, source: object) -> None:
        self.source = source
        config = getattr(source, "config", None)
        navigation = getattr(config, "navigation", None)
        self.device_classes = frozenset(
            (
                {"aircraft"}
                if navigation is not None and navigation.approval.mode == "flight"
                else set()
            )
            | (
                {"ground_vehicle"}
                if getattr(config, "ground_navigation", None) is not None
                else set()
            )
        )

    def preview(self, session: str, preview: Mapping[str, object]) -> Mapping[str, object]:
        handler = getattr(self.source, "preview_platform_navigation", None)
        if not callable(handler):
            raise ValueError("qualified aircraft navigation is unavailable")
        return handler(session, preview)

    def confirm(self, session: str, preview: Mapping[str, object]) -> Mapping[str, object]:
        handler = getattr(self.source, "confirm_platform_navigation", None)
        if not callable(handler):
            raise ValueError("qualified aircraft navigation is unavailable")
        return handler(session, preview)

    def tag_destinations(self, map_ref: dict[str, object]) -> tuple[object, ...]:
        config = getattr(self.source, "config", None)
        deployment = getattr(config, "navigation", None)
        resolver = getattr(deployment, "tag_destinations", None)
        pin = map_ref.get("mapPin")
        if (
            not callable(resolver)
            or not isinstance(pin, dict)
            or set(pin) != {"version", "contentSha256"}
        ):
            return ()
        return resolver(ArtifactPin(pin["version"], pin["contentSha256"]))


class PlatformServices:
    def __init__(
        self,
        runtime: RelayRuntime,
        *,
        motion_configuration: dict | None = None,
        environment: Mapping[str, str] | None = None,
        flight_execution: object | None = None,
    ) -> None:
        self.runtime = runtime
        self.failed = False
        directory = runtime.settings.log_dir / "platform"
        self.maps = MapAuthoringStore(directory / "maps.sqlite3", clock_ms=runtime.clock)
        # Configuration comes from the same loaded composition as the motion
        # arbiter, never from a browser request or guessed dataclass defaults.
        motion_config = json.loads(json.dumps(motion_configuration))
        with ExitStack() as cleanup:
            execution = (
                None if flight_execution is None else _FlightExecutionAdapter(flight_execution)
            )
            self.navigation = NavigationService(
                directory / "navigation.sqlite3",
                clock_ms=runtime.clock,
                approved_bundle=self.maps.approved_bundle,
                state=lambda session: self.session(session).current_state(),
                motion_config=lambda _session: motion_config,
                flight_execution=execution,
                tag_destinations=None if execution is None else execution.tag_destinations,
            )
            cleanup.callback(self.navigation.close)
            self.observations = WorldObservationService.from_env(
                os.environ if environment is None else environment,
                approved_bundle=self.navigation.current_approved_bundle,
                database=directory / "observations.sqlite3",
                clock=runtime.clock,
            )
            close = getattr(self.observations, "close", None)
            if close is not None:
                cleanup.callback(close)
            observation_configuration = runtime.settings.observation_configuration
            configured_sources = (
                set()
                if observation_configuration is None
                else {binding.source_id for binding in observation_configuration.bindings}
            )
            if not set(self.observations.registrations) <= configured_sources:
                raise SettingsError(
                    "World observation registrations require a canonical observation binding"
                )
            cleanup.pop_all()

    def session(self, session: str):
        result = self.runtime.sessions.get(session)
        if result is None:
            raise MapAuthoringError("session_unavailable", "Connect to the relay session first.")
        return result

    def observe_state(self, session: str, event: dict) -> None:
        try:
            self.navigation.observe_state(session, event)
            self.observations.observe_state(session, event)
        except Exception:
            # Missing an authoritative transition must retire the platform's
            # review authority, while existing relay control stays independent.
            self.failed = True
            _LOGGER.error("Platform state observation failed; navigation and observations disabled")

    def require_current(self) -> None:
        if self.failed:
            raise MapAuthoringError(
                "platform_state_unavailable", "Platform state tracking is unavailable.", 503
            )

    def map_changed(self, session: str) -> None:
        try:
            self.navigation.invalidate(session)
            self.observations.invalidate(session)
        except Exception:
            self.failed = True
            _LOGGER.error("Map saved but navigation retirement failed; platform tracking disabled")

    def close(self) -> None:
        try:
            self.navigation.close()
        finally:
            close = getattr(self.observations, "close", None)
            if close is not None:
                close()


def _exact(value: dict, fields: set[str]) -> dict:
    if set(value) != fields:
        raise MapAuthoringError("invalid_request", "Request fields do not match the contract.", 400)
    return value


async def _body(request: Request) -> dict:
    if request.headers.get("content-type", "").split(";", 1)[0].lower() != "application/json":
        raise HTTPException(415, "Expected application/json.")
    raw = bytearray()
    try:
        async with asyncio.timeout(10):
            async for chunk in request.stream():
                raw.extend(chunk)
                if len(raw) > MAX_REQUEST_BYTES:
                    raise HTTPException(413, "Platform request exceeds 16 MiB.")
    except TimeoutError:
        raise HTTPException(408, "Platform upload timed out.") from None

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    def invalid(_value):
        raise ValueError("non-finite JSON")

    try:
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)
    except (ValueError, UnicodeError, RecursionError):
        raise HTTPException(400, "Expected bounded, finite JSON with unique fields.") from None
    if type(value) is not dict:
        raise HTTPException(400, "Expected a JSON object.")
    return value


def install_platform_routes(application: FastAPI, authorize: Callable) -> None:
    def services(session: str, authorization: str | None) -> PlatformServices:
        runtime = authorize(authorization)
        result: PlatformServices = application.state.platform_services
        if result.runtime is not runtime:
            raise HTTPException(503, "Platform runtime is unavailable.")
        try:
            result.session(session)
        except MapAuthoringError as error:
            raise HTTPException(error.status_code, error.detail) from None
        return result

    async def call(operation: Callable, *args) -> JSONResponse:
        try:
            value = await asyncio.to_thread(operation, *args)
            return JSONResponse(value, headers={"Cache-Control": "no-store"})
        except (MapAuthoringError, NavigationError, WorldObservationError) as error:
            return JSONResponse(
                {"code": error.code, "detail": error.detail},
                status_code=getattr(error, "status_code", 409),
                headers={"Cache-Control": "no-store"},
            )

    @application.get("/api/sessions/{session_id}/platform")
    def capabilities(session_id: str, authorization: str | None = Header(default=None)):
        service = services(session_id, authorization)
        operations = list(MAP_OPERATIONS)
        if service.observations.available and not service.failed:
            operations += ["observe", "record"]
        return JSONResponse(
            {
                "v": 1,
                "sessionId": session_id,
                "mapAuthoring": {"operations": operations},
                "navigation": {
                    "review": not service.failed,
                    "dispatch": service.navigation.flight_execution is not None
                    and not service.failed,
                },
            },
            headers={"Cache-Control": "no-store"},
        )

    @application.get("/api/sessions/{session_id}/maps/revisions")
    async def revisions(session_id: str, authorization: str | None = Header(default=None)):
        return await call(services(session_id, authorization).maps.list, session_id)

    @application.post("/api/sessions/{session_id}/maps/{operation}")
    async def maps(
        session_id: str,
        operation: str,
        request: Request,
        authorization: str | None = Header(default=None),
    ):
        service = services(session_id, authorization)
        value = await _body(request)
        actor = "console"  # Authenticated principal, never a claimed request-body name.

        def perform():
            if operation in {"load", "validate"}:
                _exact(value, {"reference"})
                if operation == "load":
                    return service.maps.load(session_id, value["reference"])
                return service.maps.validate(session_id, value["reference"], actor)
            if operation == "save":
                _exact(value, {"draft", "expectedRevision"})
                result = service.maps.save(
                    session_id,
                    value["draft"],
                    value["expectedRevision"],
                    actor,
                )
                service.map_changed(session_id)
                return result
            if operation == "approve":
                _exact(value, {"reference", "validationId"})
                result = service.maps.approve(
                    session_id,
                    value["reference"],
                    value["validationId"],
                    actor,
                )
                service.map_changed(session_id)
                return result
            if operation == "compare":
                _exact(value, {"left", "right"})
                return service.maps.compare(session_id, value["left"], value["right"])
            if operation in {"positions", "record"}:
                service.require_current()
                current = service.session(session_id).current_state()
                if operation == "positions":
                    return service.observations.positions(session_id, value, current)
                return service.observations.record(session_id, value, current, actor)
            raise MapAuthoringError("unknown_operation", "Unknown map operation.", 404)

        return await call(perform)

    @application.get("/api/sessions/{session_id}/navigation/catalog")
    async def catalog(session_id: str, authorization: str | None = Header(default=None)):
        service = services(session_id, authorization)

        def perform():
            service.require_current()
            return service.navigation.catalog(session_id)

        return await call(perform)

    @application.post("/api/sessions/{session_id}/navigation/{operation}")
    async def navigation(
        session_id: str,
        operation: str,
        request: Request,
        authorization: str | None = Header(default=None),
    ):
        service = services(session_id, authorization)
        value = await _body(request)

        def perform():
            service.require_current()
            if operation == "select-map":
                result = service.navigation.select_map(session_id, value, actor="console")
                service.map_changed(session_id)
                return result
            if operation not in {"preview", "resolve", "confirm", "compile"}:
                raise MapAuthoringError("unknown_operation", "Unknown navigation operation.", 404)
            return getattr(service.navigation, operation)(session_id, value)

        return await call(perform)

    @application.post("/api/sessions/{session_id}/observations")
    async def observation(
        session_id: str,
        request: Request,
        authorization: str | None = Header(default=None),
        source: str | None = Header(default=None, alias="X-Sweep-Source"),
        device_id: int | None = Header(default=None, alias="X-Sweep-Device-Id"),
    ):
        runtime = application.state.relay_runtime
        if source not in {"adapter", "localization"} or device_id is None:
            raise HTTPException(401, "A device-bound observation principal is required.")
        token = authorization[7:] if authorization and authorization.startswith("Bearer ") else ""
        try:
            principal = authenticate(
                {"v": 1, "type": "auth", "source": source, "drone_id": device_id, "token": token},
                runtime.credential_resolver,
            )
        except AuthenticationError:
            raise HTTPException(401, "Authentication required.") from None
        service: PlatformServices = application.state.platform_services
        value = await _body(request)

        try:
            service.require_current()
            session = service.session(session_id)
            submission = ObservationSubmission.parse(value)
            events = await runtime.process_and_publish(
                session_id,
                lambda: runtime.process_frame(session, submission.to_mapping(), principal),
            )
            event = next((item for item in events if item.get("type") == "observation"), None)
            refusal = next((item for item in events if item.get("type") == "refusal"), None)
            if refusal is not None:
                raise WorldObservationError(refusal["reason"], refusal["detail"], 400)
            if event is None:
                raise WorldObservationError(
                    "observation_not_accepted", "relay did not accept an observation", 400
                )
            observation = Observation.parse(event)
            if session.observation_ingress is None:
                raise ObservationError("source_not_configured", "observation ingress is disabled")
            receipt_ms, capture_ms = session.observation_ingress.host_times(observation)
            try:
                accepted = await asyncio.to_thread(
                    service.observations.ingest,
                    session_id,
                    observation,
                    receipt_ms=receipt_ms,
                    capture_ms=capture_ms,
                    state=session.current_state(),
                )
            except WorldObservationError as error:
                if error.code != "state_changed":
                    raise
                accepted = await asyncio.to_thread(
                    service.observations.ingest,
                    session_id,
                    observation,
                    receipt_ms=receipt_ms,
                    capture_ms=capture_ms,
                    state=session.current_state(),
                )
            registration = service.observations.registrations[
                observation.submission.source_id
            ].to_dict()
            approved = service.maps.approved_bundle(session_id, registration["reference"])
            await asyncio.to_thread(
                session.record_world_observation,
                observation,
                registration,
                approved["bundle"]["manifest"],
            )
            return JSONResponse(accepted, headers={"Cache-Control": "no-store"})
        except (MapAuthoringError, NavigationError, WorldObservationError) as error:
            return JSONResponse(
                {"code": error.code, "detail": error.detail}, status_code=error.status_code
            )
        except ObservationError as error:
            return JSONResponse({"code": error.code, "detail": error.detail}, status_code=400)
