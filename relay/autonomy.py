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
import hashlib
import hmac
import json
import logging
import os
import stat
import threading
import uuid
from collections import deque
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import asdict, dataclass, field, fields, is_dataclass, replace
from enum import Enum
from pathlib import Path
from typing import get_origin, get_type_hints

from fastapi import FastAPI, Header, HTTPException, Request

from adapters.dispatch import AdapterDispatcher
from adapters.dji_mini3.remote import CommandRequest, NodeLink
from adapters.ohmni.dispatcher import GroundCommandDispatcher
from adapters.sim.camera import SimCameraConfig
from arbiter.safety import SafetyArbiter, SafetyConfig
from planner.controller import AutonomyController, RelayExecution
from planner.ground_navigation import GroundNavigationDeployment
from planner.models import (
    CommandAcknowledgement,
    ExecutionResult,
    FleetSnapshot,
    FlightState,
    LandingRecoveryEvidence,
    LifecycleStatus,
    LocalHeightEvidence,
    Plan,
    PreparedExecution,
    Refusal,
    RefusalReason,
    RelayAircraftSafetyEnrichment,
    RelaySnapshotEnrichment,
)
from planner.navigation import DronePose, Pose
from planner.navigation_authorization import content_digest
from planner.navigation_deployment import NavigationDeployment, load_navigation_deployment
from planner.navigation_runtime import navigation_capability_profile
from planner.planner import DeterministicPlanner, PlanningConfig
from planner.roster import authorize_graceful_removal
from relay.app import RelayRuntime, TranscriptServiceFactory, create_app
from relay.bridge import RelayNodeLink, build_dispatcher
from relay.capabilities import (
    C1_CAPABILITY_PROFILE,
    SURVEY_ADDITIONAL_INTENT_NAMES,
    CapabilityProfile,
    with_ground_capabilities,
)
from relay.contracts import AdapterAcknowledgement as WireAcknowledgement
from relay.contracts import CapabilitiesFrame, CaptureReadinessFrame, MediaFileRecord, NodeType
from relay.contracts import LifecycleStatus as WireLifecycleStatus
from relay.control_localization import (
    ClockMapping,
    ControlLocalizationPins,
    ControlLocalizationProjector,
)
from relay.ground_navigation_execution import GroundPlatformNavigation, PreparedGroundNavigation
from relay.intent_v1 import AcceptedIntent, IntentName, IntentV1, Mode, validate_intent
from relay.navigation_wire import NavigationTrackingError, NavigationWirePublisher
from relay.search_deployment import load_search_config
from relay.search_detection import SearchDetectionConfig, SearchDetectionFactory
from relay.search_detection_deployment import load_search_detection_config
from relay.search_runtime import SearchRuntime, SearchRuntimeConfig
from relay.session import Clock, EventIdFactory, IntentSink, LeaveAuthorizer, RelaySession
from relay.settings import AdapterBackend, RelaySettings, SettingsError
from relay.supervised_vertical import (
    SUPERVISED_VERTICAL_PROFILE,
    SupervisedVerticalArbiter,
    SupervisedVerticalConfig,
    SupervisedVerticalPlanner,
)

LIFECYCLE_SOURCE = "autonomy"
PREEMPTED_BY_ESTOP = "preempted_by_estop"
PREEMPTED_BY_HOLD = "preempted_by_hold"
HOLD_PREEMPTS = frozenset(
    {
        IntentName.TAKEOFF,
        IntentName.TRANSLATE,
        IntentName.ALTITUDE,
        IntentName.FORMATION_NEXT,
        IntentName.FORMATION_SET,
        IntentName.SPACING,
        IntentName.SWEEP,
        IntentName.COME_HOME,
        IntentName.NAVIGATE,
        IntentName.SEARCH,
        IntentName.CAPTURE_ROOM,
        IntentName.GROUND_VELOCITY,
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
MAX_CAPTURE_READINESS_AGE_MS = 5_000
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
    """One explicit world policy or the separate supervised vertical policy."""

    planning: PlanningConfig | None = None
    safety: SafetyConfig | None = None
    supervised_vertical: SupervisedVerticalConfig | None = None
    sim_camera: SimCameraConfig | None = None
    control_localization_projector: ControlLocalizationProjector | None = None
    navigation: NavigationDeployment | None = None
    ground_navigation: GroundNavigationDeployment | None = None
    search: SearchRuntimeConfig | None = None
    search_detection: SearchDetectionConfig | None = None

    def __post_init__(self) -> None:
        world = self.planning is not None or self.safety is not None
        if world == (self.supervised_vertical is not None):
            raise ValueError("configure either planning+safety or supervised_vertical")
        if world and (self.planning is None or self.safety is None):
            raise ValueError("world policy requires both planning and safety")
        if self.supervised_vertical is not None and (
            self.control_localization_projector is not None
            or self.navigation is not None
            or self.ground_navigation is not None
            or self.search is not None
            or self.search_detection is not None
        ):
            raise ValueError("supervised_vertical does not accept world localization or navigation")

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> AutonomyConfig:
        """Load one deployment policy and reject a mixed world/vertical configuration."""
        values = os.environ if environ is None else environ
        camera_raw = values.get("SWEEP_SIM_CAMERA_JSON", "")
        localization_raw = values.get("SWEEP_CONTROL_LOCALIZATION_JSON", "")
        navigation_path = values.get("SWEEP_NAVIGATION_CONFIG", "")
        ground_path = values.get("SWEEP_GROUND_NAVIGATION_CONFIG", "")
        ground_key_path = values.get("SWEEP_GROUND_NAVIGATION_KEY_FILE", "")
        if bool(ground_path) != bool(ground_key_path):
            raise SettingsError(
                "ground navigation requires both configuration and signing key file"
            )
        vertical_raw = values.get("SWEEP_SUPERVISED_VERTICAL_JSON", "")
        if vertical_raw:
            if any(
                (
                    values.get("SWEEP_PLANNING_JSON", ""),
                    values.get("SWEEP_SAFETY_JSON", ""),
                    localization_raw,
                    navigation_path,
                    ground_path,
                    values.get("SWEEP_SEARCH_CONFIG", ""),
                    values.get("SWEEP_SEARCH_DETECTION_CONFIG", ""),
                )
            ):
                raise SettingsError(
                    "SWEEP_SUPERVISED_VERTICAL_JSON cannot be combined with "
                    "world planning, safety, localization, navigation, or search"
                )
            return cls(
                supervised_vertical=_config_from_json(
                    SupervisedVerticalConfig, vertical_raw, "SWEEP_SUPERVISED_VERTICAL_JSON"
                ),
                sim_camera=(
                    None
                    if not camera_raw
                    else _config_from_json(SimCameraConfig, camera_raw, "SWEEP_SIM_CAMERA_JSON")
                ),
            )
        navigation = None if not navigation_path else load_navigation_deployment(navigation_path)
        search = load_search_config(values, navigation)
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
            navigation=navigation,
            ground_navigation=(
                None if not ground_path else _load_ground_navigation(ground_path, ground_key_path)
            ),
            search=search,
            search_detection=load_search_detection_config(values, search),
        )


def _load_ground_navigation(path: str, key_path: str) -> GroundNavigationDeployment:
    key_file = Path(key_path)
    info = key_file.stat(follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
        raise SettingsError("ground navigation signing key must be a regular mode-0600 file")
    with key_file.open("rb") as stream:
        key = stream.read(4097)
    return GroundNavigationDeployment.load(Path(path), key)


def relay_snapshot(
    state: Mapping[str, object],
    *,
    operator_last_seen_ms: int | None,
    estop_requested: bool = False,
    capture_readiness: ReadinessSource | None = None,
    landing_recovery: Callable[[int, int], LandingRecoveryEvidence | None] | None = None,
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
    ``FlightState``, are excluded from commands and mark the fleet observation
    incomplete. That fact prevents the session-wide arm authorization from being
    withdrawn until every registered aircraft is proven physically disarmed.
    """
    drones_raw = state.get("drones")
    if not isinstance(drones_raw, list):
        raise ValueError("relay state requires a drones list")
    drones: list[Mapping[str, object]] = []
    ground_ids: list[int] = []
    enrichment: dict[int, RelayAircraftSafetyEnrichment] = {}
    fleet_observation_complete = True
    for drone in drones_raw:
        if not isinstance(drone, Mapping):
            raise ValueError("relay drone entries must be mappings")
        node_type = drone.get("node_type", "aircraft")
        if node_type not in ("aircraft", "ground"):
            raise ValueError("relay node type is unknown")
        if node_type == "ground":
            drone_id = drone.get("drone_id")
            if not isinstance(drone_id, int) or isinstance(drone_id, bool) or drone_id <= 0:
                raise ValueError("relay drone entries require a positive drone_id")
            ground_ids.append(drone_id)
            continue
        drone_id = drone.get("drone_id")
        if not isinstance(drone_id, int) or isinstance(drone_id, bool) or drone_id <= 0:
            raise ValueError("relay drone entries require a positive drone_id")
        telemetry = drone.get("telemetry")
        if not isinstance(telemetry, Mapping) or telemetry.get("state") not in _FLIGHT_STATES:
            fleet_observation_complete = False
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
                and isinstance(state.get("t"), int)
                and -1_000 <= state["t"] - readiness.t <= MAX_CAPTURE_READINESS_AGE_MS
                and readiness.camera_ok
                and readiness.storage_ok
            ),
            active_task_id=None,
            position_loss_since_ms=None,
            local_height=_local_height_evidence(drone.get("node_status")),
            readiness_reasons=_readiness_reasons(drone.get("readiness_reasons")),
            landing_recovery=(
                None
                if landing_recovery is None
                else landing_recovery(drone_id, drone.get("connection_epoch"))
            ),
        )
        drones.append(drone)
    snapshot = FleetSnapshot.from_relay_state(
        {**state, "drones": drones, "ground_ids": tuple(ground_ids)},
        enrichment=RelaySnapshotEnrichment(
            operator_present=operator_last_seen_ms is not None,
            operator_last_seen_ms=0 if operator_last_seen_ms is None else operator_last_seen_ms,
            aircraft=enrichment,
            fleet_observation_complete=fleet_observation_complete,
        ),
    )
    if estop_requested and not snapshot.estop_active:
        snapshot = replace(snapshot, estop_active=True)
    return snapshot


def _readiness_reasons(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(reason, str) or not reason for reason in value
    ):
        raise ValueError("relay readiness reasons must be non-empty strings")
    return tuple(value)


def _local_height_evidence(node_status: object) -> LocalHeightEvidence | None:
    if not isinstance(node_status, Mapping):
        return None
    raw = node_status.get("local_height")
    if not isinstance(raw, Mapping):
        return None
    z_m = raw.get("z_m")
    source = raw.get("source")
    age_ms = raw.get("age_ms")
    reported_at_ms = raw.get("reported_at_ms")
    if (
        isinstance(z_m, bool)
        or not isinstance(z_m, int | float)
        or isinstance(source, bool)
        or not isinstance(source, str)
        or not isinstance(age_ms, int)
        or isinstance(age_ms, bool)
        or not isinstance(reported_at_ms, int)
        or isinstance(reported_at_ms, bool)
        or age_ms < 0
        or reported_at_ms < age_ms
    ):
        return None
    try:
        return LocalHeightEvidence(
            z_m=float(z_m), observed_at_ms=reported_at_ms - age_ms, source=source
        )
    except ValueError:
        return None


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
        if plan.formation_update is not None:
            projection["formation"] = plan.formation_update
        if plan.spacing_update is not None:
            projection["spacing"] = plan.spacing_update
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

    Selection, arm, formation, and spacing updates apply only while the plan's
    roster is still the session's roster; otherwise they are dropped and the result
    becomes ``invalidated`` with ``stale_roster``. The network stop latch is never
    dropped.
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
        and any(field in projection for field in ("selection", "armed", "formation", "spacing"))
    ):
        for field in ("selection", "armed", "formation", "spacing"):
            projection.pop(field, None)
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
    if result.status is not LifecycleStatus.EXECUTING and (
        intent.name is IntentName.CAPTURE_ROOM or result.capture_bundle is not None
    ):
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
    cancelled_by: str | None = None
    finished: bool = False

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
        navigation = composition.config.navigation
        navigation_runtime = (
            None
            if navigation is None
            else navigation.for_session(
                session_id,
                lambda drone_id: composition.runtime.sessions[session_id].control_pose(drone_id),
                composition.config.control_localization_projector,
            )
        )
        self.navigation_runtime = navigation_runtime
        self.search_runtime = (
            None
            if composition.config.search is None or navigation_runtime is None
            else SearchRuntime(composition.config.search, navigation_runtime)
        )
        self.search_detection = (
            None
            if composition.config.search_detection is None or self.search_runtime is None
            else SearchDetectionFactory(
                composition.config.search_detection,
                self.search_runtime,
                **composition.detection_factory_args,
            )
        )
        if self.search_detection is not None:
            self.search_detection.start()
        self.ground_navigation = (
            None
            if composition.config.ground_navigation is None
            else GroundPlatformNavigation(
                composition.runtime,
                session_id,
                composition.config.ground_navigation,
            )
        )
        self._platform_ground_dispatch: dict[str, PreparedGroundNavigation] = {}
        self._platform_navigation: dict[str, tuple[int, PreparedExecution]] = {}
        self._platform_navigation_reservations: dict[str, PreparedExecution] = {}
        self._platform_dispatch: dict[str, PreparedExecution] = {}
        self._platform_navigation_intents_by_preview: dict[str, str] = {}
        self._platform_navigation_aliases: dict[str, str] = {}
        self._platform_stop_generation = 0
        self.navigation_wire = (
            NavigationWirePublisher(
                navigation_runtime,
                navigation.wire_profiles,
                session=session_id,
                signing_key=composition.runtime.settings.adapter_keys.get,
                event_ids=composition.runtime.event_ids,
                clock=composition.runtime.clock,
            )
            if navigation_runtime is not None and navigation.approval.mode == "flight"
            else None
        )
        if composition.config.supervised_vertical is not None:
            self.planner = SupervisedVerticalPlanner(composition.config.supervised_vertical)
            self.arbiter = SupervisedVerticalArbiter(composition.config.supervised_vertical)
        else:
            assert composition.config.planning is not None
            assert composition.config.safety is not None
            self.planner = DeterministicPlanner(
                composition.config.planning,
                self.capability_profile,
                navigation_runtime=navigation_runtime,
            )
            self.arbiter = SafetyArbiter(composition.config.safety)
        self._lock = threading.Lock()
        self._operator_last_seen_ms: int | None = None
        self._stop_requested = False
        self._normal = _Lane("normal", self._lock)
        self._hold = _Lane("hold", self._lock)
        self._estop = _Lane("estop", self._lock)
        self._lanes = (self._normal, self._hold, self._estop)
        self._awaiting: dict[str, _AwaitingExecution] = {}
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
        runtime = self._composition.runtime_if_bound()
        session = None if runtime is None else runtime.sessions.get(self.session_id)
        policy = self._composition.config.supervised_vertical or self._composition.config.safety

        def recovery(drone_id: int, epoch: int) -> LandingRecoveryEvidence | None:
            if session is None or policy is None:
                return None
            current = session.registry.current_node_status_with_receipt(drone_id)
            if current is None:
                return None
            status, received_at = current
            now_ms = state.get("t")
            if (
                type(now_ms) is not int
                or not 0 <= now_ms - received_at <= policy.max_link_age_ms
                or status.connection_epoch != epoch
                or status.control_authority is not False
                or status.virtual_stick_enabled is not False
                or status.watchdog_state.value != "nominal"
                or status.authority_change_reason != "virtual_stick_dropped"
            ):
                return None
            return LandingRecoveryEvidence(received_at, "virtual_stick_dropped")

        return relay_snapshot(
            state,
            operator_last_seen_ms=operator_last_seen_ms,
            estop_requested=estop_requested,
            capture_readiness=capture_readiness,
            landing_recovery=recovery,
        )

    def preview_search(self, intent: IntentV1, state: Mapping[str, object]) -> object:
        if intent.name is not IntentName.SEARCH or self.search_runtime is None:
            return Refusal(
                intent.intent_id,
                0,
                None,
                None,
                RefusalReason.UNSUPPORTED,
                "search is unavailable",
            )
        snapshot = self.snapshot(state)
        return self.search_runtime.prepare(intent, snapshot)

    def preview_platform_navigation(self, preview: Mapping[str, object]) -> dict[str, object]:
        selected = preview.get("selected")
        if (
            isinstance(selected, list)
            and selected
            and all(
                isinstance(target, Mapping) and target.get("deviceClass") == "ground_vehicle"
                for target in selected
            )
        ):
            if self.ground_navigation is None:
                raise ValueError("qualified ground navigation is unavailable")
            return self.ground_navigation.preview(preview)
        runtime = self.navigation_runtime
        if runtime is None or runtime.approval.mode != "flight":
            raise ValueError("qualified aircraft navigation is unavailable")
        preview_id = preview.get("previewId")
        intent_id = preview.get("intentId")
        expires_at = preview.get("expiresAt")
        destination = preview.get("destination")
        selected = preview.get("selected")
        if (
            not isinstance(preview_id, str)
            or not isinstance(intent_id, str)
            or not isinstance(expires_at, int)
            or not isinstance(destination, Mapping)
            or not isinstance(destination.get("zoneId"), str)
            or not isinstance(selected, list)
            or not selected
            or any(
                not isinstance(target, Mapping)
                or target.get("deviceClass") != "aircraft"
                or type(target.get("id")) is not int
                for target in selected
            )
        ):
            raise ValueError("qualified navigation requires selected aircraft")
        session = self._composition.runtime.sessions.get(self.session_id)
        if session is None:
            raise ValueError("relay session is unavailable")
        now = self._composition.runtime.clock()
        if expires_at <= now:
            raise ValueError("qualified navigation preview has expired")
        precision_return = runtime.config.precision_return(destination["zoneId"])
        if precision_return is not None and (
            len(selected) != 1
            or selected[0].get("id") != precision_return.drone_id
            or selected[0].get("deviceClass") != "aircraft"
            or selected[0].get("epoch") != precision_return.connection_epoch
        ):
            raise ValueError("precision return requires its marked aircraft identity")
        trusted_start = preview.get("trustedStart")
        trusted_pose = None
        if trusted_start is not None:
            if not isinstance(trusted_start, Mapping):
                raise ValueError("trusted navigation start is invalid")
            target = trusted_start.get("target")
            position = trusted_start.get("position")
            if (
                not isinstance(target, Mapping)
                or target not in selected
                or not isinstance(position, Mapping)
                or set(position) != {"xM", "yM", "zM", "floorId", "frame"}
                or position.get("frame") != "world"
                or type(position.get("xM")) not in {int, float}
                or type(position.get("yM")) not in {int, float}
                or type(position.get("zM")) not in {int, float}
                or not isinstance(position.get("floorId"), str)
            ):
                raise ValueError("trusted navigation start is invalid")
            try:
                trusted_pose = DronePose(
                    target["id"],
                    target["epoch"],
                    Pose(position["xM"], position["yM"], position["zM"], position["floorId"]),
                )
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError("trusted navigation start is invalid") from error

        intent = IntentV1(
            v=1,
            t=now,
            type="intent",
            intent_id=f"platform:{preview_id}",
            retry_of=None,
            source="platform",
            session=self.session_id,
            name=IntentName.NAVIGATE,
            args={"zone_id": destination["zoneId"]},
            selection=tuple(target["id"] for target in selected),
            mode=Mode.INDOOR,
            confirm=True,
        )
        snapshot = self.snapshot(
            session.current_state(), capture_readiness=session.capture_readiness
        )
        refusal = self.arbiter.check_intent(intent, snapshot)
        if refusal is not None:
            raise ValueError(refusal.detail)
        planned = (
            runtime.prepare(intent, snapshot)
            if trusted_pose is None
            else runtime.prepare_from_trusted_start(intent, snapshot, trusted_pose)
        )
        if isinstance(planned, Refusal):
            raise ValueError(planned.detail)
        refusal = self.arbiter.check_plan(planned, snapshot)
        if refusal is not None:
            raise ValueError(refusal.detail)
        navigation = planned.navigation
        if navigation is None:
            raise ValueError("qualified navigation did not produce a route")
        map_ref = preview.get("map")
        authoring_pin = runtime.config.authoring_map_pin
        reviewed_pin = authoring_pin or navigation.route.map_pin
        if not isinstance(map_ref, Mapping) or map_ref.get("mapPin") != {
            "version": reviewed_pin.version,
            "contentSha256": reviewed_pin.content_sha256,
        }:
            raise ValueError("platform map revision differs from the approved navigation artifact")
        targets = {target["id"]: target for target in selected}
        routes = []
        for route in navigation.route.routes:
            target = targets.get(route.drone.drone_id)
            if target is None or target.get("epoch") != route.drone.connection_epoch:
                raise ValueError("qualified navigation route target differs from the preview")

            def point(pose):
                return {
                    "xM": pose.x_m,
                    "yM": pose.y_m,
                    "zM": pose.z_m,
                    "floorId": pose.floor_id,
                    "frame": "world",
                }

            points = [point(pose) for pose in route.waypoints]
            routes.append(
                {
                    "target": target,
                    "waypoints": points,
                    "arrivalSlot": {
                        "slotId": route.arrival_slot.slot_id,
                        "zoneId": route.arrival_slot.zone_id,
                        "position": points[-1],
                    },
                    "holdBehavior": "hover",
                }
            )
        plan_hash = content_digest(planned.to_dict())
        execution = {
            "planHash": plan_hash,
            "mapPin": {
                "version": navigation.route.map_pin.version,
                "contentSha256": navigation.route.map_pin.content_sha256,
            },
            "geometryPin": {
                "version": navigation.route.geometry_pin.version,
                "contentSha256": navigation.route.geometry_pin.content_sha256,
            },
            "navigationPin": {
                "version": navigation.route.navigation_pin.version,
                "contentSha256": navigation.route.navigation_pin.content_sha256,
            },
            "approvalId": navigation.approval_id,
            "configurationSha256": navigation.configuration_sha256,
            "permissionZoneIds": sorted(navigation.route.permission.permitted_zone_ids),
        }
        if authoring_pin is not None:
            execution["authoringMapPin"] = {
                "version": authoring_pin.version,
                "contentSha256": authoring_pin.content_sha256,
            }
        with self._lock:
            self._prune_platform_navigation(now)
            self._platform_navigation[preview_id] = (
                expires_at,
                PreparedExecution(intent, planned, snapshot),
            )
            self._platform_navigation_intents_by_preview[preview_id] = intent_id
        return {
            "routes": routes,
            "outcomes": [
                {
                    "target": target,
                    "status": "planned",
                    "code": "route_qualified",
                    "detail": "A signed flight deployment qualified this route.",
                }
                for target in selected
            ],
            "execution": execution,
        }

    def confirm_platform_navigation(self, preview: Mapping[str, object]) -> dict[str, object]:
        selected = preview.get("selected")
        if (
            isinstance(selected, list)
            and selected
            and all(
                isinstance(target, Mapping) and target.get("deviceClass") == "ground_vehicle"
                for target in selected
            )
        ):
            if self.ground_navigation is None:
                raise ValueError("qualified ground navigation is unavailable")
            prepared_ground = self.ground_navigation.take(preview)
            runtime = self._composition.runtime
            session = runtime.sessions.get(self.session_id)
            if session is None:
                raise ValueError("relay session is unavailable")
            with self._lock:
                self._platform_ground_dispatch[prepared_ground.intent.intent_id] = prepared_ground
            try:
                self._publish(
                    runtime, lambda: session.admit_platform_navigation(prepared_ground.intent)
                )
                self._publish(
                    runtime,
                    lambda: session.execute_pending_intent(
                        prepared_ground.intent.intent_id, defer_resume=True
                    ),
                )
            except Exception:
                with self._lock:
                    self._platform_ground_dispatch.pop(prepared_ground.intent.intent_id, None)
                raise
            return {
                "status": "accepted",
                "code": "navigation_accepted",
                "detail": "The frozen qualified ground routes were accepted for scheduling.",
            }
        preview_id = preview.get("previewId")
        execution = preview.get("execution")
        if not isinstance(preview_id, str) or not isinstance(execution, Mapping):
            raise ValueError("retained navigation preview is invalid")
        runtime = self._composition.runtime
        with self._lock:
            self._prune_platform_navigation(runtime.clock())
            retained = self._platform_navigation.pop(preview_id, None)
            self._platform_navigation_intents_by_preview.pop(preview_id, None)
            prepared = retained[1] if retained is not None else None
            if prepared is None or execution.get("planHash") != content_digest(
                prepared.plan.to_dict()
            ):
                raise ValueError("retained navigation plan is unavailable")
            admitted = replace(prepared.intent, t=runtime.clock())
            prepared = PreparedExecution(admitted, prepared.plan, prepared.snapshot)
            self._platform_dispatch[admitted.intent_id] = prepared
        session = runtime.sessions.get(self.session_id)
        if session is None:
            with self._lock:
                self._platform_dispatch.pop(prepared.intent.intent_id, None)
                self._platform_navigation_aliases.pop(prepared.intent.intent_id, None)
            raise ValueError("relay session is unavailable")
        try:
            self._publish(runtime, lambda: session.admit_platform_navigation(prepared.intent))
            self._publish(
                runtime,
                lambda: session.execute_pending_intent(
                    prepared.intent.intent_id, defer_resume=True
                ),
            )
        except Exception:
            with self._lock:
                self._platform_dispatch.pop(prepared.intent.intent_id, None)
            raise
        return {
            "status": "accepted",
            "code": "navigation_accepted",
            "detail": "The frozen qualified aircraft route was accepted for scheduling.",
        }

    def reserve_platform_navigation(self, preview: Mapping[str, object]) -> dict[str, object]:
        """Consume an aircraft review without dispatching its route yet."""
        preview_id = preview.get("previewId")
        execution = preview.get("execution")
        if not isinstance(preview_id, str) or not isinstance(execution, Mapping):
            raise ValueError("retained navigation preview is invalid")
        runtime = self._composition.runtime
        with self._lock:
            self._prune_platform_navigation(runtime.clock())
            retained = self._platform_navigation.pop(preview_id, None)
            prepared = retained[1] if retained is not None else None
            if prepared is None or execution.get("planHash") != content_digest(
                prepared.plan.to_dict()
            ):
                self._platform_navigation_intents_by_preview.pop(preview_id, None)
                raise ValueError("retained navigation plan is unavailable")
            self._platform_navigation_reservations[preview_id] = prepared
        return {
            "status": "accepted",
            "code": "navigation_reserved",
            "detail": "The frozen qualified aircraft route is reserved for its multiview slot.",
        }

    def discard_reserved_platform_navigation(self, preview_id: str) -> dict[str, object]:
        with self._lock:
            self._platform_navigation_reservations.pop(preview_id, None)
            self._platform_navigation_intents_by_preview.pop(preview_id, None)
        return {"status": "discarded"}

    def dispatch_reserved_platform_navigation(self, preview_id: str) -> dict[str, object]:
        runtime = self._composition.runtime
        with self._lock:
            prepared = self._platform_navigation_reservations.pop(preview_id, None)
            if prepared is None:
                raise ValueError("reserved navigation plan is unavailable")
            semantic_intent_id = self._platform_navigation_intents_by_preview.pop(preview_id, None)
            admitted = replace(prepared.intent, t=runtime.clock())
            prepared = PreparedExecution(admitted, prepared.plan, prepared.snapshot)
            generation = self._platform_stop_generation
            self._platform_dispatch[admitted.intent_id] = prepared
            if semantic_intent_id is not None:
                self._platform_navigation_aliases[admitted.intent_id] = semantic_intent_id
        session = runtime.sessions.get(self.session_id)
        if session is None:
            with self._lock:
                self._platform_dispatch.pop(prepared.intent.intent_id, None)
                self._platform_navigation_aliases.pop(prepared.intent.intent_id, None)
            raise ValueError("relay session is unavailable")
        try:

            def still_current() -> bool:
                with self._lock:
                    return self._platform_stop_generation == generation

            def admit() -> list[dict[str, object]]:
                if not still_current():
                    raise ValueError("hold cancelled the reserved navigation route")
                return session.admit_platform_navigation(prepared.intent)

            def execute() -> list[dict[str, object]]:
                if not still_current():
                    raise ValueError("hold cancelled the reserved navigation route")
                return session.execute_pending_intent(prepared.intent.intent_id, defer_resume=True)

            self._publish(runtime, admit)
            self._publish(runtime, execute)
        except Exception:
            with self._lock:
                self._platform_dispatch.pop(prepared.intent.intent_id, None)
                self._platform_navigation_aliases.pop(prepared.intent.intent_id, None)
            raise
        return {
            "status": "accepted",
            "code": "navigation_accepted",
            "detail": "The reserved qualified aircraft route was accepted for scheduling.",
        }

    def confirm_platform_capture(self, capture: Mapping[str, object]) -> dict[str, object]:
        capture_id = capture.get("captureId")
        room_id = capture.get("roomId")
        navigation_intent_id = capture.get("navigationIntentId")
        selected = capture.get("selected")
        if (
            not isinstance(capture_id, str)
            or not isinstance(room_id, str)
            or not isinstance(navigation_intent_id, str)
            or not isinstance(selected, list)
            or len(selected) != 1
            or not isinstance(selected[0], Mapping)
            or type(selected[0].get("id")) is not int
        ):
            raise ValueError("platform capture request is invalid")
        runtime = self._composition.runtime
        session = runtime.sessions.get(self.session_id)
        if session is None:
            raise ValueError("relay session is unavailable")
        intent = IntentV1(
            v=1,
            t=runtime.clock(),
            type="intent",
            intent_id=f"platform-capture:{navigation_intent_id}",
            retry_of=None,
            source="platform",
            session=self.session_id,
            name=IntentName.CAPTURE_ROOM,
            args={"room_id": room_id, "capture_id": capture_id, "pattern": "single_still"},
            selection=(selected[0]["id"],),
            mode=Mode.INDOOR,
            confirm=True,
        )
        self._publish(runtime, lambda: session.admit_platform_capture(intent))
        self._publish(
            runtime,
            lambda: session.execute_pending_intent(intent.intent_id, defer_resume=True),
        )
        return {"status": "accepted", "intentId": intent.intent_id}

    def _prune_platform_navigation(self, now: int) -> None:
        for preview_id, (expires_at, _) in tuple(self._platform_navigation.items()):
            if expires_at <= now:
                self._platform_navigation.pop(preview_id, None)
                self._platform_navigation_intents_by_preview.pop(preview_id, None)

    def multiview_execution_intent(self, intent_id: str, *, terminal: bool) -> str:
        with self._lock:
            alias = self._platform_navigation_aliases.get(intent_id)
            if terminal:
                self._platform_navigation_aliases.pop(intent_id, None)
        return intent_id if alias is None else alias

    def accepted_observation(self, observation: object) -> list[dict[str, object]]:
        from relay.observations import Observation

        if self.ground_navigation is not None and isinstance(observation, Observation):
            self.ground_navigation.identities.accept(observation)
        return []

    def close(self, timeout_s: float) -> None:
        if self.search_detection is not None:
            self.search_detection.close()
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

    def _route(self, job: _Job, *, report_multiview: bool = True) -> _Lane:
        """Choose the lane and cancel the plans this intent preempts before it queues."""
        name = job.intent.name
        if name in {IntentName.HOLD, IntentName.LAND_ALL, IntentName.ESTOP}:
            with self._lock:
                self._platform_stop_generation += 1
                for preview_id in self._platform_navigation_reservations:
                    self._platform_navigation_intents_by_preview.pop(preview_id, None)
                self._platform_navigation_reservations.clear()
                self._platform_dispatch.clear()
            if report_multiview:
                try:
                    self._composition.report_multiview_lifecycle(
                        self.session_id, job.intent.intent_id, name.value, "accepted"
                    )
                except Exception:
                    _LOGGER.exception("multiview lifecycle reporting failed for safety intent")
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
                job,
                PREEMPTED_BY_HOLD,
                running_on=(self._normal,),
                running_names=HOLD_PREEMPTS,
                report_multiview=report_multiview,
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
        report_multiview: bool = True,
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
            if self.navigation_wire is not None:
                self.navigation_wire.retire_intent(victim.intent.intent_id)
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
            except Exception:
                _LOGGER.exception("could not record safety preemption")
                event = None
            with self._lock:
                victim.cancelled_by = reason
                self._awaiting.pop(victim.intent.intent_id, None)
                self._platform_dispatch.pop(victim.intent.intent_id, None)
                ground = self._platform_ground_dispatch.pop(victim.intent.intent_id, None)
                if ground is not None and self.ground_navigation is not None:
                    self.ground_navigation.deployment.cancel(ground.plan)
            if event is not None:
                stop.publications.append(event)
            if report_multiview:
                try:
                    self._composition.report_multiview_lifecycle(
                        self.session_id,
                        victim.intent.intent_id,
                        victim.intent.name.value,
                        "invalidated",
                    )
                except Exception:
                    _LOGGER.exception("multiview lifecycle reporting failed for safety preemption")

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

        def current() -> FleetSnapshot:
            job.check()
            return self.snapshot(
                session.current_state(), capture_readiness=session.capture_readiness
            )

        def gate(link: RelayNodeLink) -> NodeLink:
            return _PreemptibleLink(link, job, session)

        try:
            with self._lock:
                prepared_ground = self._platform_ground_dispatch.pop(intent.intent_id, None)
            if prepared_ground is not None:
                if prepared_ground.intent != intent or self.ground_navigation is None:
                    raise ValueError("ground intent differs from its frozen qualified plan")
                ground_link = RelayNodeLink(
                    runtime,
                    self.session_id,
                    delivery_timeout_ms=runtime.settings.command_ttl_ms,
                )
                try:
                    result = self.ground_navigation.dispatch(
                        prepared_ground,
                        gate(ground_link),
                        job.check,
                        send_stop=ground_link.send,
                    )
                finally:
                    self.ground_navigation.deployment.cancel(prepared_ground.plan)
                dispatcher = None
            elif intent.name is IntentName.GROUND_VELOCITY:
                link = gate(
                    RelayNodeLink(
                        runtime,
                        self.session_id,
                        delivery_timeout_ms=runtime.settings.command_ttl_ms,
                    )
                )
                result = GroundCommandDispatcher(
                    link,
                    acknowledgement_timeout_ms=runtime.settings.command_ttl_ms,
                    command_deadline_ms=runtime.settings.command_deadline_ms,
                ).dispatch(intent, session.current_state())
                dispatcher = None
            elif intent.name is IntentName.COME_HOME and _ground_return_selected(
                intent, session.current_state()
            ):
                link = gate(
                    RelayNodeLink(
                        runtime,
                        self.session_id,
                        delivery_timeout_ms=runtime.settings.command_ttl_ms,
                    )
                )
                result = GroundCommandDispatcher(
                    link,
                    acknowledgement_timeout_ms=runtime.settings.command_ttl_ms,
                    command_deadline_ms=runtime.settings.command_deadline_ms,
                ).dispatch_return(
                    intent,
                    session.current_state(),
                    return_id=runtime.settings.ground_return_id,
                )
                dispatcher = None
            else:
                snapshot = current()
                if intent.name in {IntentName.HOLD, IntentName.ESTOP}:
                    ground_dispatcher = None
                    ground_state = session.current_state()
                    if _ground_stop_targets(intent, ground_state):
                        link = gate(
                            RelayNodeLink(
                                runtime,
                                self.session_id,
                                delivery_timeout_ms=runtime.settings.command_ttl_ms,
                            )
                        )
                        ground_dispatcher = GroundCommandDispatcher(
                            link,
                            acknowledgement_timeout_ms=runtime.settings.command_ttl_ms,
                            command_deadline_ms=runtime.settings.command_deadline_ms,
                        )
                    air_intent, air_snapshot = _air_only_stop(intent, snapshot)
                    dispatch_aircraft = bool(air_snapshot.aircraft) and (
                        intent.name is IntentName.ESTOP or bool(air_intent.selection)
                    )
                    if dispatch_aircraft:
                        dispatcher = build_dispatcher(
                            runtime,
                            self.session_id,
                            air_snapshot,
                            arbiter=self.arbiter,
                            sim_camera_config=self._composition.config.sim_camera,
                            link_wrapper=gate,
                            navigation_publisher=self.navigation_wire,
                            navigation_runtime=self.navigation_runtime,
                        )
                        controller = AutonomyController(
                            planner=self.planner, arbiter=self.arbiter, dispatcher=dispatcher
                        )
                    else:
                        dispatcher = None
                    if ground_dispatcher is not None and dispatch_aircraft:
                        with ThreadPoolExecutor(max_workers=2) as workers:
                            ground_future = workers.submit(
                                ground_dispatcher.dispatch_stop, intent, ground_state
                            )
                            air_future = workers.submit(
                                controller.execute,
                                air_intent,
                                air_snapshot,
                                current_snapshot=lambda: _air_only_stop(intent, current())[1],
                            )
                            ground_result = ground_future.result()
                            air_result = air_future.result()
                    elif ground_dispatcher is not None:
                        ground_result = ground_dispatcher.dispatch_stop(intent, ground_state)
                        air_result = None
                    elif dispatch_aircraft:
                        ground_result = None
                        air_result = controller.execute(
                            air_intent,
                            air_snapshot,
                            current_snapshot=lambda: _air_only_stop(intent, current())[1],
                        )
                    else:
                        ground_result = air_result = None
                    result = _aggregate_stop_results(intent, snapshot, ground_result, air_result)
                else:
                    dispatcher = build_dispatcher(
                        runtime,
                        self.session_id,
                        snapshot,
                        arbiter=self.arbiter,
                        sim_camera_config=self._composition.config.sim_camera,
                        link_wrapper=gate,
                        navigation_publisher=self.navigation_wire,
                        navigation_runtime=self.navigation_runtime,
                    )
                    controller = AutonomyController(
                        planner=self.planner, arbiter=self.arbiter, dispatcher=dispatcher
                    )
                    if intent.name is IntentName.SEARCH:
                        search = self.search_runtime
                        if search is None or not search.accepts_intent(intent, snapshot.now_ms):
                            result = ExecutionResult(
                                intent_id=intent.intent_id,
                                roster_version=snapshot.roster_version,
                                status=LifecycleStatus.REFUSED,
                                refusal=Refusal(
                                    intent_id=intent.intent_id,
                                    roster_version=snapshot.roster_version,
                                    drone_id=None,
                                    connection_epoch=None,
                                    reason=RefusalReason.INVALID_PLAN,
                                    detail="search intent has no matching frozen preview",
                                ),
                            )
                        else:
                            detection_started = (
                                self.search_detection is None
                                or self.search_detection.start_mission(intent.intent_id, session)
                            )
                            if not detection_started:
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
                                result = search.execute(
                                    intent.intent_id,
                                    dispatcher,
                                    snapshot,
                                    current_snapshot=current,
                                )
                    else:
                        with self._lock:
                            prepared = self._platform_dispatch.pop(intent.intent_id, None)
                        if prepared is not None:
                            if prepared.intent != intent:
                                raise RuntimeError(
                                    "platform navigation intent does not match its frozen plan"
                                )
                            refusal = self.arbiter.check_intent(intent, snapshot)
                            if refusal is not None:
                                result = ExecutionResult(
                                    intent_id=intent.intent_id,
                                    roster_version=snapshot.roster_version,
                                    status=LifecycleStatus.REFUSED,
                                    refusal=refusal,
                                )
                            else:
                                scope = (
                                    self.navigation_wire.command_scope(prepared.plan, current)
                                    if self.navigation_wire is not None
                                    and prepared.plan.navigation is not None
                                    else nullcontext()
                                )
                                with scope:
                                    result = controller.dispatch_prepared(
                                        prepared, current_snapshot=current
                                    )
                        else:
                            prepared = controller.prepare(
                                intent, snapshot, current_snapshot=current
                            )
                            if isinstance(prepared, PreparedExecution):
                                scope = (
                                    self.navigation_wire.command_scope(prepared.plan, current)
                                    if self.navigation_wire is not None
                                    and prepared.plan.navigation is not None
                                    else nullcontext()
                                )
                                with scope:
                                    result = controller.dispatch_prepared(
                                        prepared, current_snapshot=current
                                    )
                            else:
                                result = prepared
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
            cancelled = job.cancelled_by
            if cancelled is not None:
                self._awaiting.pop(intent.intent_id, None)
                job.finished = True
            elif result.status is LifecycleStatus.EXECUTING:
                if dispatcher is None:
                    raise RuntimeError("ground dispatcher returned a nonterminal command result")
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
        if cancelled is not None:
            return  # a stop already recorded this plan's terminal lifecycle
        if intent.name is IntentName.SEARCH and result.status is not LifecycleStatus.EXECUTING:
            if self.search_detection is not None:
                self.search_detection.finish_mission(intent.intent_id)
        if (
            intent.name is IntentName.SEARCH
            and result.status is LifecycleStatus.EXECUTING
            and self.search_detection is not None
        ):
            self.search_detection.monitor_mission(
                intent.intent_id,
                lambda reason: self._fail_search_detection(intent.intent_id, session, reason),
            )
        self._report(runtime, session, job, result)

    def fail_navigation_tracking(self, error: NavigationTrackingError) -> list[dict[str, object]]:
        with self._lock:
            owner = self._awaiting.get(error.intent_id)
            if owner is None:
                job = next(
                    (
                        lane.running
                        for lane in self._lanes
                        if lane.running is not None
                        and lane.running.intent.intent_id == error.intent_id
                    ),
                    None,
                )
                if job is None or job.session is None or job.cancelled_by is not None:
                    return []
                job.cancelled_by = "navigation_tracking_refused"
                job.finished = True
                pending = None
                session = job.session
            elif owner.job.cancelled_by is not None or not any(
                acknowledgement.command_id == error.command_id
                and acknowledgement.status in {LifecycleStatus.ACCEPTED, LifecycleStatus.EXECUTING}
                for acknowledgement in owner.pending.acknowledgements
            ):
                return []
            else:
                owner.job.cancelled_by = "navigation_tracking_refused"
                owner.job.finished = True
                self._awaiting.pop(error.intent_id, None)
                job = owner.job
                pending = owner.pending
                session = owner.session
        if pending is None:
            snapshot = self.snapshot(
                session.current_state(), capture_readiness=session.capture_readiness
            )
            result = ExecutionResult(
                intent_id=job.intent.intent_id,
                roster_version=snapshot.roster_version,
                status=LifecycleStatus.FAILED,
                refusal=Refusal(
                    intent_id=job.intent.intent_id,
                    roster_version=snapshot.roster_version,
                    drone_id=error.drone_id,
                    connection_epoch=error.connection_epoch,
                    reason=RefusalReason.INVALID_PLAN,
                    detail=error.detail,
                    status=LifecycleStatus.FAILED,
                ),
                degraded_aircraft=(error.drone_id,),
            )
        else:
            result = self._tracking_failure_result(pending, error)
        session.discard_command_waiter(error.command_id)
        navigation_wire = getattr(self, "navigation_wire", None)
        if navigation_wire is not None:
            navigation_wire.retire(error.command_id)
            for command_id in navigation_wire.retire_intent(error.intent_id):
                session.discard_command_waiter(command_id)
        for acknowledgement in result.acknowledgements:
            session.discard_command_waiter(acknowledgement.command_id)
        try:
            events = apply_result(session, job.intent, result)
        except Exception:
            _LOGGER.exception("navigation tracking failure could not be published")
            events = []
        if getattr(job.intent, "name", None) is IntentName.SEARCH:
            search = self.search_runtime
            if search is not None:
                try:
                    search.complete_execution(error.intent_id, result)
                except Exception:
                    _LOGGER.exception("search execution cleanup failed after navigation tracking")
            if self.search_detection is not None:
                try:
                    self.search_detection.finish_mission(error.intent_id)
                except Exception:
                    _LOGGER.exception("search detection cleanup failed after navigation tracking")
        events.extend(self._queue_navigation_tracking_hold(session, job.intent))
        self._defer_tracking_callback(self._report_navigation_tracking_failure, job.intent, result)
        return events

    def _defer_tracking_callback(self, callback: Callable[..., None], *args: object) -> None:
        runtime = self._composition.runtime_if_bound()
        loop = None if runtime is None else runtime.loop
        if loop is None or loop.is_closed():
            threading.Thread(target=callback, args=args, daemon=True).start()
            return

        def schedule() -> None:
            task = asyncio.create_task(asyncio.to_thread(callback, *args))
            runtime._track_background_operation(task)

        loop.call_soon_threadsafe(schedule)

    def _report_navigation_tracking_failure(
        self, intent: IntentV1, result: ExecutionResult
    ) -> None:
        try:
            self._composition.report_multiview_execution(self.session_id, intent, result)
        except Exception:
            _LOGGER.exception("multiview execution reporting failed for navigation tracking")

    def _queue_navigation_tracking_hold(
        self, session: RelaySession, failed_intent: IntentV1
    ) -> list[dict[str, object]]:
        safety_intent = IntentV1(
            v=1,
            t=session.clock(),
            type="intent",
            intent_id=(
                "safety:navigation-tracking:"
                f"{hashlib.sha256(failed_intent.intent_id.encode()).hexdigest()[:24]}"
            ),
            retry_of=None,
            source="safety",
            session=self.session_id,
            name=IntentName.HOLD,
            args={},
            selection=failed_intent.selection,
            mode=Mode.INDOOR,
            confirm=True,
        )
        try:
            events = [session.admit_safety_stop(safety_intent)]
        except Exception:
            _LOGGER.exception("navigation tracking safety hold could not be recorded")
            try:
                events = [session.admit_safety_stop(safety_intent)]
            except Exception:
                _LOGGER.exception("navigation tracking safety hold retry could not be recorded")
                events = []
        hold_job = _Job(safety_intent, session)
        try:
            hold_lane = self._route(hold_job, report_multiview=False)
        except Exception:
            _LOGGER.exception("navigation tracking safety hold could not be routed")
            hold_lane = self._hold
        with hold_lane.ready:
            hold_lane.pending.append(hold_job)
            hold_lane.ready.notify()
        events.extend(hold_job.publications)
        self._defer_tracking_callback(self._report_navigation_tracking_hold, safety_intent)
        return events

    def _report_navigation_tracking_hold(self, intent: IntentV1) -> None:
        try:
            self._composition.report_multiview_lifecycle(
                self.session_id, intent.intent_id, intent.name.value, "accepted"
            )
        except Exception:
            _LOGGER.exception("multiview lifecycle reporting failed for navigation tracking hold")

    @staticmethod
    def _tracking_failure_result(
        pending: ExecutionResult, failure: NavigationTrackingError
    ) -> ExecutionResult:
        return ExecutionResult(
            intent_id=pending.intent_id,
            roster_version=pending.roster_version,
            status=LifecycleStatus.FAILED,
            plan=pending.plan,
            acknowledgements=pending.acknowledgements,
            refusal=Refusal(
                intent_id=pending.intent_id,
                roster_version=pending.roster_version,
                drone_id=failure.drone_id,
                connection_epoch=failure.connection_epoch,
                reason=RefusalReason.INVALID_PLAN,
                detail=failure.detail,
                status=LifecycleStatus.FAILED,
            ),
            degraded_aircraft=(failure.drone_id,),
        )

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
            scope = (
                self.navigation_wire.command_scope(owner.pending.plan, current)
                if self.navigation_wire is not None and owner.pending.plan.navigation is not None
                else nullcontext()
            )
            with scope:
                return owner.dispatcher.resume_after_completion(
                    owner.pending.plan,
                    owner.pending,
                    token.acknowledgement,
                    owner.snapshot,
                    current_snapshot=current,
                    owner_still_valid=lambda: self._owns_resume(token),
                )
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
        try:
            events = apply_result(owner.session, owner.job.intent, result)
        except ValueError:
            if owner.job.cancelled_by is None:
                raise
            return None
        if result.status is not LifecycleStatus.EXECUTING:
            self._defer_tracking_callback(
                self._report_navigation_tracking_failure, owner.job.intent, result
            )
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
                return apply_result(session, job.intent, result)
            except ValueError:
                if job.cancelled_by is None:
                    raise
                _LOGGER.info("intent %s was cancelled as it completed", job.intent.intent_id)
                return []

        self._publish(runtime, operation)
        self._composition.report_multiview_execution(self.session_id, job.intent, result)

    def _fail_search_detection(self, intent_id: str, session: RelaySession, reason: str) -> None:
        runtime = self._composition.runtime_if_bound()
        search = self.search_runtime
        if runtime is None or search is None:
            return
        with self._lock:
            owner = self._awaiting.get(intent_id)
            if owner is None or owner.session is not session or owner.job.cancelled_by is not None:
                return
            result = ExecutionResult(
                intent_id=intent_id,
                roster_version=owner.snapshot.roster_version,
                status=LifecycleStatus.FAILED,
                plan=owner.pending.plan,
                refusal=Refusal(
                    intent_id=intent_id,
                    roster_version=owner.snapshot.roster_version,
                    drone_id=None,
                    connection_epoch=None,
                    reason=RefusalReason.ADAPTER_FAILURE,
                    detail=reason,
                    status=LifecycleStatus.FAILED,
                ),
            )

        def operation() -> list[dict[str, object]]:
            with self._lock:
                current = self._awaiting.get(intent_id)
                if current is not owner or owner.job.cancelled_by is not None:
                    return []
                events = apply_result(session, owner.job.intent, result)
                owner.job.cancelled_by = reason
                owner.job.finished = True
                self._awaiting.pop(intent_id, None)
            search.complete_execution(intent_id, result)
            if self.search_detection is not None:
                self.search_detection.finish_mission(intent_id)
            safety_intent = IntentV1(
                v=1,
                t=session.clock(),
                type="intent",
                intent_id=f"safety:search-detection:{hashlib.sha256(intent_id.encode()).hexdigest()[:24]}",
                retry_of=None,
                source="safety",
                session=self.session_id,
                name=IntentName.HOLD,
                args={},
                selection=owner.job.intent.selection,
                mode=Mode.INDOOR,
                confirm=True,
            )
            events.append(session.admit_safety_stop(safety_intent))
            hold_job = _Job(safety_intent, session)
            hold_lane = self._route(hold_job)
            with hold_lane.ready:
                hold_lane.pending.append(hold_job)
                hold_lane.ready.notify()
            events.extend(hold_job.publications)
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


class SurveyIntentRouter:
    """Combines pilot-assisted ground recording with the existing autonomous intent sink."""

    def __init__(self, autonomy: AutonomySession, session: RelaySession) -> None:
        from relay.survey_area import SurveyAreaLifecycle, SurveyCandidateRegistry

        self.autonomy = autonomy
        self.capability_profile = autonomy.capability_profile
        self.survey = SurveyAreaLifecycle(
            session, SurveyCandidateRegistry(session.audit_log.root / "survey_candidates")
        )

    def __call__(self, intent: IntentV1, state: dict[str, object]) -> object:
        if intent.name is IntentName.SURVEY_AREA:
            return self.survey.start(intent)
        return self.autonomy(intent, state)

    def survey_lifecycle(self, request: object) -> list[dict[str, object]]:
        from relay.survey_area import SurveyLifecycleRequest

        if not isinstance(request, SurveyLifecycleRequest):
            raise ValueError("survey lifecycle request is invalid")
        return self.survey.process(request)

    def accepted_observation(self, observation: object) -> list[dict[str, object]]:
        from relay.observations import Observation

        self.autonomy.accepted_observation(observation)
        return (
            []
            if not isinstance(observation, Observation)
            else self.survey.accepted_observation(observation)
        )

    def adapter_disconnected(
        self, *, drone_id: int, connection_epoch: int, relay_state: dict[str, object]
    ) -> list[dict[str, object]]:
        events: list[dict[str, object]] = []
        underlying = getattr(self.autonomy, "adapter_disconnected", None)
        if callable(underlying):
            events.extend(
                underlying(
                    drone_id=drone_id,
                    connection_epoch=connection_epoch,
                    relay_state=relay_state,
                )
            )
        events.extend(
            self.survey.adapter_disconnected(drone_id=drone_id, connection_epoch=connection_epoch)
        )
        return events

    def periodic_events(self, state: object) -> list[dict[str, object]]:
        events: list[dict[str, object]] = []
        underlying = getattr(self.autonomy, "periodic_events", None)
        if callable(underlying):
            events.extend(underlying(state))
        events.extend(self.survey.periodic_events())
        return events

    def __getattr__(self, name: str) -> object:
        return getattr(self.autonomy, name)


class AutonomyComposition:
    """Per-session autonomy workers behind ``create_app``'s sink and leave factories."""

    def __init__(
        self,
        config: AutonomyConfig,
        capability_profile: CapabilityProfile = C1_CAPABILITY_PROFILE,
        *,
        node_types: Mapping[int, NodeType] | None = None,
        ground_return_id: str | None = None,
        detection_factory_args: Mapping[str, object] | None = None,
    ) -> None:
        self.config = config
        self.detection_factory_args = dict(detection_factory_args or {})
        if config.supervised_vertical is not None:
            profile = SUPERVISED_VERTICAL_PROFILE
        else:
            assert config.planning is not None
            profile = config.planning.effective_capability_profile(capability_profile)
            if node_types is not None and any(
                node_type is NodeType.GROUND for node_type in node_types.values()
            ):
                profile = with_ground_capabilities(profile)
                profile = CapabilityProfile(
                    profile.name, profile.enabled_intent_names | SURVEY_ADDITIONAL_INTENT_NAMES
                )
            if config.navigation is not None:
                profile = navigation_capability_profile(profile, config.navigation.config)
            if config.search is not None:
                profile = CapabilityProfile(
                    f"{profile.name}.search", profile.enabled_intent_names | {IntentName.SEARCH}
                )
        if (
            config.supervised_vertical is not None
            and node_types is not None
            and any(node_type is NodeType.GROUND for node_type in node_types.values())
        ):
            # Ground pulses/approved return use their independent local guards.
            # This grants no aircraft translation or world navigation policy.
            profile = with_ground_capabilities(profile)
            if ground_return_id:
                profile = CapabilityProfile(
                    profile.name,
                    profile.enabled_intent_names | {IntentName.COME_HOME},
                    requires_home_pose=profile.requires_home_pose,
                )
        self.capability_profile = profile
        self._runtime_source: Callable[[], RelayRuntime | None] = _no_runtime
        self._sessions: dict[str, AutonomySession] = {}
        self._multiview_listener: Callable[[str, str, str, str], None] | None = None
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

    def intent_sink_factory(self, session: RelaySession) -> IntentSink:
        return SurveyIntentRouter(self.session(session.session_id), session)

    def leave_authorizer_factory(self, session_id: str) -> LeaveAuthorizer:
        return self.session(session_id).authorize_leave

    def session(self, session_id: str) -> AutonomySession:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                session = AutonomySession(self, session_id)
                self._sessions[session_id] = session
            return session

    def preview_platform_navigation(
        self, session_id: str, preview: Mapping[str, object]
    ) -> dict[str, object]:
        return self.session(session_id).preview_platform_navigation(preview)

    def confirm_platform_navigation(
        self, session_id: str, preview: Mapping[str, object]
    ) -> dict[str, object]:
        return self.session(session_id).confirm_platform_navigation(preview)

    def reserve_platform_navigation(
        self, session_id: str, preview: Mapping[str, object]
    ) -> dict[str, object]:
        return self.session(session_id).reserve_platform_navigation(preview)

    def discard_reserved_platform_navigation(
        self, session_id: str, preview_id: str
    ) -> dict[str, object]:
        return self.session(session_id).discard_reserved_platform_navigation(preview_id)

    def dispatch_reserved_platform_navigation(
        self, session_id: str, preview_id: str
    ) -> dict[str, object]:
        return self.session(session_id).dispatch_reserved_platform_navigation(preview_id)

    def confirm_platform_capture(
        self, session_id: str, capture: Mapping[str, object]
    ) -> dict[str, object]:
        return self.session(session_id).confirm_platform_capture(capture)

    def set_multiview_listener(self, listener: Callable[[str, str, str, str], None]) -> None:
        self._multiview_listener = listener

    def report_multiview_execution(
        self, session_id: str, intent: IntentV1, result: ExecutionResult
    ) -> None:
        listener = self._multiview_listener
        if listener is not None and result.status is not LifecycleStatus.EXECUTING:
            listener(
                session_id,
                self.session(session_id).multiview_execution_intent(
                    intent.intent_id, terminal=True
                ),
                intent.name.value,
                result.status.value,
            )

    def report_multiview_lifecycle(
        self, session_id: str, intent_id: str, intent_name: str, status: str
    ) -> None:
        listener = self._multiview_listener
        if listener is not None:
            listener(
                session_id,
                self.session(session_id).multiview_execution_intent(
                    intent_id, terminal=status != "executing"
                ),
                intent_name,
                status,
            )

    def navigation_events(
        self, session_id: str, events: list[dict[str, object]]
    ) -> list[dict[str, object]]:
        with self._lock:
            owner = self._sessions.get(session_id)
        publisher = None if owner is None else owner.navigation_wire
        if publisher is None:
            return []
        session = self.runtime.sessions.get(session_id)
        if session is None:
            return []
        output = []
        for event in events:
            if event.get("type") == "control_pose":
                drone_id = event.get("drone_id")
                if type(drone_id) is int:
                    pose = session.control_pose(drone_id)
                    if pose is not None:
                        try:
                            frames = publisher.update(pose)
                            for frame in frames:
                                session.record_navigation_evidence(frame)
                            output.extend(frames)
                        except NavigationTrackingError as error:
                            _LOGGER.warning("navigation tracking refused for aircraft %s", drone_id)
                            output.extend(owner.fail_navigation_tracking(error))
                        except PlanPreempted:
                            continue
                        except ValueError:
                            _LOGGER.exception(
                                "navigation tracking publisher failed for aircraft %s", drone_id
                            )
            elif event.get("status") in {"completed", "failed", "refused", "invalidated"}:
                command_id = event.get("command_id")
                intent_id = event.get("intent_id")
                if isinstance(command_id, str):
                    if event.get("status") != "completed" or not publisher.retain_arrival(
                        command_id
                    ):
                        publisher.retire(command_id)
                elif isinstance(intent_id, str) and event.get("status") != "completed":
                    publisher.retire_intent(intent_id)
        return output

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
    transcript_service_factory: TranscriptServiceFactory | None = None,
    detection_stream_factory: object | None = None,
    detection_detector_factory: object | None = None,
    detection_pose_provider_factory: object | None = None,
    detection_camera_provider_factory: object | None = None,
) -> tuple[FastAPI, AutonomyComposition]:
    """Build the relay app with the planner and arbiter consuming every accepted intent.

    ``transcript_service_factory`` is ``create_app``'s hook for the voice endpoint;
    ``relay.main`` builds one that compiles transcripts against this composition's
    planning policy and capability profile.
    """
    if settings.adapter_backend is AdapterBackend.SIM and config.sim_camera is None:
        raise SettingsError("SWEEP_SIM_CAMERA_JSON is required when SWEEP_ADAPTER_BACKEND is sim")
    if config.navigation is not None:
        config.navigation.validate_projector(config.control_localization_projector)
    detection_factory_args = {
        key: value
        for key, value in {
            "stream_factory": detection_stream_factory,
            "detector_factory": detection_detector_factory,
            "pose_provider_factory": detection_pose_provider_factory,
            "camera_provider_factory": detection_camera_provider_factory,
        }.items()
        if value is not None
    }
    composition = AutonomyComposition(
        config,
        settings.capability_profile,
        node_types=settings.node_types,
        ground_return_id=settings.ground_return_id,
        detection_factory_args=detection_factory_args,
    )
    control_localization_factory = (
        None
        if config.control_localization_projector is None
        else lambda _session_id: config.control_localization_projector
    )
    from relay.platform import PlatformServices

    def configuration_value(value):
        if isinstance(value, Enum):
            return value.value
        if isinstance(value, frozenset | set):
            return sorted(value)
        raise TypeError("Unsupported authoritative motion configuration value")

    motion_configuration = json.loads(
        json.dumps(
            {
                "planning": None if config.planning is None else asdict(config.planning),
                "safety": None if config.safety is None else asdict(config.safety),
                "navigation_configuration_sha256": (
                    None
                    if config.navigation is None
                    else config.navigation.approval.configuration_sha256
                ),
                "ground_navigation_configuration_sha256": (
                    None
                    if config.ground_navigation is None
                    else config.ground_navigation.configuration_sha256
                ),
                "supervised_vertical": (
                    None
                    if config.supervised_vertical is None
                    else asdict(config.supervised_vertical)
                ),
            },
            default=configuration_value,
        )
    )
    app = create_app(
        settings,
        clock=clock,
        event_ids=event_ids,
        intent_sink_factory=composition.intent_sink_factory,
        capability_profile=composition.capability_profile,
        leave_authorizer_factory=composition.leave_authorizer_factory,
        control_localization_factory=control_localization_factory,
        transcript_service_factory=transcript_service_factory,
        navigation_events=composition.navigation_events,
        platform_services_factory=lambda runtime: PlatformServices(
            runtime,
            motion_configuration=motion_configuration,
            flight_execution=(
                composition
                if (config.navigation is not None and config.navigation.approval.mode == "flight")
                or config.ground_navigation is not None
                else None
            ),
        ),
    )

    def search_runtime(authorization: str | None) -> RelayRuntime:
        runtime: RelayRuntime = app.state.relay_runtime
        expected = runtime.credential_resolver.resolve("console", None)
        supplied = (
            None
            if authorization is None or not authorization.startswith("Bearer ")
            else authorization.removeprefix("Bearer ")
        )
        if (
            expected is None
            or supplied is None
            or not hmac.compare_digest(supplied.encode(), expected)
        ):
            raise HTTPException(status_code=401, detail="authentication required")
        return runtime

    @app.post("/session/{session_id}/search/preview")
    async def search_preview(
        session_id: str,
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> dict[str, object]:
        runtime = search_runtime(authorization)
        try:
            payload = await request.json()
            raw_intent = payload.get("intent") if isinstance(payload, Mapping) else payload
            candidate = validate_intent(
                raw_intent, capability_profile=composition.capability_profile
            )
        except (ValueError, TypeError):
            raise HTTPException(status_code=422, detail="search intent is invalid") from None
        if (
            not isinstance(candidate, AcceptedIntent)
            or candidate.intent.name is not IntentName.SEARCH
            or candidate.intent.source != "console"
            or candidate.intent.session != session_id
        ):
            raise HTTPException(
                status_code=422, detail="a configured console search intent is required"
            )
        session = await runtime.activate_session(session_id)
        result = composition.session(session_id).preview_search(
            candidate.intent, session.current_state()
        )
        if isinstance(result, Refusal):
            raise HTTPException(status_code=422, detail=result.detail)
        return {
            "v": 1,
            "t": runtime.clock(),
            "type": "search_preview",
            "session": session_id,
            "intent_id": candidate.intent.intent_id,
            "preview": result.search.payload(),
            "routes": [
                {
                    "drone_id": route.drone.drone_id,
                    "connection_epoch": route.drone.connection_epoch,
                    "frame": "map_enu",
                    "waypoints": [list(point.xyz) for point in route.waypoints],
                }
                for route in result.plan.navigation.route.routes
            ],
            "plan": result.plan.to_dict(),
            "expires_at_ms": composition.session(session_id).search_runtime.preview_expires_at_ms(
                candidate.intent.intent_id
            ),
        }

    @app.get("/session/{session_id}/search/catalog")
    def search_catalog(
        session_id: str, authorization: str | None = Header(default=None)
    ) -> dict[str, object]:
        search_runtime(authorization)
        search = composition.session(session_id).search_runtime
        if search is None:
            raise HTTPException(status_code=404, detail="search is unavailable")
        from perception.object_detection import DEFAULT_TARGET_LABELS

        return {
            "session": session_id,
            "target_classes": list(DEFAULT_TARGET_LABELS),
            "zones": list(search.config.areas),
        }

    @app.post("/session/{session_id}/search/resolve")
    async def resolve_search(
        session_id: str,
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> dict[str, object]:
        search_runtime(authorization)
        search = composition.session(session_id).search_runtime
        if search is None:
            raise HTTPException(status_code=404, detail="search is unavailable")
        try:
            payload = await request.json()
        except ValueError:
            raise HTTPException(status_code=422, detail="a JSON search query is required") from None
        if (
            not isinstance(payload, dict)
            or set(payload) != {"query"}
            or not isinstance(payload["query"], str)
        ):
            raise HTTPException(status_code=422, detail="a text search query is required")
        from language.search_queries import SearchQueryFacts, resolve_search_query
        from perception.object_detection import DEFAULT_TARGET_LABELS

        correlation_id = str(uuid.uuid4())
        result = resolve_search_query(
            payload["query"],
            SearchQueryFacts(tuple(search.config.areas), tuple(DEFAULT_TARGET_LABELS)),
            session_id=session_id,
            correlation_id=correlation_id,
        )
        return {"session": session_id, "correlation_id": correlation_id, **result.to_dict()}

    @app.get("/session/{session_id}/search/{intent_id}")
    def search_status(
        session_id: str,
        intent_id: str,
        authorization: str | None = Header(default=None),
    ) -> dict[str, object]:
        search_runtime(authorization)
        search = composition.session(session_id).search_runtime
        if search is None or not search.belongs_to_session(intent_id, session_id):
            raise HTTPException(status_code=404, detail="search is unavailable")
        try:
            status = search.status_payload(intent_id)
        except ValueError:
            raise HTTPException(status_code=404, detail="search mission is unknown") from None
        owner = composition.session(session_id)
        if owner.search_detection is not None:
            status["detection_workers"] = owner.search_detection.status(intent_id)
        status["session"] = session_id
        return status

    @app.post("/session/{session_id}/search/{intent_id}/findings/{sighting_id}/ack")
    def acknowledge_search_finding(
        session_id: str,
        intent_id: str,
        sighting_id: str,
        authorization: str | None = Header(default=None),
    ) -> dict[str, object]:
        search_runtime(authorization)
        search = composition.session(session_id).search_runtime
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


def _ground_return_selected(intent: IntentV1, state: Mapping[str, object]) -> bool:
    if len(intent.selection) != 1:
        return False
    drones = state.get("drones", ())
    return isinstance(drones, (list, tuple)) and any(
        isinstance(drone, Mapping)
        and drone.get("drone_id") == intent.selection[0]
        and drone.get("node_type") == "ground"
        for drone in drones
    )


def _ground_stop_targets(intent: IntentV1, state: Mapping[str, object]) -> bool:
    if intent.name not in {IntentName.HOLD, IntentName.ESTOP}:
        return False
    drones = state.get("drones")
    if not isinstance(drones, list):
        return False
    selected = set(intent.selection)
    return any(
        isinstance(drone, Mapping)
        and drone.get("node_type") == "ground"
        and isinstance(drone.get("drone_id"), int)
        and not isinstance(drone.get("drone_id"), bool)
        and (intent.name is IntentName.ESTOP or drone["drone_id"] in selected)
        for drone in drones
    )


def _air_only_stop(intent: IntentV1, snapshot: FleetSnapshot) -> tuple[IntentV1, FleetSnapshot]:
    """Keep ground IDs out of the aircraft planner's selection projection."""
    selection = tuple(drone_id for drone_id in intent.selection if drone_id in snapshot.aircraft)
    return replace(intent, selection=selection), replace(snapshot, selection=selection)


def _aggregate_stop_results(
    intent: IntentV1,
    snapshot: FleetSnapshot,
    ground: ExecutionResult | None,
    aircraft: ExecutionResult | None,
) -> ExecutionResult:
    if ground is None and aircraft is not None:
        return aircraft
    if aircraft is None and ground is not None:
        return ground
    if ground is None or aircraft is None:
        return ExecutionResult(
            intent_id=intent.intent_id,
            roster_version=snapshot.roster_version,
            status=LifecycleStatus.REFUSED,
            refusal=Refusal(
                intent_id=intent.intent_id,
                roster_version=snapshot.roster_version,
                drone_id=None,
                connection_epoch=None,
                reason=RefusalReason.INVALID_SELECTION,
                detail="the stop has no dispatchable targets",
            ),
        )

    plans = tuple(result.plan for result in (ground, aircraft) if result.plan is not None)
    commands = tuple(command for plan in plans for command in plan.commands)
    plan = Plan(
        plan_id=f"plan:{intent.intent_id}:mixed-stop",
        intent_id=intent.intent_id,
        intent_name=intent.name,
        roster_version=snapshot.roster_version,
        selection=intent.selection,
        confirmed=True,
        commands=commands,
        hold_scope=next((item.hold_scope for item in plans if item.hold_scope is not None), None),
    )
    acknowledgements = tuple(
        acknowledgement
        for result in (ground, aircraft)
        for acknowledgement in result.acknowledgements
    )
    failed = next(
        (result for result in (ground, aircraft) if result.status is not LifecycleStatus.COMPLETED),
        None,
    )
    if failed is None:
        return ExecutionResult(
            intent_id=intent.intent_id,
            roster_version=snapshot.roster_version,
            status=LifecycleStatus.COMPLETED,
            plan=plan,
            acknowledgements=acknowledgements,
        )
    refusal = failed.refusal or Refusal(
        intent_id=intent.intent_id,
        roster_version=snapshot.roster_version,
        drone_id=None,
        connection_epoch=None,
        reason=RefusalReason.ADAPTER_FAILURE,
        detail="a stop target did not complete",
        status=LifecycleStatus.FAILED,
    )
    return ExecutionResult(
        intent_id=intent.intent_id,
        roster_version=snapshot.roster_version,
        status=LifecycleStatus.FAILED,
        plan=plan,
        acknowledgements=acknowledgements,
        refusal=refusal,
        degraded_aircraft=tuple(
            sorted(
                {drone_id for result in (ground, aircraft) for drone_id in result.degraded_aircraft}
            )
        ),
    )


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
