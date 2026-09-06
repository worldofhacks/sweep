from __future__ import annotations

import time
from collections import deque
from hashlib import sha256
from pathlib import Path

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

import relay.detection_attention as detection_attention
from perception.detection_contracts import SightingEvent
from perception.object_detection import DetectionCandidate, LiveDetectionWorker
from relay.app import create_app
from relay.capabilities import C1_CAPABILITY_PROFILE
from relay.detection_attention import DetectionAttention, HostRecordedFrameProcessor
from relay.session import CapabilityBoundIntentSink
from relay.settings import DetectionRecording, RelaySettings
from relay.tests.conftest import (
    ADAPTER_KEY,
    CONSOLE_KEY,
    SESSION,
    EventIds,
    MutableClock,
    membership_payload,
)


class Frames:
    def __init__(self) -> None:
        image = np.zeros((32, 32, 3), dtype=np.uint8)
        self.frames = deque(((image, 10.0), (image, 10.1)))

    def read(self, _timeout: float = 0.1):
        return self.frames.popleft() if self.frames else None


class Detector:
    target_labels = ("backpack",)
    detector_config_sha256 = "a" * 64

    def detect(self, _frame: np.ndarray):
        return (DetectionCandidate("backpack", 24, 0.91, (4, 4, 24, 24)),)


class RecordedFrames:
    def __init__(self) -> None:
        self.worker = LiveDetectionWorker(
            Frames(),
            Detector(),
            source_id="drone1",
            mission_id="recorded-acceptance",
            worker_run_id="acceptance-run",
            monotonic_clock=lambda: 10.2,
        )

    def __call__(self, session_id: str, drone_id: int, recording_id: str, _epoch: int):
        if session_id != SESSION or drone_id != 1 or recording_id != "recorded-backpack":
            raise ValueError("recorded frame is unavailable")
        return self.worker.poll()


def test_host_processor_uses_configured_pinned_recording_and_live_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    image_path = tmp_path / "recording.jpg"
    image = np.zeros((32, 32, 3), dtype=np.uint8)
    assert cv2.imwrite(str(image_path), image)

    monkeypatch.setattr(detection_attention, "YoloXOnnxDetector", lambda _path: Detector())
    settings = RelaySettings(
        relay_token=CONSOLE_KEY,
        detection_model_path=tmp_path / "model.onnx",
        detection_recordings=(
            DetectionRecording(
                "recorded-backpack",
                1,
                "drone1",
                "recorded-acceptance",
                image_path,
                sha256(image_path.read_bytes()).hexdigest(),
            ),
        ),
    )
    monkeypatch.setattr(detection_attention, "MAX_RECORDED_FRAME_WORKERS", 2)
    processor = HostRecordedFrameProcessor(settings)
    events = processor(SESSION, 1, "recorded-backpack", 1)
    assert any(isinstance(event, SightingEvent) for event in events)
    first = processor._workers[(SESSION, "recorded-backpack", 1)]
    closed = []
    original_close = LiveDetectionWorker.close

    def track_close(worker):
        closed.append(worker)
        original_close(worker)

    monkeypatch.setattr(LiveDetectionWorker, "close", track_close)
    for epoch in (2, 3):
        events = processor(SESSION, 1, "recorded-backpack", epoch)
        assert any(isinstance(event, SightingEvent) for event in events)
    assert closed == [first]
    assert len(processor._workers) == 2
    with pytest.raises(ValueError, match="not bound"):
        processor(SESSION, 2, "recorded-backpack", 1)


def test_failed_detection_audit_does_not_create_an_acknowledgeable_record() -> None:
    events = RecordedFrames().worker.poll()

    class Registry:
        @staticmethod
        def active_connection_identity(drone_id: int):
            return (1, 1) if drone_id == 1 else None

    class FailingSession:
        session_id = SESSION
        registry = Registry()

        @staticmethod
        def record_operator_events(_events: list[dict[str, object]]) -> None:
            raise RuntimeError("journal unavailable")

        @staticmethod
        def clock() -> int:
            return 0

        @staticmethod
        def event_ids() -> str:
            return "unused"

    attention = DetectionAttention()
    with pytest.raises(RuntimeError, match="journal unavailable"):
        attention.record(FailingSession(), 1, 1, events)
    detection_id = next(event.event_id for event in events if hasattr(event, "sighting_id"))
    with pytest.raises(ValueError, match="unknown detection_id"):
        attention.acknowledge(FailingSession(), detection_id, "console")


def test_recorded_frame_promotes_attention_acknowledges_without_motion_and_audits_every_event(
    tmp_path: Path,
) -> None:
    clock = MutableClock()
    event_ids = EventIds()
    commands: list[object] = []
    settings = RelaySettings(
        relay_token=CONSOLE_KEY,
        adapter_keys={1: ADAPTER_KEY},
        log_dir=tmp_path,
    )
    app = create_app(
        settings,
        clock=clock,
        event_ids=event_ids,
        intent_sink_factory=lambda _session: CapabilityBoundIntentSink(
            lambda intent, _state: commands.append(intent), C1_CAPABILITY_PROFILE
        ),
        recorded_frame_processor=RecordedFrames(),
    )

    with TestClient(app) as client:
        with client.websocket_connect(f"/ws/{SESSION}") as adapter:
            adapter.send_json(
                {
                    "v": 1,
                    "type": "auth",
                    "source": "adapter",
                    "token": ADAPTER_KEY.decode(),
                    "drone_id": 1,
                }
            )
            adapter.receive_json()
            adapter.receive_json()
            adapter.send_json(membership_payload(action="join", event_id="join-1"))
            adapter.receive_json()
            with client.websocket_connect(f"/ws/{SESSION}") as socket:
                socket.send_json(
                    {"v": 1, "type": "auth", "source": "console", "token": CONSOLE_KEY.decode()}
                )
                socket.receive_json()
                socket.receive_json()

                def next_detection_event():
                    while True:
                        event = socket.receive_json()
                        if event["type"] != "state":
                            return event

                started = time.monotonic()
                response = client.post(
                    f"/api/sessions/{SESSION}/detections/recorded-frame",
                    headers={"Authorization": f"Bearer {CONSOLE_KEY.decode()}"},
                    json={"recording_id": "recorded-backpack", "drone_id": 1},
                )
                assert response.status_code == 200
                frame = next_detection_event()
                detection = next_detection_event()
                assert time.monotonic() - started < 1
                assert frame["type"] == "detection_frame"
                assert detection["type"] == "detection"
                assert detection["attention"] == "promoted"
                assert detection["label"] == "backpack"
                assert detection["t"] == clock.value
                assert detection["drone_id"] == 1

                duplicate = client.post(
                    f"/api/sessions/{SESSION}/detections/recorded-frame",
                    headers={"Authorization": f"Bearer {CONSOLE_KEY.decode()}"},
                    json={"recording_id": "recorded-backpack", "drone_id": 1},
                )
                assert duplicate.status_code == 200
                assert next_detection_event()["type"] == "detection_frame"
                repeated = next_detection_event()
                assert repeated["type"] == "detection"
                assert repeated["attention"] == "suppressed_duplicate"
                assert repeated["detection_id"] == detection["detection_id"]

                socket.send_json(
                    {
                        "v": 1,
                        "type": "detection_acknowledgement",
                        "detection_id": detection["detection_id"],
                    }
                )
                acknowledgement = next_detection_event()
                assert acknowledgement["type"] == "detection_acknowledgement"
                assert acknowledgement["t"] == clock.value
                assert acknowledgement["session"] == SESSION
                assert acknowledgement["detection_id"] == detection["detection_id"]
                assert acknowledgement["drone_id"] == 1
                assert acknowledgement["operator_source"] == "console"

        replay = client.get(
            f"/session/{SESSION}", headers={"Authorization": f"Bearer {CONSOLE_KEY.decode()}"}
        ).json()

    assert commands == []
    detection_types = [
        record["event"]["type"]
        for record in replay["events"]
        if record["event"]["type"].startswith("detection")
    ]
    assert detection_types == [
        "detection_frame",
        "detection",
        "detection_frame",
        "detection",
        "detection_acknowledgement",
    ]
