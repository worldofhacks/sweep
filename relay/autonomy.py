"""Autonomy composition: accepted relay intents through the planner and arbiter to dispatch.

``relay.app`` acknowledges an Intent v1 request as ``accepted`` only when an intent
sink is configured, and the standalone ``relay.app:app`` has none. This module is
that sink for the M2.0 checkpoint and ``relay.main`` runs it. It implements the
session's sink contract: the factory receives the ``RelaySession``, the sink is
called once per accepted intent outside the session lock, and it returns an
``IntentSinkResult`` that the session applies (lifecycle, control projection, and
the network-stop latch) inside the intent's own operation.

Each session runs three worker lanes: operator intents in arrival order, ``hold`` on
its own lane, and ``estop`` on its own lane. The sink hands the intent to its lane
and blocks until the lane has a result, so the session records exactly one outcome
per intent while a ``hold`` or ``estop`` (the session runs both without its
execution lock) can still preempt the plan another lane is running. A worker projects
the relay state into the autonomy ``FleetSnapshot`` with explicit fail-closed
enrichment, runs ``AutonomyController`` (capability gate, arbiter, planner,
whole-plan arbitration, dispatch) on the adapters that ``SWEEP_ADAPTER_BACKEND``
selects, and returns the result for the session to apply.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, fields, is_dataclass, replace
from typing import get_origin, get_type_hints

from fastapi import FastAPI

from adapters.dji_mini3.remote import CommandRequest, NodeLink
from adapters.sim.camera import SimCameraConfig
from arbiter.safety import SafetyArbiter, SafetyConfig
from planner.controller import AutonomyController
from planner.models import (
    ExecutionResult,
    FleetSnapshot,
    FlightState,
    LifecycleStatus,
    Plan,
    Refusal,
    RefusalReason,
    RelayAircraftSafetyEnrichment,
    RelaySnapshotEnrichment,
)
from planner.planner import DeterministicPlanner, PlanningConfig
from planner.roster import authorize_graceful_removal
from relay.app import RelayRuntime, create_app
from relay.bridge import RelayNodeLink, build_dispatcher
from relay.contracts import AdapterAcknowledgement as WireAcknowledgement
from relay.contracts import CapabilitiesFrame, CaptureReadinessFrame, MediaFileRecord
from relay.contracts import LifecycleStatus as WireLifecycleStatus
from relay.intent_v1 import IntentName, IntentV1
from relay.session import (
    Clock,
    EventIdFactory,
    IntentSink,
    IntentSinkResult,
    LeaveAuthorizer,
    RelaySession,
)
from relay.settings import AdapterBackend, RelaySettings, SettingsError

LIFECYCLE_SOURCE = "autonomy"
PREEMPTED_BY_ESTOP = "preempted_by_estop"
PREEMPTED_BY_HOLD = "preempted_by_hold"
HOLD_PREEMPTS = frozenset(
    {IntentName.TAKEOFF, IntentName.TRANSLATE, IntentName.COME_HOME, IntentName.CAPTURE_ROOM}
)
"""Operator motion and camera plans a hold cancels; a running safety plan finishes first."""
ReadinessSource = Callable[[int], CaptureReadinessFrame | None]
_ESTOP_PREEMPTS = frozenset(IntentName) - {IntentName.ESTOP}
_SAFETY_PLANS = frozenset({IntentName.LAND_ALL, IntentName.ESTOP})
_LOGGER = logging.getLogger(__name__)
_FLIGHT_STATES = frozenset(state.value for state in FlightState)
_PHYSICALLY_DISARMED_STATES = frozenset({FlightState.DISARMED.value, FlightState.LANDED.value})


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
      intent in this session; the arbiter's operator timeout bounds how long that
      evidence lasts. No intent yet means no operator.
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


def plan_summary(plan: Plan) -> dict[str, object]:
    """The accepted-plan projection: enough for a roster change to invalidate it."""
    return {
        "plan_id": plan.plan_id,
        "intent_id": plan.intent_id,
        "intent_name": plan.intent_name.value,
        "roster_version": plan.roster_version,
        "selection": list(plan.selection),
    }


def sink_result(
    intent: IntentV1,
    result: ExecutionResult,
    *,
    roster_version: int,
    events: tuple[Mapping[str, object], ...] = (),
) -> IntentSinkResult:
    """Translate one execution result into what the session applies for the intent.

    The network stop latches from the intent itself, never from the plan, so the
    planner and arbiter path can only add commands and never remove the latch. Arm
    and selection apply only once their plan completed and only while the plan's
    roster is still the session's roster (``roster_version``); otherwise they are
    dropped and the result becomes ``invalidated`` with ``stale_roster``. The
    accepted-plan projection is not part of this result: a plan left waiting on a
    node's terminal acknowledgement publishes it explicitly, and every stop clears
    it through ``estop_update``.
    """
    plan = result.plan
    selection_update = armed_update = None
    if result.status is LifecycleStatus.COMPLETED and plan is not None:
        earned = plan.selection_update is not None or plan.armed_update is not None
        if earned and plan.roster_version != roster_version:
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
        else:
            selection_update = plan.selection_update
            armed_update = plan.armed_update
    refusal = result.refusal
    reason = None if refusal is None else refusal.reason.value
    if reason is None and result.status in {LifecycleStatus.FAILED, LifecycleStatus.INVALIDATED}:
        reason = RefusalReason.ADAPTER_FAILURE.value
    summary = result.to_dict()
    summary.pop("plan", None)
    summary["plan_id"] = None if plan is None else plan.plan_id
    summary["intent_name"] = intent.name.value
    return IntentSinkResult(
        status=WireLifecycleStatus(result.status.value),
        source=LIFECYCLE_SOURCE,
        result=summary,
        events=events,
        selection_update=selection_update,
        armed_update=armed_update,
        estop_update=True if intent.name is IntentName.ESTOP else None,
        reason=reason,
        detail=None if refusal is None else refusal.detail,
        drone_id=None if refusal is None else refusal.drone_id,
        connection_epoch=None if refusal is None else refusal.connection_epoch,
    )


def remaining_commands(result: ExecutionResult) -> int:
    """Commands an executing result still has to send once its waiting command ends."""
    plan = result.plan
    if plan is None or not result.acknowledgements:
        return 0
    waiting = result.acknowledgements[-1].command_id
    index = next(
        (
            position
            for position, command in enumerate(plan.commands)
            if command.command_id == waiting
        ),
        None,
    )
    if index is None:
        return 0
    degraded = set(result.degraded_aircraft)
    return sum(1 for command in plan.commands[index + 1 :] if command.drone_id not in degraded)


@dataclass(eq=False, slots=True)
class _Job:
    """One accepted intent; ``cancelled_by`` is the preemption flag."""

    intent: IntentV1
    publications: list[dict[str, object]] = field(default_factory=list)
    cancelled_by: str | None = None
    finished: bool = False
    done: bool = False
    result: IntentSinkResult | None = None

    def check(self) -> None:
        if self.cancelled_by is not None:
            raise PlanPreempted(self.cancelled_by)


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
            self._session.release_command_wait(command_id)
            raise PlanPreempted(self._job.cancelled_by)
        return acknowledgement

    def stop_waiting(self, command_id: str) -> None:
        self._inner.stop_waiting(command_id)

    def camera_capabilities(self, drone_id: int) -> CapabilitiesFrame | None:
        return self._inner.camera_capabilities(drone_id)

    def media_files(self, drone_id: int, capture_id: str) -> tuple[MediaFileRecord, ...]:
        return self._inner.media_files(drone_id, capture_id)


class AutonomySession:
    """One session's planner, arbiter, operator evidence, and intent lanes.

    The session calls the sink (``submit``) once per accepted intent, outside its own
    lock; the sink hands the intent to a lane and returns that lane's result. The
    ``normal`` lane runs operator intents in arrival order. ``hold`` and ``estop``
    each run at once on their own lane (``concurrent_intents`` keeps the session from
    serialising a hold behind the plan it must preempt; the session never serialises
    an estop) and cancel the plans they preempt: the stop records the cancelled intent
    as ``invalidated`` under the session lock, so ``issue_command`` refuses anything
    that plan tries to send afterwards; the plan's dispatch also checks its flag before
    every command and send and after every acknowledgement wait, then exits without a
    best-effort hold, and its own ``submit`` returns nothing because the stop already
    reported it. A hold cancels operator motion and camera plans but queues behind a
    running ``land_all`` or ``estop``; a network stop cancels whatever is running and
    latches the session's ``estop`` before it is queued, so nothing is dispatched
    after a stop was accepted without the latch in place.
    """

    concurrent_intents = frozenset({IntentName.HOLD})

    def __init__(self, composition: AutonomyComposition, session_id: str) -> None:
        self.session_id = session_id
        self._composition = composition
        self.planner = DeterministicPlanner(composition.config.planning)
        self.arbiter = SafetyArbiter(composition.config.safety)
        self._lock = threading.Lock()
        self._done = threading.Condition(self._lock)
        self._session: RelaySession | None = None
        self._operator_last_seen_ms: int | None = None
        self._stop_requested = False
        self._admitted: dict[str, _Job] = {}
        self._normal = _Lane("normal", self._lock)
        self._hold = _Lane("hold", self._lock)
        self._estop = _Lane("estop", self._lock)
        self._lanes = (self._normal, self._hold, self._estop)
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

    def attach(self, session: RelaySession) -> AutonomySession:
        """Bind the relay session this sink reports through; keyed by its session id."""
        if session.session_id != self.session_id:
            raise ValueError("relay session id does not match this autonomy session")
        with self._lock:
            if self._session is not None and self._session is not session:
                raise ValueError("autonomy session is already attached to a relay session")
            self._session = session
        return self

    def __call__(self, intent: IntentV1, state: dict[str, object]) -> IntentSinkResult | None:
        return self.submit(intent, state)

    def admit_intent(self, intent: IntentV1) -> None:
        """Record an accepted intent before it executes, so a stop can cancel it queued."""
        with self._lock:
            self._admitted.setdefault(intent.intent_id, _Job(intent))

    def cancel_intent(self, intent_id: str) -> None:
        """Forget an accepted intent the session refused before execution."""
        with self._lock:
            self._admitted.pop(intent_id, None)

    def submit(self, intent: IntentV1, _state: dict[str, object]) -> IntentSinkResult | None:
        """``IntentSink``: record operator activity, run the intent on its lane, report."""
        session = self._require_session()
        with self._lock:
            previous = self._operator_last_seen_ms
            self._operator_last_seen_ms = intent.t if previous is None else max(previous, intent.t)
            job = self._admitted.pop(intent.intent_id, None) or _Job(intent)
            cancelled = job.cancelled_by
        if cancelled is not None:
            return None  # cancelled while queued; the stop recorded its invalidation
        try:
            lane = self._route(job, session)
        except Exception:
            # A stop must reach its lane even if the preemption bookkeeping fails.
            _LOGGER.exception("preemption bookkeeping failed for intent %s", intent.intent_id)
            lane = self._lane_for(intent.name)
        with lane.ready:
            lane.pending.append(job)
            lane.ready.notify()
        with self._done:
            while not job.done:
                self._done.wait()
        return job.result

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
        return relay_snapshot(
            state,
            operator_last_seen_ms=operator_last_seen_ms,
            estop_requested=estop_requested,
            capture_readiness=capture_readiness,
        )

    def close(self, timeout_s: float) -> None:
        for lane in self._lanes:
            with lane.ready:
                lane.closed = True
                lane.ready.notify_all()
        for worker in self._workers:
            worker.join(timeout=timeout_s)

    def _require_session(self) -> RelaySession:
        with self._lock:
            session = self._session
        if session is None:
            raise RuntimeError("autonomy session is not attached to a relay session")
        return session

    def _lane_for(self, name: IntentName) -> _Lane:
        if name is IntentName.ESTOP:
            return self._estop
        if name is IntentName.HOLD:
            return self._hold
        return self._normal

    def _route(self, job: _Job, session: RelaySession) -> _Lane:
        """Choose the lane and cancel the plans this intent preempts before it queues."""
        name = job.intent.name
        if name is IntentName.ESTOP:
            with self._lock:
                self._stop_requested = True
            # Latch before the stop is queued: no worker, plan, or publish failure can
            # lose it, every later snapshot is stopped, and the session applies the
            # same latch again from the result's estop_update inside the intent
            # operation.
            job.publications.append(session.update_control_projection(estop=True))
            self._cancel(
                job,
                session,
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
                session,
                PREEMPTED_BY_HOLD,
                running_on=(self._normal,),
                running_names=HOLD_PREEMPTS,
            )
            return self._normal if behind_safety_plan else self._hold
        return self._normal

    def _cancel(
        self,
        stop: _Job,
        session: RelaySession,
        reason: str,
        *,
        running_on: tuple[_Lane, ...],
        running_names: frozenset[IntentName],
    ) -> None:
        """Invalidate the plans a stop preempts: running ones by name, queued motion ones.

        The invalidation is recorded first, under the session lock, so it is atomic
        against ``issue_command``; the flag is set afterwards so the plan exits
        promptly. The records travel with the stop's own result. A plan that reached a
        terminal state first keeps that result.
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
                for job in (*self._normal.pending, *self._admitted.values())
                if job.cancelled_by is None and job.intent.name in HOLD_PREEMPTS
            )
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
            result: IntentSinkResult | None = None
            try:
                result = self._execute(job)
            except Exception as error:
                _LOGGER.exception(
                    "autonomy %s lane failed session=%s intent=%s",
                    lane.name,
                    self.session_id,
                    job.intent.intent_id,
                )
                try:
                    result = self._failure(job, error)
                except Exception:  # the lane must outlive any failure to report one
                    _LOGGER.exception("autonomy failure result could not be built")
            finally:
                with lane.ready:
                    lane.running = None
                    job.result = result
                    job.done = True
                    self._done.notify_all()

    def _execute(self, job: _Job) -> IntentSinkResult | None:
        session = self._require_session()
        runtime = self._composition.runtime
        intent = job.intent
        if job.cancelled_by is not None:
            return None  # cancelled while queued; the stop recorded its invalidation

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
            )
            controller = AutonomyController(
                planner=self.planner, arbiter=self.arbiter, dispatcher=dispatcher
            )
            result = controller.execute(intent, snapshot, current_snapshot=current)
        except PlanPreempted as preempted:
            _LOGGER.info("intent %s stopped: %s", intent.intent_id, preempted.reason)
            return None
        except Exception as error:  # the console still receives a typed terminal result
            _LOGGER.exception(
                "autonomy dispatch path failed session=%s intent=%s",
                self.session_id,
                intent.intent_id,
            )
            result = _composition_failure(intent, session, error)
        with self._lock:
            job.finished = True
            cancelled = job.cancelled_by
        if cancelled is not None:
            return None  # a stop already recorded this plan's terminal lifecycle
        if result.status is LifecycleStatus.EXECUTING and result.plan is not None:
            # A node stopped answering inside the command deadline. The relay keeps
            # the command and settles this intent on the node's late terminal answer
            # or on the late window passing; until then the plan stays published so
            # a roster change can invalidate it by intent_id.
            session.expect_late_acknowledgement(
                intent.intent_id, completes_plan=remaining_commands(result) == 0
            )
            job.publications.append(
                session.update_control_projection(accepted_plan=plan_summary(result.plan))
            )
        return sink_result(
            intent,
            result,
            roster_version=session.registry.roster_version,
            events=tuple(job.publications),
        )

    def _failure(self, job: _Job, error: Exception) -> IntentSinkResult:
        session = self._require_session()
        return sink_result(
            job.intent,
            _composition_failure(job.intent, session, error),
            roster_version=session.registry.roster_version,
            events=tuple(job.publications),
        )


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

    def intent_sink_factory(self, session: RelaySession) -> IntentSink:
        """``IntentSinkFactory``: one autonomy session per relay session id."""
        return self.session(session.session_id).attach(session)

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
