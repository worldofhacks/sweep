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
        IntentName.SEARCH,
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
    assert runtime.status("search-runtime").state == "incomplete"
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


def test_search_counts_real_worker_frames_during_frozen_route_execution() -> None:
    from collections import deque
    from dataclasses import replace

    import numpy as np

    from perception.object_detection import DEFAULT_TARGET_LABELS, DetectionCandidate
    from perception.search_events import FramePoseEvidence
    from perception.search_localization import SearchCameraModel
    from planner.models import LifecycleStatus
    from planner.navigation import Pose
    from tests.autonomy_fixtures import make_stack

    class Frames:
        frames = deque()

        def read(self, _timeout):
            return self.frames.popleft() if self.frames else None

    class Detector:
        target_labels = DEFAULT_TARGET_LABELS
        detector_config_sha256 = "a" * 64

        def detect(self, _frame):
            return (DetectionCandidate("backpack", 24, 0.9, (3, 3, 5, 4)),)

    runtime = _search_runtime()
    snapshot = _snapshot()
    preview = runtime.prepare(_intent(), snapshot)
    assert isinstance(preview, SearchMissionPreview)
    _, _, _, dispatcher, flight, _ = make_stack(snapshot)
    dispatcher.navigation = runtime.navigation
    frames = Frames()
    clock = [snapshot.now_ms]
    observed_pose = [snapshot.aircraft[1].pose]
    covered_during_flight = []
    task = preview.search.assignments[0].task

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

    def pose_for(event):
        pose = observed_pose[0]
        return FramePoseEvidence(
            event.identity,
            task.connection_epoch,
            Pose(pose.x, pose.y, pose.z, "level_1"),
            clock[0] / 1000,
            clock[0] / 1000,
        )

    def camera_for(_event):
        pose = observed_pose[0]
        return 8, SearchCameraModel(
            ((4, 0, 4), (0, 4, 4), (0, 0, 1)),
            ((1, 0, 0, pose.x), (0, -1, 0, pose.y), (0, 0, -1, pose.z), (0, 0, 0, 1)),
        )

    worker = runtime.detection_worker(
        "search-runtime",
        1,
        frames,
        Detector(),
        pose_for,
        now_s=lambda: clock[0] / 1000,
        worker_run_id="moving-search",
        camera_for_frame=camera_for,
    )

    def process_arrival(_plan, _command, arrived):
        observed_pose[0] = arrived.aircraft[1].pose
        for _ in range(5):
            clock[0] += 1
            frames.frames.append((np.zeros((8, 8, 3), dtype=np.uint8), clock[0] / 1000))
            worker.poll()
        covered_during_flight.append(
            runtime.status_payload("search-runtime")["tasks"][0]["covered_cells"]
        )

    dispatcher.on_navigation_command_completed = process_arrival
    result = runtime.execute("search-runtime", dispatcher, snapshot, current_snapshot=current)

    assert result.status is LifecycleStatus.COMPLETED, result.refusal
    assert any(count > 0 for count in covered_during_flight[:-1])
    assert runtime.status("search-runtime").state == "covered"
    assert dispatcher.on_navigation_command_completed is process_arrival

    payload = runtime.status_payload("search-runtime")
    candidates = payload["candidates"]
    assert candidates
    candidate = candidates[0]
    assert candidate["label"] == "backpack"
    assert candidate["confidence"] == 0.9
    assert candidate["bbox_xyxy"] == (3, 3, 5, 4)
    assert candidate["position"]["zone_id"] == "atrium"
    assert candidate["position"]["floor_id"] == "level_1"
    assert candidate["frame"]["worker_run_id"] == "moving-search"
    assert candidate["observation_count"] >= 5
    assert payload["tasks"][0]["cells"]
    assert payload["tasks"][0]["covered_cell_ids"]
    count = len(flight.calls)
    assert runtime.acknowledge_finding("search-runtime", candidate["sighting_id"])
    assert runtime.status_payload("search-runtime")["candidates"][0]["acknowledged"]
    assert len(flight.calls) == count


def test_search_preview_lease_binds_the_full_intent_and_expires() -> None:
    from dataclasses import replace

    runtime = _search_runtime()
    intent = _intent("leased-search")
    preview = runtime.prepare(intent, _snapshot())
    assert isinstance(preview, SearchMissionPreview)
    expires = runtime.preview_expires_at_ms(intent.intent_id)
    assert runtime.accepts_intent(intent, expires)
    assert not runtime.accepts_intent(replace(intent, retry_of="prior"), expires)
    assert not runtime.accepts_intent(replace(intent, selection=(2,)), expires)
    assert not runtime.accepts_intent(intent, expires + 1)
