import threading
import time

import numpy as np

from perception.shared_camera_pipeline import CameraPipelineConfig
from perception.test_webcam_localization import webcam_scene
from perception.webcam_localization import WebcamLocalization, WebcamLocalizationService
from tests.test_tag_localization import scene


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
        image = (
            value if isinstance(value, np.ndarray) else np.full((4, 4, 3), value, dtype=np.uint8)
        )
        self.frames.append((image, timestamp))
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


def test_service_uses_shared_decoder_for_configured_two_tag_preview_consensus(tmp_path):
    config, image, _ = webcam_scene(tmp_path, count=2)
    config["localizer"]["consensus"] = {
        "minimum_distinct_tags": 2,
        "maximum_candidate_tags": 6,
        "maximum_translation_residual_m": 0.03,
        "maximum_rotation_residual_rad": 0.2,
    }
    loop = WebcamLocalization(config, allow_synthetic=True)
    reader = LiveReader()
    service = WebcamLocalizationService(
        loop,
        "rtsp://media.example/drone1",
        stream_factory=lambda _url: reader,
    )
    try:
        service.resume(time.monotonic())
        reader.push(image)
        state = _poll_until(service, lambda value: value["localization_consumer_state"] == "active")
    finally:
        service.close()

    pose = state["pose_observation"]
    assert pose["accepted"] is True
    assert pose["consensus_inlier_tag_ids"] == [0, 1]
    assert state["control_eligible"] is False
    assert pose["capture_time_verified"] is False


def test_service_keeps_consensus_diagnostics_visible_when_a_frame_loses_quorum(tmp_path):
    config, _, _ = webcam_scene(tmp_path / "config", count=3)
    config["localizer"]["consensus"] = {
        "minimum_distinct_tags": 2,
        "maximum_candidate_tags": 6,
        "maximum_translation_residual_m": 0.03,
        "maximum_rotation_residual_rad": 0.2,
    }
    _, agreeing, _, _, _ = scene(tmp_path / "agreeing", count=2)
    _, one_outlier, _, _, _ = scene(tmp_path / "outlier", count=3, inconsistent_tag=2)
    _, disagreeing, _, _, _ = scene(tmp_path / "disagreeing", count=2, inconsistent_tag=1)
    loop = WebcamLocalization(config, allow_synthetic=True)
    reader = LiveReader()
    service = WebcamLocalizationService(
        loop,
        "rtsp://media.example/drone1",
        stream_factory=lambda _url: reader,
    )

    def observe(image, predicate):
        reader.push(image)
        return _poll_until(service, predicate)

    try:
        service.resume(time.monotonic())
        first = observe(
            agreeing,
            lambda state: (
                (state.get("pose_observation") or {}).get("consensus_inlier_tag_ids") == [0, 1]
            ),
        )
        outlier = observe(
            one_outlier,
            lambda state: (
                (state.get("pose_observation") or {}).get("consensus_outlier_tag_ids") == [2]
            ),
        )
        rejected = observe(
            disagreeing,
            lambda state: (
                (state.get("pose_observation") or {}).get("reason") == "insufficient_tag_consensus"
            ),
        )
    finally:
        service.close()

    assert first["localization_consumer_state"] == "active"
    assert outlier["pose_observation"]["consensus_inlier_tag_ids"] == [0, 1]
    assert rejected["accepted"] is False
    assert rejected["control_eligible"] is False
    assert rejected["localization_consumer_state"] == "revalidating"
    assert sorted(rejected["pose_observation"]["tag_ids"]) == [0, 1]


def test_service_rejects_tied_rendered_consensus_clusters(tmp_path):
    config, image, _ = webcam_scene(
        tmp_path,
        count=4,
        rendered_camera_offsets={2: [0.2, 0, 0], 3: [0.2, 0, 0]},
    )
    config["localizer"]["consensus"] = {
        "minimum_distinct_tags": 2,
        "maximum_candidate_tags": 6,
        "maximum_translation_residual_m": 0.03,
        "maximum_rotation_residual_rad": 0.2,
    }
    loop = WebcamLocalization(config, allow_synthetic=True)
    reader = LiveReader()
    service = WebcamLocalizationService(
        loop,
        "rtsp://media.example/drone1",
        stream_factory=lambda _url: reader,
    )
    try:
        service.resume(time.monotonic())
        reader.push(image)
        state = _poll_until(
            service,
            lambda value: (
                (value.get("pose_observation") or {}).get("reason") == "ambiguous_consensus"
            ),
        )
    finally:
        service.close()

    assert state["accepted"] is False
    assert state["control_eligible"] is False
    assert state["localization_consumer_state"] == "revalidating"
