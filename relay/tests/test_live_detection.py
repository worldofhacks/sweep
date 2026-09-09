from __future__ import annotations

import asyncio
import base64
import json
import time
from dataclasses import replace
from hashlib import sha256
from types import SimpleNamespace

import cv2
import httpx
import numpy as np
import pytest

from perception.object_detection import DetectionCandidate
from relay.app import RelayRuntime, create_app
from relay.live_detection import (
    LiveDetectionService,
    LiveDetectionSource,
    DEFAULT_CONFIDENCE_THRESHOLD,
    load_live_detection_sources,
)
from relay.settings import RelaySettings, SettingsError


def source(tmp_path):
    return LiveDetectionSource(
        11,
        "front",
        "ground-1-front",
        "rtsp://127.0.0.1/front",
        (64, 32),
        tmp_path / "isolated-model",
        "a" * 64,
    )


class Stream:
    def __init__(self):
        self.closed = False
        self.timestamp = time.monotonic()

    def start(self):
        return self

    def read(self, timeout=0.1):
        time.sleep(0.01)
        image = np.zeros((32, 64, 3), dtype=np.uint8)
        image[:, :, 2] = 220
        return image, self.timestamp

    def close(self):
        self.closed = True


def service(tmp_path, streams, detector=None):
    def create(_):
        stream = Stream()
        streams.append(stream)
        return stream

    return LiveDetectionService(
        (source(tmp_path),),
        stream_factory=create,
        detector_factory=lambda _: (
            detector
            or SimpleNamespace(
                detect=lambda _: (DetectionCandidate("person", 0, 0.9, (2.0, 3.0, 40.0, 30.0)),)
            )
        ),
    )


def wait_frame(reader):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        result = reader()
        if result["state"] == "live":
            return result
        time.sleep(0.01)
    raise AssertionError(result)


def test_real_jpeg_and_boxes_share_frame_and_old_epoch_decoder_is_closed(tmp_path):
    streams = []
    owner = service(tmp_path, streams)
    try:
        assert owner.snapshot(11, "front", 1, "wrong-stream") == {
            "state": "unconfigured",
            "frame": None,
        }
        assert not streams
        first = wait_frame(lambda: owner.snapshot(11, "front", 1, "ground-1-front"))
        frame = first["frame"]
        image = cv2.imdecode(
            np.frombuffer(base64.b64decode(frame["jpeg_base64"]), np.uint8), cv2.IMREAD_COLOR
        )
        assert image.shape == (frame["height"], frame["width"], 3) == (32, 64, 3)
        assert image[0, 0, 2] > 200
        assert frame["detections"] == [
            {"label": "person", "confidence": 0.9, "bbox_xyxy": [2.0, 3.0, 40.0, 30.0]}
        ]
        old = streams[0]
        wait_frame(lambda: owner.snapshot(11, "front", 2, "ground-1-front"))
        assert old.closed
        owner._workers[(11, "front")].decoded_at -= 2
        assert owner.snapshot(11, "front", 2, "ground-1-front") == {"state": "stale", "frame": None}
    finally:
        owner.close()
    assert all(stream.closed for stream in streams)


def test_out_of_frame_box_fails_without_publishing_image(tmp_path):
    owner = service(
        tmp_path,
        [],
        SimpleNamespace(
            detect=lambda _: (DetectionCandidate("person", 0, 0.9, (0.0, 0.0, 100.0, 100.0)),)
        ),
    )
    try:
        owner.snapshot(11, "front", 1, "ground-1-front")
        worker = owner._workers[(11, "front")]
        worker.thread.join(2)
        assert worker.snapshot() == {"state": "failed", "frame": None}
    finally:
        owner.close()


def test_source_config_checks_model_and_explicit_camera_binding(tmp_path):
    model = tmp_path / "model.onnx"
    model.write_bytes(b"isolated model bytes")
    config = tmp_path / "detection.json"
    entry = {
        "device_id": 11,
        "camera_id": "front",
        "stream": "ground-1-front",
        "stream_url": "rtsp://127.0.0.1/front",
        "resolution": [1280, 720],
        "model_path": "model.onnx",
        "model_sha256": sha256(model.read_bytes()).hexdigest(),
    }
    config.write_text(json.dumps({"schema_version": 1, "sources": [entry]}))
    parsed = load_live_detection_sources(config)
    assert parsed[0].camera_id == "front"
    assert parsed[0].resolution == (1280, 720)
    assert load_live_detection_sources(None) == ()
    for patch in (
        {"model_sha256": "a" * 64},
        {"resolution": [10000, 10000]},
        {"target_labels": ["unknown"]},
    ):
        config.write_text(json.dumps({"schema_version": 1, "sources": [{**entry, **patch}]}))
        with pytest.raises(SettingsError):
            load_live_detection_sources(config)


def test_authenticated_http_camera_epoch_and_stream_identity(tmp_path):
    key = b"isolated-live-detection-auth-key-32-bytes"
    settings = RelaySettings(relay_token=key, log_dir=tmp_path)
    runtime = RelayRuntime(settings)
    session = runtime.session("live-test")
    node = {
        "drone_id": 11,
        "connection_epoch": 2,
        "membership": "ready",
        "cameras": [{"camera_id": "front", "stream": "ground-1-front"}],
    }
    session.current_state = lambda: {"drones": [node]}
    owner = service(tmp_path, [])
    app = create_app(settings)
    app.state.relay_runtime, app.state.live_detection = runtime, owner

    async def requests():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            path = "/api/sessions/live-test/live-detections/11/front/2"
            assert (await client.get(path)).status_code == 401
            headers = {"Authorization": "Bearer " + key.decode()}
            assert (await client.get(path[:-1] + "1", headers=headers)).status_code == 409
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                response = await client.get(path, headers=headers)
                value = response.json()
                if value.get("state") == "live":
                    break
                await asyncio.sleep(0.01)
            assert response.status_code == 200, value
            assert value["frame"]["detections"][0]["label"] == "person"
            assert (
                value["session"],
                value["device_id"],
                value["connection_epoch"],
                value["camera_id"],
                value["stream"],
            ) == ("live-test", 11, 2, "front", "ground-1-front")
            assert response.headers["cache-control"] == "no-store"
            node["membership"] = "disconnected"
            assert (await client.get(path, headers=headers)).status_code == 409

    try:
        asyncio.run(requests())
    finally:
        owner.close()


def test_settings_load_detection_path_without_navigation_policy(tmp_path):
    settings = RelaySettings.from_env(
        {"SWEEP_RELAY_TOKEN": "x" * 32, "SWEEP_LIVE_DETECTION_CONFIG": str(tmp_path / "live.json")}
    )
    assert settings.live_detection_config_path == tmp_path / "live.json"
    assert replace(settings, live_detection_config_path=None).live_detection_config_path is None


def _confidence_config(tmp_path, **patch):
    model = tmp_path / "model.onnx"
    model.write_bytes(b"isolated model bytes")
    config = tmp_path / "confidence.json"
    entry = {
        "device_id": 11,
        "camera_id": "front",
        "stream": "ground-1-front",
        "stream_url": "rtsp://127.0.0.1/front",
        "resolution": [1280, 720],
        "model_path": "model.onnx",
        "model_sha256": sha256(model.read_bytes()).hexdigest(),
        **patch,
    }
    config.write_text(json.dumps({"schema_version": 1, "sources": [entry]}))
    return config


def test_source_without_a_declared_threshold_keeps_the_conservative_default(tmp_path):
    parsed = load_live_detection_sources(_confidence_config(tmp_path))
    assert parsed[0].confidence_threshold == DEFAULT_CONFIDENCE_THRESHOLD


def test_a_declared_threshold_is_carried_onto_the_source(tmp_path):
    parsed = load_live_detection_sources(_confidence_config(tmp_path, confidence_threshold=0.35))
    assert parsed[0].confidence_threshold == 0.35


def test_a_threshold_outside_the_unit_interval_is_refused(tmp_path):
    for value in (0, 1.5, -0.1, True, "0.4"):
        with pytest.raises(SettingsError):
            load_live_detection_sources(_confidence_config(tmp_path, confidence_threshold=value))
