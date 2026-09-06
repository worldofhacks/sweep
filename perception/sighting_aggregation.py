"""Transactional, bounded aggregation of per-frame detection candidates."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, replace

from perception.detection_contracts import (
    DetectionCandidate,
    FrameIdentity,
    SightingEvent,
    _finite_nonnegative,
    _finite_positive,
    _identity_token,
    _iou,
    _probability,
    _sha256_digest,
)

MAX_ACTIVE_SIGHTINGS = 4096


@dataclass(slots=True)
class _Aggregate:
    sighting_id: str
    identity: FrameIdentity
    first_timestamp_s: float
    last_timestamp_s: float
    candidate: DetectionCandidate
    detector_config_sha256: str
    observation_count: int = 1


class SightingAggregator:
    """Keeps a bounded per-stream, per-mission IoU deduplication window."""

    def __init__(
        self,
        *,
        dedup_window_s: float = 2.0,
        iou_threshold: float = 0.5,
        max_sightings: int = 256,
    ) -> None:
        _finite_positive(dedup_window_s, "dedup_window_s")
        _probability(iou_threshold, "iou_threshold")
        if (
            isinstance(max_sightings, bool)
            or not isinstance(max_sightings, int)
            or not 1 <= max_sightings <= MAX_ACTIVE_SIGHTINGS
        ):
            raise ValueError("invalid sighting aggregation limits")
        self._dedup_window_s = dedup_window_s
        self._iou_threshold = iou_threshold
        self._max_sightings = max_sightings
        self._sightings: list[_Aggregate] = []
        self._next_id = 0
        self._lock = threading.Lock()

    def observe_frame(
        self,
        identity: FrameIdentity,
        frame_decoded_at_monotonic_s: float,
        evaluation_started_at_monotonic_s: float,
        candidates: tuple[DetectionCandidate, ...],
        detector_config_sha256: str,
        *,
        completion_clock: Callable[[], float],
        max_frame_age_s: float,
    ) -> tuple[tuple[SightingEvent, ...], float, bool]:
        """Atomically aggregate one frame, rolling back if completion is stale."""
        if not isinstance(identity, FrameIdentity):
            raise ValueError("identity must be a FrameIdentity")
        if not isinstance(candidates, tuple) or len(candidates) > 256:
            raise ValueError("candidates must contain at most 256 DetectionCandidate values")
        if any(not isinstance(candidate, DetectionCandidate) for candidate in candidates):
            raise ValueError("candidates must contain only DetectionCandidate values")
        _sha256_digest(detector_config_sha256, "detector_config_sha256")
        _finite_nonnegative(frame_decoded_at_monotonic_s, "frame_decoded_at_monotonic_s")
        _finite_nonnegative(evaluation_started_at_monotonic_s, "evaluation_started_at_monotonic_s")
        _finite_positive(max_frame_age_s, "max_frame_age_s")
        if evaluation_started_at_monotonic_s < frame_decoded_at_monotonic_s:
            raise ValueError("evaluation start must not precede decoded time")
        if not callable(completion_clock):
            raise ValueError("completion_clock must be callable")

        with self._lock:
            previous_sightings = [replace(sighting) for sighting in self._sightings]
            previous_next_id = self._next_id
            try:
                provisional = tuple(
                    self._observe(
                        identity,
                        frame_decoded_at_monotonic_s,
                        evaluation_started_at_monotonic_s,
                        evaluation_started_at_monotonic_s,
                        candidate,
                        detector_config_sha256,
                    )
                    for candidate in candidates
                )
                evaluation_completed_at_monotonic_s = completion_clock()
                _finite_nonnegative(
                    evaluation_completed_at_monotonic_s,
                    "evaluation_completed_at_monotonic_s",
                )
                if evaluation_completed_at_monotonic_s < evaluation_started_at_monotonic_s:
                    raise ValueError("evaluation completion must not precede its start")
                stale = (
                    evaluation_completed_at_monotonic_s - frame_decoded_at_monotonic_s
                    > max_frame_age_s
                )
                if stale:
                    self._sightings = previous_sightings
                    self._next_id = previous_next_id
                    return (), evaluation_completed_at_monotonic_s, True
                sightings = tuple(
                    replace(
                        sighting,
                        evaluation_completed_at_monotonic_s=(evaluation_completed_at_monotonic_s),
                    )
                    for sighting in provisional
                )
            except Exception:
                self._sightings = previous_sightings
                self._next_id = previous_next_id
                raise
        return sightings, evaluation_completed_at_monotonic_s, False

    def _observe(
        self,
        identity: FrameIdentity,
        frame_decoded_at_monotonic_s: float,
        evaluation_started_at_monotonic_s: float,
        evaluation_completed_at_monotonic_s: float,
        candidate: DetectionCandidate,
        detector_config_sha256: str,
    ) -> SightingEvent:
        same_stream_times = (
            sighting.last_timestamp_s
            for sighting in self._sightings
            if sighting.identity.mission_id == identity.mission_id
            and sighting.identity.source_id == identity.source_id
            and sighting.identity.worker_run_id == identity.worker_run_id
        )
        previous_time = max(same_stream_times, default=None)
        if previous_time is not None and frame_decoded_at_monotonic_s < previous_time:
            raise ValueError("frame decoded times must not regress within a worker run")
        self._sightings = [
            sighting
            for sighting in self._sightings
            if frame_decoded_at_monotonic_s - sighting.last_timestamp_s <= self._dedup_window_s
        ]
        matching = [
            sighting
            for sighting in self._sightings
            if sighting.identity.source_id == identity.source_id
            and sighting.identity.mission_id == identity.mission_id
            and sighting.identity.worker_run_id == identity.worker_run_id
            and sighting.detector_config_sha256 == detector_config_sha256
            and sighting.candidate.class_id == candidate.class_id
            and _iou(sighting.candidate.bbox_xyxy, candidate.bbox_xyxy) >= self._iou_threshold
        ]
        if matching:
            sighting = max(matching, key=lambda item: item.last_timestamp_s)
            sighting.last_timestamp_s = frame_decoded_at_monotonic_s
            sighting.identity = identity
            sighting.observation_count += 1
            sighting.candidate = candidate
        else:
            self._next_id += 1
            sighting = _Aggregate(
                sighting_id=(
                    f"sighting:{_identity_token(identity.mission_id)}:"
                    f"{_identity_token(identity.source_id)}:"
                    f"{_identity_token(identity.worker_run_id)}:{self._next_id}"
                ),
                identity=identity,
                first_timestamp_s=frame_decoded_at_monotonic_s,
                last_timestamp_s=frame_decoded_at_monotonic_s,
                candidate=candidate,
                detector_config_sha256=detector_config_sha256,
            )
            self._sightings.append(sighting)
            if len(self._sightings) > self._max_sightings:
                self._sightings.sort(key=lambda item: item.last_timestamp_s, reverse=True)
                del self._sightings[self._max_sightings :]
        return SightingEvent(
            sighting_id=sighting.sighting_id,
            identity=identity,
            first_frame_decoded_at_monotonic_s=sighting.first_timestamp_s,
            last_frame_decoded_at_monotonic_s=sighting.last_timestamp_s,
            evaluation_started_at_monotonic_s=evaluation_started_at_monotonic_s,
            evaluation_completed_at_monotonic_s=evaluation_completed_at_monotonic_s,
            candidate=sighting.candidate,
            observation_count=sighting.observation_count,
            detector_config_sha256=sighting.detector_config_sha256,
        )
