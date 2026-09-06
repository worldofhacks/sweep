from __future__ import annotations

from collections import deque
from dataclasses import replace

import numpy as np
import pytest

from perception.object_detection import DecodedFrame, DetectionCandidate, LiveDetectionWorker
from perception.search_events import CameraPolicy, FramePoseEvidence, SearchMissionIdentity
from planner.navigation import (
    ArtifactPin,
    DronePose,
    GridLevel,
    MotionConfig,
    NavigationArtifact,
    NavigationPermission,
    Pose,
    Zone,
    preview_evidence,
)
from planner.search import SearchArea, SearchDrone, SearchPlanner, SearchRefusal, SearchRequest

MOTION = MotionConfig(0.15, 0.2, 0.05, 0.03, 0.1, 0.05)
CAMERA = CameraPolicy(90, 90, 1, -90, -90, 0, 0.25)
MISSION = SearchMissionIdentity("search-7", 3, 2)
AREA = SearchArea("search-zone", "level_1", ((0, 0), (12, 0), (12, 4), (0, 4)))


def pose(x: float, y: float) -> Pose:
    return Pose(x, y, 1, "level_1")


def artifact(blocked: frozenset[tuple[int, int]] = frozenset()) -> NavigationArtifact:
    return NavigationArtifact(
        ArtifactPin("map-v3", "a" * 64),
        ArtifactPin("geometry-v3", "b" * 64),
        ArtifactPin("preview", "c" * 64),
        preview_evidence("synthetic"),
        0.5,
        ((0.0, 0.0), (14.0, 0.0), (14.0, 5.0), (0.0, 5.0), (0.0, 0.0)),
        0.0,
        3.0,
        (GridLevel("level_1", 1, (0, 0), 1, 14, 5, blocked),),
        (
            Zone(
                "search-zone",
                "level_1",
                True,
                ((0.0, 0.0), (14.0, 0.0), (14.0, 5.0), (0.0, 5.0), (0.0, 0.0)),
                0.0,
                3.0,
                (),
            ),
        ),
    )


def request(count: int, *, map_pin: ArtifactPin | None = None) -> SearchRequest:
    positions = tuple(
        DronePose(index, 7, pose(0.5 + (index - 1) % 2, 0.5 + ((index - 1) // 2) * 2))
        for index in range(1, count + 1)
    )
    return SearchRequest(
        MISSION,
        AREA,
        "backpack",
        12,
        12,
        tuple(SearchDrone(drone, f"drone{drone.drone_id}") for drone in positions),
        positions,
        map_pin or artifact().map_pin,
        "search-camera-v1",
        CAMERA,
        MOTION,
        NavigationPermission(frozenset({"search-zone"})),
        "operator-confirmation-9",
    )


def _components(cells: set[tuple[int, int]]) -> int:
    unvisited = set(cells)
    components = 0
    while unvisited:
        components += 1
        todo = [unvisited.pop()]
        while todo:
            x, y = todo.pop()
            for neighbor in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
                if neighbor in unvisited:
                    unvisited.remove(neighbor)
                    todo.append(neighbor)
    return components


@pytest.mark.parametrize("count", [1, 2, 4])
def test_planner_makes_balanced_contiguous_allocations_and_navigation_transits(count: int) -> None:
    preview = SearchPlanner().plan(request(count), artifact())

    assert not isinstance(preview, SearchRefusal)
    assert len(preview.assignments) == count
    workloads = [assignment.workload_cells for assignment in preview.assignments]
    assert max(workloads) - min(workloads) <= 1
    assert preview.execution_order == tuple(range(1, count + 1))
    for assignment in preview.assignments:
        cells = {(int(cell.pose.x_m), int(cell.pose.y_m)) for cell in assignment.task.cells}
        assert _components(cells) == 1
        assert assignment.transit.waypoints[-1] == assignment.task.cells[0].pose
        assert all(AREA.contains(cell.pose.x_m, cell.pose.y_m) for cell in assignment.task.cells)
        assert {cell.cell_id for lane in assignment.lanes for cell in lane.cells} == {
            cell.cell_id for cell in assignment.task.cells
        }
    assert preview.payload()["type"] == "perception.search_preview"


def test_search_refuses_disconnected_occupancy_and_changed_map() -> None:
    blocked = frozenset((6, y) for y in range(4))
    disconnected = SearchPlanner().plan(request(2), artifact(blocked))
    changed = SearchPlanner().plan(request(1, map_pin=ArtifactPin("old", "c" * 64)), artifact())

    assert disconnected == SearchRefusal(
        "area_disconnected", "search area has disconnected known free space"
    )
    assert changed == SearchRefusal(
        "map_changed", "search request map pin does not match navigation map"
    )


class _Frames:
    def __init__(self, timestamps: list[float], *, capture_time_verified: bool = True) -> None:
        self._frames = deque(
            DecodedFrame(
                np.zeros((2, 2, 3), dtype=np.uint8),
                captured_at_s=timestamp,
                received_at_s=timestamp,
                capture_time_verified=capture_time_verified,
            )
            for timestamp in timestamps
        )

    def read(self, timeout: float = 0.1):
        assert timeout == 0
        return self._frames.popleft() if self._frames else None


class _Detector:
    target_labels = ("backpack",)
    detector_config_sha256 = "a" * 64

    def __init__(self, candidates=()):
        self._candidates = candidates

    def detect(self, image: np.ndarray):
        assert image.shape == (2, 2, 3)
        return self._candidates


def _worker(
    source_id: str,
    timestamps: list[float],
    *,
    empty: bool = False,
    capture_time_verified: bool = True,
) -> LiveDetectionWorker:
    clock = [0.0]
    worker = LiveDetectionWorker(
        _Frames(timestamps, capture_time_verified=capture_time_verified),
        _Detector(() if empty else (DetectionCandidate("backpack", 24, 0.9, (0, 0, 1, 1)),)),
        source_id=source_id,
        mission_id=MISSION.frame_mission_id,
        max_frame_age_s=1,
        monotonic_clock=lambda: clock[0],
    )
    worker.test_clock = clock
    return worker


def _poll(worker: LiveDetectionWorker, now: float):
    worker.test_clock[0] = now
    return worker.poll()


def _evidence(event, cell, epoch: int, timestamp: float) -> FramePoseEvidence:
    return FramePoseEvidence(event.identity, epoch, cell.pose, timestamp, timestamp + 0.01)


def test_processed_frames_from_live_detector_complete_coverage_and_upsert_candidate() -> None:
    preview = SearchPlanner().plan(request(1), artifact())
    assert not isinstance(preview, SearchRefusal)
    ledger = preview.ledger()
    task = preview.assignments[0].task
    ledger.activate(task.task_id)
    worker = _worker(task.source_id, [10 + index / 10 for index in range(len(task.cells))])
    candidate_events = []

    for index, cell in enumerate(task.cells):
        processed, sighting = _poll(worker, 10.01 + index / 10)
        observation = ledger.observe_processed(
            processed,
            _evidence(processed, cell, task.connection_epoch, 10 + index / 10),
            10.02 + index / 10,
        )
        assert observation.accepted
        candidate_events.append(ledger.observe_sighting(sighting))

    assert ledger.progress(task.task_id) == (len(task.cells), len(task.cells))
    assert ledger.task_state(task.task_id) == "covered"
    assert candidate_events[0] is not None and candidate_events[0].updated is False
    assert candidate_events[-1] is not None and candidate_events[-1].updated is True
    assert len(ledger.candidates()) == 1


def test_stale_and_mismatched_processed_frames_cannot_claim_coverage() -> None:
    preview = SearchPlanner().plan(request(1), artifact())
    assert not isinstance(preview, SearchRefusal)
    ledger = preview.ledger()
    task = preview.assignments[0].task
    ledger.activate(task.task_id)
    worker = _worker(task.source_id, [1])
    processed, _ = _poll(worker, 1.01)
    evidence = _evidence(processed, task.cells[0], task.connection_epoch, 1)

    stale = ledger.observe_processed(processed, evidence, 2)
    other_worker = _worker("other-camera", [1])
    other_processed, _ = _poll(other_worker, 1.01)
    source_mismatch = ledger.observe_processed(
        other_processed,
        FramePoseEvidence(
            other_processed.identity, task.connection_epoch, task.cells[0].pose, 1, 1.01
        ),
        1.02,
    )
    old_mission_clock = [0.0]
    old_mission_worker = LiveDetectionWorker(
        _Frames([1]),
        _Detector(),
        source_id=task.source_id,
        mission_id="different-search",
        max_frame_age_s=1,
        monotonic_clock=lambda: old_mission_clock[0],
    )
    old_mission_worker.test_clock = old_mission_clock
    old_mission = _poll(old_mission_worker, 1.01)[0]
    mission_mismatch = ledger.observe_processed(
        old_mission,
        FramePoseEvidence(old_mission.identity, task.connection_epoch, task.cells[0].pose, 1, 1.01),
        1.02,
    )
    wrong_source = ledger.observe_processed(
        processed,
        FramePoseEvidence(
            type(processed.identity)(
                "other-camera",
                processed.identity.mission_id,
                processed.identity.worker_run_id,
                processed.identity.frame_sequence,
            ),
            task.connection_epoch,
            task.cells[0].pose,
            1,
            1.01,
        ),
        1.02,
    )
    reconnected = ledger.observe_processed(
        processed, _evidence(processed, task.cells[0], task.connection_epoch + 1, 1), 1.02
    )

    assert stale.reason == "stale_frame"
    assert source_mismatch.reason == "source_mismatch"
    assert mission_mismatch.reason == "mission_mismatch"
    assert wrong_source.reason == "pose_frame_mismatch"
    assert reconnected.reason == "connection_epoch_mismatch"
    assert ledger.progress(task.task_id) == (0, len(task.cells))


def test_sighting_needs_processed_frame_and_cancel_preserves_pending_coverage() -> None:
    preview = SearchPlanner().plan(request(2), artifact())
    assert not isinstance(preview, SearchRefusal)
    ledger = preview.ledger()
    active, pending = (assignment.task for assignment in preview.assignments)
    ledger.activate(active.task_id)
    worker = _worker(active.source_id, [1])
    processed, sighting = _poll(worker, 1.01)

    assert ledger.observe_sighting(sighting) is None
    assert ledger.observe_processed(
        processed, _evidence(processed, active.cells[0], active.connection_epoch, 1), 1.02
    ).accepted
    assert ledger.observe_sighting(sighting) is not None
    incomplete = ledger.mark_incomplete(active.task_id, "link_lost")
    assert incomplete.requires_fresh_confirmation
    cancel_events = ledger.cancel("operator_cancelled")
    assert {event.task_id for event in cancel_events} == {pending.task_id}
    assert ledger.task_state(pending.task_id) == "cancel"
    assert ledger.task_state(active.task_id) == "incomplete"


def test_fresh_empty_processed_frame_advances_coverage() -> None:
    preview = SearchPlanner().plan(request(1), artifact())
    assert not isinstance(preview, SearchRefusal)
    ledger = preview.ledger()
    task = preview.assignments[0].task
    ledger.activate(task.task_id)
    worker = _worker(task.source_id, [1], empty=True)
    (processed,) = _poll(worker, 1.01)

    observation = ledger.observe_processed(
        processed, _evidence(processed, task.cells[0], task.connection_epoch, 1), 1.02
    )

    assert processed.outcome == "empty"
    assert observation.accepted
    assert observation.newly_covered_cell_ids
    assert (
        ledger.observe_processed(
            processed, _evidence(processed, task.cells[-1], task.connection_epoch, 1), 1.02
        ).reason
        == "duplicate_frame"
    )


def test_unverified_capture_time_cannot_advance_coverage() -> None:
    preview = SearchPlanner().plan(request(1), artifact())
    assert not isinstance(preview, SearchRefusal)
    ledger = preview.ledger()
    task = preview.assignments[0].task
    ledger.activate(task.task_id)
    worker = _worker(task.source_id, [1], capture_time_verified=False)
    processed, _ = _poll(worker, 1.01)

    observation = ledger.observe_processed(
        processed, _evidence(processed, task.cells[0], task.connection_epoch, 1), 1.02
    )

    assert processed.capture_time_verified is False
    assert observation.accepted is False
    assert observation.reason == "capture_time_unverified"
    assert ledger.progress(task.task_id)[0] == 0


def test_camera_policy_uses_yaw_independent_inscribed_footprint() -> None:
    camera = CameraPolicy(120, 60, 2, -90, -90, 0, 0.25)
    camera_pose = pose(0, 0)
    outside = pose(camera.conservative_footprint_side_m / 2 + 0.01, 0)

    assert camera.lane_spacing_m == pytest.approx(
        camera.conservative_footprint_side_m * (1 - camera.overlap_fraction)
    )
    assert not camera.covers(camera_pose, outside)


def test_same_floor_coverage_uses_the_grid_nearest_the_camera_height() -> None:
    levels = (
        GridLevel("level_1", 0.2, (0, 0), 1, 14, 5, frozenset()),
        GridLevel("level_1", 1.0, (0, 0), 1, 14, 5, frozenset()),
    )
    preview = SearchPlanner().plan(request(1), replace(artifact(), grids=levels))

    assert not isinstance(preview, SearchRefusal)
    assert {cell.pose.z_m for cell in preview.assignments[0].task.cells} == {1.0}


def test_coverage_requires_requested_class_and_keeps_detector_configuration_per_source() -> None:
    preview = SearchPlanner().plan(request(1), artifact())
    assert not isinstance(preview, SearchRefusal)
    ledger = preview.ledger()
    task = preview.assignments[0].task
    ledger.activate(task.task_id)
    worker = _worker(task.source_id, [1, 1.1], empty=True)
    (processed,) = _poll(worker, 1.01)
    evidence = _evidence(processed, task.cells[0], task.connection_epoch, 1)

    unrelated = ledger.observe_processed(
        replace(processed, target_labels=("person",)), evidence, 1.02
    )
    assert unrelated.reason == "target_class_mismatch"
    assert ledger.progress(task.task_id)[0] == 0
    assert ledger.observe_processed(processed, evidence, 1.02).accepted
    covered_before = ledger.progress(task.task_id)
    (next_frame,) = _poll(worker, 1.11)
    changed = ledger.observe_processed(
        replace(next_frame, detector_config_sha256="b" * 64),
        _evidence(next_frame, task.cells[-1], task.connection_epoch, 1.1),
        1.12,
    )
    assert changed.reason == "detector_configuration_changed"
    assert ledger.progress(task.task_id) == covered_before


def test_sighting_requires_requested_class_and_accepted_frame_detector_digest() -> None:
    preview = SearchPlanner().plan(request(1), artifact())
    assert not isinstance(preview, SearchRefusal)
    ledger = preview.ledger()
    task = preview.assignments[0].task
    ledger.activate(task.task_id)
    processed, sighting = _poll(_worker(task.source_id, [1]), 1.01)
    assert ledger.observe_processed(
        processed, _evidence(processed, task.cells[0], task.connection_epoch, 1), 1.02
    ).accepted

    assert ledger.observe_sighting(replace(sighting, detector_config_sha256="b" * 64)) is None
    assert (
        ledger.observe_sighting(
            replace(sighting, candidate=replace(sighting.candidate, label="person", class_id=0))
        )
        is None
    )
    assert ledger.candidates() == ()
    assert ledger.observe_sighting(sighting) is not None


@pytest.mark.parametrize("changed", [replace(MISSION, version=4), replace(MISSION, epoch=3)])
def test_changed_mission_version_or_epoch_cannot_reuse_processed_frames(changed) -> None:
    preview = SearchPlanner().plan(request(1), artifact())
    assert not isinstance(preview, SearchRefusal)
    task = preview.assignments[0].task
    processed, _ = _poll(_worker(task.source_id, [1]), 1.01)
    ledger = replace(preview, mission=changed).ledger()
    ledger.activate(task.task_id)

    observation = ledger.observe_processed(
        processed, _evidence(processed, task.cells[0], task.connection_epoch, 1), 1.02
    )

    assert observation.reason == "mission_mismatch"
    assert ledger.progress(task.task_id)[0] == 0
