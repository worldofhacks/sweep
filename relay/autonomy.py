"""Autonomy composition: accepted relay intents through the planner and arbiter to dispatch.

``relay.app`` acknowledges an Intent v1 request as ``accepted`` only after its intent
sink hands the request to a planner/arbiter consumer, and the standalone
``relay.app:app`` has none. This module is that consumer for the M2.0 checkpoint and
``relay.main`` runs it. One worker thread per session takes each accepted intent in
arrival order, projects the relay state into the autonomy ``FleetSnapshot`` with
explicit fail-closed enrichment, runs ``AutonomyController`` (capability gate,
arbiter, planner, whole-plan arbitration, dispatch) on the adapters that
``SWEEP_ADAPTER_BACKEND`` selects, then applies the accepted control state and the
resulting lifecycle back through the session so consoles and the audit log see them.

The relay session calls the sink while holding its own lock inside the intent
operation, so the sink only queues. Dispatch runs on the worker: the remote adapter
blocks on node acknowledgements that arrive through that same session, which would
deadlock inside the intent operation.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields, is_dataclass
from typing import get_origin, get_type_hints

from fastapi import FastAPI

from adapters.sim.camera import SimCameraConfig
from arbiter.safety import SafetyArbiter, SafetyConfig
from planner.controller import AutonomyController
from planner.models import (
    ExecutionResult,
    FleetSnapshot,
    FlightState,
    LifecycleStatus,
    Refusal,
    RefusalReason,
    RelayAircraftSafetyEnrichment,
    RelaySnapshotEnrichment,
)
from planner.planner import DeterministicPlanner, PlanningConfig
from planner.roster import authorize_graceful_removal
from relay.app import RelayRuntime, create_app
from relay.bridge import build_dispatcher
from relay.contracts import LifecycleStatus as WireLifecycleStatus
from relay.intent_v1 import IntentV1
from relay.session import Clock, EventIdFactory, IntentSink, LeaveAuthorizer, RelaySession
from relay.settings import AdapterBackend, RelaySettings, SettingsError

LIFECYCLE_SOURCE = "autonomy"
_LOGGER = logging.getLogger(__name__)
_FLIGHT_STATES = frozenset(state.value for state in FlightState)
_PHYSICALLY_DISARMED_STATES = frozenset({FlightState.DISARMED.value, FlightState.LANDED.value})
_PUBLISH_TIMEOUT_S = 30.0


@dataclass(frozen=True, slots=True)
class AutonomyConfig:
    """Planner, arbiter, and sim camera values; none of them has a deployment default."""

    planning: PlanningConfig
    safety: SafetyConfig
    sim_camera: SimCameraConfig | None = None

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> AutonomyConfig:
        """Read ``SWEEP_PLANNING_JSON``, ``SWEEP_SAFETY_JSON``, and ``SWEEP_SIM_CAMERA_JSON``.

        Each value is one JSON object whose keys are exactly the config's fields; the
        config's own validation then rejects values that could disable a gate. The sim
        camera is optional here and required by ``create_autonomy_app`` on ``sim``.
        """
        values = os.environ if environ is None else environ
        camera_raw = values.get("SWEEP_SIM_CAMERA_JSON", "")
        return cls(
            planning=_config_from_json(
                PlanningConfig, values.get("SWEEP_PLANNING_JSON", ""), "SWEEP_PLANNING_JSON"
            ),
            safety=_config_from_json(
                SafetyConfig, values.get("SWEEP_SAFETY_JSON", ""), "SWEEP_SAFETY_JSON"
            ),
            sim_camera=(
                None
                if not camera_raw
                else _config_from_json(SimCameraConfig, camera_raw, "SWEEP_SIM_CAMERA_JSON")
            ),
        )


def relay_snapshot(
    state: Mapping[str, object], *, operator_last_seen_ms: int | None
) -> FleetSnapshot:
    """Project one relay ``state`` event into the autonomy snapshot.

    Appendix B carries no physical armed, physical-RC, storage, camera-readiness,
    active-task, position-loss, or Sweep-operator facts, so the composition asserts
    them explicitly and fails closed where it cannot:

    - ``operator_present`` and ``operator_last_seen_ms`` come from the latest accepted
      console or keyboard intent in this session; the arbiter's operator timeout
      bounds how long that evidence lasts. No intent yet means no operator.
    - ``armed`` is derived from the authoritative telemetry flight state exactly as
      the simulator reports it: every state except ``disarmed`` and ``landed`` is
      physically armed. Telemetry v1 has no separate motor-state field.
    - ``physical_rc_available`` is the node's signed ``rc_safety_operator_present``
      readiness claim; on the DJI stack the phone reaches the aircraft only through
      that RC.
    - ``storage_remaining_bytes`` and ``camera_ready`` come from the node's
      current-epoch ``capabilities`` frame; without one storage is zero and the
      camera is not ready.
    - ``active_task_id`` is null because one session executes intents strictly in
      order, and ``position_loss_since_ms`` is null so the controller's dwell falls
      back to the position timestamp.

    Aircraft without current-epoch telemetry, or whose telemetry state is not a
    ``FlightState``, are excluded: they cannot be selected or commanded until the
    node reports.
    """
    drones_raw = state.get("drones")
    if not isinstance(drones_raw, list):
        raise ValueError("relay state requires a drones list")
    drones: list[Mapping[str, object]] = []
    enrichment: dict[int, RelayAircraftSafetyEnrichment] = {}
    for drone in drones_raw:
        if not isinstance(drone, Mapping):
            raise ValueError("relay drone entries must be mappings")
        drone_id = drone.get("drone_id")
        if not isinstance(drone_id, int) or isinstance(drone_id, bool) or drone_id <= 0:
            raise ValueError("relay drone entries require a positive drone_id")
        telemetry = drone.get("telemetry")
        if not isinstance(telemetry, Mapping) or telemetry.get("state") not in _FLIGHT_STATES:
            continue
        capabilities = drone.get("camera_capabilities")
        storage = (
            capabilities.get("storage_remaining_bytes")
            if isinstance(capabilities, Mapping)
            else None
        )
        enrichment[drone_id] = RelayAircraftSafetyEnrichment(
            drone_id=drone_id,
            armed=telemetry["state"] not in _PHYSICALLY_DISARMED_STATES,
            physical_rc_available=drone.get("rc_safety_operator_present") is True,
            storage_remaining_bytes=(
                storage
                if isinstance(storage, int) and not isinstance(storage, bool) and storage >= 0
                else 0
            ),
            camera_ready=isinstance(capabilities, Mapping),
            active_task_id=None,
            position_loss_since_ms=None,
        )
        drones.append(drone)
    return FleetSnapshot.from_relay_state(
        {**state, "drones": drones},
        enrichment=RelaySnapshotEnrichment(
            operator_present=operator_last_seen_ms is not None,
            operator_last_seen_ms=0 if operator_last_seen_ms is None else operator_last_seen_ms,
            aircraft=enrichment,
        ),
    )


def control_projection(result: ExecutionResult) -> dict[str, object]:
    """Return the control state the relay applies for one execution result.

    The planner marks accepted state changes explicitly: ``arm`` carries
    ``armed_update``, ``select`` carries ``selection_update``, and ``estop`` carries
    ``estop_update``. Arm and selection apply only once their plan completed; the
    network stop latches as soon as its plan exists, even when adapter commands fail.
    A plan still waiting on a node's terminal acknowledgement is published as the
    session's ``accepted_plan`` so a roster change can invalidate it by ``intent_id``.
    """
    plan = result.plan
    if plan is None:
        return {}
    projection: dict[str, object] = {}
    if plan.estop_update is True:
        projection["estop"] = True
    if result.status is LifecycleStatus.COMPLETED:
        if plan.selection_update is not None:
            projection["selection"] = plan.selection_update
        if plan.armed_update is not None:
            projection["armed"] = plan.armed_update
    if result.status is LifecycleStatus.EXECUTING:
        projection["accepted_plan"] = {
            "plan_id": plan.plan_id,
            "intent_id": plan.intent_id,
            "intent_name": plan.intent_name.value,
            "roster_version": plan.roster_version,
            "selection": list(plan.selection),
        }
    return projection


def record_result(session: RelaySession, result: ExecutionResult) -> dict[str, object]:
    """Report one execution result as the intent's lifecycle event through the session."""
    refusal = result.refusal
    reason = None if refusal is None else refusal.reason.value
    if reason is None and result.status in {LifecycleStatus.FAILED, LifecycleStatus.INVALIDATED}:
        reason = RefusalReason.ADAPTER_FAILURE.value
    return session.record_lifecycle(
        intent_id=result.intent_id,
        status=WireLifecycleStatus(result.status.value),
        source=LIFECYCLE_SOURCE,
        drone_id=None if refusal is None else refusal.drone_id,
        connection_epoch=None if refusal is None else refusal.connection_epoch,
        reason=reason,
        detail=None if refusal is None else refusal.detail,
    )


class AutonomySession:
    """One session's planner, arbiter, operator evidence, and sequential intent worker."""

    def __init__(self, composition: AutonomyComposition, session_id: str) -> None:
        self.session_id = session_id
        self._composition = composition
        self.planner = DeterministicPlanner(composition.config.planning)
        self.arbiter = SafetyArbiter(composition.config.safety)
        self._queue: queue.SimpleQueue[IntentV1 | None] = queue.SimpleQueue()
        self._operator_last_seen_ms: int | None = None
        self._lock = threading.Lock()
        self._worker = threading.Thread(
            target=self._run, name=f"autonomy-{session_id}", daemon=True
        )
        self._worker.start()

    def submit(self, intent: IntentV1, _state: dict[str, object]) -> None:
        """``IntentSink``: record operator activity and queue the intent without blocking."""
        with self._lock:
            previous = self._operator_last_seen_ms
            self._operator_last_seen_ms = intent.t if previous is None else max(previous, intent.t)
        self._queue.put(intent)

    def authorize_leave(
        self, drone_id: int, connection_epoch: int, state: dict[str, object]
    ) -> bool:
        """``LeaveAuthorizer``: approve only a landed, disarmed, task-free current aircraft."""
        snapshot = self.snapshot(state)
        aircraft = snapshot.aircraft.get(drone_id)
        if aircraft is None or aircraft.connection_epoch != connection_epoch:
            return False
        return authorize_graceful_removal(snapshot, drone_id).allowed

    def snapshot(self, state: Mapping[str, object]) -> FleetSnapshot:
        with self._lock:
            operator_last_seen_ms = self._operator_last_seen_ms
        return relay_snapshot(state, operator_last_seen_ms=operator_last_seen_ms)

    def close(self, timeout_s: float) -> None:
        self._queue.put(None)
        self._worker.join(timeout=timeout_s)

    def _run(self) -> None:
        while True:
            intent = self._queue.get()
            if intent is None:
                return
            try:
                self._execute(intent)
            except Exception:
                _LOGGER.exception(
                    "autonomy worker failed session=%s intent=%s",
                    self.session_id,
                    intent.intent_id,
                )

    def _execute(self, intent: IntentV1) -> None:
        runtime = self._composition.runtime
        session = runtime.sessions.get(self.session_id)
        if session is None:
            _LOGGER.error(
                "session %s is not active; intent %s was not dispatched",
                self.session_id,
                intent.intent_id,
            )
            return

        def current() -> FleetSnapshot:
            return self.snapshot(session.current_state())

        try:
            snapshot = current()
            dispatcher = build_dispatcher(
                runtime,
                self.session_id,
                snapshot,
                arbiter=self.arbiter,
                sim_camera_config=self._composition.config.sim_camera,
            )
            controller = AutonomyController(
                planner=self.planner, arbiter=self.arbiter, dispatcher=dispatcher
            )
            result = controller.execute(intent, snapshot, current_snapshot=current)
        except Exception as error:  # the console still receives a typed terminal result
            _LOGGER.exception(
                "autonomy dispatch path failed session=%s intent=%s",
                self.session_id,
                intent.intent_id,
            )
            result = _composition_failure(intent, session, error)
        self._report(runtime, session, result)

    def _report(
        self, runtime: RelayRuntime, session: RelaySession, result: ExecutionResult
    ) -> None:
        def operation() -> list[dict[str, object]]:
            events: list[dict[str, object]] = []
            projection = control_projection(result)
            if projection:
                events.append(session.update_control_projection(**projection))  # type: ignore[arg-type]
            events.append(record_result(session, result))
            return events

        loop = runtime.loop
        if loop is None or loop.is_closed():
            # The relay stopped while this intent was executing; keep the audit record.
            operation()
            return
        future = asyncio.run_coroutine_threadsafe(
            runtime.process_and_publish(self.session_id, operation), loop
        )
        future.result(timeout=_PUBLISH_TIMEOUT_S)


class AutonomyComposition:
    """Per-session autonomy workers behind ``create_app``'s sink and leave factories."""

    def __init__(self, config: AutonomyConfig) -> None:
        self.config = config
        self._runtime_source: Callable[[], RelayRuntime | None] = _no_runtime
        self._sessions: dict[str, AutonomySession] = {}
        self._lock = threading.Lock()

    def bind(self, target: FastAPI | RelayRuntime) -> None:
        """Point the composition at the runtime the app creates in its lifespan."""
        if isinstance(target, RelayRuntime):

            def runtime_source() -> RelayRuntime | None:
                return target

        else:

            def runtime_source() -> RelayRuntime | None:
                return getattr(target.state, "relay_runtime", None)

        self._runtime_source = runtime_source

    @property
    def runtime(self) -> RelayRuntime:
        runtime = self._runtime_source()
        if runtime is None:
            raise RuntimeError("the autonomy composition is not bound to a started relay")
        return runtime

    def intent_sink_factory(self, session_id: str) -> IntentSink:
        return self.session(session_id).submit

    def leave_authorizer_factory(self, session_id: str) -> LeaveAuthorizer:
        return self.session(session_id).authorize_leave

    def session(self, session_id: str) -> AutonomySession:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                session = AutonomySession(self, session_id)
                self._sessions[session_id] = session
            return session

    def close(self, *, timeout_s: float = 5.0) -> None:
        with self._lock:
            sessions = tuple(self._sessions.values())
        for session in sessions:
            session.close(timeout_s)


def create_autonomy_app(
    settings: RelaySettings,
    config: AutonomyConfig,
    *,
    clock: Clock | None = None,
    event_ids: EventIdFactory | None = None,
) -> tuple[FastAPI, AutonomyComposition]:
    """Build the relay app with the planner and arbiter consuming every accepted intent."""
    if settings.adapter_backend is AdapterBackend.SIM and config.sim_camera is None:
        raise SettingsError("SWEEP_SIM_CAMERA_JSON is required when SWEEP_ADAPTER_BACKEND is sim")
    composition = AutonomyComposition(config)
    app = create_app(
        settings,
        clock=clock,
        event_ids=event_ids,
        intent_sink_factory=composition.intent_sink_factory,
        leave_authorizer_factory=composition.leave_authorizer_factory,
    )
    composition.bind(app)
    return app, composition


def _composition_failure(
    intent: IntentV1, session: RelaySession, error: Exception
) -> ExecutionResult:
    roster_version = session.registry.roster_version
    refusal = Refusal(
        intent_id=intent.intent_id,
        roster_version=roster_version,
        drone_id=None,
        connection_epoch=None,
        reason=RefusalReason.ADAPTER_FAILURE,
        detail=f"autonomy composition raised {type(error).__name__} before dispatch completed",
        status=LifecycleStatus.FAILED,
    )
    return ExecutionResult(
        intent_id=intent.intent_id,
        roster_version=roster_version,
        status=LifecycleStatus.FAILED,
        refusal=refusal,
    )


def _no_runtime() -> RelayRuntime | None:
    return None


def _config_from_json[T](cls: type[T], raw: str, name: str) -> T:
    if not raw:
        raise SettingsError(f"{name} is required")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        raise SettingsError(f"{name} must be valid JSON") from None
    return _build_config(cls, value, name)


def _build_config[T](cls: type[T], value: object, name: str) -> T:
    if not isinstance(value, Mapping):
        raise SettingsError(f"{name} must be a JSON object")
    expected = {field.name for field in fields(cls)}  # type: ignore[arg-type]
    if set(value) != expected:
        missing = sorted(expected - set(value))
        unexpected = sorted(set(value) - expected)
        raise SettingsError(
            f"{name} keys must be exactly {sorted(expected)}: "
            f"missing {missing}, unexpected {unexpected}"
        )
    hints = get_type_hints(cls)
    arguments: dict[str, object] = {}
    for field in fields(cls):  # type: ignore[arg-type]
        item = value[field.name]
        hint = hints[field.name]
        if is_dataclass(hint):
            item = _build_config(hint, item, f"{name}.{field.name}")  # type: ignore[type-var]
        elif get_origin(hint) is tuple:
            if not isinstance(item, list):
                raise SettingsError(f"{name}.{field.name} must be a JSON array")
            item = tuple(item)
        arguments[field.name] = item
    try:
        return cls(**arguments)
    except (TypeError, ValueError) as error:
        raise SettingsError(f"{name}: {error}") from None
