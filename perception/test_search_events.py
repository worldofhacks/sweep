from __future__ import annotations

from collections import deque
from dataclasses import replace

import numpy as np

from perception.object_detection import (
    DEFAULT_TARGET_LABELS,
    LiveDetectionWorker,
    ProcessedFrameEvent,
)
from perception.search_events import (
    CameraPolicy,
    CoverageCell,
    CoverageLedger,
    CoverageTask,
    FramePoseEvidence,
    SearchMissionIdentity,
)
from planner.navigation import Pose


class _Frames:
    def __init__(self, frames: list[tuple[np.ndarray, float]]) -> None:
        self._frames = deque(frames)

    def read(self, _timeout: float) -> tuple[np.ndarray, float] | None:
        return self._frames.popleft() if self._frames else None


class _EmptyDetector:
    target_labels = DEFAULT_TARGET_LABELS
    detector_config_sha256 = "a" * 64

    def detect(self, _image: np.ndarray) -> tuple[()]:
        return ()


class _Clock:
    def __init__(self, values: list[float]) -> None:
        self._values = deque(values)

    def __call__(self) -> float:
        if len(self._values) > 1:
            return self._values.popleft()
        return self._values[0]


def _ledger(*, target_x_m: float = 1, **kwargs: object) -> CoverageLedger:
    mission = SearchMissionIdentity("search-1", 1, 2)
    camera = CameraPolicy(90, 90, 2, -90, -90, 0, 0.25)
    task = CoverageTask(
        "task-1",
        "drone1",
        3,
        (CoverageCell("cell-1", Pose(target_x_m, 1, 0, "floor-1")),),
    )
    ledger = CoverageLedger(mission, camera, (task,), **kwargs)
    ledger.activate(task.task_id)
    return ledger


def test_live_detector_event_advances_coverage_to_a_terminal_task() -> None:
    ledger = _ledger()
    worker = LiveDetectionWorker(
        _Frames([(np.zeros((8, 8, 3), dtype=np.uint8), 10.0)]),
        _EmptyDetector(),
        source_id="drone1",
        mission_id="search-1.v1.e2",
        worker_run_id="run-1",
        monotonic_clock=_Clock([10.0, 10.01]),
    )

    (event,) = worker.poll()
    assert isinstance(event, ProcessedFrameEvent)
    observation = ledger.observe_processed(
        event,
        FramePoseEvidence(event.identity, 3, Pose(1, 1, 0, "floor-1"), 10.0, 10.01),
        10.02,
    )

    assert observation.accepted
    assert observation.newly_covered_cell_ids == ("cell-1",)
    assert observation.task_event is not None
    assert observation.task_event.state == "covered"


def test_coverage_refuses_a_fresh_frame_when_pose_provenance_does_not_match() -> None:
    ledger = _ledger()
    worker = LiveDetectionWorker(
        _Frames([(np.zeros((8, 8, 3), dtype=np.uint8), 10.0)]),
        _EmptyDetector(),
        source_id="drone1",
        mission_id="search-1.v1.e2",
        worker_run_id="run-1",
        monotonic_clock=_Clock([10.0, 10.01]),
    )
    (event,) = worker.poll()
    assert isinstance(event, ProcessedFrameEvent)

    observation = ledger.observe_processed(
        event,
        FramePoseEvidence(event.identity, 4, Pose(1, 1, 0, "floor-1"), 10.0, 10.01),
        10.02,
    )

    assert not observation.accepted
    assert observation.reason == "connection_epoch_mismatch"


def _worker_event() -> ProcessedFrameEvent:
    worker = LiveDetectionWorker(
        _Frames([(np.zeros((8, 8, 3), dtype=np.uint8), 10.0)]),
        _EmptyDetector(),
        source_id="drone1",
        mission_id="search-1.v1.e2",
        worker_run_id="run-1",
        monotonic_clock=_Clock([10.0, 10.01]),
    )
    (event,) = worker.poll()
    assert isinstance(event, ProcessedFrameEvent)
    return event


def test_coverage_uses_decoded_frame_time_and_rejects_future_receipts() -> None:
    ledger = _ledger(max_frame_age_s=0.5, max_pose_age_s=0.5)
    event = _worker_event()
    old_decoded = replace(
        event,
        frame_decoded_at_monotonic_s=1.0,
        evaluation_started_at_monotonic_s=10.0,
        evaluation_completed_at_monotonic_s=10.01,
    )
    future_completion = replace(event, evaluation_completed_at_monotonic_s=10.03)
    future_pose = FramePoseEvidence(event.identity, 3, Pose(1, 1, 0, "floor-1"), 10.0, 10.03)
    fresh_pose = FramePoseEvidence(event.identity, 3, Pose(1, 1, 0, "floor-1"), 10.0, 10.01)

    assert ledger.observe_processed(old_decoded, fresh_pose, 10.02).reason == "stale_frame"
    assert ledger.observe_processed(future_completion, fresh_pose, 10.02).reason == "stale_frame"
    assert ledger.observe_processed(event, future_pose, 10.02).reason == "stale_pose"
    assert ledger.observe_processed(event, fresh_pose, 10.02).accepted


def test_rejected_mission_and_outcome_do_not_change_coverage_receipts() -> None:
    ledger = _ledger()
    event = _worker_event()
    pose = FramePoseEvidence(event.identity, 3, Pose(1, 1, 0, "floor-1"), 10.0, 10.01)
    wrong_mission = replace(event, identity=replace(event.identity, mission_id="other-search"))
    refused_outcome = replace(event, outcome="dropped_stale")

    assert ledger.observe_processed(wrong_mission, pose, 10.02).reason == "mission_mismatch"
    assert (
        ledger.observe_processed(refused_outcome, pose, 10.02).reason
        == "processed_outcome_not_covering"
    )
    assert ledger.progress("task-1") == (0, 1)
    assert ledger.observe_processed(event, pose, 10.02).accepted


def test_accepted_frame_receipts_are_bounded() -> None:
    ledger = _ledger(target_x_m=100, max_accepted_frame_receipts=1)
    first = _worker_event()
    second = replace(first, identity=replace(first.identity, frame_sequence=2))
    first_pose = FramePoseEvidence(first.identity, 3, Pose(1, 1, 0, "floor-1"), 10.0, 10.01)
    second_pose = FramePoseEvidence(second.identity, 3, Pose(1, 1, 0, "floor-1"), 10.0, 10.01)

    assert ledger.observe_processed(first, first_pose, 10.02).accepted
    assert ledger.observe_processed(second, second_pose, 10.02).accepted
    assert ledger.observe_processed(first, first_pose, 10.02).reason == "duplicate_frame"
    assert len(ledger._accepted_frames) == 1


def test_coverage_binds_a_source_to_its_first_worker_run_after_receipt_eviction() -> None:
    ledger = _ledger(target_x_m=3, max_accepted_frame_receipts=1)
    first = _worker_event()
    pose = FramePoseEvidence(first.identity, 3, Pose(1, 1, 0, "floor-1"), 10.0, 10.01)
    assert ledger.observe_processed(first, pose, 10.02).accepted

    second = replace(
        first, identity=replace(first.identity, worker_run_id="run-2", frame_sequence=1)
    )
    second_pose = replace(pose, identity=second.identity)
    rejected = ledger.observe_processed(second, second_pose, 10.02)

    assert not rejected.accepted
    assert rejected.reason == "duplicate_frame"
