from __future__ import annotations

from dataclasses import replace
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from planner.models import (
    CommandAcknowledgement,
    ExecutionResult,
    LifecycleStatus,
    PreparedExecution,
)
from planner.test_navigation_runtime import stack
from relay.audit import SessionAuditLog
from relay.auth import Principal
from relay.autonomy import (
    PREEMPTED_BY_HOLD,
    PREEMPTED_BY_LAND,
    AutonomyComposition,
    AutonomyConfig,
    _AwaitingExecution,
    _Job,
    _ResumeToken,
)
from relay.contracts import AdapterAcknowledgement
from relay.contracts import LifecycleStatus as WireLifecycleStatus
from relay.intent_v1 import IntentName
from relay.session import RelayLimits, RelaySession
from relay.tests.conftest import (
    ADAPTER_KEY,
    CONSOLE_KEY,
    SESSION,
    EventIds,
    MutableClock,
    membership_payload,
    telemetry_payload,
)
from relay.tests.test_autonomy import _intent
from tests.autonomy_fixtures import planning_config, safety_config


class _Runtime:
    def __init__(self, session: RelaySession) -> None:
        self.sessions = {session.session_id: session}
        self.loop = None


def _owner_session(tmp_path):
    controller, _, _, snapshot, current, _, route_intent = stack()
    prepared = controller.prepare(route_intent, snapshot, current_snapshot=current)
    assert isinstance(prepared, PreparedExecution)
    composition = AutonomyComposition(
        AutonomyConfig(
            planning=planning_config(),
            safety=safety_config(),
            navigation=controller.planner.navigation,
        )
    )
    clock = MutableClock()
    owner = composition.session(SESSION)
    session = RelaySession(
        session_id=SESSION,
        limits=RelayLimits(5000, 5000, 1000, 1000),
        audit_log=SessionAuditLog(tmp_path, SESSION),
        clock=clock,
        event_ids=EventIds(),
        intent_sink=owner,
        capability_profile=composition.capability_profile,
    )
    runtime = _Runtime(session)
    composition._runtime_source = lambda: runtime
    adapter = Principal(source="adapter", drone_id=1, signing_key=ADAPTER_KEY)
    session.process_membership(membership_payload(action="join", event_id="join"), adapter)
    session.process_telemetry(
        {**telemetry_payload(event_id="pose", state="hovering"), "x": 0.5, "y": 1.5, "z": 1.0},
        adapter,
    )
    session.process_membership(membership_payload(action="readiness", event_id="ready"), adapter)
    session.update_control_projection(selection=(1,), armed=True)
    route_job = _Job(route_intent, session)
    pending = ExecutionResult(
        intent_id=route_intent.intent_id,
        roster_version=prepared.plan.roster_version,
        status=LifecycleStatus.EXECUTING,
        plan=prepared.plan,
    )
    awaiting = _AwaitingExecution(route_job, session, SimpleNamespace(), snapshot, pending)
    owner._awaiting[route_intent.intent_id] = awaiting
    return composition, owner, session, awaiting


def test_normal_lane_refuses_motion_while_a_mapped_route_awaits(tmp_path, monkeypatch) -> None:
    composition, owner, session, awaiting = _owner_session(tmp_path)
    builds = []
    reported = Event()
    results = []
    original_report = owner._report

    def report(*args):
        results.append(args[-1])
        original_report(*args)
        reported.set()

    monkeypatch.setattr(owner, "_report", report)
    monkeypatch.setattr(
        "relay.autonomy.build_dispatcher", lambda *args, **kwargs: builds.append(args)
    )
    console = Principal(source="console", drone_id=None, signing_key=CONSOLE_KEY)
    accepted = session.process_intent(
        _intent("translate", intent_id="second-motion", selection=[1], args={"dx": 1, "dy": 0}),
        console,
    )
    try:
        assert accepted[0]["status"] == "accepted"
        owner.submit(session._pending_intents["second-motion"].intent, session.current_state())
        assert reported.wait(1)
        assert results[-1].status is LifecycleStatus.REFUSED
        assert results[-1].refusal is not None
        assert results[-1].refusal.reason.value == "active_task"
        assert builds == []

        monkeypatch.setattr(session, "record_lifecycle", lambda **kwargs: kwargs)
        hold = _Job(
            replace(awaiting.job.intent, intent_id="hold-after-route", name=IntentName.HOLD),
            session,
        )
        owner._route(hold)
        assert awaiting.job.cancelled_by == PREEMPTED_BY_HOLD
    finally:
        composition.close()


def test_watchdog_and_late_resume_serialize_dispatcher_io(tmp_path, monkeypatch) -> None:
    composition, owner, session, awaiting = _owner_session(tmp_path)
    command = awaiting.pending.plan.commands[0]
    accepted = CommandAcknowledgement(
        command.command_id,
        command.intent_id,
        command.roster_version,
        command.drone_id,
        command.connection_epoch,
        LifecycleStatus.EXECUTING,
    )
    awaiting.pending = replace(awaiting.pending, acknowledgements=(accepted,))
    entered = Event()
    release = Event()
    calls = []

    class Dispatcher:
        def expire_navigation(self, *args, **kwargs):
            calls.append("expire")
            entered.set()
            assert release.wait(1)
            return replace(awaiting.pending, status=LifecycleStatus.INVALIDATED)

        def resume_after_completion(self, *args, **kwargs):
            calls.append("resume")
            return replace(awaiting.pending, status=LifecycleStatus.COMPLETED)

    awaiting.dispatcher = Dispatcher()
    monkeypatch.setattr("relay.autonomy.apply_result", lambda *args: [])
    watch = Thread(target=owner._watch_navigation, args=(awaiting,))
    watch.start()
    try:
        assert entered.wait(1)
        terminal = replace(accepted, status=LifecycleStatus.COMPLETED)
        token = _ResumeToken(awaiting.job.intent.intent_id, awaiting, awaiting.pending, 0, terminal)
        awaiting.resume_pending = True
        resumed = Thread(target=owner.resume_io, args=(token,))
        resumed.start()
        assert calls == ["expire"]
        release.set()
        watch.join(1)
        resumed.join(1)
        assert not watch.is_alive()
        assert not resumed.is_alive()
        assert calls == ["expire", "resume"]
    finally:
        release.set()
        composition.close()


@pytest.mark.parametrize(
    ("name", "selection"),
    [(IntentName.LAND, (1,)), (IntentName.LAND_ALL, ())],
)
def test_landing_cancels_a_waiting_route_before_a_late_ack_can_resume_it(
    tmp_path, monkeypatch, name, selection
) -> None:
    composition, owner, session, awaiting = _owner_session(tmp_path)
    command = awaiting.pending.plan.commands[0]
    waiting = CommandAcknowledgement(
        command.command_id,
        command.intent_id,
        command.roster_version,
        command.drone_id,
        command.connection_epoch,
        LifecycleStatus.EXECUTING,
    )
    awaiting.pending = replace(awaiting.pending, acknowledgements=(waiting,))
    resumed = []
    awaiting.dispatcher = SimpleNamespace(
        resume_after_completion=lambda *args, **kwargs: resumed.append(args)
    )
    monkeypatch.setattr(session, "record_lifecycle", lambda **kwargs: kwargs)
    landing = _Job(
        replace(
            awaiting.job.intent,
            intent_id=f"{name.value}-after-route",
            name=name,
            selection=selection,
        ),
        session,
    )
    late_ack = AdapterAcknowledgement(
        1,
        2,
        "acknowledgement",
        "late-route-ack",
        session.session_id,
        command.intent_id,
        command.command_id,
        WireLifecycleStatus.COMPLETED,
        command.drone_id,
        command.connection_epoch,
        command.roster_version,
        None,
        None,
    )
    try:
        owner._route(landing)

        assert awaiting.job.cancelled_by == PREEMPTED_BY_LAND
        assert command.intent_id not in owner._awaiting
        assert owner.resume_after_acknowledgement(session, late_ack) is None
        assert resumed == []
    finally:
        composition.close()


def test_lost_route_owner_cannot_resume_a_navigation_suffix(tmp_path) -> None:
    composition, owner, _, awaiting = _owner_session(tmp_path)
    command = awaiting.pending.plan.commands[0]
    waiting = CommandAcknowledgement(
        command.command_id,
        command.intent_id,
        command.roster_version,
        command.drone_id,
        command.connection_epoch,
        LifecycleStatus.EXECUTING,
    )
    awaiting.pending = replace(awaiting.pending, acknowledgements=(waiting,))
    sent = []
    awaiting.dispatcher = SimpleNamespace(
        resume_after_completion=lambda *args, **kwargs: sent.append(args)
    )
    token = _ResumeToken(
        awaiting.job.intent.intent_id,
        awaiting,
        awaiting.pending,
        awaiting.generation,
        replace(waiting, status=LifecycleStatus.COMPLETED),
    )
    awaiting.resume_pending = True
    awaiting.job.cancelled_by = PREEMPTED_BY_HOLD
    owner._awaiting.pop(awaiting.job.intent.intent_id)
    try:
        owner.resume_io(token)
        assert sent == []
    finally:
        composition.close()
