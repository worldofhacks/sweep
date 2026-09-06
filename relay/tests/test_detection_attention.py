from __future__ import annotations

import time
from collections import deque
from pathlib import Path

import numpy as np
from fastapi.testclient import TestClient

from perception.object_detection import DetectionCandidate, LiveDetectionWorker
from relay.app import create_app
from relay.capabilities import C1_CAPABILITY_PROFILE
from relay.session import CapabilityBoundIntentSink
from relay.settings import RelaySettings
from relay.tests.conftest import CONSOLE_KEY, SESSION, EventIds, MutableClock


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

    def __call__(self, session_id: str, drone_id: int, recording_id: str):
        if session_id != SESSION or drone_id != 1 or recording_id != "recorded-backpack":
            raise ValueError("recorded frame is unavailable")
        return self.worker.poll()


def test_recorded_frame_promotes_attention_acknowledges_without_motion_and_audits_every_event(
    tmp_path: Path,
) -> None:
    clock = MutableClock()
    event_ids = EventIds()
    commands: list[object] = []
    settings = RelaySettings(relay_token=CONSOLE_KEY, log_dir=tmp_path)
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
        with client.websocket_connect(f"/ws/{SESSION}") as socket:
            socket.send_json(
                {"v": 1, "type": "auth", "source": "console", "token": CONSOLE_KEY.decode()}
            )
            socket.receive_json()
            socket.receive_json()
            started = time.monotonic()
            response = client.post(
                f"/api/sessions/{SESSION}/detections/recorded-frame",
                headers={"Authorization": f"Bearer {CONSOLE_KEY.decode()}"},
                json={"recording_id": "recorded-backpack", "drone_id": 1},
            )
            assert response.status_code == 200
            frame = socket.receive_json()
            detection = socket.receive_json()
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
            socket.receive_json()
            repeated = socket.receive_json()
            assert repeated["type"] == "detection"
            assert repeated["attention"] == "suppressed_duplicate"

            socket.send_json(
                {"v": 1, "type": "detection_acknowledgement", "detection_id": detection["detection_id"]}
            )
            acknowledgement = socket.receive_json()
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
    assert [record["event"]["type"] for record in replay["events"]] == [
        "detection_frame",
        "detection",
        "detection_frame",
        "detection",
        "detection_acknowledgement",
    ]
