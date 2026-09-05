"""Bounded COCO object detections from the latest decoded webcam frame."""

from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Sequence

import numpy as np

from perception.detection_contracts import (
    COCO_LABELS,
    DEFAULT_TARGET_LABELS,
    FRAME_CLOCK_DOMAIN,
    FRAME_TIME_PROVENANCE,
    DetectionCandidate,
    Detector,
    FrameIdentity,
    FrameOutcome,
    FrameReader,
    PerceptionEvent,
    ProcessedFrameEvent,
    SightingEvent,
    _finite_nonnegative,
    _finite_positive,
    _identity_component,
    _sha256_digest,
    _target_labels,
)
from perception.sighting_aggregation import MAX_ACTIVE_SIGHTINGS, SightingAggregator
from perception.yolox_onnx import (
    MAX_MODEL_BYTES,
    YOLOX_S_ONNX_SHA256,
    YOLOX_S_ONNX_URL,
    YoloXOnnxDetector,
)

MAX_RETAINED_EVENTS = 4096

__all__ = (
    "COCO_LABELS",
    "DEFAULT_TARGET_LABELS",
    "FRAME_CLOCK_DOMAIN",
    "FRAME_TIME_PROVENANCE",
    "MAX_ACTIVE_SIGHTINGS",
    "MAX_MODEL_BYTES",
    "MAX_RETAINED_EVENTS",
    "YOLOX_S_ONNX_SHA256",
    "YOLOX_S_ONNX_URL",
    "DetectionCandidate",
    "Detector",
    "FrameIdentity",
    "FrameOutcome",
    "FrameReader",
    "LiveDetectionWorker",
    "PerceptionEvent",
    "ProcessedFrameEvent",
    "SightingAggregator",
    "SightingEvent",
    "YoloXOnnxDetector",
)


class LiveDetectionWorker:
    """Samples the latest frame only and emits bounded observation events."""

    def __init__(
        self,
        stream: FrameReader,
        detector: Detector,
        *,
        source_id: str,
        mission_id: str,
        worker_run_id: str | None = None,
        on_event: Callable[[PerceptionEvent], None] | None = None,
        max_frame_age_s: float = 0.5,
        sample_interval_s: float = 0.1,
        retained_events: int = 512,
        aggregator: SightingAggregator | None = None,
        monotonic_clock: Callable[[], float] | None = None,
    ) -> None:
        _identity_component(source_id, "source_id")
        _identity_component(mission_id, "mission_id")
        if worker_run_id is None:
            worker_run_id = uuid.uuid4().hex
        _identity_component(worker_run_id, "worker_run_id")
        try:
            target_labels = _target_labels(detector.target_labels, "detector target_labels")
            detector_config_sha256 = detector.detector_config_sha256
        except AttributeError:
            raise ValueError(
                "detector must declare target_labels and detector_config_sha256"
            ) from None
        _sha256_digest(detector_config_sha256, "detector_config_sha256")
        if on_event is not None and not callable(on_event):
            raise ValueError("on_event must be callable")
        if monotonic_clock is not None and not callable(monotonic_clock):
            raise ValueError("monotonic_clock must be callable")
        _finite_positive(max_frame_age_s, "max_frame_age_s")
        _finite_positive(sample_interval_s, "sample_interval_s")
        if (
            isinstance(retained_events, bool)
            or not isinstance(retained_events, int)
            or not 1 <= retained_events <= MAX_RETAINED_EVENTS
        ):
            raise ValueError("invalid live detection worker limits")
        self._stream = stream
        self._detector = detector
        self._source_id = source_id
        self._mission_id = mission_id
        self._worker_run_id = worker_run_id
        self._target_labels = target_labels
        self._detector_config_sha256 = detector_config_sha256
        self._on_event = on_event
        self._max_frame_age_s = max_frame_age_s
        self._sample_interval_s = sample_interval_s
        self._events: deque[PerceptionEvent] = deque(maxlen=retained_events)
        self._aggregator = SightingAggregator() if aggregator is None else aggregator
        self._monotonic_clock = time.monotonic if monotonic_clock is None else monotonic_clock
        self._frame_sequence = 0
        self._last_frame_decoded_at_monotonic_s: float | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._poll_lock = threading.Lock()
        self._events_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._failure_lock = threading.Lock()
        self._failure_reason: str | None = None

    def poll(self) -> tuple[PerceptionEvent, ...]:
        with self._poll_lock:
            frame = self._stream.read(0)
            if frame is None:
                return ()
            evaluation_started_at_monotonic_s = self._now()
            image, frame_decoded_at_monotonic_s = frame
            _finite_nonnegative(frame_decoded_at_monotonic_s, "frame decoded time")
            self._frame_sequence += 1
            identity = FrameIdentity(
                source_id=self._source_id,
                mission_id=self._mission_id,
                worker_run_id=self._worker_run_id,
                frame_sequence=self._frame_sequence,
            )

            def processed(
                outcome: FrameOutcome,
                candidate_count: int = 0,
                completed_at: float = evaluation_started_at_monotonic_s,
            ) -> ProcessedFrameEvent:
                return self._processed_event(
                    identity,
                    frame_decoded_at_monotonic_s,
                    evaluation_started_at_monotonic_s,
                    completed_at,
                    outcome,
                    candidate_count,
                )

            if frame_decoded_at_monotonic_s > evaluation_started_at_monotonic_s:
                events = (processed("dropped_future"),)
            elif (
                self._last_frame_decoded_at_monotonic_s is not None
                and frame_decoded_at_monotonic_s < self._last_frame_decoded_at_monotonic_s
            ):
                events = (processed("dropped_regressed"),)
            elif (
                evaluation_started_at_monotonic_s - frame_decoded_at_monotonic_s
                > self._max_frame_age_s
            ):
                self._last_frame_decoded_at_monotonic_s = frame_decoded_at_monotonic_s
                events = (processed("dropped_stale"),)
            elif not self._valid_frame(image):
                self._last_frame_decoded_at_monotonic_s = frame_decoded_at_monotonic_s
                events = (processed("invalid_frame"),)
            else:
                self._last_frame_decoded_at_monotonic_s = frame_decoded_at_monotonic_s
                try:
                    raw_candidates = self._detector.detect(image)
                    if not isinstance(raw_candidates, Sequence) or len(raw_candidates) > 256:
                        raise ValueError("detector must return at most 256 candidates")
                    candidates = tuple(raw_candidates)
                    self._validate_candidates(image, candidates)
                except Exception:
                    evaluation_completed_at_monotonic_s = self._now()
                    events = (
                        processed(
                            "detector_error", completed_at=evaluation_completed_at_monotonic_s
                        ),
                    )
                else:
                    evaluation_completed_at_monotonic_s = self._now()
                    if (
                        evaluation_completed_at_monotonic_s - frame_decoded_at_monotonic_s
                        > self._max_frame_age_s
                    ):
                        events = (
                            processed(
                                "dropped_stale",
                                completed_at=evaluation_completed_at_monotonic_s,
                            ),
                        )
                    else:
                        try:
                            sightings, evaluation_completed_at_monotonic_s, stale = (
                                self._aggregator.observe_frame(
                                    identity,
                                    frame_decoded_at_monotonic_s,
                                    evaluation_started_at_monotonic_s,
                                    candidates,
                                    self._detector_config_sha256,
                                    completion_clock=self._now,
                                    max_frame_age_s=self._max_frame_age_s,
                                )
                            )
                        except Exception:
                            self._set_failure("aggregation_failed")
                            evaluation_completed_at_monotonic_s = self._now()
                            events = (
                                processed(
                                    "aggregation_error",
                                    len(candidates),
                                    evaluation_completed_at_monotonic_s,
                                ),
                            )
                        else:
                            if stale:
                                events = (
                                    processed(
                                        "dropped_stale",
                                        completed_at=evaluation_completed_at_monotonic_s,
                                    ),
                                )
                            else:
                                events = (
                                    processed(
                                        "detections" if candidates else "empty",
                                        len(candidates),
                                        evaluation_completed_at_monotonic_s,
                                    ),
                                    *sightings,
                                )
            self._publish(events)
            return events

    def _publish(self, events: tuple[PerceptionEvent, ...]) -> None:
        with self._events_lock:
            self._events.extend(events)
        for event in events:
            if self._on_event is not None:
                try:
                    self._on_event(event)
                except Exception:
                    self._set_failure("event_callback_failed")
                    raise

    def _processed_event(
        self,
        identity: FrameIdentity,
        frame_decoded_at_monotonic_s: float,
        evaluation_started_at_monotonic_s: float,
        evaluation_completed_at_monotonic_s: float,
        outcome: FrameOutcome,
        candidate_count: int,
    ) -> ProcessedFrameEvent:
        return ProcessedFrameEvent(
            identity=identity,
            frame_decoded_at_monotonic_s=frame_decoded_at_monotonic_s,
            evaluation_started_at_monotonic_s=evaluation_started_at_monotonic_s,
            evaluation_completed_at_monotonic_s=evaluation_completed_at_monotonic_s,
            outcome=outcome,
            candidate_count=candidate_count,
            target_labels=self._target_labels,
            detector_config_sha256=self._detector_config_sha256,
        )

    def _validate_candidates(
        self, frame: np.ndarray, candidates: tuple[DetectionCandidate, ...]
    ) -> None:
        for candidate in candidates:
            if not isinstance(candidate, DetectionCandidate):
                raise ValueError("detector must return DetectionCandidate values")
            if candidate.label not in self._target_labels:
                raise ValueError("detector returned a label outside target_labels")
            if candidate.bbox_xyxy[2] > frame.shape[1] or candidate.bbox_xyxy[3] > frame.shape[0]:
                raise ValueError("detector bounding box exceeds the frame")

    def _now(self) -> float:
        value = self._monotonic_clock()
        _finite_nonnegative(value, "monotonic clock value")
        return value

    @staticmethod
    def _valid_frame(frame: object) -> bool:
        return (
            isinstance(frame, np.ndarray)
            and frame.ndim == 3
            and frame.shape[2] == 3
            and frame.dtype == np.uint8
            and frame.shape[0] > 0
            and frame.shape[1] > 0
        )

    def start(self) -> LiveDetectionWorker:
        with self._lifecycle_lock:
            if self._thread is not None:
                raise RuntimeError("live detection worker is already running")
            with self._failure_lock:
                self._failure_reason = None
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="live-detection", daemon=True)
            try:
                self._thread.start()
            except Exception:
                self._thread = None
                self._stop.set()
                raise RuntimeError("cannot start live detection worker") from None
        return self

    def close(self) -> None:
        with self._lifecycle_lock:
            self._stop.set()
            if self._thread is not None:
                self._thread.join(self._sample_interval_s + 0.2)
                if not self._thread.is_alive():
                    self._thread = None
                else:
                    self._set_failure("shutdown_timeout")

    def events(self) -> tuple[PerceptionEvent, ...]:
        with self._events_lock:
            return tuple(self._events)

    @property
    def failure_reason(self) -> str | None:
        with self._failure_lock:
            return self._failure_reason

    def _set_failure(self, reason: str) -> None:
        with self._failure_lock:
            if self._failure_reason is None:
                self._failure_reason = reason

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll()
            except Exception:
                self._set_failure("poll_failed")
                self._stop.set()
                return
            if self.failure_reason is not None:
                self._stop.set()
                return
            self._stop.wait(self._sample_interval_s)
