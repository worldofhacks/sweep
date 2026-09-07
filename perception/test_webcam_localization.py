import hashlib
import json

import numpy as np
import pytest

from perception.webcam_localization import (
    WebcamLocalization,
    configured_stream_paths,
    load_config,
)
from relay.contracts import NodeType
from relay.settings import RelaySettings
from tests.test_tag_localization import scene, world_config


def webcam_scene(tmp_path, *, count=2, **scene_kwargs):
    _, image, camera, body_camera, config = scene(tmp_path, count=count, **scene_kwargs)
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
    expected_map = next(iter(config["localizer"]["accepted_versions"].values()))
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
    assert result["publisher_identity_verified"] is False
    assert result["map_sha256"] == expected_map
    assert result["bundle_version"] in config["localizer"]["accepted_versions"]
    assert "accepted_versions" not in result
    assert loop.at(10.5)["confidence"] == "amber"
    assert loop.at(12)["confidence"] == "red"


def test_world_tag_pixels_enter_filter_with_world_identity_and_age(tmp_path):
    config, image, expected = webcam_scene(tmp_path, count=1)
    config["localizer"] = world_config(config["localizer"], tmp_path)
    loop = WebcamLocalization(config, allow_synthetic=True)
    result = loop.update(image, 10.1, 10.12)
    assert result["pose_observation"]["filter_status"] == "accepted"
    np.testing.assert_allclose(result["position_world_m"], expected[:3, 3], atol=0.04)
    assert "position_map_m" not in result
    assert result["pose_frame"]["name"] == "world"
    assert result["pose_frame"]["map_id"] == "level-1-fixture"
    assert result["pose_frame"] == result["pose_observation"]["pose_frame"]
    assert result["control_eligible"] is False
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


def test_wrong_calibration_pin_refuses_start(tmp_path):
    config, _, _ = webcam_scene(tmp_path)
    config["localizer"]["calibration_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="hash mismatch"):
        WebcamLocalization(config, allow_synthetic=True)


def test_wrong_accepted_map_digest_refuses_start(tmp_path):
    config, _, _ = webcam_scene(tmp_path)
    version = next(iter(config["localizer"]["accepted_versions"]))
    config["localizer"]["accepted_versions"][version] = "0" * 64
    with pytest.raises(ValueError, match="accepted version content hash mismatch"):
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


def test_duplicate_latency_keys_are_rejected_even_when_bytes_are_pinned(tmp_path):
    config, _, _ = webcam_scene(tmp_path)
    path = tmp_path / "latency.json"
    payload = path.read_text().rstrip()[:-1] + ', "status": "offline"}'
    path.write_text(payload)
    config["latency_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="duplicate key"):
        WebcamLocalization(config, allow_synthetic=True)


def test_config_loader_rejects_ambiguous_json(tmp_path):
    path = tmp_path / "webcam.json"
    path.write_text('{"localizer": {}, "localizer": {}, "latency_path": "latency.json"}')
    with pytest.raises(ValueError, match="duplicate key"):
        load_config(path)


def test_ground_source_uses_the_configured_device_stream_and_localizes(tmp_path):
    config, image, expected = webcam_scene(tmp_path)
    config.update(stream_path="drone12", source_device_id=12)
    settings = RelaySettings(
        relay_token=b"r" * 32,
        adapter_keys={1: b"a" * 32, 11: b"b" * 32, 12: b"c" * 32},
        node_types={11: NodeType.GROUND, 12: NodeType.GROUND},
    )
    streams = configured_stream_paths(settings)

    result = WebcamLocalization(config, allow_synthetic=True, configured_streams=streams).update(
        image, 10.1, 10.12
    )

    assert streams[12] == "drone12"
    assert result["stream_path"] == "drone12"
    assert np.linalg.norm(np.array(result["position_map_m"]) - expected[:3, 3]) < 0.04


@pytest.mark.parametrize(
    ("source_device_id", "stream_path", "message"),
    [(13, "drone13", "configured source"), (12, "drone11", "match the configured")],
)
def test_configured_source_refuses_unconfigured_or_mismatched_stream(
    tmp_path, source_device_id, stream_path, message
):
    config, _, _ = webcam_scene(tmp_path)
    config.update(stream_path=stream_path, source_device_id=source_device_id)

    with pytest.raises(ValueError, match=message):
        WebcamLocalization(
            config,
            allow_synthetic=True,
            configured_streams={11: "drone11", 12: "drone12"},
        )


def test_consensus_rejection_keeps_the_previous_preview_fix_age(tmp_path):
    config, good, _ = webcam_scene(tmp_path / "good", count=2)
    config["localizer"]["consensus"] = {
        "minimum_distinct_tags": 2,
        "maximum_candidate_tags": 6,
        "maximum_translation_residual_m": 0.03,
        "maximum_rotation_residual_rad": 0.2,
    }
    _, disagreeing, _, _, _ = scene(tmp_path / "disagree", count=2, inconsistent_tag=1)
    loop = WebcamLocalization(config, allow_synthetic=True)

    accepted = loop.update(good, 10.1, 10.12)
    rejected = loop.update(disagreeing, 10.4, 10.4)

    assert accepted["pose_observation"]["consensus_inlier_tag_ids"] == [0, 1]
    assert rejected["pose_observation"]["reason"] == "insufficient_tag_consensus"
    assert sorted(rejected["pose_observation"]["tag_ids"]) == [0, 1]
    assert rejected["fix_age_s"] == pytest.approx(0.4)
