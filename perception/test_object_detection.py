from __future__ import annotations

import hashlib
from collections import deque
from collections.abc import Collection
from pathlib import Path

import numpy as np
import pytest

from perception.object_detection import (
    DEFAULT_TARGET_LABELS,
    DecodedFrame,
    DetectionCandidate,
    FrameIdentity,
    LiveDetectionWorker,
    ProcessedFrameEvent,
    SightingAggregator,
    SightingEvent,
    YoloXOnnxDetector,
)

_CANONICAL_DEFAULT_LABELS = ("person", "backpack", "suitcase", "bottle")
_TEST_MODEL_SHA256 = "a" * 64
_TEST_CONFIG_SHA256 = "b" * 64


def _image() -> np.ndarray:
    return np.zeros((64, 64, 3), dtype=np.uint8)


def _candidate(
    *,
    label: str = "backpack",
    confidence: float = 0.8,
    box: tuple[float, float, float, float] = (10, 10, 50, 50),
) -> DetectionCandidate:
    return DetectionCandidate(label, 0 if label == "person" else 24, confidence, box)


def _identity(sequence: int, *, run_id: str = "run-1") -> FrameIdentity:
    return FrameIdentity("drone1", "mission-7", run_id, sequence)


def _aggregate(
    aggregator: SightingAggregator,
    identity: FrameIdentity,
    decoded_at: float,
    started_at: float,
    completed_at: float,
    candidate: DetectionCandidate,
    config_sha256: str = _TEST_CONFIG_SHA256,
) -> SightingEvent:
    events, actual_completion, stale = aggregator.observe_frame(
        identity,
        decoded_at,
        started_at,
        (candidate,),
        config_sha256,
        completion_clock=_Clock(completed_at),
        max_frame_age_s=60,
    )
    assert actual_completion == completed_at
    assert not stale
    return events[0]


class _Frames:
    def __init__(self, frames: list[tuple[np.ndarray, float] | None]) -> None:
        self.frames = deque(frames)
        self.timeouts: list[float] = []
        self.read_called = False

    def read(self, timeout: float = 0.1) -> tuple[np.ndarray, float] | None:
        self.read_called = True
        self.timeouts.append(timeout)
        return self.frames.popleft() if self.frames else None


class _Clock:
    def __init__(self, *values: float) -> None:
        self.values = deque(values)

    def __call__(self) -> float:
        if len(self.values) > 1:
            return self.values.popleft()
        return self.values[0]


class _Detector:
    def __init__(
        self,
        result: tuple[object, ...] = (),
        error: Exception | None = None,
        *,
        target_labels: Collection[str] = DEFAULT_TARGET_LABELS,
    ) -> None:
        selected = frozenset(target_labels)
        self.target_labels = tuple(
            label for label in _CANONICAL_DEFAULT_LABELS if label in selected
        )
        self.detector_config_sha256 = _TEST_CONFIG_SHA256
        self.result = result
        self.error = error
        self.frames: list[np.ndarray] = []

    def detect(self, frame: np.ndarray) -> tuple[object, ...]:
        self.frames.append(frame)
        if self.error is not None:
            raise self.error
        return self.result


def test_worker_emits_explicit_identity_timing_and_target_configuration() -> None:
    image = _image()
    stream = _Frames([(image, 10.0)])
    received = []
    worker = LiveDetectionWorker(
        stream,
        _Detector((_candidate(),)),
        source_id="drone1",
        mission_id="mission-7",
        worker_run_id="run-1",
        on_event=received.append,
        monotonic_clock=_Clock(10.2),
    )

    events = worker.poll()

    processed, sighting = events
    assert stream.timeouts == [0]
    assert isinstance(processed, ProcessedFrameEvent)
    assert processed.outcome == "detections"
    assert processed.candidate_count == 1
    assert processed.target_labels == _CANONICAL_DEFAULT_LABELS
    assert processed.identity == _identity(1)
    assert isinstance(sighting, SightingEvent)
    assert sighting.sighting_id == "sighting:mission-7:drone1:run-1:1"
    assert sighting.observation_count == 1
    assert received == list(events)
    assert [event.payload()["type"] for event in events] == [
        "perception.frame_processed",
        "perception.sighting",
    ]
    payload = processed.payload()
    assert payload["frame_decoded_at_monotonic_s"] == 10.0
    assert payload["evaluation_started_at_monotonic_s"] == 10.2
    assert payload["evaluation_completed_at_monotonic_s"] == 10.2
    assert payload["clock_domain"] == "host_monotonic"
    assert payload["frame_time_provenance"] == "decoder_completion"
    assert payload["target_labels"] == list(_CANONICAL_DEFAULT_LABELS)
    assert payload["detector_config_sha256"] == _TEST_CONFIG_SHA256
    assert "capture" not in " ".join(payload)
    assert sighting.payload()["detector_config_sha256"] == _TEST_CONFIG_SHA256


def test_worker_uses_decoder_receipt_time_for_unverified_compatibility_frames() -> None:
    detector = _Detector()
    worker = LiveDetectionWorker(
        _Frames([DecodedFrame(_image(), 1.0, 9.0, False)]),
        detector,
        source_id="drone1",
        mission_id="mission-7",
        max_frame_age_s=0.5,
    )

    event = worker.poll(now=10.0)[0]

    assert event.outcome == "dropped_stale"
    assert event.received_at_s == 9.0
    assert event.payload()["received_at_s"] == 9.0
    assert detector.frames == []


def test_legacy_event_construction_keeps_the_publisher_contract() -> None:
    event = ProcessedFrameEvent(
        FrameIdentity("camera-1", "frame-1", "mission-7:v1:e7"),
        10.0,
        10.1,
        "empty",
        0,
    )

    assert event.event_id == "processed:mission-7:v1:e7:camera-1:frame-1"
    assert event.frame_timestamp_s == 10.0
    assert event.processed_at_s == 10.1
    assert event.received_at_s is None
    assert event.capture_time_verified is False
    assert FrameIdentity(
        source_id="camera-1", frame_id="frame-1", mission_id="mission-7:v1:e7"
    ) == (event.identity)


def test_worker_accepts_the_legacy_clock_and_poll_override() -> None:
    worker = LiveDetectionWorker(
        _Frames([(_image(), 10.0), (_image(), 10.1)]),
        _Detector(),
        source_id="drone1",
        mission_id="mission-7",
        clock=_Clock(10.2),
    )

    assert worker.poll()[0].processed_at_s == 10.2
    assert worker.poll(10.3)[0].processed_at_s == 10.3


def test_default_worker_run_ids_prevent_identity_collisions_across_restarts() -> None:
    def make_events() -> tuple[ProcessedFrameEvent | SightingEvent, ...]:
        worker = LiveDetectionWorker(
            _Frames([(_image(), 1.0)]),
            _Detector((_candidate(),)),
            source_id="drone1",
            mission_id="mission-7",
            monotonic_clock=_Clock(1.1),
        )
        return worker.poll()

    first = make_events()
    restarted = make_events()

    assert first[0].identity.worker_run_id != restarted[0].identity.worker_run_id
    assert first[0].identity.frame_id != restarted[0].identity.frame_id
    assert first[0].event_id != restarted[0].event_id
    assert first[1].sighting_id != restarted[1].sighting_id
    assert first[1].event_id != restarted[1].event_id


def test_ids_include_source_and_mission_when_an_injected_epoch_is_reused() -> None:
    def make_events(source_id: str, mission_id: str):
        worker = LiveDetectionWorker(
            _Frames([(_image(), 1.0)]),
            _Detector((_candidate(),)),
            source_id=source_id,
            mission_id=mission_id,
            worker_run_id="injected-run",
            monotonic_clock=_Clock(1.1),
        )
        return worker.poll()

    events = [
        make_events("drone1", "mission-1"),
        make_events("drone2", "mission-1"),
        make_events("drone1", "mission-2"),
    ]

    assert len({batch[0].identity.frame_id for batch in events}) == 3
    assert len({batch[0].event_id for batch in events}) == 3
    assert len({batch[1].sighting_id for batch in events}) == 3
    assert len({batch[1].event_id for batch in events}) == 3


def test_worker_drops_future_and_regressed_times_without_running_detector() -> None:
    detector = _Detector((_candidate(),))
    worker = LiveDetectionWorker(
        _Frames([(_image(), 1.0), (_image(), 0.9)]),
        detector,
        source_id="drone1",
        mission_id="mission-7",
        worker_run_id="run-1",
        monotonic_clock=_Clock(1.0, 1.0),
    )

    assert worker.poll()[0].outcome == "detections"
    assert worker.poll()[0].outcome == "dropped_regressed"
    future_worker = LiveDetectionWorker(
        _Frames([(_image(), 2.0)]),
        detector,
        source_id="drone1",
        mission_id="mission-7",
        worker_run_id="run-2",
        monotonic_clock=_Clock(1.9),
    )
    assert future_worker.poll()[0].outcome == "dropped_future"


def test_worker_does_not_let_dropped_frames_advance_its_regression_watermark() -> None:
    detector = _Detector((_candidate(),))
    worker = LiveDetectionWorker(
        _Frames([(_image(), 1.0), (_image(), 0.9)]),
        detector,
        source_id="drone1",
        mission_id="mission-7",
        worker_run_id="run-1",
        max_frame_age_s=0.5,
        monotonic_clock=_Clock(1.6, 1.0),
    )

    assert worker.poll()[0].outcome == "dropped_stale"
    assert worker.poll()[0].outcome == "detections"
    assert len(detector.frames) == 1


def test_worker_does_not_advance_the_watermark_after_slow_inference_becomes_stale() -> None:
    worker = LiveDetectionWorker(
        _Frames([(_image(), 10.0), (_image(), 9.9)]),
        _Detector(),
        source_id="drone1",
        mission_id="mission-7",
        worker_run_id="run-1",
        max_frame_age_s=0.5,
        monotonic_clock=_Clock(10.1, 10.7, 10.0),
    )

    assert worker.poll()[0].outcome == "dropped_stale"
    assert worker.poll()[0].outcome == "empty"


def test_runtime_clock_is_sampled_after_reading_the_latest_frame() -> None:
    stream = _Frames([(_image(), 10.0)])
    worker = LiveDetectionWorker(
        stream,
        _Detector(),
        source_id="drone1",
        mission_id="mission-7",
        worker_run_id="run-1",
        monotonic_clock=lambda: 10.1 if stream.read_called else 9.9,
    )

    assert worker.poll()[0].outcome == "empty"


def test_slow_inference_rechecks_freshness_before_emitting_a_sighting() -> None:
    detector = _Detector((_candidate(),))
    worker = LiveDetectionWorker(
        _Frames([(_image(), 10.0), (_image(), 10.8)]),
        detector,
        source_id="drone1",
        mission_id="mission-7",
        worker_run_id="run-1",
        max_frame_age_s=0.5,
        monotonic_clock=_Clock(10.1, 10.7, 10.9),
    )

    stale_events = worker.poll()
    fresh_events = worker.poll()

    assert len(stale_events) == 1
    assert stale_events[0].outcome == "dropped_stale"
    assert stale_events[0].evaluation_started_at_monotonic_s == 10.1
    assert stale_events[0].evaluation_completed_at_monotonic_s == 10.7
    assert isinstance(fresh_events[1], SightingEvent)
    assert fresh_events[1].observation_count == 1


def test_slow_aggregation_rolls_back_before_emitting_a_stale_sighting() -> None:
    worker = LiveDetectionWorker(
        _Frames([(_image(), 10.0), (_image(), 10.8)]),
        _Detector((_candidate(),)),
        source_id="drone1",
        mission_id="mission-7",
        worker_run_id="run-1",
        max_frame_age_s=0.5,
        monotonic_clock=_Clock(10.1, 10.2, 10.7, 10.9),
    )

    stale_events = worker.poll()
    fresh_events = worker.poll()

    assert len(stale_events) == 1
    assert stale_events[0].outcome == "dropped_stale"
    assert stale_events[0].evaluation_completed_at_monotonic_s == 10.7
    assert isinstance(fresh_events[1], SightingEvent)
    assert fresh_events[1].observation_count == 1


def test_worker_records_empty_and_detector_failure_as_processed_frame_outcomes() -> None:
    worker = LiveDetectionWorker(
        _Frames([(_image(), 1.0)]),
        _Detector(error=RuntimeError("model unavailable")),
        source_id="drone1",
        mission_id="mission-7",
        worker_run_id="run-1",
        monotonic_clock=_Clock(1.1),
    )
    assert worker.poll()[0].outcome == "detector_error"

    empty_worker = LiveDetectionWorker(
        _Frames([(_image(), 2.0)]),
        _Detector(),
        source_id="drone1",
        mission_id="mission-7",
        worker_run_id="run-2",
        monotonic_clock=_Clock(2.1),
    )
    assert empty_worker.poll()[0].outcome == "empty"


def test_worker_records_an_invalid_frame_without_running_the_detector() -> None:
    detector = _Detector((_candidate(),))
    worker = LiveDetectionWorker(
        _Frames([(np.zeros((3, 3), dtype=np.uint8), 1.0)]),
        detector,
        source_id="drone1",
        mission_id="mission-7",
        worker_run_id="run-1",
        monotonic_clock=_Clock(1.1),
    )

    assert worker.poll()[0].outcome == "invalid_frame"
    assert detector.frames == []


def test_worker_pauses_cleanly_when_the_stream_has_no_frame() -> None:
    detector = _Detector((_candidate(),))
    worker = LiveDetectionWorker(
        _Frames([None]),
        detector,
        source_id="drone1",
        mission_id="mission-7",
        worker_run_id="run-1",
    )

    assert worker.poll() == ()
    assert worker.events() == ()
    assert detector.frames == []


def test_callback_failure_stops_background_work_after_retaining_the_event_batch() -> None:
    def fail(_: object) -> None:
        raise RuntimeError("sink unavailable")

    worker = LiveDetectionWorker(
        _Frames([(_image(), 1.0)]),
        _Detector((_candidate(),)),
        source_id="drone1",
        mission_id="mission-7",
        worker_run_id="run-1",
        on_event=fail,
        monotonic_clock=_Clock(1.1),
    )

    worker.start()
    assert worker._thread is not None
    worker._thread.join(1)

    assert not worker._thread.is_alive()
    assert len(worker.events()) == 2
    assert worker.failure_reason == "event_callback_failed"
    worker.close()


def test_aggregator_keeps_current_candidate_with_current_frame_identity() -> None:
    aggregator = SightingAggregator(dedup_window_s=2, iou_threshold=0.5)
    first_candidate = _candidate(confidence=0.95)
    current_candidate = _candidate(confidence=0.7, box=(12, 12, 52, 52))
    first = _aggregate(aggregator, _identity(1), 1, 1.1, 1.1, first_candidate)
    repeated = _aggregate(aggregator, _identity(2), 1.5, 1.6, 1.6, current_candidate)
    distinct = _aggregate(
        aggregator,
        _identity(3),
        1.6,
        1.7,
        1.7,
        _candidate(box=(1, 1, 5, 5)),
    )

    assert repeated.sighting_id == first.sighting_id
    assert repeated.identity == _identity(2)
    assert repeated.candidate == current_candidate
    assert repeated.observation_count == 2
    assert distinct.sighting_id != first.sighting_id


def test_aggregator_rejects_regressed_time_without_mutating_the_sighting() -> None:
    aggregator = SightingAggregator()
    first = _aggregate(aggregator, _identity(1), 2.0, 2.1, 2.1, _candidate())

    with pytest.raises(ValueError, match="must not regress"):
        _aggregate(aggregator, _identity(2), 1.9, 2.2, 2.2, _candidate())

    next_event = _aggregate(aggregator, _identity(3), 2.1, 2.2, 2.2, _candidate())
    assert next_event.sighting_id == first.sighting_id
    assert next_event.observation_count == 2
    assert next_event.first_frame_decoded_at_monotonic_s == 2.0


def test_aggregator_never_deduplicates_across_worker_runs() -> None:
    aggregator = SightingAggregator()
    first = _aggregate(
        aggregator,
        _identity(1, run_id="run-1"),
        1.0,
        1.1,
        1.1,
        _candidate(),
    )
    restarted = _aggregate(
        aggregator,
        _identity(1, run_id="run-2"),
        1.1,
        1.2,
        1.2,
        _candidate(),
    )

    assert first.sighting_id != restarted.sighting_id
    assert restarted.observation_count == 1


def test_aggregator_never_deduplicates_across_detector_configurations() -> None:
    aggregator = SightingAggregator()
    first = _aggregate(aggregator, _identity(1), 1.0, 1.1, 1.1, _candidate())
    changed = _aggregate(aggregator, _identity(2), 1.1, 1.2, 1.2, _candidate(), "c" * 64)

    assert first.sighting_id != changed.sighting_id
    assert changed.observation_count == 1


@pytest.mark.parametrize(
    ("candidate", "message"),
    [
        (("person", False, 0.8, (1, 1, 2, 2)), "class_id"),
        (("person", 0, True, (1, 1, 2, 2)), "confidence"),
        (("person", 0, 0.8, (False, 1, 2, 2)), "coordinates"),
        (("person", 0, float("nan"), (1, 1, 2, 2)), "confidence"),
        (("person", 0, 0.8, (-1, 1, 2, 2)), "nonnegative"),
    ],
)
def test_detection_candidate_rejects_ambiguous_or_nonfinite_scalars(
    candidate: tuple[object, ...], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        DetectionCandidate(*candidate)


@pytest.mark.parametrize(
    ("outcome", "candidate_count", "decoded_at", "started_at"),
    [
        ("unknown", 0, 1.0, 1.1),
        ("detections", 0, 1.0, 1.1),
        ("empty", 1, 1.0, 1.1),
        ("dropped_future", 0, 1.0, 1.1),
        ("empty", 0, 1.2, 1.1),
    ],
)
def test_processed_event_rejects_inconsistent_runtime_values(
    outcome: str, candidate_count: int, decoded_at: float, started_at: float
) -> None:
    with pytest.raises(ValueError):
        ProcessedFrameEvent(
            identity=_identity(1),
            frame_decoded_at_monotonic_s=decoded_at,
            evaluation_started_at_monotonic_s=started_at,
            evaluation_completed_at_monotonic_s=started_at,
            outcome=outcome,
            candidate_count=candidate_count,
            target_labels=_CANONICAL_DEFAULT_LABELS,
            detector_config_sha256=_TEST_CONFIG_SHA256,
        )


@pytest.mark.parametrize(
    "detector",
    [
        _Detector((object(),)),
        _Detector((_candidate(),), target_labels=("person",)),
        _Detector((_candidate(box=(10, 10, 100, 100)),)),
    ],
)
def test_worker_fail_closes_malformed_or_out_of_contract_detector_output(
    detector: _Detector,
) -> None:
    worker = LiveDetectionWorker(
        _Frames([(_image(), 1.0)]),
        detector,
        source_id="drone1",
        mission_id="mission-7",
        worker_run_id="run-1",
        monotonic_clock=_Clock(1.1),
    )

    assert worker.poll()[0].outcome == "detector_error"


def test_worker_bounds_detector_output_before_aggregation() -> None:
    worker = LiveDetectionWorker(
        _Frames([(_image(), 1.0)]),
        _Detector(tuple(_candidate() for _ in range(257))),
        source_id="drone1",
        mission_id="mission-7",
        worker_run_id="run-1",
        monotonic_clock=_Clock(1.1),
    )

    assert worker.poll()[0].outcome == "detector_error"


def test_aggregator_failure_retains_an_event_and_stops_background_work() -> None:
    class BrokenAggregator(SightingAggregator):
        def observe_frame(self, *args: object, **kwargs: object) -> object:
            raise RuntimeError("aggregation failed")

    worker = LiveDetectionWorker(
        _Frames([(_image(), 1.0)]),
        _Detector((_candidate(),)),
        source_id="drone1",
        mission_id="mission-7",
        worker_run_id="run-1",
        aggregator=BrokenAggregator(),
        monotonic_clock=_Clock(1.1),
    )

    worker.start()
    assert worker._thread is not None
    worker._thread.join(1)
    events = worker.events()

    assert not worker._thread.is_alive()
    assert len(events) == 1
    assert isinstance(events[0], ProcessedFrameEvent)
    assert events[0].outcome == "aggregation_error"
    assert events[0].candidate_count == 1
    assert worker.events() == events
    assert worker.failure_reason == "aggregation_failed"
    worker.close()


def test_worker_defaults_legacy_detectors_to_the_coco_target_set() -> None:
    class LegacyDetector:
        def detect(self, _: np.ndarray) -> tuple[()]:
            return ()

    event = LiveDetectionWorker(
        _Frames([(_image(), 1.0)]),
        LegacyDetector(),
        source_id="drone1",
        mission_id="mission-7",
        monotonic_clock=_Clock(1.1),
    ).poll()[0]

    assert event.target_labels == _CANONICAL_DEFAULT_LABELS
    assert event.detector_config_sha256 == "0" * 64


def test_worker_preserves_explicit_falsy_dependencies() -> None:
    class FalsyClock(_Clock):
        def __bool__(self) -> bool:
            return False

    class FalsyAggregator(SightingAggregator):
        called = False

        def __bool__(self) -> bool:
            return False

        def observe_frame(self, *args: object, **kwargs: object) -> object:
            self.called = True
            return super().observe_frame(*args, **kwargs)

    aggregator = FalsyAggregator()
    worker = LiveDetectionWorker(
        _Frames([(_image(), 1.0)]),
        _Detector(),
        source_id="drone1",
        mission_id="mission-7",
        worker_run_id="run-1",
        aggregator=aggregator,
        monotonic_clock=FalsyClock(1.1),
    )

    event = worker.poll()[0]

    assert event.outcome == "empty"
    assert event.evaluation_completed_at_monotonic_s == 1.1
    assert aggregator.called


@pytest.mark.parametrize(
    ("component", "value"),
    [("source_id", "drone:1"), ("worker_run_id", "run:1")],
)
def test_worker_rejects_ambiguous_identity_components(component: str, value: str) -> None:
    arguments = {
        "source_id": "drone1",
        "mission_id": "mission-7",
        "worker_run_id": "run-1",
    }
    arguments[component] = value

    with pytest.raises(ValueError, match="reserved"):
        LiveDetectionWorker(_Frames([]), _Detector(), **arguments)


def test_worker_escapes_colon_delimited_mission_ids_in_frame_identity() -> None:
    worker = LiveDetectionWorker(
        _Frames([(_image(), 1.0)]),
        _Detector(),
        source_id="drone1",
        mission_id="intent-1:v1:e7",
        worker_run_id="run-1",
        monotonic_clock=_Clock(1.1),
    )

    event = worker.poll()[0]

    assert event.identity.frame_id == "frame:intent-1%3Av1%3Ae7:drone1:run-1:1"
    assert (
        event.identity.frame_id
        != FrameIdentity("drone1", "intent-1%3Av1%3Ae7", "run-1", 1).frame_id
    )


class _BlockedThread:
    def __init__(self) -> None:
        self.alive = True
        self.joins: list[float] = []

    def join(self, timeout: float) -> None:
        self.joins.append(timeout)

    def is_alive(self) -> bool:
        return self.alive


def test_close_does_not_forget_a_blocked_worker_or_allow_a_duplicate() -> None:
    worker = LiveDetectionWorker(
        _Frames([]),
        _Detector(),
        source_id="drone1",
        mission_id="mission-7",
        worker_run_id="run-1",
    )
    thread = _BlockedThread()
    worker._thread = thread

    worker.close()
    assert worker._thread is thread
    assert worker.failure_reason == "shutdown_timeout"
    with pytest.raises(RuntimeError, match="already running"):
        worker.start()

    thread.alive = False
    worker.close()
    assert worker._thread is None


def test_background_poll_failure_is_observable() -> None:
    class FailingFrames:
        def read(self, _: float = 0.1) -> tuple[np.ndarray, float] | None:
            raise RuntimeError("stream failed")

    worker = LiveDetectionWorker(
        FailingFrames(),
        _Detector(),
        source_id="drone1",
        mission_id="mission-7",
        worker_run_id="run-1",
    ).start()
    assert worker._thread is not None
    worker._thread.join(1)

    assert worker.failure_reason == "poll_failed"
    worker.close()


@pytest.mark.parametrize("value", [True, "0.5", float("nan"), float("inf"), 0])
def test_numeric_limits_reject_bool_non_numeric_and_nonpositive_values(value: object) -> None:
    with pytest.raises(ValueError):
        SightingAggregator(dedup_window_s=value)
    with pytest.raises(ValueError):
        YoloXOnnxDetector(
            "unused.onnx",
            net=object(),
            injected_model_sha256=_TEST_MODEL_SHA256,
            confidence_threshold=value,
        )
    with pytest.raises(ValueError):
        LiveDetectionWorker(
            _Frames([]),
            _Detector(),
            source_id="drone1",
            mission_id="mission-7",
            max_frame_age_s=value,
        )


def test_in_memory_retention_limits_have_hard_upper_bounds() -> None:
    with pytest.raises(ValueError, match="aggregation limits"):
        SightingAggregator(max_sightings=4097)
    with pytest.raises(ValueError, match="worker limits"):
        LiveDetectionWorker(
            _Frames([]),
            _Detector(),
            source_id="drone1",
            mission_id="mission-7",
            retained_events=4097,
        )


def test_yolox_detector_decodes_filters_and_declares_default_labels() -> None:
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
    detector = YoloXOnnxDetector(
        "unused.onnx",
        net=net,
        injected_model_sha256=_TEST_MODEL_SHA256,
        confidence_threshold=0.6,
    )
    candidates = detector.detect(np.zeros((320, 640, 3), dtype=np.uint8))

    assert detector.target_labels == _CANONICAL_DEFAULT_LABELS
    assert "person" in detector.target_labels
    assert net.blob.shape == (1, 3, 640, 640)
    assert len(candidates) == 1
    assert candidates[0].label == "backpack"
    assert candidates[0].confidence == pytest.approx(0.81)
    assert candidates[0].bbox_xyxy == pytest.approx((64, 64, 96, 96))
    assert len(detector.detector_config_sha256) == 64


def test_yolox_detector_keeps_the_model_bgr_channel_order() -> None:
    predictions = np.zeros((1, 8400, 85), dtype=np.float32)
    predictions[0, 0, :5] = [10, 10, np.log(4), np.log(4), 0.9]
    predictions[0, 0, 29] = 0.9

    class Net:
        def setInput(self, blob: np.ndarray) -> None:
            self.channels = tuple(blob[0, :, 0, 0])

        def forward(self) -> np.ndarray:
            return predictions

    net = Net()
    YoloXOnnxDetector("unused.onnx", net=net, injected_model_sha256=_TEST_MODEL_SHA256).detect(
        np.full((2, 2, 3), (11, 22, 33), dtype=np.uint8)
    )

    assert net.channels == pytest.approx((11, 22, 33))


def test_detector_configuration_digest_binds_model_thresholds_and_labels() -> None:
    def configured(**overrides: object) -> YoloXOnnxDetector:
        arguments = {
            "net": object(),
            "injected_model_sha256": _TEST_MODEL_SHA256,
        }
        arguments.update(overrides)
        return YoloXOnnxDetector("unused.onnx", **arguments)

    digests = {
        configured().detector_config_sha256,
        configured(injected_model_sha256="c" * 64).detector_config_sha256,
        configured(confidence_threshold=0.7).detector_config_sha256,
        configured(nms_iou_threshold=0.4).detector_config_sha256,
        configured(target_labels=("person",)).detector_config_sha256,
        configured(max_candidates=8).detector_config_sha256,
    }

    assert len(digests) == 6


def test_real_model_bytes_are_bounded_verified_and_loaded_from_the_same_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"synthetic ONNX fixture"
    model_path = tmp_path / "model.onnx"
    model_path.write_bytes(payload)
    loaded = []
    net = object()

    def load(buffer: np.ndarray) -> object:
        loaded.append(buffer.tobytes())
        return net

    monkeypatch.setattr("perception.yolox_onnx.cv2.dnn.readNetFromONNX", load)
    detector = YoloXOnnxDetector(
        model_path,
        expected_model_sha256=hashlib.sha256(payload).hexdigest(),
    )

    assert loaded == [payload]
    assert detector._net is net
    assert len(detector.detector_config_sha256) == 64

    with pytest.raises(ValueError, match="hash mismatch"):
        YoloXOnnxDetector(model_path, expected_model_sha256="0" * 64)


def test_real_model_read_has_a_hard_size_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model_path = tmp_path / "oversize.onnx"
    model_path.write_bytes(b"12345")
    monkeypatch.setattr("perception.yolox_onnx.MAX_MODEL_BYTES", 4)

    with pytest.raises(ValueError, match="128 MiB"):
        YoloXOnnxDetector(
            model_path,
            expected_model_sha256=hashlib.sha256(b"12345").hexdigest(),
        )


def test_injected_net_requires_an_explicit_synthetic_fingerprint() -> None:
    with pytest.raises(ValueError, match="injected_model_sha256"):
        YoloXOnnxDetector("unused.onnx", net=object())


@pytest.mark.parametrize(
    "target_labels",
    [(), ("person", "person"), ("not-a-coco-label",), "person"],
)
def test_yolox_detector_rejects_invalid_target_label_configuration(
    target_labels: object,
) -> None:
    with pytest.raises(ValueError, match="target_labels"):
        YoloXOnnxDetector("unused.onnx", net=object(), target_labels=target_labels)


@pytest.mark.parametrize(
    "frame",
    [None, np.zeros((3, 3), dtype=np.uint8), np.zeros((3, 3, 3))],
)
def test_yolox_detector_rejects_invalid_frames(frame: object) -> None:
    detector = YoloXOnnxDetector(
        "unused.onnx", net=object(), injected_model_sha256=_TEST_MODEL_SHA256
    )
    with pytest.raises(ValueError, match="frame"):
        detector.detect(frame)
