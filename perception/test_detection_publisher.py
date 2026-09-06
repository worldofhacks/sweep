from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass

import numpy as np

from perception.detection_publisher import (
    AsyncDetectionTransport,
    ClockMappedFrameReader,
    DetectionPublisher,
    DetectionPublisherConfig,
)
from perception.object_detection import (
    DecodedFrame,
    FrameIdentity,
    LiveDetectionWorker,
    ProcessedFrameEvent,
)
from relay.auth import verify_event_signature
from relay.control_localization import ClockMapping

_DETECTOR_CONFIG_SHA256 = "a" * 64
_MISSION_ID = "b" * 64


def _config(queue_limit: int = 2) -> DetectionPublisherConfig:
    return DetectionPublisherConfig(
        "session-1",
        "intent-1",
        _MISSION_ID,
        1,
        7,
        "camera-1",
        "camera-serial-1",
        ClockMapping("camera-pts", "relay-monotonic", 0, 10_000, 1000, 15, True),
        ClockMapping("processing-monotonic", "relay-monotonic", 0, 10_000, 1000, 15, True),
        queue_limit,
    )


def _event(frame_id: str, *, verified: bool = False) -> ProcessedFrameEvent:
    return ProcessedFrameEvent(
        FrameIdentity("camera-1", _MISSION_ID, frame_id, 1),
        11,
        11.01,
        11.01,
        "empty",
        0,
        ("backpack",),
        _DETECTOR_CONFIG_SHA256,
        capture_time_verified=verified,
        received_at_s=11,
    )


def test_publisher_preserves_capture_verification_and_bounds_backlog() -> None:
    publisher = DetectionPublisher(_config(), b"perception-key")
    first = publisher.enqueue(_event("1"))
    publisher.enqueue(_event("2", verified=True))
    last = publisher.enqueue(_event("3", verified=True))

    assert first["capture_timestamp_ms"] == 11_000
    assert first["processed_at_ms"] == 11_010
    assert first["received_at_ms"] == 11_000
    assert first["capture_time_verified"] is False
    assert first["frame_decoded_at_monotonic_ms"] == 11_000
    assert first["evaluation_started_at_monotonic_ms"] == 11_010
    assert first["evaluation_completed_at_monotonic_ms"] == 11_010
    assert first["detector_config_sha256"] == _DETECTOR_CONFIG_SHA256
    unsigned = {key: value for key, value in first.items() if key != "signature"}
    assert verify_event_signature(unsigned, first["signature"], b"perception-key")
    assert [frame["frame_id"] for frame in publisher.drain()] == [
        f"frame:{_MISSION_ID}:camera-1:2:1",
        f"frame:{_MISSION_ID}:camera-1:3:1",
    ]
    assert last["event_id"] == f"event:frame:{_MISSION_ID}:camera-1:3:1:processed"


@dataclass
class _Frames:
    frames: list[DecodedFrame]

    def read_timed(self, timeout: float):
        assert timeout == 0
        return self.frames.pop(0) if self.frames else None


class _Detector:
    target_labels = ("backpack",)
    detector_config_sha256 = _DETECTOR_CONFIG_SHA256

    def detect(self, image):
        return ()


def test_mapped_pts_and_receipt_time_drop_buffered_frames() -> None:
    reader = ClockMappedFrameReader(
        _Frames([DecodedFrame(np.zeros((2, 2, 3), dtype=np.uint8), 1, 4, True)]),
        _config().clock_mapping,
        _config().processing_clock_mapping,
    )
    worker = LiveDetectionWorker(
        reader,
        _Detector(),
        source_id="camera-1",
        mission_id=_MISSION_ID,
        max_frame_age_s=0.5,
        monotonic_clock=lambda: 14,
    )

    (event,) = worker.poll()

    assert event.outcome == "dropped_stale"
    assert event.capture_time_verified is True
    assert event.frame_decoded_at_monotonic_s == 11
    assert event.evaluation_completed_at_monotonic_s == 14


def test_slow_detector_uses_completion_time_from_mapped_clock() -> None:
    local_time_s = [4.2]

    def relay_clock() -> float:
        return _config().processing_clock_mapping.to_relay_ms(local_time_s[0]) / 1000

    class SlowDetector:
        target_labels = ("backpack",)
        detector_config_sha256 = _DETECTOR_CONFIG_SHA256

        def detect(self, image: np.ndarray) -> tuple[object, ...]:
            local_time_s[0] = 5.0
            return ()

    reader = ClockMappedFrameReader(
        _Frames([DecodedFrame(np.zeros((2, 2, 3), dtype=np.uint8), 1, 4, False)]),
        _config().clock_mapping,
        _config().processing_clock_mapping,
    )
    worker = LiveDetectionWorker(
        reader,
        SlowDetector(),
        source_id="camera-1",
        mission_id=_MISSION_ID,
        max_frame_age_s=0.5,
        monotonic_clock=relay_clock,
    )

    (frame,) = DetectionPublisher(_config(), b"perception-key").poll_worker(worker)

    assert frame["outcome"] == "dropped_stale"
    assert frame["capture_time_verified"] is False
    assert frame["received_at_ms"] == 14_000
    assert frame["processed_at_ms"] == 15_000


class _Socket:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, message: str) -> None:
        self.messages.append(message)


def test_async_transport_authenticates_and_sends_only_bounded_latest_queue() -> None:
    publisher = DetectionPublisher(_config(), "perception-key")
    for frame_id in ("1", "2", "3"):
        publisher.enqueue(_event(frame_id, verified=True))
    socket = _Socket()

    @asynccontextmanager
    async def connect():
        yield socket

    sent = asyncio.run(
        AsyncDetectionTransport(
            "ws://relay.example/ws", "perception-key", socket_factory=connect
        ).publish_pending(publisher)
    )

    assert sent == 2
    assert publisher.pending == 0
    auth, *frames = (json.loads(message) for message in socket.messages)
    assert auth == {"v": 1, "type": "auth", "source": "perception", "token": "perception-key"}
    assert [frame["frame_id"] for frame in frames] == [
        f"frame:{_MISSION_ID}:camera-1:2:1",
        f"frame:{_MISSION_ID}:camera-1:3:1",
    ]
    assert all(frame["source"] == "perception" for frame in frames)
    assert all(
        verify_event_signature(
            {key: value for key, value in frame.items() if key != "signature"},
            frame["signature"],
            b"perception-key",
        )
        for frame in frames
    )


def test_producer_config_accepts_source_video_and_yolox_model_aliases() -> None:
    from perception.detection_publisher import DetectionProducerConfig

    config = DetectionProducerConfig.from_mapping(
        {
            "publisher": {
                "session": "session-1",
                "intent_id": "intent-1",
                "mission_id": _MISSION_ID,
                "drone_id": 1,
                "connection_epoch": 7,
                "source_id": "camera-1",
                "camera_id": "camera-serial-1",
                "pts_clock_mapping": _config().clock_mapping.to_mapping(),
                "processing_clock_mapping": _config().processing_clock_mapping.to_mapping(),
            },
            "websocket_url": "ws://relay.example/ws",
            "source_video": "rtsp://camera.example/live",
            "yolox_model": "yolox.onnx",
            "key_environment": "PERCEPTION_KEY",
        }
    )

    assert config.source_url == "rtsp://camera.example/live"
    assert config.model_path == "yolox.onnx"
