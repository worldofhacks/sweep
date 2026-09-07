import threading
import time

import numpy as np

from perception.shared_camera_pipeline import CameraPipelineConfig
from perception.webcam_localization import WebcamLocalizationService


class Reader:
    def __init__(self, frames):
        self.frames = list(frames)
        self.closed = False

    def start(self):
        return self

    def read(self, timeout=0):
        assert timeout == 0
        return self.frames.pop(0) if self.frames else None

    def close(self):
        self.closed = True


class Localizer:
    def __init__(self, results):
        self.results = list(results)

    def update(self, _image, _decoded_at, _now):
        return self.results.pop(0)

    def at(self, _now):
        return {"accepted": False, "pose_observation": None}


class PersistentLocalizer(Localizer):
    def at(self, _now):
        return {"accepted": True, "pose_observation": {"accepted": True}}


class LiveReader:
    def __init__(self):
        self.frames = []
        self.closed = False
        self._available = threading.Event()

    def start(self):
        return self

    def push(self, value, timestamp=None):
        if timestamp is None:
            timestamp = time.monotonic()
        self.frames.append((np.full((4, 4, 3), value, dtype=np.uint8), timestamp))
        self._available.set()

    def read(self, timeout=0):
        self._available.wait(timeout)
        if not self.frames:
            return None
        frame = self.frames.pop(0)
        if not self.frames:
            self._available.clear()
        return frame

    def close(self):
        self.closed = True


class Detector:
    target_labels = ("person",)
    detector_config_sha256 = "a" * 64

    def detect(self, _image):
        return ()


def _accepted(label):
    return {
        "type": "webcam_localization",
        "accepted": True,
        "pose_observation": {"accepted": True, "label": label},
    }


def _poll_until(service, predicate):
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        state = service.poll(time.monotonic())
        if predicate(state):
            return state
        time.sleep(0.005)
    raise AssertionError("timed out waiting for shared camera service")


def test_production_service_pause_releases_decoder_and_resume_revalidates_a_new_fix():
    first = LiveReader()
    second = LiveReader()
    readers = iter((first, second))
    localizer = Localizer((_accepted("first"), _accepted("fresh")))
    service = WebcamLocalizationService(
        localizer, "rtsp://media.example/drone12", stream_factory=lambda _url: next(readers)
    )

    service.resume(time.monotonic())
    first.push(1)
    assert _poll_until(service, lambda state: state["localization_consumer_state"] == "active")
    paused = service.pause()
    resumed_at = time.monotonic()
    resumed = service.resume(resumed_at)
    second.push(2, resumed_at - 0.1)

    assert first.closed
    assert paused.state.value == "paused"
    assert resumed.state.value == "revalidating"
    assert _poll_until(
        service, lambda state: state["localization_consumer_state"] == "revalidating"
    )
    second.push(3)
    active = _poll_until(service, lambda state: state["localization_consumer_state"] == "active")
    assert active["localization_consumer_state"] == "active"
    assert active["stream_status"] == "active"
    assert active["pose_observation"]["label"] == "fresh"


def test_production_service_shares_one_decoder_with_detector_and_map_consumer():
    reader = LiveReader()
    stream_factory_calls = []
    localizer = PersistentLocalizer((_accepted("shared"),))
    detections = []
    keyframes = []
    service = WebcamLocalizationService(
        localizer,
        "rtsp://media.example/drone12",
        stream_factory=lambda _url: stream_factory_calls.append(_url) or reader,
        detector=Detector(),
        mission_id="mission-1",
        on_detection=detections.append,
        map_builder=keyframes.append,
        camera_pipeline_config=CameraPipelineConfig(
            detector_sample_interval_s=0.01,
            detector_max_frame_age_s=0.5,
            keyframe_sample_interval_s=0.01,
            keyframe_max_frame_age_s=0.5,
            decoder_read_timeout_s=0.01,
        ),
    )
    try:
        service.resume(time.monotonic())
        reader.push(1)
        state = _poll_until(
            service,
            lambda value: (
                value["localization_consumer_state"] == "active"
                and bool(detections)
                and bool(keyframes)
            ),
        )
        assert state["stream_status"] == "active"
        assert stream_factory_calls == ["rtsp://media.example/drone12"]
        assert int(keyframes[0].image[0, 0, 0]) == 1
        assert keyframes[0].capture_time_monotonic_s is None
    finally:
        service.close()

    assert reader.closed
