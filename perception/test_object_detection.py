from __future__ import annotations

from collections import deque

import numpy as np
import pytest

from perception.object_detection import (
    DetectionCandidate,
    FrameIdentity,
    LiveDetectionWorker,
    ProcessedFrameEvent,
    SightingAggregator,
    SightingEvent,
    YoloXOnnxDetector,
)


def _candidate(
    *, confidence: float = 0.8, box: tuple[float, float, float, float] = (10, 10, 50, 50)
):
    return DetectionCandidate("backpack", 24, confidence, box)


class _Frames:
    def __init__(self, frames: list[tuple[np.ndarray, float] | None]) -> None:
        self.frames = deque(frames)
        self.timeouts: list[float] = []

    def read(self, timeout: float = 0.1) -> tuple[np.ndarray, float] | None:
        self.timeouts.append(timeout)
        return self.frames.popleft() if self.frames else None


class _Detector:
    def __init__(
        self, result: tuple[DetectionCandidate, ...] = (), error: Exception | None = None
    ) -> None:
        self.result = result
        self.error = error
        self.frames: list[np.ndarray] = []

    def detect(self, frame: np.ndarray) -> tuple[DetectionCandidate, ...]:
        self.frames.append(frame)
        if self.error is not None:
            raise self.error
        return self.result


def test_worker_reads_one_latest_frame_and_emits_processed_and_sighting_events() -> None:
    image = np.zeros((720, 1280, 3), dtype=np.uint8)
    stream = _Frames([(image, 10.0)])
    received = []
    worker = LiveDetectionWorker(
        stream,
        _Detector((_candidate(),)),
        source_id="drone1",
        mission_id="mission-7",
        on_event=received.append,
    )

    events = worker.poll(now=10.2)

    processed, sighting = events
    assert stream.timeouts == [0]
    assert isinstance(processed, ProcessedFrameEvent)
    assert processed.outcome == "detections"
    assert processed.candidate_count == 1
    assert processed.identity == FrameIdentity("drone1", "1", "mission-7")
    assert isinstance(sighting, SightingEvent)
    assert sighting.sighting_id == "mission-7:drone1:1"
    assert sighting.observation_count == 1
    assert received == list(events)
    assert [event.payload()["type"] for event in events] == [
        "perception.frame_processed",
        "perception.sighting",
    ]


def test_worker_drops_stale_frame_without_calling_detector() -> None:
    detector = _Detector((_candidate(),))
    worker = LiveDetectionWorker(
        _Frames([(np.zeros((2, 2, 3), dtype=np.uint8), 1.0)]),
        detector,
        source_id="drone1",
        mission_id="mission-7",
        max_frame_age_s=0.5,
    )

    events = worker.poll(now=1.6)

    assert len(events) == 1
    assert events[0].outcome == "dropped_stale"
    assert detector.frames == []


def test_worker_records_empty_and_detector_failure_as_processed_frame_outcomes() -> None:
    image = np.zeros((2, 2, 3), dtype=np.uint8)
    worker = LiveDetectionWorker(
        _Frames([(image, 1.0)]),
        _Detector(error=RuntimeError("model unavailable")),
        source_id="drone1",
        mission_id="mission-7",
    )

    assert worker.poll(now=1.1)[0].outcome == "detector_error"
    empty_worker = LiveDetectionWorker(
        _Frames([(image, 2.0)]), _Detector(), source_id="drone1", mission_id="mission-7"
    )
    assert empty_worker.poll(now=2.1)[0].outcome == "empty"


def test_worker_pauses_cleanly_when_the_stream_has_no_frame() -> None:
    detector = _Detector((_candidate(),))
    worker = LiveDetectionWorker(
        _Frames([None]), detector, source_id="drone1", mission_id="mission-7"
    )

    assert worker.poll(now=1.0) == ()
    assert worker.events() == ()
    assert detector.frames == []


def test_aggregator_updates_one_sighting_for_overlapping_candidates() -> None:
    aggregator = SightingAggregator(dedup_window_s=2, iou_threshold=0.5)
    first = aggregator.observe(FrameIdentity("drone1", "1", "mission-7"), 1, 1.1, _candidate())
    repeated = aggregator.observe(
        FrameIdentity("drone1", "2", "mission-7"),
        1.5,
        1.6,
        _candidate(confidence=0.9, box=(12, 12, 52, 52)),
    )
    distinct = aggregator.observe(
        FrameIdentity("drone1", "3", "mission-7"), 1.6, 1.7, _candidate(box=(100, 100, 150, 150))
    )

    assert repeated.sighting_id == first.sighting_id
    assert repeated.observation_count == 2
    assert repeated.candidate.confidence == 0.9
    assert distinct.sighting_id != first.sighting_id


def test_yolox_detector_decodes_and_filters_coco_predictions() -> None:
    predictions = np.zeros((1, 8400, 85), dtype=np.float32)
    predictions[0, 0, :5] = [10, 10, np.log(4), np.log(4), 0.9]
    predictions[0, 0, 29] = 0.9  # COCO class 24 (backpack) starts at column 5.
    predictions[0, 1, :5] = [10.2, 10.2, np.log(4), np.log(4), 0.8]
    predictions[0, 1, 29] = 0.9

    class Net:
        def __init__(self) -> None:
            self.blob = None

        def setInput(self, blob: np.ndarray) -> None:
            self.blob = blob

        def forward(self) -> np.ndarray:
            return predictions

    net = Net()
    detector = YoloXOnnxDetector("unused.onnx", net=net, confidence_threshold=0.6)
    candidates = detector.detect(np.zeros((320, 640, 3), dtype=np.uint8))

    assert net.blob.shape == (1, 3, 640, 640)
    assert len(candidates) == 1
    assert candidates[0].label == "backpack"
    assert candidates[0].confidence == pytest.approx(0.81)
    assert candidates[0].bbox_xyxy == pytest.approx((64, 64, 96, 96))


@pytest.mark.parametrize("frame", [None, np.zeros((3, 3), dtype=np.uint8), np.zeros((3, 3, 3))])
def test_yolox_detector_rejects_invalid_frames(frame: object) -> None:
    detector = YoloXOnnxDetector("unused.onnx", net=object())
    with pytest.raises(ValueError, match="frame"):
        detector.detect(frame)
