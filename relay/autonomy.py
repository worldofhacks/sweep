"""Autonomy composition: accepted relay intents through the planner and arbiter to dispatch.

``relay.app`` acknowledges an Intent v1 request as ``accepted`` only after its intent
sink hands the request to a planner/arbiter consumer, and the standalone
``relay.app:app`` has none. This module is that consumer for the M2.0 checkpoint and
``relay.main`` runs it. Each session runs three worker lanes: operator intents in
arrival order, ``hold`` on its own lane, and ``estop`` on its own lane. A worker
projects the relay state into the autonomy ``FleetSnapshot`` with explicit
fail-closed enrichment, runs ``AutonomyController`` (capability gate, arbiter,
planner, whole-plan arbitration, dispatch) on the adapters that
``SWEEP_ADAPTER_BACKEND`` selects, then applies the accepted control state and the
resulting lifecycle back through the session so consoles and the audit log see them.

The relay session calls the sink while holding its own lock inside the intent
operation, so the sink only queues; for a network stop it also latches the session's
``estop`` and records the preemption of the plans the stop cancels, all inside that
same operation. Dispatch runs on the lanes: the remote adapter blocks on node
acknowledgements that arrive through that same session, which would deadlock inside
the intent operation.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, fields, is_dataclass, replace
from typing import get_origin, get_type_hints

from fastapi import FastAPI

from adapters.dispatch import AdapterDispatcher
from adapters.dji_mini3.remote import CommandRequest, NodeLink
from adapters.protocols import AdapterError
from adapters.sim.camera import SimCameraConfig
from adapters.sim.flight import SimFlightAdapter
from arbiter.safety import SafetyArbiter, SafetyConfig
from planner.controller import AutonomyController, RelayExecution
from planner.mapped_formation_runtime import MappedFormationRuntime
from planner.models import (
    CommandAcknowledgement,
    ExecutionResult,
    FleetSnapshot,
    FlightState,
    LifecycleStatus,
    PreparedExecution,
    Refusal,
    RefusalReason,
    RelayAircraftSafetyEnrichment,
    RelaySnapshotEnrichment,
)
from planner.navigation_deployment import NavigationDeployment, load_navigation_deployment
from planner.navigation_runtime import NavigationRuntime
from planner.planner import DeterministicPlanner, PlanningConfig
from planner.roster import authorize_graceful_removal
from relay.app import RelayRuntime, create_app
from relay.auth import Principal
from relay.bridge import RelayNodeLink, build_dispatcher
from relay.capabilities import CapabilityProfile
from relay.contracts import AdapterAcknowledgement as WireAcknowledgement
from relay.contracts import CapabilitiesFrame, CaptureReadinessFrame, MediaFileRecord
from relay.contracts import LifecycleStatus as WireLifecycleStatus
from relay.control_config import ControlRuntimeConfig
from relay.control_runtime import ControlRuntime
from relay.intent_v1 import IntentName, IntentV1
from relay.language_runtime import LanguageCompilationOutcome, LanguageRuntime
from relay.mission_config import load_detection_camera_ids, load_mission_config
from relay.navigation_control import NavigationControl, NavigationControlConfig
from relay.search_bridge import SearchBridge, search_progress
from relay.search_runtime import SearchRuntime, SearchRuntimeConfig
from relay.session import Clock, EventIdFactory, IntentSink, LeaveAuthorizer, RelaySession
from relay.settings import AdapterBackend, RelaySettings, SettingsError
from relay.sim_projection import record_sim_telemetry, sim_snapshot
from relay.voice import TranscriptService

LIFECYCLE_SOURCE = "autonomy"
PREEMPTED_BY_ESTOP = "preempted_by_estop"
PREEMPTED_BY_HOLD = "preempted_by_hold"
PREEMPTED_BY_LAND = "preempted_by_land"
HOLD_PREEMPTS = frozenset(
    {
        IntentName.TAKEOFF,
        IntentName.TRANSLATE,
        IntentName.ALTITUDE,
        IntentName.COME_HOME,
        IntentName.CAPTURE_ROOM,
        IntentName.NAVIGATE,
        IntentName.SEARCH,
        IntentName.FORMATION_SET,
    }
)
"""Operator motion and camera plans a hold cancels; a running safety plan finishes first."""
ReadinessSource = Callable[[int], CaptureReadinessFrame | None]
_ESTOP_PREEMPTS = frozenset(IntentName) - {IntentName.ESTOP}
_SAFETY_PLANS = frozenset({IntentName.LAND_ALL, IntentName.ESTOP})
_MAPPED_ROUTE_INTENTS = frozenset(
    {IntentName.NAVIGATE, IntentName.SEARCH, IntentName.FORMATION_SET}
)
_ROUTE_OWNERSHIP_EXEMPTIONS = frozenset(
    {IntentName.HOLD, IntentName.ESTOP, IntentName.LAND, IntentName.LAND_ALL}
)
_TERMINAL = frozenset(
    {
        LifecycleStatus.REFUSED,
        LifecycleStatus.COMPLETED,
        LifecycleStatus.FAILED,
        LifecycleStatus.INVALIDATED,
    }
)
_LOGGER = logging.getLogger(__name__)
_FLIGHT_STATES = frozenset(state.value for state in FlightState)
_PHYSICALLY_DISARMED_STATES = frozenset({FlightState.DISARMED.value, FlightState.LANDED.value})
_PUBLISH_TIMEOUT_S = 30.0


class PlanPreempted(BaseException):
    """Raised inside a cancelled plan's dispatch so it sends nothing further.

    A ``BaseException`` so the dispatcher's adapter-failure handling never converts it
    into a best-effort hold: the stop that cancelled the plan is the safety action.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class AutonomyConfig:
    """Planner, arbiter, and sim camera values; none of them has a deployment default."""

    planning: PlanningConfig
    safety: SafetyConfig
    sim_camera: SimCameraConfig | None = None
    navigation: NavigationRuntime | None = None
    navigation_deployment: NavigationDeployment | None = None
    control_localization: ControlRuntimeConfig | None = None
    mapped_formations: MappedFormationRuntime | None = None
    search: SearchRuntimeConfig | None = None
    language: LanguageRuntime | None = None
    detection_camera_ids: Mapping[int, str] = field(default_factory=dict)
    enable_localized_navigation: bool = False

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> AutonomyConfig:
        """Read planner, safety, camera, and optional control-localization configuration.

        The JSON-valued planner and safety settings require their exact dataclass fields.
        The optional localization path points to deployment pins used for every session.
        """
        values = os.environ if environ is None else environ
        camera_raw = values.get("SWEEP_SIM_CAMERA_JSON", "")
        deployment = load_navigation_deployment(
            values,
            backend=(
                "remote" if values.get("SWEEP_ADAPTER_BACKEND", "sim") == "remote" else "synthetic"
            ),
        )
        try:
            control = (
                ControlRuntimeConfig.from_env(values)
                if values.get("SWEEP_CONTROL_LOCALIZATION_CONFIG")
                else None
            )
        except (ValueError, OSError) as error:
            raise SettingsError(f"invalid control localization configuration: {error}") from error
        if deployment is None and values.get("SWEEP_MISSION_CONFIG"):
            raise SettingsError("missions require an explicit navigation deployment")
        missions = None if deployment is None else load_mission_config(deployment.runtime, values)
        enabled_navigation = values.get("SWEEP_ENABLE_LOCALIZED_NAVIGATION", "false")
        if enabled_navigation not in {"true", "false"}:
            raise SettingsError("SWEEP_ENABLE_LOCALIZED_NAVIGATION must be true or false")
        return cls(
            enable_localized_navigation=enabled_navigation == "true",
            mapped_formations=None if missions is None else missions.mapped_formations,
            search=None if missions is None else missions.search,
            detection_camera_ids=load_detection_camera_ids(values),
            navigation=None if deployment is None else deployment.runtime,
            navigation_deployment=deployment,
            control_localization=control,
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
    state: Mapping[str, object],
    *,
    operator_last_seen_ms: int | None,
    estop_requested: bool = False,
    capture_readiness: ReadinessSource | None = None,
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
      physically armed. Telemetry v1 has no separate motor-state field, so the
      arbiter's physical-armed gate cannot refuse anything its flight-state gates do
      not already refuse; that is an accepted limitation until a node reports one.
    - ``physical_rc_available`` is the node's signed ``rc_safety_operator_present``
      readiness claim. The wire carries no separate RC-link fact, so the arbiter's
      two RC gates are intentionally collapsed into one at this stage, not defence
      in depth.
    - ``storage_remaining_bytes`` comes from the node's current-epoch
      ``capabilities`` frame; without one storage is zero.
    - ``camera_ready`` is true only when the node's latest current-epoch
      ``capture_readiness`` frame (``capture_readiness``, normally
      ``RelaySession.capture_readiness``) reports both ``camera_ok`` and
      ``storage_ok``; no frame means not ready.
    - ``active_task_id`` is null because operator plans run one at a time and a stop
      cancels the plan it overlaps, and ``position_loss_since_ms`` is null so the
      controller's dwell falls back to the position timestamp.

    ``estop_requested`` marks the snapshot stopped as soon as a network stop has been
    accepted, independently of the relay projection the sink latches at the same
    time, so an operator intent that starts in between is refused as
    ``estop_active`` rather than sent.

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
        readiness = None if capture_readiness is None else capture_readiness(drone_id)
        enrichment[drone_id] = RelayAircraftSafetyEnrichment(
            drone_id=drone_id,
            armed=telemetry["state"] not in _PHYSICALLY_DISARMED_STATES,
            physical_rc_available=drone.get("rc_safety_operator_present") is True,
            storage_remaining_bytes=(
                storage
                if isinstance(storage, int) and not isinstance(storage, bool) and storage >= 0
                else 0
            ),
            camera_ready=(
                readiness is not None
                and readiness.connection_epoch == drone.get("connection_epoch")
                and readiness.camera_ok
                and readiness.storage_ok
            ),
            active_task_id=None,
            position_loss_since_ms=None,
        )
        drones.append(drone)
    snapshot = FleetSnapshot.from_relay_state(
        {**state, "drones": drones},
        enrichment=RelaySnapshotEnrichment(
            operator_present=operator_last_seen_ms is not None,
            operator_last_seen_ms=0 if operator_last_seen_ms is None else operator_last_seen_ms,
            aircraft=enrichment,
        ),
    )
    if estop_requested and not snapshot.estop_active:
        snapshot = replace(snapshot, estop_active=True)
    return snapshot


def control_projection(intent_name: IntentName, result: ExecutionResult) -> dict[str, object]:
    """Return the control state the relay applies for one execution result.

    The network stop latches from the intent itself, never from the plan, so the
    planner and arbiter path can only add commands and never remove the latch. Arm
    and selection apply only once their plan completed. A plan still waiting on a
    node's terminal acknowledgement is published as the session's ``accepted_plan``
    so a roster change can invalidate it by ``intent_id``; every terminal result
    clears it.
    """
    projection: dict[str, object] = {}
    if intent_name is IntentName.ESTOP:
        projection["estop"] = True
    plan = result.plan
    if result.status is LifecycleStatus.EXECUTING:
        if plan is not None:
            projection["accepted_plan"] = {
                "plan_id": plan.plan_id,
                "intent_id": plan.intent_id,
                "intent_name": plan.intent_name.value,
                "roster_version": plan.roster_version,
                "selection": list(plan.selection),
            }
    elif result.status in _TERMINAL:
        projection["accepted_plan"] = None
    if plan is not None and result.status is LifecycleStatus.COMPLETED:
        if plan.selection_update is not None:
            projection["selection"] = plan.selection_update
        if plan.armed_update is not None:
            projection["armed"] = plan.armed_update
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


def apply_result(
    session: RelaySession, intent: IntentV1, result: ExecutionResult
) -> list[dict[str, object]]:
    """Apply one result's control projection and lifecycle inside a session operation.

    Selection and arm updates apply only while the plan's roster is still the
    session's roster; otherwise they are dropped and the result becomes
    ``invalidated`` with ``stale_roster``. The network stop latch is never dropped.
    """
    projection = control_projection(intent.name, result)
    plan = result.plan
    roster_version = session.registry.roster_version
    if (
        plan is not None
        and plan.roster_version != roster_version
        and ("selection" in projection or "armed" in projection)
    ):
        projection.pop("selection", None)
        projection.pop("armed", None)
        result = replace(
            result,
            status=LifecycleStatus.INVALIDATED,
            refusal=Refusal(
                intent_id=result.intent_id,
                roster_version=roster_version,
                drone_id=None,
                connection_epoch=None,
                reason=RefusalReason.STALE_ROSTER,
                detail="the roster changed before the accepted control state could be applied",
                status=LifecycleStatus.INVALIDATED,
            ),
        )
    events: list[dict[str, object]] = []
    if projection:
        events.append(session.update_control_projection(**projection))  # type: ignore[arg-type]
    events.append(record_result(session, result))
    return events


@dataclass(eq=False, slots=True)
class _Job:
    """One accepted intent on a lane; ``cancelled_by`` is the preemption flag."""

    intent: IntentV1
    session: RelaySession | None
    publications: list[dict[str, object]] = field(default_factory=list)
    cancelled_by: str | None = None
    finished: bool = False
    prepared: PreparedExecution | None = None

    def check(self) -> None:
        if self.cancelled_by is not None:
            raise PlanPreempted(self.cancelled_by)


@dataclass(slots=True)
class _AwaitingExecution:
    job: _Job
    session: RelaySession
    dispatcher: AdapterDispatcher
    snapshot: FleetSnapshot
    pending: ExecutionResult
    watchdog_running: bool = False
    generation: int = 0
    resume_pending: bool = False
    io_lock: threading.RLock = field(default_factory=threading.RLock, repr=False)


@dataclass(frozen=True, slots=True, eq=False)
class _ResumeToken:
    intent_id: str
    owner: _AwaitingExecution
    pending: ExecutionResult
    generation: int
    acknowledgement: CommandAcknowledgement


class _Lane:
    """One worker thread and its queue; ``pending`` and ``running`` share the session lock."""

    def __init__(self, name: str, lock: threading.Lock) -> None:
        self.name = name
        self.pending: deque[_Job] = deque()
        self.running: _Job | None = None
        self.closed = False
        self.ready = threading.Condition(lock)


class _PreemptibleLink:
    """Gate one plan's wire sends and acknowledgement waits on its cancellation flag.

    The relay session's intent ledger is the atomic guard: the sink records a
    preempted intent as terminal under the session lock, so ``issue_command`` refuses
    any later command for it. This wrapper adds the prompt exit, before each send and
    after each acknowledgement wait, so the plan does not grind through the commands
    the ledger would refuse anyway.
    """

    def __init__(self, inner: RelayNodeLink, job: _Job, session: RelaySession) -> None:
        self._inner = inner
        self._job = job
        self._session = session

    def connection_epoch(self, drone_id: int) -> int | None:
        return self._inner.connection_epoch(drone_id)

    def send(self, request: CommandRequest) -> None:
        self._job.check()
        self._inner.send(request)

    def authorize_navigation(self, plan: object, command: object, snapshot: FleetSnapshot) -> None:
        self._job.check()
        authorize = getattr(self._inner, "authorize_navigation", None)
        if not callable(authorize):
            raise AdapterError("navigation authorization is unavailable")
        authorize(plan, command, snapshot)

    def await_acknowledgement(
        self, command_id: str, *, timeout_ms: int
    ) -> WireAcknowledgement | None:
        acknowledgement = self._inner.await_acknowledgement(command_id, timeout_ms=timeout_ms)
        if self._job.cancelled_by is not None:
            self._session.discard_command_waiter(command_id)
            raise PlanPreempted(self._job.cancelled_by)
        return acknowledgement

    def camera_capabilities(self, drone_id: int) -> CapabilitiesFrame | None:
        return self._inner.camera_capabilities(drone_id)

    def media_files(self, drone_id: int, capture_id: str) -> tuple[MediaFileRecord, ...]:
        return self._inner.media_files(drone_id, capture_id)


class AutonomySession:
    """One session's planner, arbiter, operator evidence, and intent lanes.

    The ``normal`` lane runs operator intents in arrival order. ``hold`` and ``estop``
    each run at once on their own lane and cancel the plans they preempt: the stop
    records the cancelled intent as ``invalidated`` inside its own intent operation,
    under the session lock, so ``issue_command`` refuses anything that plan tries to
    send afterwards; the plan's dispatch also checks its flag before every command
    and send and after every acknowledgement wait, then exits without a best-effort
    hold. A hold cancels operator motion and camera plans but queues behind a running
    ``land_all`` or ``estop``; a network stop cancels whatever is running and latches
    the session's ``estop`` in the same operation that accepted it.
    """

    def __init__(self, composition: AutonomyComposition, session_id: str) -> None:
        self.session_id = session_id
        self._composition = composition
        self.capability_profile = composition.capability_profile
        self.search = (
            SearchRuntime(composition.config.search, composition.config.navigation)
            if composition.config.search is not None and composition.config.navigation is not None
            else None
        )
        self.planner = DeterministicPlanner(
            composition.config.planning,
            self.capability_profile,
            navigation=composition.config.navigation,
            mapped_formations=composition.config.mapped_formations,
            search=self.search,
        )
        self.arbiter = SafetyArbiter(composition.config.safety)
        self._lock = threading.Lock()
        self._control_lock = threading.RLock()
        self.control = (
            None
            if composition.config.control_localization is None
            else ControlRuntime(
                composition.config.control_localization, node_keys=composition.node_keys
            )
        )
        self.navigation_control = (
            None
            if not composition.config.enable_localized_navigation
            else NavigationControl(
                NavigationControlConfig(
                    composition.config.navigation,
                    composition.config.control_localization,
                    composition.config.navigation_deployment.configuration_id,
                    composition.node_keys,
                )
            )
        )
        self.search_bridge = (
            SearchBridge(
                session_id,
                self.search,
                self.control.config,
                composition.config.detection_camera_ids,
            )
            if self.search is not None and self.control is not None
            else None
        )
        self._search_intents: deque[str] = deque(maxlen=32)
        self._operator_last_seen_ms: int | None = None
        self._stop_requested = False
        self._normal = _Lane("normal", self._lock)
        self._hold = _Lane("hold", self._lock)
        self._estop = _Lane("estop", self._lock)
        self._lanes = (self._normal, self._hold, self._estop)
        self._awaiting: dict[str, _AwaitingExecution] = {}
        self._navigation_previews: dict[str, tuple[int, PreparedExecution]] = {}
        self._workers = [
            threading.Thread(
                target=self._run,
                args=(lane,),
                name=f"autonomy-{session_id}-{lane.name}",
                daemon=True,
            )
            for lane in self._lanes
        ]
        for worker in self._workers:
            worker.start()

    def submit(self, intent: IntentV1, _state: dict[str, object]) -> None:
        """``IntentSink``: record operator activity and route the intent without blocking."""
        with self._lock:
            previous = self._operator_last_seen_ms
            self._operator_last_seen_ms = intent.t if previous is None else max(previous, intent.t)
        runtime = self._composition.runtime_if_bound()
        job = _Job(intent, None if runtime is None else runtime.sessions.get(self.session_id))
        if self.requires_route_preview(intent):
            with self._lock:
                preview = self._navigation_previews.pop(intent.intent_id, None)
            if (
                preview is None
                or preview[0] < _state["t"]
                or not self._same_preview_intent(preview[1].intent, intent)
            ):
                raise ValueError("navigation requires a current matching server preview")
            job.prepared = replace(preview[1], intent=intent)
        elif intent.name in {IntentName.HOLD, IntentName.ESTOP, IntentName.SELECT}:
            with self._lock:
                self._navigation_previews.clear()
        try:
            lane = self._route(job)
        except Exception:
            # A stop must reach its lane even if the preemption bookkeeping fails.
            _LOGGER.exception("preemption bookkeeping failed for intent %s", intent.intent_id)
            lane = self._lane_for(intent.name)
        with lane.ready:
            lane.pending.append(job)
            lane.ready.notify()

    def __call__(self, intent: IntentV1, state: dict[str, object]) -> None:
        self.submit(intent, state)

    def compile_text(self, text: str, correlation_id: str) -> LanguageCompilationOutcome:
        from language.navigation import navigation_from_record

        runtime = self._composition.runtime
        session = runtime.sessions[self.session_id]
        state = session.current_state()
        with self._lock:
            self._operator_last_seen_ms = state["t"]
        snapshot = self.snapshot(state, capture_readiness=session.capture_readiness)
        metadata = self.navigation_metadata()
        navigation = (
            None
            if metadata is None
            else navigation_from_record(
                {
                    **metadata,
                    **self.capability_profile.state_value(),
                },
                self.capability_profile,
            )
        )
        return self._composition.language.compile(
            text,
            snapshot,
            navigation,
            runtime.authoritative_rooms(session),
            session_id=self.session_id,
            state_event_id=state["event_id"],
            capability_profile=self.capability_profile,
            translation=self.planner.config.translation_grounding(snapshot),
            altitude_grounding=self.planner.config.altitude_grounding(),
            correlation_id=correlation_id,
        )

    def requires_route_preview(self, intent: IntentV1) -> bool:
        return intent.name in {IntentName.NAVIGATE, IntentName.SEARCH} or (
            intent.name is IntentName.FORMATION_SET
            and self._composition.config.mapped_formations is not None
        )

    def route_runtime(self, name: IntentName) -> object:
        if name is IntentName.FORMATION_SET:
            return self._composition.config.mapped_formations
        if name is IntentName.SEARCH and self.search is not None:
            return self.search
        return self._composition.config.navigation

    def cancel_navigation_previews(self, intent_id: str | None = None) -> None:
        with self._lock:
            if intent_id is None:
                self._navigation_previews.clear()
            else:
                self._navigation_previews.pop(intent_id, None)

    @staticmethod
    def _same_preview_intent(first: IntentV1, second: IntentV1) -> bool:
        return replace(first, t=second.t, confirm=second.confirm) == second

    def prepare_navigation_preview(
        self, intent: IntentV1, state: dict[str, object]
    ) -> tuple[int, PreparedExecution] | Refusal:
        with self._lock:
            self._operator_last_seen_ms = state["t"]
        snapshot = self.snapshot(state)
        if self._normal_intent_conflicts_with_awaiting_route(intent):
            return _route_ownership_refusal(intent, snapshot)
        with self._lock:
            if intent.intent_id in self._navigation_previews:
                return Refusal(
                    intent.intent_id,
                    snapshot.roster_version,
                    None,
                    None,
                    RefusalReason.INVALID_PLAN,
                    "preview ID is already in use; create a new request",
                )
        preview_intent = replace(intent, confirm=True)
        refusal = self.arbiter.check_intent(preview_intent, snapshot)
        if refusal is not None:
            return refusal
        planned = self.planner.plan(preview_intent, snapshot)
        if isinstance(planned, Refusal):
            return planned
        if planned.navigation is None:
            return Refusal(
                intent.intent_id,
                snapshot.roster_version,
                None,
                None,
                RefusalReason.UNSUPPORTED,
                "mapped route runtime is unavailable",
            )
        refusal = self.arbiter.check_plan(planned, snapshot)
        if refusal is not None:
            return refusal
        prepared = PreparedExecution(intent, planned, snapshot)
        expires = snapshot.now_ms + 15_000
        with self._lock:
            self._navigation_previews = {
                key: value
                for key, value in self._navigation_previews.items()
                if value[0] >= snapshot.now_ms
            }
            if len(self._navigation_previews) >= 32:
                self._navigation_previews.pop(next(iter(self._navigation_previews)))
            self._navigation_previews[intent.intent_id] = (expires, prepared)
        return expires, prepared

    def validate_navigation_confirmation(
        self, intent: IntentV1, state: dict[str, object]
    ) -> str | None:
        with self._lock:
            cached = self._navigation_previews.get(intent.intent_id)
        if cached is None or cached[0] < state["t"]:
            return "navigation preview is missing or expired; prepare again"
        if not self._same_preview_intent(cached[1].intent, intent):
            return "navigation confirmation differs from the preview; prepare again"
        if not intent.confirm:
            return "route dispatch requires operator confirmation"
        snapshot = self.snapshot(state)
        if self._normal_intent_conflicts_with_awaiting_route(intent):
            return "an active mapped route owns this session; hold or land before changing motion"
        refusal = self.arbiter.check_intent(intent, snapshot)
        if refusal is not None:
            return refusal.detail
        refusal = self.arbiter.check_plan(cached[1].plan, snapshot)
        if refusal is not None:
            return refusal.detail
        navigation = self.route_runtime(intent.name)
        if navigation is None:
            return "navigation deployment is unavailable"
        for command in cached[1].plan.commands[:1]:
            refusal = navigation.check(cached[1].plan, command, snapshot)
            if refusal is not None:
                return refusal.detail
        return None

    def navigation_metadata(self) -> dict[str, object] | None:
        from relay.navigation_metadata import navigation_metadata

        navigation = self._composition.config.navigation
        if navigation is None:
            return None
        try:
            metadata = navigation_metadata(navigation)
            formations = self._composition.config.mapped_formations
            if formations is not None:
                metadata["formations"] = [
                    {"name": name, "zone_id": entry.zone.zone_id}
                    for name, entry in formations.config.formations.items()
                ]
            if self.search is not None:
                metadata["search"] = {
                    "zones": [{"zone_id": key} for key in self.search.config.areas],
                    "target_classes": ["backpack", "bottle", "suitcase"],
                }
            return metadata
        except (ValueError, OSError):
            return None

    def authorize_leave(
        self, drone_id: int, connection_epoch: int, state: dict[str, object]
    ) -> bool:
        """``LeaveAuthorizer``: approve only a landed, disarmed, task-free current aircraft."""
        snapshot = self.snapshot(state)
        aircraft = snapshot.aircraft.get(drone_id)
        if aircraft is None or aircraft.connection_epoch != connection_epoch:
            return False
        return authorize_graceful_removal(snapshot, drone_id).allowed

    def snapshot(
        self, state: Mapping[str, object], *, capture_readiness: ReadinessSource | None = None
    ) -> FleetSnapshot:
        with self._lock:
            operator_last_seen_ms = self._operator_last_seen_ms
            estop_requested = self._stop_requested
            route_owners = {
                drone_id: owner.job.intent.intent_id
                for owner in self._awaiting.values()
                if owner.job.cancelled_by is None
                and owner.pending.plan is not None
                and owner.pending.plan.navigation is not None
                for drone_id in owner.pending.plan.selection
            }
        snapshot = relay_snapshot(
            state,
            operator_last_seen_ms=operator_last_seen_ms,
            estop_requested=estop_requested,
            capture_readiness=capture_readiness,
        )
        if route_owners:
            snapshot = replace(
                snapshot,
                aircraft={
                    drone_id: replace(
                        aircraft,
                        active_task_id=route_owners.get(drone_id, aircraft.active_task_id),
                    )
                    for drone_id, aircraft in snapshot.aircraft.items()
                },
            )
        if self.control is None:
            return snapshot
        runtime = self._composition.runtime_if_bound()
        session = None if runtime is None else runtime.sessions.get(self.session_id)
        snapshot = (
            self.control.apply(snapshot)
            if session is None
            else session.apply_control_localization(snapshot)
        )
        if self.search_bridge is not None:
            self.search_bridge.observe_snapshot(snapshot)
        return snapshot

    def _start_search(
        self, session: RelaySession, intent: IntentV1, snapshot: FleetSnapshot
    ) -> list[dict[str, object]]:
        assert self.search is not None
        if not intent.confirm:
            raise ValueError("search start requires confirmation")
        event = session.record_search_event(
            {
                "type": "search_started",
                "intent_id": intent.intent_id,
                "selection": list(intent.selection),
            }
        )
        self.search.start(intent.intent_id, snapshot)
        self._search_intents.append(intent.intent_id)
        return [event, session.record_search_event(search_progress(self.search, intent.intent_id))]

    def _search_command_completed(self, plan, command, snapshot: FleetSnapshot) -> None:
        assert self.search is not None
        session = self._composition.runtime.sessions[self.session_id]

        def operation() -> list[dict[str, object]]:
            event = session.record_search_event(
                {
                    "type": "search_command_completed",
                    "intent_id": plan.intent_id,
                    "command_id": command.command_id,
                }
            )
            self.search.on_command(plan.intent_id, command.command_id, snapshot)
            return [
                event,
                session.record_search_event(search_progress(self.search, plan.intent_id)),
            ]

        self._publish(self._composition.runtime, operation)

    def process_perception(
        self, raw: dict[str, object], principal: Principal, now_ms: int
    ) -> list[dict[str, object]]:
        session = self._composition.runtime.sessions[self.session_id]
        if self.search_bridge is None:
            return [
                session.protocol_refusal(
                    reason="perception_unavailable",
                    detail="search control evidence is unconfigured",
                )
            ]
        self.snapshot(session.current_state())
        result = self.search_bridge.consume(raw, principal, now_ms)
        events = [session.record_search_event(result)]
        intent_id = result.get("intent_id")
        if result["accepted"] and raw["type"] == "perception.sighting":
            events.append(
                session.record_search_event(
                    {
                        key: value
                        for key, value in raw.items()
                        if key not in {"signature", "event_id"}
                    }
                )
            )
        if isinstance(intent_id, str):
            events.append(session.record_search_event(search_progress(self.search, intent_id)))
        return events

    def periodic_events(self, state: dict[str, object]) -> list[dict[str, object]]:
        with self._lock:
            waiting = [
                owner
                for owner in self._awaiting.values()
                if not owner.watchdog_running
                and owner.pending.plan is not None
                and owner.pending.plan.navigation is not None
            ]
            for owner in waiting:
                owner.watchdog_running = True
        for owner in waiting:
            threading.Thread(target=self._watch_navigation, args=(owner,), daemon=True).start()
        if self.control is None:
            return []
        runtime = self._composition.runtime_if_bound()
        session = None if runtime is None else runtime.sessions.get(self.session_id)
        if session is None:
            return []
        snapshot = self.snapshot(state if state.get("type") == "state" else session.current_state())
        packets = []
        with self._control_lock:
            for drone_id in self.control.config.pins:
                aircraft = snapshot.aircraft.get(drone_id)
                if (
                    aircraft is None
                    or aircraft.connection_epoch
                    != self.control.config.pins[drone_id].connection_epoch
                ):
                    continue
                packet = self.control.control_pose(
                    drone_id, snapshot, self.session_id, snapshot.now_ms
                )
                if packet is not None:
                    packets.append(packet)
        events = [session.record_control_pose(packet) for packet in packets]
        if self.navigation_control is not None:
            events.extend(
                session.record_navigation_pose(packet)
                for packet in self.navigation_control.pose(snapshot, self.session_id)
            )
        return events

    def _watch_navigation(self, owner: _AwaitingExecution) -> None:
        pending = owner.pending
        generation = owner.generation
        intent_id = owner.job.intent.intent_id

        def owns() -> bool:
            with self._lock:
                return (
                    self._awaiting.get(intent_id) is owner
                    and owner.pending is pending
                    and owner.generation == generation
                    and not owner.resume_pending
                    and owner.job.cancelled_by is None
                )

        def current() -> FleetSnapshot:
            return self.snapshot(
                owner.session.current_state(), capture_readiness=owner.session.capture_readiness
            )

        try:
            with owner.io_lock:
                if not owns():
                    return
                result = owner.dispatcher.expire_navigation(
                    pending.plan,
                    pending,
                    owner.snapshot,
                    current_snapshot=current,
                    owner_still_valid=owns,
                )
                if result is None:
                    return

                def operation() -> list[dict[str, object]]:
                    if not owns():
                        return []
                    events = apply_result(owner.session, owner.job.intent, result)
                    with self._lock:
                        if (
                            self._awaiting.get(intent_id) is owner
                            and owner.pending is pending
                            and owner.generation == generation
                        ):
                            self._awaiting.pop(intent_id)
                            owner.job.finished = True
                    if owner.job.intent.name is IntentName.SEARCH and self.search is not None:
                        self.search.hold(intent_id, "navigation_watchdog")
                        events.append(
                            owner.session.record_search_event(
                                search_progress(self.search, intent_id)
                            )
                        )
                    return events

                self._publish(self._composition.runtime, operation)
        except Exception:
            _LOGGER.exception("navigation watchdog failed for %s", intent_id)
        finally:
            with self._lock:
                owner.watchdog_running = False

    def close(self, timeout_s: float) -> None:
        for lane in self._lanes:
            with lane.ready:
                lane.closed = True
                lane.ready.notify_all()
        for worker in self._workers:
            worker.join(timeout=timeout_s)

    def _lane_for(self, name: IntentName) -> _Lane:
        if name is IntentName.ESTOP:
            return self._estop
        if name is IntentName.HOLD:
            return self._hold
        return self._normal

    def _route(self, job: _Job) -> _Lane:
        """Choose the lane and cancel the plans this intent preempts before it queues."""
        name = job.intent.name
        if name is IntentName.ESTOP:
            with self._lock:
                self._stop_requested = True
            if job.session is not None:
                # Latch inside the accepting operation: no worker, plan, or publish
                # failure can lose it, and every later snapshot is stopped.
                job.publications.append(job.session.update_control_projection(estop=True))
            self._cancel(
                job,
                PREEMPTED_BY_ESTOP,
                running_on=(self._normal, self._hold),
                running_names=_ESTOP_PREEMPTS,
            )
            return self._estop
        if name is IntentName.HOLD:
            with self._lock:
                running = self._normal.running
                behind_safety_plan = (
                    running is not None
                    and not running.finished
                    and running.intent.name in _SAFETY_PLANS
                )
            self._cancel(
                job, PREEMPTED_BY_HOLD, running_on=(self._normal,), running_names=HOLD_PREEMPTS
            )
            return self._normal if behind_safety_plan else self._hold
        if name in {IntentName.LAND, IntentName.LAND_ALL}:
            self._cancel(
                job,
                PREEMPTED_BY_LAND,
                running_on=(self._normal,),
                running_names=_MAPPED_ROUTE_INTENTS,
                queued_names=_MAPPED_ROUTE_INTENTS,
                affected_drones=(
                    None if name is IntentName.LAND_ALL else frozenset(job.intent.selection)
                ),
            )
        return self._normal

    def _normal_intent_conflicts_with_awaiting_route(self, intent: IntentV1) -> bool:
        if intent.name in _ROUTE_OWNERSHIP_EXEMPTIONS:
            return False
        with self._lock:
            return any(
                owner.job.cancelled_by is None
                and owner.pending.plan is not None
                and owner.pending.plan.navigation is not None
                for owner in self._awaiting.values()
            )

    def _cancel(
        self,
        stop: _Job,
        reason: str,
        *,
        running_on: tuple[_Lane, ...],
        running_names: frozenset[IntentName],
        queued_names: frozenset[IntentName] = HOLD_PREEMPTS,
        affected_drones: frozenset[int] | None = None,
    ) -> None:
        """Invalidate the plans a stop preempts: running ones by name, queued motion ones.

        The invalidation is recorded first, under the session lock the sink already
        holds, so it is atomic against ``issue_command``; the flag is set afterwards so
        the plan exits promptly. The stop publishes the records when it starts. A plan
        that reached a terminal state first keeps that result.
        """

        def conflicts(victim: _Job) -> bool:
            return affected_drones is None or bool(
                affected_drones.intersection(victim.intent.selection)
            )

        with self._lock:
            victims = [
                lane.running
                for lane in running_on
                if lane.running is not None
                and not lane.running.finished
                and lane.running.cancelled_by is None
                and lane.running.intent.name in running_names
                and conflicts(lane.running)
            ]
            victims.extend(
                queued
                for queued in self._normal.pending
                if queued.cancelled_by is None
                and queued.intent.name in queued_names
                and conflicts(queued)
            )
            victims.extend(
                owner.job
                for owner in self._awaiting.values()
                if owner.job.cancelled_by is None
                and owner.job.intent.name in running_names
                and conflicts(owner.job)
            )
        victims = list({id(victim): victim for victim in victims}.values())
        session = stop.session
        if not victims or session is None:
            return
        for victim in victims:
            try:
                event = session.record_lifecycle(
                    intent_id=victim.intent.intent_id,
                    status=WireLifecycleStatus.INVALIDATED,
                    source=LIFECYCLE_SOURCE,
                    reason=reason,
                    detail=(
                        f"{stop.intent.name.value} {stop.intent.intent_id} cancelled this "
                        f"{victim.intent.name.value}; its remaining commands are not sent"
                    ),
                )
            except ValueError:
                continue
            with self._lock:
                victim.cancelled_by = reason
                self._awaiting.pop(victim.intent.intent_id, None)
            if self.navigation_control is not None:
                self.navigation_control.invalidate(victim.intent.intent_id)
            stop.publications.append(event)
            if victim.intent.name is IntentName.SEARCH and self.search is not None:
                self.search.cancel(victim.intent.intent_id, reason)
                stop.publications.append(
                    session.record_search_event(
                        search_progress(self.search, victim.intent.intent_id)
                    )
                )

    def _run(self, lane: _Lane) -> None:
        while True:
            with lane.ready:
                while not lane.pending and not lane.closed:
                    lane.ready.wait()
                if not lane.pending:
                    return
                job = lane.pending.popleft()
                lane.running = job
            try:
                self._execute(job)
            except Exception:
                _LOGGER.exception(
                    "autonomy %s lane failed session=%s intent=%s",
                    lane.name,
                    self.session_id,
                    job.intent.intent_id,
                )
            finally:
                with lane.ready:
                    lane.running = None

    def _execute(self, job: _Job) -> None:
        runtime = self._composition.runtime
        session = job.session or runtime.sessions.get(self.session_id)
        intent = job.intent
        if session is None:
            _LOGGER.error(
                "session %s is not active; intent %s cannot be reported or dispatched",
                self.session_id,
                intent.intent_id,
            )
            return
        if job.publications:
            publications = list(job.publications)
            self._publish(runtime, lambda: publications)
        if job.cancelled_by is not None:
            return  # cancelled while queued; the stop recorded its invalidation
        if self._normal_intent_conflicts_with_awaiting_route(intent):
            snapshot = self.snapshot(
                session.current_state(), capture_readiness=session.capture_readiness
            )
            with self._lock:
                job.finished = True
            self._report(runtime, session, job, _route_ownership_refusal(intent, snapshot))
            return

        dispatcher = None
        sim_observation_ms: int | None = None

        def current() -> FleetSnapshot:
            nonlocal sim_observation_ms
            job.check()
            live = self.snapshot(
                session.current_state(), capture_readiness=session.capture_readiness
            )
            if dispatcher is not None and isinstance(dispatcher.flight, SimFlightAdapter):
                # Synchronous simulation needs a new observation tick after each command.
                sim_observation_ms = max(
                    live.now_ms,
                    (live.now_ms - 1 if sim_observation_ms is None else sim_observation_ms) + 1,
                )
                return sim_snapshot(replace(live, now_ms=sim_observation_ms), dispatcher.flight)
            return live

        def gate(link: RelayNodeLink) -> NodeLink:
            return _PreemptibleLink(link, job, session)

        try:
            snapshot = current()
            dispatcher = build_dispatcher(
                runtime,
                self.session_id,
                snapshot,
                arbiter=self.arbiter,
                sim_camera_config=self._composition.config.sim_camera,
                link_wrapper=gate,
                navigation_control=self.navigation_control,
            )
            dispatcher.navigation = self.route_runtime(intent.name)
            if intent.name is IntentName.SEARCH and self.search is not None:
                self._publish(runtime, lambda: self._start_search(session, intent, snapshot))
                dispatcher.on_navigation_command_completed = self._search_command_completed
            controller = AutonomyController(
                planner=self.planner, arbiter=self.arbiter, dispatcher=dispatcher
            )
            result = (
                controller.execute(intent, snapshot, current_snapshot=current)
                if job.prepared is None
                else controller.dispatch_prepared(job.prepared, current_snapshot=current)
            )
        except PlanPreempted as preempted:
            _LOGGER.info("intent %s stopped: %s", intent.intent_id, preempted.reason)
            return
        except Exception as error:  # the console still receives a typed terminal result
            _LOGGER.exception(
                "autonomy dispatch path failed session=%s intent=%s",
                self.session_id,
                intent.intent_id,
            )
            result = _composition_failure(intent, session, error)
        with self._lock:
            if result.status is LifecycleStatus.EXECUTING:
                self._awaiting[intent.intent_id] = _AwaitingExecution(
                    job=job,
                    session=session,
                    dispatcher=dispatcher,
                    snapshot=snapshot,
                    pending=result,
                )
                job.finished = False
            else:
                self._awaiting.pop(intent.intent_id, None)
                job.finished = True
            cancelled = job.cancelled_by
        if cancelled is not None:
            if self.navigation_control is not None:
                self.navigation_control.invalidate(intent.intent_id)
            return  # a stop already recorded this plan's terminal lifecycle
        if result.status is not LifecycleStatus.EXECUTING and self.navigation_control is not None:
            self.navigation_control.invalidate(intent.intent_id)
        if (
            intent.name is IntentName.SEARCH
            and self.search is not None
            and result.status
            in {
                LifecycleStatus.FAILED,
                LifecycleStatus.INVALIDATED,
                LifecycleStatus.REFUSED,
            }
        ):
            self.search.hold(intent.intent_id, result.status.value)
        if dispatcher is not None and isinstance(dispatcher.flight, SimFlightAdapter):
            self._publish(runtime, lambda: record_sim_telemetry(session, dispatcher.flight))
        self._report(runtime, session, job, result)

    def prepare_resume(
        self, session: RelaySession, acknowledgement: WireAcknowledgement
    ) -> _ResumeToken | None:
        """Claim a late terminal result only for this session's exact waiting command."""
        if acknowledgement.status not in {
            WireLifecycleStatus.COMPLETED,
            WireLifecycleStatus.FAILED,
            WireLifecycleStatus.INVALIDATED,
        }:
            return None
        with self._lock:
            owner = self._awaiting.get(acknowledgement.intent_id)
            if (
                owner is None
                or owner.session is not session
                or owner.job.cancelled_by is not None
                or owner.pending.status is not LifecycleStatus.EXECUTING
            ):
                return None
            waiting = next(
                (
                    item
                    for item in owner.pending.acknowledgements
                    if item.command_id == acknowledgement.command_id
                    and item.status in {LifecycleStatus.ACCEPTED, LifecycleStatus.EXECUTING}
                ),
                None,
            )
            command = (
                next(
                    (
                        item
                        for item in owner.pending.plan.commands
                        if item.command_id == acknowledgement.command_id
                    ),
                    None,
                )
                if owner.pending.plan is not None
                else None
            )
            if waiting is None or command is None:
                return None
            reason = _domain_refusal_reason(acknowledgement.reason)
            terminal = CommandAcknowledgement(
                command_id=acknowledgement.command_id,
                intent_id=acknowledgement.intent_id,
                roster_version=acknowledgement.roster_version,
                drone_id=acknowledgement.drone_id,
                connection_epoch=acknowledgement.connection_epoch,
                status=LifecycleStatus(acknowledgement.status.value),
                reason=reason,
                detail=acknowledgement.detail or "",
            )
            if (
                terminal.intent_id != command.intent_id
                or terminal.roster_version != command.roster_version
                or terminal.drone_id != command.drone_id
                or terminal.connection_epoch != command.connection_epoch
            ):
                return None
            owner.resume_pending = True
            return _ResumeToken(
                acknowledgement.intent_id,
                owner,
                owner.pending,
                owner.generation,
                terminal,
            )

    def resume_io(self, token: _ResumeToken) -> ExecutionResult:
        """Resume dependent commands outside relay/session locks after a late result."""
        owner = token.owner

        def current() -> FleetSnapshot:
            return self.snapshot(
                owner.session.current_state(),
                capture_readiness=owner.session.capture_readiness,
            )

        with owner.io_lock:
            if not self._owns_resume(token):
                return token.pending
            try:
                assert token.pending.plan is not None
                return owner.dispatcher.resume_after_completion(
                    token.pending.plan,
                    token.pending,
                    token.acknowledgement,
                    owner.snapshot,
                    current_snapshot=current,
                    owner_still_valid=lambda: self._owns_resume(token),
                )
            except Exception as error:
                return _resume_failure(token, error)

    def commit_resume(self, token: _ResumeToken, result: ExecutionResult) -> RelayExecution | None:
        """Commit a still-owned late result and retain ownership if another command waits."""
        resumed_snapshot = (
            self.snapshot(
                token.owner.session.current_state(),
                capture_readiness=token.owner.session.capture_readiness,
            )
            if result.status is LifecycleStatus.EXECUTING
            else None
        )
        with self._lock:
            if (
                self._awaiting.get(token.intent_id) is not token.owner
                or token.owner.job.cancelled_by is not None
                or token.owner.pending is not token.pending
                or token.owner.generation != token.generation
            ):
                return None
            owner = token.owner
            owner.pending = result
            owner.generation += 1
            owner.resume_pending = False
            if result.status is LifecycleStatus.EXECUTING:
                assert resumed_snapshot is not None
                owner.snapshot = resumed_snapshot
            else:
                self._awaiting.pop(token.intent_id, None)
                owner.job.finished = True
        try:
            events = apply_result(owner.session, owner.job.intent, result)
        except ValueError:
            if owner.job.cancelled_by is None:
                raise
            return None
        return RelayExecution(result, tuple(events))

    def resume_after_acknowledgement(
        self, session: RelaySession, acknowledgement: WireAcknowledgement
    ) -> RelayExecution | None:
        """Synchronous compatibility path used outside the asynchronous relay runtime."""
        token = self.prepare_resume(session, acknowledgement)
        if token is None:
            return None
        return self.commit_resume(token, self.resume_io(token))

    def _owns_resume(self, token: _ResumeToken) -> bool:
        with self._lock:
            return (
                self._awaiting.get(token.intent_id) is token.owner
                and token.owner.job.cancelled_by is None
                and token.owner.pending is token.pending
                and token.owner.generation == token.generation
                and token.owner.resume_pending
            )

    def _report(
        self, runtime: RelayRuntime, session: RelaySession, job: _Job, result: ExecutionResult
    ) -> None:
        def operation() -> list[dict[str, object]]:
            try:
                return apply_result(session, job.intent, result)
            except ValueError:
                if job.cancelled_by is None:
                    raise
                _LOGGER.info("intent %s was cancelled as it completed", job.intent.intent_id)
                return []

        self._publish(runtime, operation)

    def _publish(
        self, runtime: RelayRuntime, operation: Callable[[], list[dict[str, object]]]
    ) -> None:
        """Run ``operation`` under the session's ordering and fan its events out.

        The operation runs exactly once: if the relay loop is gone or refuses the
        work before running it, it is applied directly so the audit record and the
        control projection are never lost; consoles then catch up from the periodic
        state fan-out or replay.
        """
        ran = False

        def guarded() -> list[dict[str, object]]:
            nonlocal ran
            ran = True
            return operation()

        loop = runtime.loop
        if loop is not None and not loop.is_closed():
            future = asyncio.run_coroutine_threadsafe(
                runtime.process_and_publish(self.session_id, guarded), loop
            )
            try:
                future.result(timeout=_PUBLISH_TIMEOUT_S)
                return
            except Exception:
                if ran:
                    raise
                _LOGGER.exception(
                    "relay loop did not run the result operation for session %s; "
                    "applying it directly",
                    self.session_id,
                )
        guarded()


class AutonomyComposition:
    """Per-session autonomy workers behind ``create_app``'s sink and leave factories."""

    def __init__(
        self, config: AutonomyConfig, *, node_keys: Mapping[int, bytes] | None = None
    ) -> None:
        self.config = config
        self.node_keys = dict(node_keys or {})
        self.language = config.language or LanguageRuntime.from_env()
        self.capability_profile: CapabilityProfile = config.planning.effective_capability_profile()
        if config.navigation is not None:
            self.capability_profile = CapabilityProfile(
                name="mapped_navigation",
                enabled_intent_names=self.capability_profile.enabled_intent_names
                | {IntentName.NAVIGATE},
            )
        if config.search is not None and config.navigation is not None:
            self.capability_profile = CapabilityProfile(
                "mapped_search", self.capability_profile.enabled_intent_names | {IntentName.SEARCH}
            )
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

    def runtime_if_bound(self) -> RelayRuntime | None:
        return self._runtime_source()

    def transcript_service_factory(self, _runtime: RelayRuntime) -> TranscriptService:
        composition = self

        class VoiceCompiler:
            def compile(
                self, transcript: str, _state: object, **kwargs: object
            ) -> tuple[object, None]:
                session_id = kwargs["session_id"]
                return composition.session(session_id).compile_text(
                    transcript, kwargs["correlation_id"]
                ), None

        return TranscriptService(compiler=VoiceCompiler())

    def intent_sink_factory(self, session: RelaySession) -> IntentSink:
        return self.session(session.session_id)

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
    if config.enable_localized_navigation and (
        settings.adapter_backend is not AdapterBackend.REMOTE
        or config.navigation is None
        or config.navigation_deployment is None
        or config.control_localization is None
    ):
        raise SettingsError(
            "localized navigation requires remote deployment and control localization"
        )
    if (
        config.mapped_formations is not None or config.search is not None
    ) and config.navigation is None:
        raise SettingsError("mapped missions require an explicit navigation deployment")
    if config.mapped_formations is not None and (
        config.mapped_formations.config.navigation != config.navigation.config
        or config.mapped_formations.artifact() != config.navigation.artifact()
    ):
        raise SettingsError(
            "mapped formations must use the navigation deployment geometry and bounds"
        )
    if (
        settings.adapter_backend is AdapterBackend.REMOTE
        and config.navigation is not None
        and not config.enable_localized_navigation
    ):
        raise SettingsError("remote navigation requires SWEEP_ENABLE_LOCALIZED_NAVIGATION=true")
    if settings.adapter_backend is AdapterBackend.REMOTE and config.navigation is not None:
        deployment = config.navigation_deployment
        control = config.control_localization
        if (
            deployment is None
            or deployment.backend != "remote"
            or deployment.runtime is not config.navigation
            or control is None
            or control.identity != deployment.control_store_identity
        ):
            raise SettingsError(
                "remote navigation requires matching accepted deployment and control localization"
            )
        artifact = config.navigation.artifact()
        if any(
            pin.map_id != artifact.map_pin.content_sha256
            or pin.geometry_id != artifact.geometry_pin.content_sha256
            for pin in control.pins.values()
        ):
            raise SettingsError(
                "control localization pins differ from accepted navigation artifacts"
            )
        if (
            control.max_fix_age_ms > min(500, config.navigation.config.position_max_age_ms)
            or control.max_position_uncertainty_m
            > config.navigation.config.motion.pose_uncertainty_m
        ):
            raise SettingsError("control localization bounds exceed navigation allowances")
        config.navigation.control_pins = control.pins
        config.navigation.maximum_aircraft = deployment.max_aircraft
        config.navigation.require_phone_authorization = config.enable_localized_navigation
        if config.mapped_formations is not None:
            config.mapped_formations.navigation.control_pins = control.pins
            config.mapped_formations.navigation.maximum_aircraft = deployment.max_aircraft
        if config.search is not None and (
            settings.perception_key is None
            or not set(config.search.source_by_drone).issubset(config.detection_camera_ids)
            or not set(config.search.source_by_drone).issubset(control.pins)
        ):
            raise SettingsError("remote search requires authenticated pinned detector cameras")
    control_localization = config.control_localization
    if settings.localization_keys and control_localization is None:
        raise SettingsError(
            "SWEEP_CONTROL_LOCALIZATION_CONFIG is required when localization credentials are set"
        )
    if control_localization is not None and set(control_localization.pins) != set(
        settings.localization_keys
    ):
        raise SettingsError("control-localization pins must exactly match localization credentials")
    composition = AutonomyComposition(config, node_keys=settings.adapter_keys)
    app = create_app(
        settings,
        clock=clock,
        event_ids=event_ids,
        intent_sink_factory=composition.intent_sink_factory,
        capability_profile=composition.capability_profile,
        leave_authorizer_factory=composition.leave_authorizer_factory,
        transcript_service_factory=composition.transcript_service_factory,
        control_localization_store_factory=(
            None
            if control_localization is None
            else lambda session_id: composition.session(session_id).control.store
        ),
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


def _route_ownership_refusal(intent: IntentV1, snapshot: FleetSnapshot) -> ExecutionResult:
    return ExecutionResult(
        intent_id=intent.intent_id,
        roster_version=snapshot.roster_version,
        status=LifecycleStatus.REFUSED,
        refusal=Refusal(
            intent_id=intent.intent_id,
            roster_version=snapshot.roster_version,
            drone_id=None,
            connection_epoch=None,
            reason=RefusalReason.ACTIVE_TASK,
            detail="an active mapped route owns this session; hold or land before changing motion",
        ),
    )


def _resume_failure(token: _ResumeToken, error: Exception) -> ExecutionResult:
    pending = token.owner.pending
    acknowledgements = tuple(
        token.acknowledgement if item.command_id == token.acknowledgement.command_id else item
        for item in pending.acknowledgements
    )
    return ExecutionResult(
        intent_id=token.intent_id,
        roster_version=pending.roster_version,
        status=LifecycleStatus.FAILED,
        plan=pending.plan,
        acknowledgements=acknowledgements,
        refusal=Refusal(
            intent_id=token.intent_id,
            roster_version=pending.roster_version,
            drone_id=token.acknowledgement.drone_id,
            connection_epoch=token.acknowledgement.connection_epoch,
            reason=RefusalReason.ADAPTER_FAILURE,
            detail=f"late command completion resume raised {type(error).__name__}",
            status=LifecycleStatus.FAILED,
        ),
    )


def _domain_refusal_reason(value: str | None) -> RefusalReason | None:
    if value is None:
        return None
    try:
        return RefusalReason(value)
    except ValueError:
        return RefusalReason.ADAPTER_FAILURE


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
    expected = {item_field.name for item_field in fields(cls)}  # type: ignore[arg-type]
    if set(value) != expected:
        missing = sorted(expected - set(value))
        unexpected = sorted(set(value) - expected)
        raise SettingsError(
            f"{name} keys must be exactly {sorted(expected)}: "
            f"missing {missing}, unexpected {unexpected}"
        )
    hints = get_type_hints(cls)
    arguments: dict[str, object] = {}
    for item_field in fields(cls):  # type: ignore[arg-type]
        item = value[item_field.name]
        hint = hints[item_field.name]
        if is_dataclass(hint):
            item = _build_config(hint, item, f"{name}.{item_field.name}")  # type: ignore[type-var]
        elif get_origin(hint) is tuple:
            if not isinstance(item, list):
                raise SettingsError(f"{name}.{item_field.name} must be a JSON array")
            item = tuple(item)
        arguments[item_field.name] = item
    try:
        return cls(**arguments)
    except Exception as error:  # every validator failure is a configuration error
        raise SettingsError(f"{name}: {error}") from None
