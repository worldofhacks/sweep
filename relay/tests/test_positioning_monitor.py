"""Live periodic ingress reaches safety lanes and preserves late completion ownership."""

from contextlib import contextmanager
from queue import Queue
from threading import Event

from adapters.dispatch import AdapterDispatcher
from adapters.protocols import AdapterAcknowledgement, AdapterTimeout
from planner.models import (
    CommandOperation,
    DeviceClass,
    ExecutionResult,
    LifecycleStatus,
    RefusalReason,
)
from relay.app import RelayRuntime
from relay.auth import Principal
from relay.autonomy import AutonomyComposition, AutonomyConfig
from relay.settings import AdapterBackend, RelaySettings
from relay.tests.conftest import (
    ADAPTER_KEY,
    CONSOLE_KEY,
    GROUND_KEY,
    SESSION,
    acknowledgement_payload,
    ground_membership_payload,
    ground_telemetry_payload,
    intent_payload,
    membership_payload,
    telemetry_payload,
)
from tests.autonomy_fixtures import camera_config, make_stack, planning_config, safety_config

GROUND_KEYS = {drone_id: GROUND_KEY + str(drone_id).encode() for drone_id in (11, 12, 13)}


class MemorySafetyDevices:
    def __init__(self) -> None:
        self.calls: list[tuple[CommandOperation, int]] = []
        self.pending: set[int] = set()
        self.timeouts: set[int] = set()

    def hover(self, ids: list[int]) -> tuple[AdapterAcknowledgement, ...]:
        return self._acknowledge(ids, CommandOperation.HOVER)

    def land(self, ids: list[int]) -> tuple[AdapterAcknowledgement, ...]:
        assert all(drone_id < 11 for drone_id in ids), "robots cannot land"
        return self._acknowledge(ids, CommandOperation.LAND)

    def _acknowledge(
        self, ids: list[int], operation: CommandOperation
    ) -> tuple[AdapterAcknowledgement, ...]:
        self.calls.extend((operation, drone_id) for drone_id in ids)
        if any(drone_id in self.timeouts for drone_id in ids):
            raise AdapterTimeout(ids[0], operation)
        return tuple(
            AdapterAcknowledgement(
                drone_id,
                1,
                operation,
                LifecycleStatus.EXECUTING
                if drone_id in self.pending
                else LifecycleStatus.COMPLETED,
            )
            for drone_id in ids
        )


@contextmanager
def monitor_stack(tmp_path, clock, event_ids, monkeypatch, *, aircraft=False, armed=True):
    ground_ids = (11, 12, 13)
    config = AutonomyConfig(
        planning=planning_config(), safety=safety_config(), sim_camera=camera_config()
    )
    composition = AutonomyComposition(config)
    runtime = RelayRuntime(
        RelaySettings(
            relay_token=CONSOLE_KEY,
            adapter_keys={**({1: ADAPTER_KEY} if aircraft else {}), **GROUND_KEYS},
            device_classes={drone_id: DeviceClass.GROUND_VEHICLE for drone_id in ground_ids},
            log_dir=tmp_path,
            adapter_backend=AdapterBackend.REMOTE,
        ),
        clock=clock,
        event_ids=event_ids,
        intent_sink_factory=composition.intent_sink_factory,
    )
    composition.bind(runtime)
    session = runtime.session(SESSION)
    autonomy = composition.session(SESSION)
    devices = MemorySafetyDevices()
    reports: Queue[ExecutionResult] = Queue()

    def dispatcher(_runtime, _session_id, snapshot, *, arbiter, **_kwargs):
        return AdapterDispatcher(flight=devices, camera=make_stack(snapshot)[-1], arbiter=arbiter)

    monkeypatch.setattr("relay.autonomy.build_dispatcher", dispatcher)
    original_report = autonomy._report

    def report(*args):
        original_report(*args)
        reports.put(args[-1])

    monkeypatch.setattr(autonomy, "_report", report)
    try:
        for drone_id in ((1,) if aircraft else ()) + ground_ids:
            ground = drone_id >= 11
            key = GROUND_KEYS[drone_id] if ground else ADAPTER_KEY
            principal = Principal("adapter", drone_id, key)
            membership = ground_membership_payload if ground else membership_payload
            telemetry = ground_telemetry_payload if ground else telemetry_payload
            for frame in (
                membership(action="join", event_id=f"join-{drone_id}", drone_id=drone_id, key=key),
                telemetry(event_id=f"telemetry-{drone_id}", drone_id=drone_id),
                membership(
                    action="readiness", event_id=f"ready-{drone_id}", drone_id=drone_id, key=key
                ),
            ):
                assert session.process_frame(frame, principal)[0]["type"] != "refusal"
        session.update_control_projection(armed=armed, selection=(11,))
        yield runtime, session, autonomy, devices, reports
    finally:
        composition.close(timeout_s=1.0)


def _ground_evidence(session, clock, drone_id=11, *, quality=0.0, state="idle"):
    clock.value += 1
    events = session.process_telemetry(
        ground_telemetry_payload(
            event_id=f"evidence-{drone_id}-{clock.value}",
            timestamp=clock.value,
            drone_id=drone_id,
            pos_quality=quality,
            state=state,
        ),
        Principal("adapter", drone_id, GROUND_KEYS[drone_id]),
    )
    assert events[0]["type"] == "telemetry"


def test_periodic_loss_holds_the_ground_fleet_once_without_changing_operator_state(
    tmp_path, clock, event_ids, monkeypatch
):
    with monitor_stack(tmp_path, clock, event_ids, monkeypatch) as (
        runtime,
        session,
        autonomy,
        devices,
        reports,
    ):
        _ground_evidence(session, clock)

        events = runtime.periodic_events(session)
        result = reports.get(timeout=3)

        assert any(event.get("intent_id") == result.intent_id for event in events)
        assert result.status is LifecycleStatus.COMPLETED
        assert devices.calls == [(CommandOperation.HOVER, device_id) for device_id in (11, 12, 13)]
        assert session.current_state()["selection"] == [11]
        assert autonomy._operator_last_seen_ms is None
        for _ in range(5):
            _ground_evidence(session, clock)
            runtime.periodic_events(session)
        assert reports.empty()
        assert len(devices.calls) == 3
        clock.value += 5_000
        runtime.periodic_events(session)
        assert reports.empty(), "a ground-only fleet never attempts LAND_ALL"


def test_unarmed_idle_robots_do_not_trigger_monitor_but_reported_motion_does(
    tmp_path, clock, event_ids, monkeypatch
):
    with monitor_stack(tmp_path, clock, event_ids, monkeypatch, armed=False) as (
        runtime,
        session,
        _autonomy,
        devices,
        reports,
    ):
        _ground_evidence(session, clock)
        runtime.periodic_events(session)
        assert reports.empty() and devices.calls == []

        _ground_evidence(session, clock, state="moving")
        runtime.periodic_events(session)
        assert reports.get(timeout=3).status is LifecycleStatus.COMPLETED
        assert len(devices.calls) == 3


def test_fresh_bad_quality_does_not_restart_dwell_and_land_only_reaches_aircraft(
    tmp_path, clock, event_ids, monkeypatch
):
    with monitor_stack(tmp_path, clock, event_ids, monkeypatch, aircraft=True) as (
        runtime,
        session,
        _autonomy,
        devices,
        reports,
    ):
        _ground_evidence(session, clock)
        runtime.periodic_events(session)
        assert reports.get(timeout=3).status is LifecycleStatus.COMPLETED
        started = clock.value
        for elapsed in (1_000, 2_000, 3_000):
            clock.value = started + elapsed
            _ground_evidence(session, clock)
            for drone_id in (12, 13):
                _ground_evidence(session, clock, drone_id, quality=0.6)
            assert (
                session.process_telemetry(
                    telemetry_payload(event_id=f"aircraft-{elapsed}", timestamp=clock.value),
                    Principal("adapter", 1, ADAPTER_KEY),
                )[0]["type"]
                == "telemetry"
            )
            runtime.periodic_events(session)

        landing = reports.get(timeout=3)
        assert landing.status is LifecycleStatus.COMPLETED
        assert landing.plan is not None
        assert [command.drone_id for command in landing.plan.commands] == [1]
        assert devices.calls[-1] == (CommandOperation.LAND, 1)
        runtime.periodic_events(session)
        assert reports.empty()


def test_recovered_evidence_starts_a_new_debounced_loss_episode(
    tmp_path, clock, event_ids, monkeypatch
):
    with monitor_stack(tmp_path, clock, event_ids, monkeypatch) as (
        runtime,
        session,
        _autonomy,
        _devices,
        reports,
    ):
        _ground_evidence(session, clock)
        runtime.periodic_events(session)
        first = reports.get(timeout=3)
        _ground_evidence(session, clock, quality=0.6)
        runtime.periodic_events(session)
        assert reports.empty()
        _ground_evidence(session, clock)
        runtime.periodic_events(session)
        second = reports.get(timeout=3)
        assert first.intent_id != second.intent_id


def test_an_unresponsive_robot_does_not_prevent_other_robots_from_being_stopped(
    tmp_path, clock, event_ids, monkeypatch
):
    with monitor_stack(tmp_path, clock, event_ids, monkeypatch) as (
        runtime,
        session,
        _autonomy,
        devices,
        reports,
    ):
        devices.timeouts = {11}
        _ground_evidence(session, clock)
        runtime.periodic_events(session)
        result = reports.get(timeout=3)

        assert result.status is LifecycleStatus.FAILED
        assert {drone_id for _operation, drone_id in devices.calls} == {11, 12, 13}
        call_count = len(devices.calls)
        for _ in range(3):
            runtime.periodic_events(session)
        assert reports.empty() and len(devices.calls) == call_count


def test_telemetry_expiry_during_planning_retries_hold_against_the_new_roster(
    tmp_path, clock, event_ids, monkeypatch
):
    with monitor_stack(tmp_path, clock, event_ids, monkeypatch) as (
        runtime,
        session,
        autonomy,
        devices,
        reports,
    ):
        original = autonomy.planner.fleet_position_loss_plan
        expired = False

        def expire_after_planning(**kwargs):
            nonlocal expired
            plan = original(**kwargs)
            if not expired:
                expired = True
                clock.value += 5_000
                session.periodic_events()
            return plan

        monkeypatch.setattr(autonomy.planner, "fleet_position_loss_plan", expire_after_planning)
        _ground_evidence(session, clock)
        runtime.periodic_events(session)
        raced = reports.get(timeout=3)
        assert raced.refusal is not None
        assert raced.refusal.reason is RefusalReason.STALE_ROSTER
        assert devices.calls == []

        runtime.periodic_events(session)
        retried = reports.get(timeout=3)
        assert retried.intent_id != raced.intent_id
        assert retried.status is LifecycleStatus.COMPLETED
        assert {drone_id for _operation, drone_id in devices.calls} == {11, 12, 13}


def test_late_hold_completions_resume_all_robots_through_the_session_ledger(
    tmp_path, clock, event_ids, monkeypatch
):
    with monitor_stack(tmp_path, clock, event_ids, monkeypatch) as (
        runtime,
        session,
        autonomy,
        devices,
        reports,
    ):
        devices.pending = {11, 12}
        _ground_evidence(session, clock)
        runtime.periodic_events(session)
        initial = reports.get(timeout=3)
        assert initial.status is LifecycleStatus.EXECUTING
        for drone_id in (11, 12):
            owner = autonomy._awaiting[initial.intent_id]
            pending = next(
                ack for ack in owner.pending.acknowledgements if ack.drone_id == drone_id
            )
            assert pending.drone_id == drone_id
            devices.pending.remove(drone_id)
            events = session.process_acknowledgement(
                acknowledgement_payload(
                    event_id=f"hold-completed-{drone_id}",
                    timestamp=clock.value,
                    drone_id=drone_id,
                    intent_id=pending.intent_id,
                    command_id=pending.command_id,
                    connection_epoch=pending.connection_epoch,
                    roster_version=pending.roster_version,
                    status="completed",
                ),
                Principal("adapter", drone_id, GROUND_KEYS[drone_id]),
            )
            assert not any(event["type"] == "refusal" for event in events), events
        assert initial.intent_id not in autonomy._awaiting
        assert devices.calls == [(CommandOperation.HOVER, drone_id) for drone_id in (11, 12, 13)]
        assert events[-1]["status"] == "completed"


def test_already_expired_loss_waits_for_delayed_fleet_hold_before_landing(
    tmp_path, clock, event_ids, monkeypatch
):
    with monitor_stack(tmp_path, clock, event_ids, monkeypatch, aircraft=True) as (
        runtime,
        session,
        autonomy,
        devices,
        reports,
    ):
        devices.pending = {1}
        clock.value += 5_000
        # Settle the telemetry membership transition before observing the loss.
        session.periodic_events()
        runtime.periodic_events(session)
        holding = reports.get(timeout=3)
        assert holding.status is LifecycleStatus.EXECUTING
        for _ in range(3):
            runtime.periodic_events(session)
        assert devices.calls == [(CommandOperation.HOVER, drone_id) for drone_id in (1, 11, 12, 13)]
        assert reports.empty(), "landing must not overlap a pending HOLD"

        owner = autonomy._awaiting[holding.intent_id]
        pending = next(ack for ack in owner.pending.acknowledgements if ack.drone_id == 1)
        devices.pending.clear()
        events = session.process_acknowledgement(
            acknowledgement_payload(
                event_id="delayed-aircraft-hold-completed",
                timestamp=clock.value,
                drone_id=1,
                intent_id=pending.intent_id,
                command_id=pending.command_id,
                connection_epoch=pending.connection_epoch,
                roster_version=pending.roster_version,
                status="completed",
            ),
            Principal("adapter", 1, ADAPTER_KEY),
        )
        assert events[-1]["status"] == "completed"
        assert devices.calls == [(CommandOperation.HOVER, drone_id) for drone_id in (1, 11, 12, 13)]

        runtime.periodic_events(session)
        landing = reports.get(timeout=3)
        assert landing.status is LifecycleStatus.COMPLETED
        assert devices.calls[-1] == (CommandOperation.LAND, 1)


def test_never_terminal_hold_stops_every_device_then_expires_before_landing(
    tmp_path, clock, event_ids, monkeypatch
):
    with monitor_stack(tmp_path, clock, event_ids, monkeypatch, aircraft=True) as (
        runtime,
        session,
        autonomy,
        devices,
        reports,
    ):
        devices.pending = {1}
        _ground_evidence(session, clock)
        runtime.periodic_events(session)
        holding = reports.get(timeout=3)
        assert holding.status is LifecycleStatus.EXECUTING
        assert devices.calls == [(CommandOperation.HOVER, drone_id) for drone_id in (1, 11, 12, 13)]
        owner = autonomy._awaiting[holding.intent_id]
        missing = next(ack for ack in holding.acknowledgements if ack.drone_id == 1)
        assert missing.status is LifecycleStatus.EXECUTING
        # Future LANDs complete, but the already-issued HOLD never supplies a terminal ACK.
        devices.pending.clear()
        events = []
        for elapsed in range(1, 61):
            clock.value += 1_000
            for drone_id in (11, 12, 13):
                _ground_evidence(session, clock, drone_id, quality=0.0 if drone_id == 11 else 0.6)
            session.process_telemetry(
                telemetry_payload(event_id=f"still-flying-{elapsed}", timestamp=clock.value),
                Principal("adapter", 1, ADAPTER_KEY),
            )
            events.extend(runtime.periodic_events(session))
        landing = reports.get(timeout=3)
        assert landing.status is LifecycleStatus.COMPLETED
        assert devices.calls[-1] == (CommandOperation.LAND, 1)
        assert holding.intent_id not in autonomy._awaiting
        assert owner.job.finished and owner.pending.status is LifecycleStatus.FAILED
        assert owner.pending.refusal.reason is RefusalReason.ADAPTER_TIMEOUT
        assert any(
            event.get("intent_id") == holding.intent_id and event.get("reason") == "adapter_timeout"
            for event in events
        )
        assert len(devices.calls) == 5

        late = session.process_acknowledgement(
            acknowledgement_payload(
                event_id="expired-hold-late-completion",
                timestamp=clock.value,
                drone_id=1,
                intent_id=missing.intent_id,
                command_id=missing.command_id,
                connection_epoch=missing.connection_epoch,
                roster_version=missing.roster_version,
                status="completed",
            ),
            Principal("adapter", 1, ADAPTER_KEY),
        )
        assert late[0]["type"] == "refusal" and late[0]["reason"] == "command_expired"
        assert len(devices.calls) == 5


def test_normal_motion_stays_blocked_when_position_recovers_before_hold_completion(
    tmp_path, clock, event_ids, monkeypatch
):
    with monitor_stack(tmp_path, clock, event_ids, monkeypatch, aircraft=True) as (
        runtime,
        session,
        _autonomy,
        devices,
        reports,
    ):
        devices.pending = {1}
        _ground_evidence(session, clock)
        runtime.periodic_events(session)
        assert reports.get(timeout=3).status is LifecycleStatus.EXECUTING
        _ground_evidence(session, clock, quality=0.6)
        runtime.periodic_events(session)
        session.update_control_projection(selection=(1,))
        intent = intent_payload(timestamp=clock.value, intent_id="motion-during-safety") | {
            "name": "translate",
            "args": {"dx": 1, "dy": 0},
            "selection": [1],
        }
        assert (
            session.process_frame(intent, Principal("console", None, CONSOLE_KEY))[0]["status"]
            == "accepted"
        )
        session.mark_pending_intent_delivered("motion-during-safety")
        session.execute_pending_intent("motion-during-safety")
        refused = reports.get(timeout=3)
        assert refused.status is LifecycleStatus.REFUSED
        assert refused.refusal.reason is RefusalReason.ACTIVE_TASK
        assert {operation for operation, _ in devices.calls} == {CommandOperation.HOVER}


def test_normal_motion_stays_blocked_when_position_recovers_during_hold_adapter_io(
    tmp_path, clock, event_ids, monkeypatch
):
    with monitor_stack(tmp_path, clock, event_ids, monkeypatch, aircraft=True) as (
        runtime,
        session,
        autonomy,
        devices,
        reports,
    ):
        entered, release = Event(), Event()
        original_hover = devices.hover

        def delayed_hover(ids):
            if ids == [1]:
                entered.set()
                assert release.wait(timeout=3), "test must release the safety adapter"
            return original_hover(ids)

        monkeypatch.setattr(devices, "hover", delayed_hover)
        try:
            _ground_evidence(session, clock)
            runtime.periodic_events(session)
            assert entered.wait(timeout=3)
            assert not autonomy._awaiting
            _ground_evidence(session, clock, quality=0.6)
            runtime.periodic_events(session)
            assert not autonomy._position_loss_since
            session.update_control_projection(selection=(1,))
            intent_id = "motion-during-safety-io"
            intent = intent_payload(timestamp=clock.value, intent_id=intent_id) | {
                "name": "translate",
                "args": {"dx": 1, "dy": 0},
                "selection": [1],
            }
            assert (
                session.process_frame(intent, Principal("console", None, CONSOLE_KEY))[0]["status"]
                == "accepted"
            )
            session.mark_pending_intent_delivered(intent_id)
            session.execute_pending_intent(intent_id)
            refused = reports.get(timeout=3)
            assert refused.intent_id == intent_id
            assert refused.status is LifecycleStatus.REFUSED
            assert refused.refusal.reason is RefusalReason.ACTIVE_TASK
            assert devices.calls == []
        finally:
            release.set()
        assert reports.get(timeout=3).status is LifecycleStatus.COMPLETED
        assert devices.calls == [(CommandOperation.HOVER, drone_id) for drone_id in (1, 11, 12, 13)]
