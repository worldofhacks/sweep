from __future__ import annotations

import threading
import time
from collections import deque

import numpy as np
import pytest

from perception.shared_camera_pipeline import CameraPipelineConfig, SharedCameraPipeline


class Stream:
    def __init__(self, *, close_error: bool = False) -> None:
        self.frames: deque[tuple[np.ndarray, float]] = deque()
        self.started = False
        self.closed = False
        self.close_error = close_error
        self._available = threading.Event()

    def start(self):
        self.started = True
        return self

    def push(self, value: int, timestamp: float) -> None:
        self.frames.append((np.full((4, 4, 3), value, dtype=np.uint8), timestamp))
        self._available.set()

    def read(self, timeout=0):
        self._available.wait(timeout)
        if not self.frames:
            return None
        result = self.frames.popleft()
        if not self.frames:
            self._available.clear()
        return result

    def close(self) -> None:
        self.closed = True
        if self.close_error:
            raise OSError("decoder survived shutdown")


class Localizer:
    def __init__(self) -> None:
        self.images: list[int] = []

    def update(self, image, decoded_at, now):
        self.images.append(int(image[0, 0, 0]))
        return {"accepted": True, "pose_observation": {"accepted": True, "decoded": decoded_at}}

    def at(self, now):
        return {"accepted": True, "pose_observation": {"accepted": True}}


class SlowDetector:
    target_labels = ("person",)
    detector_config_sha256 = "a" * 64

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

    def detect(self, image):
        self.entered.set()
        self.release.wait(1)
        return ()


class InvalidDetector:
    pass


def _wait(predicate) -> None:
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("timed out waiting for camera worker")


def _config() -> CameraPipelineConfig:
    return CameraPipelineConfig(
        detector_sample_interval_s=0.01,
        detector_max_frame_age_s=0.5,
        keyframe_sample_interval_s=0.01,
        keyframe_max_frame_age_s=0.5,
        decoder_read_timeout_s=0.01,
    )


def test_blocked_detector_does_not_delay_fresh_localization() -> None:
    stream = Stream()
    localizer = Localizer()
    detector = SlowDetector()
    pipeline = SharedCameraPipeline(
        "rtsp://camera/drone1",
        localizer,
        source_id="drone1",
        stream_factory=lambda _url: stream,
        detector=detector,
        mission_id="mission1",
        config=_config(),
    ).start()
    try:
        started = time.monotonic()
        pipeline.resume_localization(started)
        stream.push(1, time.monotonic())
        _wait(detector.entered.is_set)
        stream.push(2, time.monotonic())
        _wait(lambda: not stream.frames)
        _wait(lambda: pipeline.poll_localization(time.monotonic()).localization_usable)
        assert localizer.images[-1] == 2
    finally:
        detector.release.set()
        pipeline.close()


def test_invalid_detector_constructor_closes_the_started_decoder() -> None:
    stream = Stream()

    with pytest.raises(ValueError, match="detector must declare"):
        SharedCameraPipeline(
            "rtsp://camera/drone1",
            Localizer(),
            source_id="drone1",
            stream_factory=lambda _url: stream,
            detector=InvalidDetector(),
            mission_id="mission1",
        )

    assert stream.started
    assert stream.closed


def test_blocked_detector_cannot_call_back_after_pipeline_close() -> None:
    stream = Stream()
    detector = SlowDetector()
    callbacks = []
    pipeline = SharedCameraPipeline(
        "rtsp://camera/drone1",
        Localizer(),
        source_id="drone1",
        stream_factory=lambda _url: stream,
        detector=detector,
        mission_id="mission1",
        on_detection=callbacks.append,
        config=_config(),
    ).start()
    stream.push(1, time.monotonic())
    _wait(detector.entered.is_set)

    with pytest.raises(RuntimeError, match="detector did not stop"):
        pipeline.close()
    detector.release.set()
    with pytest.raises(RuntimeError, match="detector did not stop"):
        pipeline.close()

    assert callbacks == []


def test_localization_subscription_discards_backlog_and_receives_latest_frame() -> None:
    stream = Stream()
    localizer = Localizer()
    pipeline = SharedCameraPipeline(
        "rtsp://camera/drone1", localizer, source_id="drone1", stream_factory=lambda _url: stream
    )
    try:
        pipeline.resume_localization(time.monotonic())
        stream.push(1, time.monotonic())
        stream.push(2, time.monotonic())
        _wait(lambda: not stream.frames)
        assert pipeline.poll_localization(time.monotonic()).localization_usable
        assert localizer.images == [2]
    finally:
        pipeline.close()


def test_pausing_localization_keeps_detector_and_single_decoder_alive() -> None:
    stream = Stream()
    localizer = Localizer()
    detector = SlowDetector()
    detector.release.set()
    pipeline = SharedCameraPipeline(
        "rtsp://camera/drone1",
        localizer,
        source_id="drone1",
        stream_factory=lambda _url: stream,
        detector=detector,
        mission_id="mission1",
        config=_config(),
    ).start()
    try:
        pipeline.resume_localization(time.monotonic())
        stream.push(1, time.monotonic())
        _wait(lambda: pipeline.poll_localization(time.monotonic()).localization_usable)
        paused = pipeline.pause_localization()
        assert paused.state.value == "paused"
        assert pipeline.subscriber_count == 1
        assert not stream.closed
        resumed = pipeline.resume_localization(time.monotonic())
        assert resumed.state.value == "revalidating"
        stream.push(2, time.monotonic())
        _wait(lambda: pipeline.poll_localization(time.monotonic()).localization_usable)
        assert stream.started
    finally:
        pipeline.close()


def test_last_consumer_close_failure_blocks_reopen() -> None:
    streams: list[Stream] = []

    def factory(_url):
        stream = Stream(close_error=not streams)
        streams.append(stream)
        return stream

    pipeline = SharedCameraPipeline(
        "rtsp://camera/drone1", Localizer(), source_id="drone1", stream_factory=factory
    )
    pipeline.resume_localization(time.monotonic())

    paused = pipeline.pause_localization()
    reopened = pipeline.resume_localization(time.monotonic())

    assert paused.state.value == "unavailable"
    assert reopened.state.value == "unavailable"
    assert pipeline.decoder_status == "shutdown_failed"
    assert len(streams) == 1


def test_restart_requires_a_new_frame_and_fix() -> None:
    streams: list[Stream] = []

    def factory(_url):
        stream = Stream()
        streams.append(stream)
        return stream

    localizer = Localizer()
    pipeline = SharedCameraPipeline(
        "rtsp://camera/drone1", localizer, source_id="drone1", stream_factory=factory
    )
    try:
        pipeline.resume_localization(time.monotonic())
        streams[0].push(1, time.monotonic())
        _wait(lambda: pipeline.poll_localization(time.monotonic()).localization_usable)
        pipeline.pause_localization()
        resumed = pipeline.resume_localization(time.monotonic())
        assert resumed.state.value == "revalidating"
        assert pipeline.poll_localization(time.monotonic()).state.value == "revalidating"
        streams[1].push(2, time.monotonic())
        _wait(lambda: pipeline.poll_localization(time.monotonic()).localization_usable)
        assert localizer.images == [1, 2]
    finally:
        pipeline.close()


def test_map_builder_receives_selected_latest_frames_without_capture_timestamps() -> None:
    stream = Stream()
    received = []
    pipeline = SharedCameraPipeline(
        "rtsp://camera/drone1",
        Localizer(),
        source_id="drone1",
        stream_factory=lambda _url: stream,
        map_builder=received.append,
        config=_config(),
    )
    try:
        stream.push(1, time.monotonic())
        stream.push(2, time.monotonic())
        _wait(lambda: not stream.frames)
        pipeline.start()
        _wait(lambda: bool(received))
        keyframe = received[0]
        assert int(keyframe.image[0, 0, 0]) == 2
        assert keyframe.source_id == "drone1"
        assert keyframe.capture_time_monotonic_s is None
        assert keyframe.capture_time_provenance == "unavailable"
    finally:
        pipeline.close()
