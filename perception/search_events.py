"""Fresh-frame coverage and candidate state for a confirmed visual search."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from perception.object_detection import (
    DetectionCandidate,
    FrameIdentity,
    ProcessedFrameEvent,
    SightingEvent,
)
from planner.navigation import Pose


def _identifier(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")


def _finite(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    return float(value)


def _nonnegative(value: float, name: str) -> float:
    value = _finite(value, name)
    if value < 0:
        raise ValueError(f"{name} must be nonnegative")
    return value


@dataclass(frozen=True, slots=True)
class SearchMissionIdentity:
    mission_id: str
    version: int
    epoch: int

    def __post_init__(self) -> None:
        _identifier(self.mission_id, "mission_id")
        if (
            isinstance(self.version, bool)
            or not isinstance(self.version, int)
            or self.version < 1
            or isinstance(self.epoch, bool)
            or not isinstance(self.epoch, int)
            or self.epoch < 0
        ):
            raise ValueError("search mission version and epoch are invalid")

    @property
    def frame_mission_id(self) -> str:
        return f"{self.mission_id}:v{self.version}:e{self.epoch}"

    def payload(self) -> dict[str, object]:
        return {
            "mission_id": self.mission_id,
            "mission_version": self.version,
            "mission_epoch": self.epoch,
        }


@dataclass(frozen=True, slots=True)
class CameraPolicy:
    horizontal_fov_deg: float
    vertical_fov_deg: float
    height_agl_m: float
    gimbal_pitch_deg: float
    gimbal_min_pitch_deg: float
    gimbal_max_pitch_deg: float
    overlap_fraction: float

    def __post_init__(self) -> None:
        for name in ("horizontal_fov_deg", "vertical_fov_deg", "height_agl_m", "overlap_fraction"):
            _nonnegative(getattr(self, name), name)
        for name in ("gimbal_pitch_deg", "gimbal_min_pitch_deg", "gimbal_max_pitch_deg"):
            _finite(getattr(self, name), name)
        if not 0 < self.horizontal_fov_deg < 180 or not 0 < self.vertical_fov_deg < 180:
            raise ValueError("camera FOV must be between zero and 180 degrees")
        if self.height_agl_m <= 0 or not 0 <= self.overlap_fraction < 1:
            raise ValueError("camera height and overlap are invalid")
        if not self.gimbal_min_pitch_deg <= self.gimbal_pitch_deg <= self.gimbal_max_pitch_deg:
            raise ValueError("gimbal pitch is outside its measured limits")
        if self.gimbal_pitch_deg != -90:
            raise ValueError("coverage footprint requires a nadir gimbal pitch of -90 degrees")

    @property
    def footprint_width_m(self) -> float:
        return 2 * self.height_agl_m * math.tan(math.radians(self.horizontal_fov_deg / 2))

    @property
    def footprint_depth_m(self) -> float:
        return 2 * self.height_agl_m * math.tan(math.radians(self.vertical_fov_deg / 2))

    @property
    def conservative_footprint_side_m(self) -> float:
        return min(self.footprint_width_m, self.footprint_depth_m) / math.sqrt(2)

    @property
    def lane_spacing_m(self) -> float:
        return self.conservative_footprint_side_m * (1 - self.overlap_fraction)

    def covers(self, camera_pose: Pose, target_pose: Pose) -> bool:
        if camera_pose.floor_id != target_pose.floor_id:
            return False
        return (
            abs(camera_pose.x_m - target_pose.x_m) <= self.conservative_footprint_side_m / 2
            and abs(camera_pose.y_m - target_pose.y_m) <= self.conservative_footprint_side_m / 2
        )

    def payload(self) -> dict[str, float]:
        return {
            "horizontal_fov_deg": self.horizontal_fov_deg,
            "vertical_fov_deg": self.vertical_fov_deg,
            "height_agl_m": self.height_agl_m,
            "gimbal_pitch_deg": self.gimbal_pitch_deg,
            "gimbal_min_pitch_deg": self.gimbal_min_pitch_deg,
            "gimbal_max_pitch_deg": self.gimbal_max_pitch_deg,
            "overlap_fraction": self.overlap_fraction,
            "footprint_width_m": self.footprint_width_m,
            "footprint_depth_m": self.footprint_depth_m,
            "conservative_footprint_side_m": self.conservative_footprint_side_m,
            "lane_spacing_m": self.lane_spacing_m,
        }


@dataclass(frozen=True, slots=True)
class CoverageCell:
    cell_id: str
    pose: Pose

    def __post_init__(self) -> None:
        _identifier(self.cell_id, "cell_id")


@dataclass(frozen=True, slots=True)
class CoverageTask:
    task_id: str
    source_id: str
    connection_epoch: int
    cells: tuple[CoverageCell, ...]

    def __post_init__(self) -> None:
        _identifier(self.task_id, "task_id")
        _identifier(self.source_id, "source_id")
        if (
            isinstance(self.connection_epoch, bool)
            or not isinstance(self.connection_epoch, int)
            or self.connection_epoch < 0
        ):
            raise ValueError("connection_epoch must be a nonnegative integer")
        if not self.cells or len({cell.cell_id for cell in self.cells}) != len(self.cells):
            raise ValueError("coverage task must contain uniquely identified cells")


@dataclass(frozen=True, slots=True)
class FramePoseEvidence:
    identity: FrameIdentity
    connection_epoch: int
    pose: Pose
    pose_timestamp_s: float
    observed_at_s: float

    def __post_init__(self) -> None:
        if (
            isinstance(self.connection_epoch, bool)
            or not isinstance(self.connection_epoch, int)
            or self.connection_epoch < 0
        ):
            raise ValueError("connection_epoch must be a nonnegative integer")
        _nonnegative(self.pose_timestamp_s, "pose_timestamp_s")
        _nonnegative(self.observed_at_s, "observed_at_s")


TaskState = Literal["pending", "active", "covered", "incomplete", "hold", "cancel"]


@dataclass(frozen=True, slots=True)
class SearchTaskEvent:
    task_id: str
    state: TaskState
    reason: str
    requires_fresh_confirmation: bool = False

    def payload(self) -> dict[str, object]:
        return {
            "type": "perception.search_task",
            "task_id": self.task_id,
            "state": self.state,
            "reason": self.reason,
            "requires_fresh_confirmation": self.requires_fresh_confirmation,
        }


@dataclass(frozen=True, slots=True)
class CoverageObservation:
    accepted: bool
    reason: str
    newly_covered_cell_ids: tuple[str, ...]
    task_event: SearchTaskEvent | None = None

    def payload(self) -> dict[str, object]:
        return {
            "type": "perception.search_coverage",
            "accepted": self.accepted,
            "reason": self.reason,
            "newly_covered_cell_ids": list(self.newly_covered_cell_ids),
            "task": None if self.task_event is None else self.task_event.payload(),
        }


@dataclass(frozen=True, slots=True)
class SearchCandidateEvent:
    sighting_id: str
    source_id: str
    observation_count: int
    candidate: DetectionCandidate
    updated: bool

    def payload(self) -> dict[str, object]:
        return {
            "type": "perception.search_candidate",
            "sighting_id": self.sighting_id,
            "source_id": self.source_id,
            "observation_count": self.observation_count,
            "updated": self.updated,
            **self.candidate.payload(),
        }


class CoverageLedger:
    """Coverage changes only when a fresh processed frame has matching pose evidence."""

    def __init__(
        self,
        mission: SearchMissionIdentity,
        camera: CameraPolicy,
        tasks: tuple[CoverageTask, ...],
        *,
        max_frame_age_s: float = 0.5,
        max_pose_age_s: float = 0.5,
        max_pose_skew_s: float = 0.2,
    ) -> None:
        if not tasks or len({task.task_id for task in tasks}) != len(tasks):
            raise ValueError("coverage tasks must be nonempty and unique")
        if len({task.source_id for task in tasks}) != len(tasks):
            raise ValueError("each coverage task needs a distinct source")
        for name, value in (
            ("max_frame_age_s", max_frame_age_s),
            ("max_pose_age_s", max_pose_age_s),
            ("max_pose_skew_s", max_pose_skew_s),
        ):
            if _nonnegative(value, name) <= 0:
                raise ValueError(f"{name} must be positive")
        self._mission = mission
        self._camera = camera
        self._tasks = {task.task_id: task for task in tasks}
        self._task_for_source = {task.source_id: task.task_id for task in tasks}
        self._states: dict[str, TaskState] = {task.task_id: "pending" for task in tasks}
        self._covered: dict[str, set[str]] = {task.task_id: set() for task in tasks}
        self._accepted_frames: set[tuple[str, str, str]] = set()
        self._candidates: dict[str, SearchCandidateEvent] = {}
        self._max_frame_age_s = max_frame_age_s
        self._max_pose_age_s = max_pose_age_s
        self._max_pose_skew_s = max_pose_skew_s

    def task_state(self, task_id: str) -> TaskState:
        return self._states[task_id]

    def activate(self, task_id: str) -> SearchTaskEvent:
        if self._states[task_id] != "pending":
            raise ValueError("only pending coverage tasks can activate")
        self._states[task_id] = "active"
        return SearchTaskEvent(task_id, "active", "route_ready")

    def hold(self, reason: str) -> tuple[SearchTaskEvent, ...]:
        return self._transition_nonterminal("hold", reason)

    def cancel(self, reason: str) -> tuple[SearchTaskEvent, ...]:
        return self._transition_nonterminal("cancel", reason)

    def mark_incomplete(self, task_id: str, reason: str) -> SearchTaskEvent:
        if self._states[task_id] not in {"pending", "active", "hold"}:
            raise ValueError("only unfinished coverage tasks can become incomplete")
        self._states[task_id] = "incomplete"
        return SearchTaskEvent(task_id, "incomplete", reason, requires_fresh_confirmation=True)

    def observe_processed(
        self, event: ProcessedFrameEvent, pose: FramePoseEvidence, now_s: float
    ) -> CoverageObservation:
        now_s = _nonnegative(now_s, "now_s")
        task_id = self._task_for_source.get(event.identity.source_id)
        if task_id is None:
            return CoverageObservation(False, "source_mismatch", ())
        task = self._tasks[task_id]
        if event.identity.mission_id != self._mission.frame_mission_id:
            return CoverageObservation(False, "mission_mismatch", ())
        if self._states[task_id] != "active":
            return CoverageObservation(False, "task_not_active", ())
        if event.outcome not in {"detections", "empty"}:
            return CoverageObservation(False, "processed_outcome_not_covering", ())
        if not event.capture_time_verified:
            return CoverageObservation(False, "capture_time_unverified", ())
        if (
            now_s - event.evaluation_completed_at_monotonic_s > self._max_frame_age_s
            or event.evaluation_completed_at_monotonic_s < event.frame_decoded_at_monotonic_s
        ):
            return CoverageObservation(False, "stale_frame", ())
        if pose.identity != event.identity:
            return CoverageObservation(False, "pose_frame_mismatch", ())
        if pose.connection_epoch != task.connection_epoch:
            return CoverageObservation(False, "connection_epoch_mismatch", ())
        if (
            now_s - pose.observed_at_s > self._max_pose_age_s
            or abs(pose.pose_timestamp_s - event.frame_decoded_at_monotonic_s)
            > self._max_pose_skew_s
        ):
            return CoverageObservation(False, "stale_pose", ())
        frame_key = (event.identity.source_id, event.identity.frame_id, event.identity.mission_id)
        if frame_key in self._accepted_frames:
            return CoverageObservation(False, "duplicate_frame", ())
        covered = self._covered[task_id]
        newly_covered = tuple(
            cell.cell_id
            for cell in task.cells
            if cell.cell_id not in covered and self._camera.covers(pose.pose, cell.pose)
        )
        covered.update(newly_covered)
        self._accepted_frames.add(frame_key)
        task_event = None
        if len(covered) == len(task.cells):
            self._states[task_id] = "covered"
            task_event = SearchTaskEvent(task_id, "covered", "footprint_coverage_complete")
        return CoverageObservation(True, "accepted", newly_covered, task_event)

    def observe_sighting(self, event: SightingEvent) -> SearchCandidateEvent | None:
        task_id = self._task_for_source.get(event.identity.source_id)
        if (
            task_id is None
            or event.identity.mission_id != self._mission.frame_mission_id
            or (event.identity.source_id, event.identity.frame_id, event.identity.mission_id)
            not in self._accepted_frames
        ):
            return None
        previous = self._candidates.get(event.sighting_id)
        candidate = SearchCandidateEvent(
            event.sighting_id,
            event.identity.source_id,
            event.observation_count,
            event.candidate,
            updated=previous is not None,
        )
        self._candidates[event.sighting_id] = candidate
        return candidate

    def progress(self, task_id: str) -> tuple[int, int]:
        return len(self._covered[task_id]), len(self._tasks[task_id].cells)

    def candidates(self) -> tuple[SearchCandidateEvent, ...]:
        return tuple(sorted(self._candidates.values(), key=lambda item: item.sighting_id))

    def _transition_nonterminal(self, state: TaskState, reason: str) -> tuple[SearchTaskEvent, ...]:
        events = []
        for task_id, current in self._states.items():
            if current in {"pending", "active", "hold"}:
                self._states[task_id] = state
                events.append(SearchTaskEvent(task_id, state, reason))
        return tuple(events)
