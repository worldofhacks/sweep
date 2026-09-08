import asyncio
import threading
from types import SimpleNamespace

import pytest

from planner.models import ExecutionResult, LifecycleStatus
from relay.app import RelayRuntime
from relay.autonomy import (
    AutonomyComposition,
    AutonomySession,
    _AwaitingExecution,
    _Job,
    _ResumeToken,
)
from relay.intent_v1 import IntentName
from relay.multiview import MultiviewService
from relay.settings import RelaySettings
from relay.tests.test_multiview_contract import _Navigation


def _confirm(service, navigation, suffix):
    preview = service.preview(
        "s",
        {
            "intentId": f"multiview-{suffix}",
            "selected": [{"id": 1, "deviceClass": "aircraft", "epoch": 2}],
            "viewpoints": [
                {
                    "viewpointId": suffix,
                    "zoneId": f"zone-{suffix}",
                    "captureId": f"capture-{suffix}",
                }
            ],
        },
    )
    service.confirm("s", {key: preview[key] for key in ("previewId", "intentId", "previewHash")})
    return preview["previewId"], navigation.confirmed["intentId"]


def _composition(service, runtime):
    composition = SimpleNamespace(
        _multiview_listener=service.observe_execution,
        runtime_if_bound=lambda: runtime,
        session=lambda _: SimpleNamespace(
            multiview_execution_intent=lambda intent_id, **_: intent_id
        ),
    )
    composition.report_multiview_execution = lambda *args: (
        AutonomyComposition.report_multiview_execution(composition, *args)
    )
    composition.report_multiview_lifecycle = lambda *args: (
        AutonomyComposition.report_multiview_lifecycle(composition, *args)
    )
    return composition


def test_late_navigation_completion_admits_capture_without_blocking_heartbeat(
    tmp_path, monkeypatch
):
    async def exercise():
        runtime = RelayRuntime(
            RelaySettings(relay_token=b"multiview-test-key-1234567890123456", log_dir=tmp_path)
        )
        runtime.loop = asyncio.get_running_loop()
        owner = AutonomySession.__new__(AutonomySession)
        owner.session_id = "s"
        owner._lock = threading.RLock()
        owner._awaiting = {}
        entered = threading.Event()
        admitted = threading.Event()

        def capture(_session_id, raw):
            entered.set()
            owner._publish(runtime, lambda: admitted.set() or [])
            return {
                "status": "accepted",
                "intentId": f"platform-capture:{raw['navigationIntentId']}",
            }

        navigation = _Navigation()
        service = MultiviewService(navigation, SimpleNamespace(confirm_platform_capture=capture))
        workflow_id, child = _confirm(service, navigation, "arrival")
        intent = SimpleNamespace(intent_id=child, name=IntentName.NAVIGATE)
        pending = _AwaitingExecution(
            _Job(intent, None), None, None, None, SimpleNamespace(status=LifecycleStatus.EXECUTING)
        )
        owner._awaiting[child] = pending
        owner._composition = _composition(service, runtime)
        token = _ResumeToken(child, pending, None)
        result = SimpleNamespace(status=LifecycleStatus.COMPLETED)
        session = SimpleNamespace(
            resume_io=lambda _: result,
            commit_resume=lambda work, outcome: list(
                owner.commit_resume(work, outcome).relay_events
            ),
            prepare_resume=lambda: None,
        )
        monkeypatch.setattr("relay.autonomy.apply_result", lambda *_: [])
        monkeypatch.setattr("relay.autonomy._PUBLISH_TIMEOUT_S", 5)
        resume = asyncio.create_task(runtime._resume_and_publish("s", session, token))
        assert await asyncio.to_thread(entered.wait, 1)
        heartbeat = asyncio.create_task(runtime._publish_control_heartbeats("s", session))
        await asyncio.wait_for(asyncio.gather(resume, heartbeat), timeout=1)
        assert await asyncio.to_thread(admitted.wait, 1)
        for _ in range(100):
            state = service.status("s", workflow_id)["views"][0]["state"]
            if state == "capturing":
                break
            await asyncio.sleep(0.01)
        assert state == "capturing"
        assert pending.job.finished
        assert child not in owner._awaiting

    asyncio.run(exercise())


@pytest.mark.parametrize("preempt_during_snapshot", [False, True])
def test_late_resume_can_retain_its_next_command_without_reentering_the_owner_lock(
    relay_session, clock, monkeypatch, preempt_during_snapshot
):
    owner = AutonomySession.__new__(AutonomySession)
    owner.session_id = relay_session.session_id
    owner._lock = threading.Lock()
    owner._awaiting = {}
    owner._operator_last_seen_ms = None
    owner._stop_requested = False
    owner._composition = SimpleNamespace(
        runtime_if_bound=lambda: SimpleNamespace(sessions={owner.session_id: relay_session}),
        config=SimpleNamespace(supervised_vertical=None, safety=None),
    )
    intent = SimpleNamespace(intent_id="late-next-command", name=IntentName.NAVIGATE)
    result = ExecutionResult(
        intent_id=intent.intent_id,
        roster_version=relay_session.registry.roster_version,
        status=LifecycleStatus.EXECUTING,
    )
    pending = _AwaitingExecution(
        _Job(intent, relay_session),
        relay_session,
        None,
        owner.current_snapshot(relay_session),
        result,
    )
    owner._awaiting[intent.intent_id] = pending
    token = _ResumeToken(intent.intent_id, pending, None)
    clock.value += 1
    if preempt_during_snapshot:
        current_snapshot = owner.current_snapshot

        def preempt(session):
            snapshot = current_snapshot(session)
            with owner._lock:
                pending.job.cancelled_by = "preempted_by_hold"
                owner._awaiting.pop(intent.intent_id)
            return snapshot

        monkeypatch.setattr(owner, "current_snapshot", preempt)
    monkeypatch.setattr("relay.autonomy.apply_result", lambda *_: [])
    finished = threading.Event()
    committed = []

    def commit():
        committed.append(owner.commit_resume(token, result))
        finished.set()

    worker = threading.Thread(target=commit, daemon=True)
    worker.start()
    assert finished.wait(1), "retaining the next command deadlocked on the owner lock"
    worker.join()
    if preempt_during_snapshot:
        assert committed == [None]
        assert intent.intent_id not in owner._awaiting
    else:
        assert pending.pending is result
        assert committed[0] is not None
        assert pending.snapshot.now_ms == clock()
        assert owner._awaiting[intent.intent_id] is pending
