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
import hmac
import json
import logging
import os
import threading
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, fields, is_dataclass, replace
from typing import get_origin, get_type_hints

from fastapi import FastAPI, Header, HTTPException, Request

from adapters.dispatch import AdapterDispatcher
from adapters.dji_mini3.remote import CommandRequest, NodeLink
from adapters.sim.camera import SimCameraConfig
from arbiter.safety import SafetyArbiter, SafetyConfig
from perception.object_detection import DEFAULT_TARGET_LABELS
from planner.controller import AutonomyController, RelayExecution
from planner.models import (
    Command,
    CommandAcknowledgement,
    ExecutionResult,
    FleetSnapshot,
    FlightState,
    LifecycleStatus,
    Plan,
    PreparedExecution,
    Refusal,
    RefusalReason,
    RelayAircraftSafetyEnrichment,
    RelaySnapshotEnrichment,
)
from planner.navigation_deployment import NavigationDeployment, load_navigation_deployment
from planner.planner import DeterministicPlanner, PlanningConfig
from planner.roster import authorize_graceful_removal
from relay.app import RelayRuntime, TranscriptServiceFactory, create_app
from relay.bridge import RelayNodeLink, build_dispatcher
from relay.capabilities import C1_CAPABILITY_PROFILE, CapabilityProfile
from relay.contracts import AdapterAcknowledgement as WireAcknowledgement
from relay.contracts import CapabilitiesFrame, CaptureReadinessFrame, MediaFileRecord
from relay.contracts import LifecycleStatus as WireLifecycleStatus
from relay.control_localization import (
    ClockMapping,
    ControlLocalizationPins,
    ControlLocalizationProjector,
)
from relay.intent_v1 import AcceptedIntent, IntentName, IntentV1, Mode, validate_intent
from relay.navigation_control import NavigationControl, NavigationControlConfig
from relay.search_deployment import load_search_runtime
from relay.search_detection import (
    CameraProviderFactory,
    DetectorFactory,
    PoseProviderFactory,
    SearchDetectionConfig,
    SearchDetectionFactory,
    StreamFactory,
)
from relay.search_detection_deployment import load_search_detection_config
from relay.search_runtime import SearchRuntime
from relay.session import Clock, EventIdFactory, IntentSink, LeaveAuthorizer, RelaySession
from relay.session_report import write_session_report
from relay.settings import AdapterBackend, RelaySettings, SettingsError

LIFECYCLE_SOURCE = "autonomy"
PREEMPTED_BY_ESTOP = "preempted_by_estop"
PREEMPTED_BY_HOLD = "preempted_by_hold"
HOLD_PREEMPTS = frozenset(
    {
        IntentName.TAKEOFF,
        IntentName.TRANSLATE,
        IntentName.ALTITUDE,
        IntentName.COME_HOME,
        IntentName.CAPTURE_ROOM,
        IntentName.NAVIGATE,
        IntentName.SEARCH,
    }
)
"""Operator motion and camera plans a hold cancels; a running safety plan finishes first."""
ReadinessSource = Callable[[int], CaptureReadinessFrame | None]
_ESTOP_PREEMPTS = frozenset(IntentName) - {IntentName.ESTOP}
_SAFETY_PLANS = frozenset({IntentName.LAND_ALL, IntentName.ESTOP})
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
    control_localization_projector: ControlLocalizationProjector | None = None
    presence_watchdog: PresenceWatchdogConfig = field(
        default_factory=lambda: PresenceWatchdogConfig()
    )

    navigation_deployment: NavigationDeployment | None = None
    search_runtime: SearchRuntime | None = None
    search_detection: SearchDetectionConfig | None = None

    def effective_capability_profile(
        self, requested: CapabilityProfile = C1_CAPABILITY_PROFILE
    ) -> CapabilityProfile:
        base_profile = self.planning.effective_capability_profile(requested)
        profile = (
            base_profile
            if self.navigation_deployment is None
            else CapabilityProfile(
                f"{base_profile.name}.navigation",
                base_profile.enabled_intent_names | {IntentName.NAVIGATE},
            )
        )
        if self.search_runtime is not None and self.navigation_deployment is not None:
            profile = CapabilityProfile(
                f"{profile.name}.search",
                profile.enabled_intent_names | {IntentName.SEARCH},
            )
        return profile

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> AutonomyConfig:
        """Load exact deployment contracts; localization requires measured pins and bounds."""
        values = os.environ if environ is None else environ
        camera_raw = values.get("SWEEP_SIM_CAMERA_JSON", "")
        localization_raw = values.get("SWEEP_CONTROL_LOCALIZATION_JSON", "")
        adapter_backend = values.get("SWEEP_ADAPTER_BACKEND", "sim")
        if adapter_backend not in {"sim", "remote"}:
            raise SettingsError("SWEEP_ADAPTER_BACKEND must be sim or remote")
        navigation = load_navigation_deployment(
            values, backend="remote" if adapter_backend == "remote" else "synthetic"
        )
        search = load_search_runtime(values, None if navigation is None else navigation.runtime)
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
            control_localization_projector=(
                None
                if not localization_raw
                else _localization_projector_from_json(
                    localization_raw, "SWEEP_CONTROL_LOCALIZATION_JSON"
                )
            ),
            navigation_deployment=navigation,
            search_runtime=search,
            search_detection=load_search_detection_config(values, search),
            presence_watchdog=_config_from_json(
                PresenceWatchdogConfig,
                values.get("SWEEP_OPERATOR_PRESENCE_WATCHDOG_JSON", '{"action":"hold"}'),
                "SWEEP_OPERATOR_PRESENCE_WATCHDOG_JSON",
            ),
        )


@dataclass(frozen=True, slots=True)
class PresenceWatchdogConfig:
    """The safety action to dispatch after operator presence expires."""

    action: str = "hold"

    def __post_init__(self) -> None:
        if self.action not in {"hold", "estop"}:
            raise ValueError("action must be hold or estop")


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
        detail=result.completion_detail if refusal is None else refusal.detail,
    )


def apply_result(
    session: RelaySession, intent: IntentV1, result: ExecutionResult
) -> list[dict[str, object]]:
    """Apply one result's control projection and lifecycle inside a session operation.

    Selection and arm updates apply only while the plan's roster is still the
    session's roster; otherwise they are dropped and the result becomes
    ``invalidated`` with ``stale_roster``. The network stop latch is never dropped.
    """
    if result.intent_id != intent.intent_id:
        raise ValueError("execution result does not match its intent")
    if result.plan is not None and (
        result.plan.intent_id != intent.intent_id or result.plan.intent_name is not intent.name
    ):
        raise ValueError("execution plan does not match its intent")
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
    if result.status is not LifecycleStatus.EXECUTING:
        # Capture evidence is a prerequisite for terminal lifecycle state. Retain it
        # before applying the control projection so malformed/conflicting evidence
        # cannot produce a false completed intent.
        events.extend(session.record_capture_bundle(result))
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
    refusal_detail: str | None = None
    cancelled_by: str | None = None
    finished: bool = False
    prepared: PreparedExecution | None = None
    watchdog_action: str | None = None

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


@dataclass(frozen=True, slots=True, eq=False)
class _ResumeToken:
    intent_id: str
    owner: _AwaitingExecution
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

    def authorize_navigation(self, plan: Plan, command: Command, snapshot: FleetSnapshot) -> None:
        self._job.check()
        self._inner.authorize_navigation(plan, command, snapshot)

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
        self.planner = DeterministicPlanner(
            composition.config.planning,
            self.capability_profile,
            navigation=composition.navigation_runtime,
        )
        self.arbiter = SafetyArbiter(composition.config.safety)
        deployment = composition.config.navigation_deployment
        projector = composition.config.control_localization_projector
        self.navigation_control = (
            None
            if deployment is None or projector is None
            else NavigationControl(
                NavigationControlConfig(
                    deployment.runtime,
                    projector,
                    deployment.configuration_id,
                    composition.node_keys,
                )
            )
        )
        self._lock = threading.Lock()
        self._operator_last_seen_ms: int | None = None
        self._presence_expiry_seen_ms: int | None = None
        self._presence_action_serial = 0
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

    def submit(self, intent: IntentV1, state: dict[str, object]) -> None:
        """``IntentSink``: record operator activity and route the intent without blocking."""
        if intent.name in {IntentName.HOLD, IntentName.ESTOP, IntentName.SELECT}:
            if search := self._composition.search_runtime:
                search.revoke_unstarted_previews(intent.session)
        if intent.name is IntentName.SEARCH:
            search = self._composition.search_runtime
            now_ms = self.snapshot(state).now_ms
            if search is None or not search.accepts_intent(intent, now_ms):
                refusal_detail = "search intent has no matching frozen preview"
            else:
                refusal_detail = None
        else:
            refusal_detail = None
        received_at = state.get("t")
        if isinstance(received_at, int) and not isinstance(received_at, bool):
            with self._lock:
                previous = self._operator_last_seen_ms
                self._operator_last_seen_ms = (
                    received_at if previous is None else max(previous, received_at)
                )
                self._presence_expiry_seen_ms = None
        runtime = self._composition.runtime_if_bound()
        job = _Job(
            intent,
            None if runtime is None else runtime.sessions.get(self.session_id),
            refusal_detail=refusal_detail,
        )
        if intent.name is IntentName.NAVIGATE:
            now_ms = state.get("t")
            with self._lock:
                preview = self._navigation_previews.pop(intent.intent_id, None)
            if (
                not isinstance(now_ms, int)
                or isinstance(now_ms, bool)
                or preview is None
                or preview[0] < now_ms
                or not self._same_navigation_preview(preview[1].intent, intent)
            ):
                job.refusal_detail = "navigation requires a current matching server preview"
            else:
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

    def preview_navigation(self, intent: IntentV1, state: Mapping[str, object]) -> Plan | Refusal:
        if intent.name is not IntentName.NAVIGATE or self._composition.navigation_runtime is None:
            return Refusal(
                intent.intent_id,
                0,
                None,
                None,
                RefusalReason.UNSUPPORTED,
                "navigation deployment is unavailable",
            )
        with self._lock:
            received_at = state.get("t")
            if type(received_at) is int:
                previous = self._operator_last_seen_ms
                self._operator_last_seen_ms = (
                    received_at if previous is None else max(previous, received_at)
                )
                self._presence_expiry_seen_ms = None
            if intent.intent_id in self._navigation_previews:
                return Refusal(
                    intent.intent_id,
                    0,
                    None,
                    None,
                    RefusalReason.INVALID_PLAN,
                    "preview ID is already in use; create a new request",
                )
        snapshot = self.snapshot(state)
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
                "navigation runtime did not produce a route",
            )
        refusal = self.arbiter.check_plan(planned, snapshot)
        if refusal is not None:
            return refusal
        prepared = PreparedExecution(intent, planned, snapshot)
        expires_at_ms = snapshot.now_ms + 15_000
        with self._lock:
            self._navigation_previews = {
                key: value
                for key, value in self._navigation_previews.items()
                if value[0] >= snapshot.now_ms
            }
            if len(self._navigation_previews) >= 32:
                self._navigation_previews.pop(next(iter(self._navigation_previews)))
            self._navigation_previews[intent.intent_id] = (expires_at_ms, prepared)
        return planned

    @staticmethod
    def _same_navigation_preview(preview: IntentV1, confirmation: IntentV1) -> bool:
        return replace(preview, t=confirmation.t, confirm=confirmation.confirm) == confirmation

    def reconcile_membership(self, session: RelaySession) -> tuple[dict[str, object], ...]:
        if session.session_id == self.session_id:
            with self._lock:
                self._navigation_previews.clear()
            if search := self._composition.search_runtime:
                search.revoke_unstarted_previews(session.session_id)
        return ()

    def navigation_preview_expiry(self, intent_id: str) -> int | None:
        with self._lock:
            preview = self._navigation_previews.get(intent_id)
            return None if preview is None else preview[0]

    def preview_search(self, intent: IntentV1, state: Mapping[str, object]):
        runtime = self._composition.search_runtime
        if intent.name is not IntentName.SEARCH or runtime is None:
            return Refusal(
                intent.intent_id, 0, None, None, RefusalReason.UNSUPPORTED, "search is unavailable"
            )
        snapshot = self.snapshot(state)
        return runtime.prepare(intent, snapshot)

    def navigation_catalog(self) -> dict[str, object] | None:
        runtime = self._composition.navigation_runtime
        if runtime is None:
            return None
        from relay.navigation_metadata import navigation_metadata

        try:
            return navigation_metadata(runtime)
        except (OSError, ValueError):
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
        snapshot = relay_snapshot(
            state,
            operator_last_seen_ms=operator_last_seen_ms,
            estop_requested=estop_requested,
            capture_readiness=capture_readiness,
        )
        runtime = self._composition.runtime_if_bound()
        session = None if runtime is None else runtime.sessions.get(self.session_id)
        if session is None or self.navigation_control is None:
            return snapshot
        return self.navigation_control.approved_snapshot(snapshot, session)

    def periodic_events(self, state: dict[str, object]) -> list[dict[str, object]]:
        return self._presence_events(state) + self._navigation_events(state)

    def _navigation_events(self, state: Mapping[str, object]) -> list[dict[str, object]]:
        if self.navigation_control is None:
            return []
        now_ms = state.get("t")
        runtime = self._composition.runtime_if_bound()
        session = None if runtime is None else runtime.sessions.get(self.session_id)
        if type(now_ms) is not int or session is None:
            return []
        return [
            session.record_navigation_packet(packet)
            for packet in self.navigation_control.periodic_poses(session, now_ms)
        ]

    def record_operator_presence(self, now_ms: int) -> None:
        with self._lock:
            previous = self._operator_last_seen_ms
            self._operator_last_seen_ms = now_ms if previous is None else max(previous, now_ms)
            self._presence_expiry_seen_ms = None

    def _presence_events(self, _relay_event: Mapping[str, object]) -> list[dict[str, object]]:
        """Dispatch one configured stop when the last accepted operator action expires."""
        runtime = self._composition.runtime_if_bound()
        if runtime is None:
            return []
        session = runtime.sessions.get(self.session_id)
        if session is None:
            return []
        state = session.current_state()
        now = state.get("t")
        if not isinstance(now, int) or isinstance(now, bool):
            return []
        with self._lock:
            last_seen = self._operator_last_seen_ms
            expired = (
                last_seen is not None
                and now - last_seen >= self.arbiter.config.operator_timeout_ms
                and self._presence_expiry_seen_ms != last_seen
            )
            if not expired:
                return []
            self._presence_action_serial += 1
            action = self._composition.config.presence_watchdog.action
            intent = IntentV1(
                v=1,
                t=now,
                type="intent",
                intent_id=(f"safety:operator-presence:{last_seen}:{self._presence_action_serial}"),
                retry_of=None,
                source="safety",
                session=self.session_id,
                name=IntentName(action),
                args={},
                selection=(),
                mode=Mode.INDOOR,
                confirm=True,
            )
        action_event = session.record_safety_action(
            reason="operator_presence_expired",
            action=action,
            operator_last_seen_ms=last_seen,
        )
        accepted = session.admit_safety_stop(intent)
        job = _Job(intent, session, watchdog_action=action)
        lane = self._route(job)
        with lane.ready:
            lane.pending.append(job)
            lane.ready.notify()
        with self._lock:
            if self._operator_last_seen_ms == last_seen:
                self._presence_expiry_seen_ms = last_seen
        return [action_event, accepted, *job.publications]

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
        return self._normal

    def _cancel(
        self,
        stop: _Job,
        reason: str,
        *,
        running_on: tuple[_Lane, ...],
        running_names: frozenset[IntentName],
    ) -> None:
        """Invalidate the plans a stop preempts: running ones by name, queued motion ones.

        The invalidation is recorded first, under the session lock the sink already
        holds, so it is atomic against ``issue_command``; the flag is set afterwards so
        the plan exits promptly. The stop publishes the records when it starts. A plan
        that reached a terminal state first keeps that result.
        """
        with self._lock:
            victims = [
                lane.running
                for lane in running_on
                if lane.running is not None
                and not lane.running.finished
                and lane.running.cancelled_by is None
                and lane.running.intent.name in running_names
            ]
            victims.extend(
                job
                for job in self._normal.pending
                if job.cancelled_by is None and job.intent.name in HOLD_PREEMPTS
            )
            victims.extend(
                owner.job
                for owner in self._awaiting.values()
                if owner.job.cancelled_by is None and owner.job.intent.name in running_names
            )
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
            if victim.intent.name is IntentName.SEARCH:
                if search := self._composition.search_runtime:
                    search.cancel(victim.intent.intent_id, reason)
                if factory := self._composition.detection_factory:
                    factory.finish_mission(victim.intent.intent_id)
            stop.publications.append(event)

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
        if job.refusal_detail is not None:
            roster_version = session.registry.roster_version
            status = (
                LifecycleStatus.FAILED
                if intent.name is IntentName.SEARCH
                else LifecycleStatus.REFUSED
            )
            result = ExecutionResult(
                intent_id=intent.intent_id,
                roster_version=roster_version,
                status=status,
                refusal=Refusal(
                    intent_id=intent.intent_id,
                    roster_version=roster_version,
                    drone_id=None,
                    connection_epoch=None,
                    reason=RefusalReason.INVALID_PLAN,
                    detail=job.refusal_detail,
                    status=status,
                ),
            )
            with self._lock:
                job.finished = True
            self._report(runtime, session, job, result)
            return

        def current() -> FleetSnapshot:
            job.check()
            return self.snapshot(
                session.current_state(), capture_readiness=session.capture_readiness
            )

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
                navigation=self._composition.navigation_runtime,
                navigation_control=self.navigation_control,
            )
            controller = AutonomyController(
                planner=self.planner, arbiter=self.arbiter, dispatcher=dispatcher
            )
            if job.watchdog_action == "hold":
                plan = self.planner.emergency_hold_plan(
                    intent_id=intent.intent_id,
                    snapshot=snapshot,
                )
                result = dispatcher.dispatch(plan, snapshot, current_snapshot=current)
            elif intent.name is IntentName.SEARCH and self._composition.search_runtime is not None:
                search = self._composition.search_runtime
                if factory := self._composition.detection_factory:
                    if not factory.start_mission(intent.intent_id, session):
                        search.hold(intent.intent_id, "detection_worker_start_failed")
                        result = ExecutionResult(
                            intent_id=intent.intent_id,
                            roster_version=snapshot.roster_version,
                            status=LifecycleStatus.FAILED,
                            refusal=Refusal(
                                intent_id=intent.intent_id,
                                roster_version=snapshot.roster_version,
                                drone_id=None,
                                connection_epoch=None,
                                reason=RefusalReason.INVALID_PLAN,
                                detail="search detection worker failed to start",
                                status=LifecycleStatus.FAILED,
                            ),
                        )
                    else:
                        result: ExecutionResult | None = None
                        try:
                            result = search.execute(
                                intent.intent_id, dispatcher, snapshot, current_snapshot=current
                            )
                        finally:
                            if result is None or result.status is not LifecycleStatus.EXECUTING:
                                factory.finish_mission(intent.intent_id)
                else:
                    result = search.execute(
                        intent.intent_id, dispatcher, snapshot, current_snapshot=current
                    )
            else:
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
            return  # a stop already recorded this plan's terminal lifecycle
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
            return _ResumeToken(acknowledgement.intent_id, owner, terminal)

    def resume_io(self, token: _ResumeToken) -> ExecutionResult:
        """Resume dependent commands outside relay/session locks after a late result."""
        owner = token.owner

        def current() -> FleetSnapshot:
            return self.snapshot(
                owner.session.current_state(),
                capture_readiness=owner.session.capture_readiness,
            )

        try:
            assert owner.pending.plan is not None
            result = owner.dispatcher.resume_after_completion(
                owner.pending.plan,
                owner.pending,
                token.acknowledgement,
                owner.snapshot,
                current_snapshot=current,
                owner_still_valid=lambda: self._owns_resume(token),
            )
            if token.owner.job.intent.name is IntentName.SEARCH:
                search = self._composition.search_runtime
                if search is not None and result.status is not LifecycleStatus.EXECUTING:
                    return search.complete_execution(token.intent_id, result)
            return result
        except Exception as error:
            return _resume_failure(token, error)

    def commit_resume(self, token: _ResumeToken, result: ExecutionResult) -> RelayExecution | None:
        """Commit a still-owned late result and retain ownership if another command waits."""
        with self._lock:
            if (
                self._awaiting.get(token.intent_id) is not token.owner
                or token.owner.job.cancelled_by is not None
            ):
                return None
            owner = token.owner
            owner.pending = result
            if result.status is LifecycleStatus.EXECUTING:
                owner.snapshot = self.snapshot(
                    owner.session.current_state(),
                    capture_readiness=owner.session.capture_readiness,
                )
            else:
                self._awaiting.pop(token.intent_id, None)
                owner.job.finished = True
                if owner.job.intent.name is IntentName.SEARCH:
                    factory = self._composition.detection_factory
                    if factory is not None:
                        factory.finish_mission(token.intent_id)
        try:
            events = apply_result(owner.session, owner.job.intent, result)
        except ValueError:
            if owner.job.cancelled_by is None:
                raise
            return None
        if result.status in _TERMINAL and self.navigation_control is not None:
            self.navigation_control.invalidate(result.intent_id)
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
            )

    def _report(
        self, runtime: RelayRuntime, session: RelaySession, job: _Job, result: ExecutionResult
    ) -> None:
        def operation() -> list[dict[str, object]]:
            try:
                events = apply_result(session, job.intent, result)
            except ValueError:
                if job.cancelled_by is None:
                    raise
                _LOGGER.info("intent %s was cancelled as it completed", job.intent.intent_id)
                return []
            if result.status in _TERMINAL and self.navigation_control is not None:
                self.navigation_control.invalidate(result.intent_id)
            return events

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
        self,
        config: AutonomyConfig,
        capability_profile: CapabilityProfile = C1_CAPABILITY_PROFILE,
        *,
        node_keys: Mapping[int, bytes] | None = None,
        detection_stream_factory: StreamFactory | None = None,
        detection_detector_factory: DetectorFactory | None = None,
        detection_pose_provider_factory: PoseProviderFactory | None = None,
        detection_camera_provider_factory: CameraProviderFactory | None = None,
    ) -> None:
        self.config = config
        self.node_keys = dict(node_keys or {})
        if (
            config.navigation_deployment is not None
            and config.control_localization_projector is not None
        ):
            config.navigation_deployment.runtime.configure_control_localization(
                config.control_localization_projector.pins,
                max_fix_age_ms=config.control_localization_projector.max_fix_age_ms,
                max_position_uncertainty_p95_m=(
                    config.control_localization_projector.max_position_uncertainty_p95_m
                ),
            )
        self.capability_profile = config.effective_capability_profile(capability_profile)
        self._runtime_source: Callable[[], RelayRuntime | None] = _no_runtime
        self._sessions: dict[str, AutonomySession] = {}
        self._lock = threading.Lock()
        factory_args: dict[str, object] = {}
        if detection_stream_factory is not None:
            factory_args["stream_factory"] = detection_stream_factory
        if detection_detector_factory is not None:
            factory_args["detector_factory"] = detection_detector_factory
        if detection_pose_provider_factory is not None:
            factory_args["pose_provider_factory"] = detection_pose_provider_factory
        if detection_camera_provider_factory is not None:
            factory_args["camera_provider_factory"] = detection_camera_provider_factory
        self._detection_factory = (
            None
            if config.search_detection is None or config.search_runtime is None
            else SearchDetectionFactory(
                config.search_detection, config.search_runtime, **factory_args
            )
        )

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

    @property
    def navigation_runtime(self):
        deployment = self.config.navigation_deployment
        return None if deployment is None else deployment.runtime

    @property
    def search_runtime(self) -> SearchRuntime | None:
        return self.config.search_runtime

    @property
    def detection_factory(self) -> SearchDetectionFactory | None:
        return self._detection_factory

    def start(self) -> None:
        if self._detection_factory is not None:
            self._detection_factory.start()

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
        if self._detection_factory is not None:
            self._detection_factory.close()
        with self._lock:
            sessions = tuple(self._sessions.values())
        for session in sessions:
            session.close(timeout_s)
        runtime = self.runtime_if_bound()
        if runtime is None:
            return
        for relay_session in tuple(runtime.sessions.values()):
            try:
                write_session_report(
                    relay_session.audit_log.root,
                    relay_session.session_id,
                    relay_session.audit_log.replay(),
                )
            except Exception:
                _LOGGER.exception(
                    "could not write session report session=%s", relay_session.session_id
                )


def create_autonomy_app(
    settings: RelaySettings,
    config: AutonomyConfig,
    *,
    clock: Clock | None = None,
    event_ids: EventIdFactory | None = None,
    detection_stream_factory: StreamFactory | None = None,
    detection_detector_factory: DetectorFactory | None = None,
    detection_pose_provider_factory: PoseProviderFactory | None = None,
    detection_camera_provider_factory: CameraProviderFactory | None = None,
    transcript_service_factory: TranscriptServiceFactory | None = None,
) -> tuple[FastAPI, AutonomyComposition]:
    """Build the relay app with the planner and arbiter consuming every accepted intent.

    ``transcript_service_factory`` is ``create_app``'s hook for the voice endpoint;
    ``relay.main`` builds one that compiles transcripts against this composition's
    planning policy and capability profile.
    """
    if settings.adapter_backend is AdapterBackend.SIM and config.sim_camera is None:
        raise SettingsError("SWEEP_SIM_CAMERA_JSON is required when SWEEP_ADAPTER_BACKEND is sim")
    composition = AutonomyComposition(
        config,
        settings.capability_profile,
        node_keys=settings.adapter_keys,
        detection_stream_factory=detection_stream_factory,
        detection_detector_factory=detection_detector_factory,
        detection_pose_provider_factory=detection_pose_provider_factory,
        detection_camera_provider_factory=detection_camera_provider_factory,
    )
    control_localization_factory = (
        None
        if config.control_localization_projector is None
        else lambda _session_id: config.control_localization_projector
    )
    app = create_app(
        settings,
        clock=clock,
        event_ids=event_ids,
        intent_sink_factory=composition.intent_sink_factory,
        capability_profile=composition.capability_profile,
        leave_authorizer_factory=composition.leave_authorizer_factory,
        control_localization_factory=control_localization_factory,
        startup_callback=composition.start,
        shutdown_callback=composition.close,
        transcript_service_factory=transcript_service_factory,
    )

    @app.get("/session/{session_id}/navigation/catalog")
    async def navigation_catalog(
        session_id: str,
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> dict[str, object]:
        runtime: RelayRuntime = request.app.state.relay_runtime
        token = (
            authorization.removeprefix("Bearer ").encode()
            if authorization and authorization.startswith("Bearer ")
            else None
        )
        expected = runtime.credential_resolver.resolve("console", None)
        if token is None or expected is None or not hmac.compare_digest(token, expected):
            raise HTTPException(status_code=401, detail="console authentication is required")
        await runtime.activate_session(session_id)
        catalog = composition.session(session_id).navigation_catalog()
        if catalog is None:
            raise HTTPException(status_code=409, detail="navigation catalog is unavailable")
        return {
            "v": 1,
            "t": runtime.clock(),
            "type": "navigation_catalog",
            "session": session_id,
            "catalog": catalog,
        }

    @app.post("/session/{session_id}/navigation/preview")
    async def navigation_preview(
        session_id: str,
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> dict[str, object]:
        runtime: RelayRuntime = request.app.state.relay_runtime
        token = (
            authorization.removeprefix("Bearer ").encode()
            if authorization and authorization.startswith("Bearer ")
            else None
        )
        expected = runtime.credential_resolver.resolve("console", None)
        if token is None or expected is None or not hmac.compare_digest(token, expected):
            raise HTTPException(status_code=401, detail="console authentication is required")
        try:
            payload = await request.json()
            candidate = payload["intent"] if isinstance(payload, Mapping) else None
        except (ValueError, KeyError):
            candidate = None
        validated = validate_intent(candidate, capability_profile=composition.capability_profile)
        if (
            not isinstance(validated, AcceptedIntent)
            or validated.intent.source not in {"console", "language"}
            or validated.intent.session != session_id
        ):
            raise HTTPException(
                status_code=422,
                detail="a configured navigation intent for this session is required",
            )
        session = await runtime.activate_session(session_id)
        owner = composition.session(session_id)
        result = owner.preview_navigation(validated.intent, session.current_state())
        if isinstance(result, Refusal):
            raise HTTPException(status_code=409, detail=result.detail)
        expires_at_ms = owner.navigation_preview_expiry(validated.intent.intent_id)
        catalog = owner.navigation_catalog()
        if expires_at_ms is None or catalog is None:
            raise HTTPException(status_code=409, detail="navigation preview is unavailable")
        return {
            "v": 1,
            "t": runtime.clock(),
            "type": "navigation_preview",
            "session": session_id,
            "intent_id": validated.intent.intent_id,
            "expires_at_ms": expires_at_ms,
            "plan": result.to_dict(),
            "rooms": catalog["zones"],
        }

    @app.post("/session/{session_id}/search/preview")
    async def search_preview(
        session_id: str, request: Request, authorization: str | None = Header(default=None)
    ) -> dict[str, object]:
        runtime: RelayRuntime = request.app.state.relay_runtime
        token = authorization.removeprefix("Bearer ").encode() if authorization else None
        expected = runtime.credential_resolver.resolve("console", None)
        if token is None or expected is None or not hmac.compare_digest(token, expected):
            raise HTTPException(status_code=401, detail="console authentication is required")
        payload = await request.json()
        candidate = payload.get("intent") if isinstance(payload, Mapping) else None
        validated = validate_intent(candidate, capability_profile=composition.capability_profile)
        if (
            not isinstance(validated, AcceptedIntent)
            or validated.intent.name is not IntentName.SEARCH
            or validated.intent.session != session_id
        ):
            raise HTTPException(
                status_code=422, detail="a configured console search intent is required"
            )
        session = await runtime.activate_session(session_id)
        result = composition.session(session_id).preview_search(
            validated.intent, session.current_state()
        )
        if isinstance(result, Refusal):
            raise HTTPException(status_code=409, detail=result.detail)
        return {
            "v": 1,
            "t": runtime.clock(),
            "type": "search_preview",
            "session": session_id,
            "intent_id": validated.intent.intent_id,
            "preview": result.search.payload(),
            "plan": result.plan.to_dict(),
            "expires_at_ms": composition.search_runtime.preview_expires_at_ms(
                validated.intent.intent_id
            ),
        }

    @app.get("/session/{session_id}/search/catalog")
    async def search_catalog(
        session_id: str,
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> dict[str, object]:
        runtime: RelayRuntime = request.app.state.relay_runtime
        token = authorization.removeprefix("Bearer ").encode() if authorization else None
        expected = runtime.credential_resolver.resolve("console", None)
        if token is None or expected is None or not hmac.compare_digest(token, expected):
            raise HTTPException(status_code=401, detail="console authentication is required")
        search = composition.search_runtime
        if search is None:
            raise HTTPException(status_code=404, detail="search is unavailable")
        return {
            "session": session_id,
            "target_classes": list(DEFAULT_TARGET_LABELS),
            "zones": list(search.config.areas),
        }

    @app.get("/session/{session_id}/search/{intent_id}")
    async def search_status(
        session_id: str,
        intent_id: str,
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> dict[str, object]:
        runtime: RelayRuntime = request.app.state.relay_runtime
        token = authorization.removeprefix("Bearer ").encode() if authorization else None
        expected = runtime.credential_resolver.resolve("console", None)
        if token is None or expected is None or not hmac.compare_digest(token, expected):
            raise HTTPException(status_code=401, detail="console authentication is required")
        search = composition.search_runtime
        if search is None or not search.belongs_to_session(intent_id, session_id):
            raise HTTPException(status_code=404, detail="search is unavailable")
        try:
            status = search.status_payload(intent_id)
        except ValueError:
            raise HTTPException(status_code=404, detail="search mission is unknown") from None
        if factory := composition.detection_factory:
            status["detection_workers"] = factory.status(intent_id)
        status["session"] = session_id
        return status

    @app.post("/session/{session_id}/search/{intent_id}/findings/{sighting_id}/ack")
    async def acknowledge_search_finding(
        session_id: str,
        intent_id: str,
        sighting_id: str,
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> dict[str, object]:
        runtime: RelayRuntime = request.app.state.relay_runtime
        token = authorization.removeprefix("Bearer ").encode() if authorization else None
        expected = runtime.credential_resolver.resolve("console", None)
        if token is None or expected is None or not hmac.compare_digest(token, expected):
            raise HTTPException(status_code=401, detail="console authentication is required")
        search = composition.search_runtime
        if (
            search is None
            or not search.belongs_to_session(intent_id, session_id)
            or not search.acknowledge_finding(intent_id, sighting_id)
        ):
            raise HTTPException(status_code=404, detail="search finding is unknown")
        status = search.status_payload(intent_id)
        status["session"] = session_id
        return status

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


def _localization_projector_from_json(raw: str, name: str) -> ControlLocalizationProjector:
    def unique_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate localization field")
            result[key] = value
        return result

    try:
        value = json.loads(raw, object_pairs_hook=unique_fields)
    except ValueError:
        raise SettingsError(f"{name} must be valid JSON with unique fields") from None
    expected = {
        "relay_clock_id",
        "max_clock_error_ms",
        "max_fix_age_ms",
        "max_velocity_age_ms",
        "max_height_age_ms",
        "max_position_uncertainty_p95_m",
        "pins",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise SettingsError(f"{name} keys must be exactly {sorted(expected)}")
    pins_raw = value["pins"]
    if not isinstance(pins_raw, list):
        raise SettingsError(f"{name}.pins must be a JSON array")
    try:
        pins = {
            item["drone_id"]: ControlLocalizationPins(
                drone_id=item["drone_id"],
                map_id=item["map_id"],
                geometry_id=item["geometry_id"],
                camera_calibration_id=item["camera_calibration_id"],
                body_extrinsics_id=item["body_extrinsics_id"],
                source_ids=item["source_ids"],
                clock_mapping=ClockMapping.from_mapping(item["clock_mapping"]),
            )
            for item in pins_raw
            if isinstance(item, Mapping)
            and set(item)
            == {
                "drone_id",
                "map_id",
                "geometry_id",
                "camera_calibration_id",
                "body_extrinsics_id",
                "source_ids",
                "clock_mapping",
            }
        }
        if len(pins) != len(pins_raw):
            raise ValueError("pins must contain exact, unique pin objects")
        return ControlLocalizationProjector(
            pins,
            relay_clock_id=value["relay_clock_id"],
            max_clock_error_ms=value["max_clock_error_ms"],
            max_fix_age_ms=value["max_fix_age_ms"],
            max_velocity_age_ms=value["max_velocity_age_ms"],
            max_height_age_ms=value["max_height_age_ms"],
            max_position_uncertainty_p95_m=value["max_position_uncertainty_p95_m"],
        )
    except (KeyError, TypeError, ValueError) as error:
        raise SettingsError(f"{name}: {error}") from None
