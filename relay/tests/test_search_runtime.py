from __future__ import annotations

from perception.search_events import CameraPolicy
from planner.models import Plan, Refusal
from planner.navigation import ArtifactPin, NavigationPermission
from planner.search import SearchArea
from planner.test_navigation_runtime import _runtime, _snapshot
from relay.intent_v1 import IntentName
from relay.search_runtime import SearchMissionPreview, SearchRuntime, SearchRuntimeConfig
from tests.autonomy_fixtures import make_intent


def _search_runtime(*, map_pin: ArtifactPin | None = None) -> SearchRuntime:
    navigation = _runtime()
    artifact = navigation.artifact()
    return SearchRuntime(
        SearchRuntimeConfig(
            {"atrium": SearchArea("atrium", "level_1", ((0, 0), (8, 0), (8, 4), (0, 4)))},
            artifact.map_pin if map_pin is None else map_pin,
            CameraPolicy(90, 90, 1, -90, -90, 0, 0.25),
            "camera-calibration-v1",
            {1: "camera-1"},
            NavigationPermission(frozenset({"atrium"})),
        ),
        navigation,
    )


def _intent(intent_id: str = "search-runtime"):
    return make_intent(
        IntentName.NAVIGATE,
        selection=(1,),
        args={"zone_id": "atrium", "target_class": "backpack"},
        confirm=True,
        intent_id=intent_id,
    )


def test_search_prepare_pins_a_transit_plan_and_start_marks_it_running() -> None:
    runtime = _search_runtime()
    preview = runtime.prepare(_intent(), _snapshot())

    assert isinstance(preview, SearchMissionPreview)
    assert isinstance(preview.plan, Plan)
    assert preview.plan.navigation is not None
    assert preview.plan.navigation.route.map_pin == preview.search.map_pin
    assert runtime.start("search-runtime").state == "running"


def test_search_prepare_refuses_changed_configuration_map_and_duplicate_intent() -> None:
    snapshot = _snapshot()
    changed = _search_runtime(map_pin=ArtifactPin("other-map", "c" * 64)).prepare(
        _intent(), snapshot
    )
    runtime = _search_runtime()

    assert isinstance(changed, Refusal)
    assert "map pin changed" in changed.detail
    assert isinstance(runtime.prepare(_intent(), snapshot), SearchMissionPreview)
    duplicate = runtime.prepare(_intent(), snapshot)
    assert isinstance(duplicate, Refusal)
    assert "already has a mission" in duplicate.detail


def test_search_executes_frozen_coverage_route_then_accepts_bounded_worker_frames() -> None:
    from collections import deque
    from dataclasses import replace

    import numpy as np

    from perception.object_detection import (
        DEFAULT_TARGET_LABELS,
        ProcessedFrameEvent,
    )
    from perception.search_events import FramePoseEvidence
    from planner.models import LifecycleStatus
    from tests.autonomy_fixtures import make_stack

    class Frames:
        def __init__(self):
            self.frames = deque([(np.zeros((8, 8, 3), dtype=np.uint8), 10.0)])

        def read(self, _timeout: float):
            return self.frames.popleft() if self.frames else None

    class Detector:
        target_labels = DEFAULT_TARGET_LABELS
        detector_config_sha256 = "a" * 64

        def detect(self, _frame):
            return ()

    runtime = _search_runtime()
    snapshot = _snapshot()
    preview = runtime.prepare(_intent(), snapshot)
    assert isinstance(preview, SearchMissionPreview)
    _, _, _, dispatcher, flight, _ = make_stack(snapshot)
    dispatcher.navigation = runtime.navigation
    clock = [snapshot.now_ms]

    def current():
        clock[0] += 1
        aircraft = {
            drone_id: replace(
                snapshot.aircraft[drone_id],
                pose=drone.pose,
                flight_state=drone.flight_state,
                position_last_seen_ms=clock[0],
            )
            for drone_id, drone in flight.aircraft.items()
        }
        return replace(snapshot, now_ms=clock[0], aircraft=aircraft)

    result = runtime.execute("search-runtime", dispatcher, snapshot, current_snapshot=current)
    assert result.status is LifecycleStatus.COMPLETED, result.refusal
    assert runtime.status("search-runtime").state == "running"
    assert any(call.operation.value == "goto" for call in flight.calls)

    task = preview.search.assignments[0].task
    now = [10.02]

    def pose_for(event: ProcessedFrameEvent):
        return FramePoseEvidence(
            event.identity, task.connection_epoch, task.cells[0].pose, 10.0, 10.01
        )

    worker = runtime.detection_worker(
        "search-runtime",
        1,
        Frames(),
        Detector(),
        pose_for,
        now_s=lambda: now[0],
        worker_run_id="run-1",
    )
    events = worker.poll()
    processed = events[0]
    assert isinstance(processed, ProcessedFrameEvent)
    accepted = runtime.observe_processed_frame(
        "search-runtime",
        processed,
        pose_for(processed),
        now_s=10.02,
    )
    assert not accepted.accepted
    assert accepted.reason == "task_not_active" or accepted.reason == "duplicate_frame"
