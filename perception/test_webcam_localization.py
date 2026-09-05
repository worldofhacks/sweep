import hashlib
import json

import numpy as np
import pytest

from perception.webcam_localization import WebcamLocalization
from tests.test_tag_localization import scene


def webcam_scene(tmp_path):
    _, image, camera, body_camera, config = scene(tmp_path)
    config["pipeline"].update(
        decoder_path="opencv-ffmpeg-rtsp", latency_endpoint="localization_decode"
    )
    calibration_path = tmp_path / "calibration.yaml"
    calibration = json.loads(calibration_path.read_text())
    calibration["pipeline"] = config["pipeline"]
    calibration_path.write_text(json.dumps(calibration))
    config["calibration_sha256"] = hashlib.sha256(calibration_path.read_bytes()).hexdigest()
    latency = {
        "schema_version": 1,
        "status": "offline",
        "camera_serial": "test",
        "pipeline": config["pipeline"],
        "evidence_kind": "synthetic",
        "duration_ms": 60000,
        "samples_ms": [100] * 21,
        "sample_times_ms": [i * 3000 for i in range(21)],
    }
    path = tmp_path / "latency.json"
    path.write_text(json.dumps(latency))
    request = {
        "localizer": config,
        "stream_path": "drone1",
        "latency_path": str(path),
        "latency_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }
    return request, image, camera @ np.linalg.inv(body_camera)


def test_real_tag_pixels_enter_capture_corrected_filter_and_age_without_frames(tmp_path):
    config, image, expected = webcam_scene(tmp_path)
    loop = WebcamLocalization(config, allow_synthetic=True)
    assert loop.at(9)["confidence"] == "red"
    result = loop.update(image, 10.1, 10.12)
    assert result["pose_observation"]["filter_status"] == "accepted"
    assert result["pose_observation"]["timing_provenance"] == result["timing_provenance"]
    assert result["pose_observation"]["capture_time_verified"] is False
    assert result["pose_observation"]["capture_time"] == pytest.approx(10)
    assert np.linalg.norm(np.array(result["position_map_m"]) - expected[:3, 3]) < 0.04
    assert result["confidence"] == "green"
    assert result["control_eligible"] is False
    assert result["spacing_certified"] is False
    assert loop.at(10.5)["confidence"] == "amber"
    assert loop.at(12)["confidence"] == "red"


def test_blank_or_stale_frames_do_not_refresh_fix_age(tmp_path):
    config, image, _ = webcam_scene(tmp_path)
    loop = WebcamLocalization(config, allow_synthetic=True)
    loop.update(image, 10.1, 10.12)
    state = loop.update(np.full_like(image, 255), 10.4, 10.4)
    assert state["pose_observation"]["reason"] == "no_tags"
    state = loop.update(image, 10.3, 10.9)
    assert state["pose_observation"]["reason"] == "stale"
    assert state["fix_age_s"] == pytest.approx(0.9)
    assert state["confidence"] == "amber"


def test_synthetic_artifacts_cannot_enter_live_mode(tmp_path):
    config, _, _ = webcam_scene(tmp_path)
    with pytest.raises(ValueError, match="recorded_live"):
        WebcamLocalization(config)


@pytest.mark.parametrize("field", ["map_sha256", "calibration_sha256"])
def test_wrong_localization_pins_refuse_start(tmp_path, field):
    config, _, _ = webcam_scene(tmp_path)
    config["localizer"][field] = "0" * 64
    with pytest.raises(ValueError, match="hash mismatch"):
        WebcamLocalization(config, allow_synthetic=True)


def test_latency_pin_and_decoder_endpoint_are_enforced(tmp_path):
    config, _, _ = webcam_scene(tmp_path)
    path = tmp_path / "latency.json"
    latency = json.loads(path.read_text())
    latency["pipeline"]["latency_endpoint"] = "console_display"
    path.write_text(json.dumps(latency))
    with pytest.raises(ValueError, match="hash mismatch"):
        WebcamLocalization(config, allow_synthetic=True)
    config["latency_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="localization decoder"):
        WebcamLocalization(config, allow_synthetic=True)


@pytest.mark.parametrize("values", [[600] * 21, [100]])
def test_slow_or_insufficient_latency_evidence_refuses_start(tmp_path, values):
    config, _, _ = webcam_scene(tmp_path)
    path = tmp_path / "latency.json"
    latency = json.loads(path.read_text())
    latency["samples_ms"] = values
    path.write_text(json.dumps(latency))
    config["latency_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="latency"):
        WebcamLocalization(config, allow_synthetic=True)
