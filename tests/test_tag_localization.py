import hashlib
import json
import shutil
from pathlib import Path

import cv2
import numpy as np
import pytest

from perception.position_replay import PositionReplay
from perception.tag_localization import TagLocalizer
from tools.map_validate import seal_manifest

K = np.array([[900.0, 0, 640], [0, 900, 360], [0, 0, 1]])


def scene(tmp_path, tilt=0.45, count=2, tag_rotation=False, nonplanar=False):
    bundle = tmp_path / "bundle"
    shutil.copytree(Path(__file__).parent / "fixtures/mapping", bundle)
    document = json.loads((bundle / "tags.yaml").read_text())
    document["tags"] = document["tags"][:count]
    transforms = []
    for i, tag in enumerate(document["tags"]):
        transform = np.eye(4)
        transform[:3, 3] = [i * 0.55, 0, 0]
        if tag_rotation:
            transform[:3, :3] = cv2.Rodrigues(np.array([0.0, 0.0, np.pi / 2]))[0]
        if nonplanar and i:
            transform[:3, :3] = cv2.Rodrigues(np.array([0.4, -0.2, 0.1]))[0]
        tag.update(
            size=0.3,
            x=i * 0.55,
            y=0,
            z=0,
            yaw=float(np.arctan2(transform[1, 0], transform[0, 0])),
            normal=transform[:3, 2].tolist(),
            T_map_tag=transform.tolist(),
        )
        transforms.append(transform)
    (bundle / "tags.yaml").write_text(json.dumps(document))
    manifest = json.loads((bundle / "manifest.yaml").read_text())
    manifest["frame"]["tag0_yaw_rad"] = document["tags"][0]["yaw"]
    (bundle / "manifest.yaml").write_text(json.dumps(manifest))
    seal_manifest(bundle)
    pipeline = dict(
        resolution_px=[1280, 720],
        codec="h264",
        decoder_path="test",
        camera_mode="test",
        android_device_id="test",
        network_id="test",
    )
    calibration = dict(
        schema_version=1,
        status="offline",
        evidence_kind="synthetic_known_intrinsics",
        camera_serial="test",
        pipeline=pipeline,
        image_size_px=[1280, 720],
        camera_matrix=K.tolist(),
        distortion_coefficients=[0] * 5,
    )
    path = tmp_path / "calibration.yaml"
    path.write_text(json.dumps(calibration))
    body_camera = np.eye(4)
    body_camera[:3, :3] = cv2.Rodrigues(np.array([0.1, 0.2, -0.3]))[0]
    body_camera[:3, 3] = [0.07, -0.04, 0.12]
    config = dict(
        bundle=str(bundle),
        accepted_versions=["synthetic-three-tags-v1"],
        map_sha256=json.loads((bundle / "manifest.yaml").read_text())["content_sha256"],
        calibration_path=str(path),
        calibration_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        camera_serial="test",
        pipeline=pipeline,
        T_body_camera=body_camera.tolist(),
    )
    camera = np.eye(4)
    camera[:3, :3] = cv2.Rodrigues(
        np.array([tilt, 0.12 if tilt else 0, 0.08 if tilt else 0], dtype=float)
    )[0] @ np.diag([1.0, -1.0, -1.0])
    camera[:3, 3] = [0.25 if count > 1 else 0, -1.5 * np.tan(tilt), 1.5]
    image = np.full((720, 1280), 255, np.uint8)
    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_APRILTAG_36h11)
    for i, transform in enumerate(transforms):
        # Independently specified physical print corners; no production corner helper.
        points = (
            np.array([[-0.15, 0.15, 0], [0.15, 0.15, 0], [0.15, -0.15, 0], [-0.15, -0.15, 0]])
            @ transform[:3, :3].T
            + transform[:3, 3]
        )
        inverse = np.linalg.inv(camera)
        pixels = cv2.projectPoints(
            points, cv2.Rodrigues(inverse[:3, :3])[0], inverse[:3, 3], K, np.zeros(5)
        )[0].reshape(4, 2)
        marker = cv2.aruco.generateImageMarker(dictionary, i, 240)
        homography = cv2.getPerspectiveTransform(
            np.float32([[0, 0], [239, 0], [239, 239], [0, 239]]), pixels.astype(np.float32)
        )
        warped = cv2.warpPerspective(marker, homography, (1280, 720), borderValue=255)
        image = np.minimum(image, warped)
    return TagLocalizer(**config), image, camera, body_camera, config


def test_detected_pixels_recover_rotated_camera_and_body(tmp_path):
    localizer, image, camera, extrinsic, _ = scene(tmp_path)
    result = localizer.estimate(image, 1, 1.1, 1.2)
    assert result["accepted"], result
    assert sorted(result["tag_ids"]) == [0, 1]
    np.testing.assert_allclose(result["T_map_camera"], camera, atol=0.02)
    np.testing.assert_allclose(result["T_map_body"], camera @ np.linalg.inv(extrinsic), atol=0.02)
    assert not result["flight_approved"]


def test_single_tilted_tag_and_decoded_corner_order(tmp_path):
    localizer, image, camera, _, _ = scene(tmp_path, count=1, tag_rotation=True)
    result = localizer.estimate(image, 1, 1, 1)
    assert result["accepted"], result
    np.testing.assert_allclose(result["T_map_camera"], camera, atol=0.025)


def test_near_frontal_single_tag_rejects_planar_ambiguity(tmp_path):
    localizer, image, _, _, _ = scene(tmp_path, tilt=0.01, count=1)
    result = localizer.estimate(image, 1, 1, 1)
    assert not result["accepted"]
    assert result["reason"] == "ambiguous"


def test_stale_missing_wrong_map_config_and_timing_fail_closed(tmp_path):
    localizer, image, _, _, config = scene(tmp_path)
    assert localizer.estimate(image, 1, 1.1, 2)["reason"] == "stale"
    assert localizer.estimate(np.full_like(image, 255), 1, 1, 1)["reason"] == "no_tags"
    for timing in [(2, 1, 2), (-1, 0, 0), (1, 2, 1), (1, 1, float("nan"))]:
        with pytest.raises(ValueError):
            localizer.estimate(image, *timing)
    with pytest.raises(ValueError):
        TagLocalizer(**(config | {"accepted_versions": ["wrong"]}))
    with pytest.raises(ValueError):
        TagLocalizer(**(config | {"camera_serial": "different"}))
    with pytest.raises(ValueError):
        TagLocalizer(**(config | {"calibration_sha256": "0" * 64}))


def test_delayed_fix_replays_later_fixes_and_rotated_map_velocity():
    events = [
        ("v0", "velocity", 0, [0.3, -0.4, 0.2], None),
        ("f0", "fix", 0.1, [0.02, -0.05, 0.02], 0.01),
        ("v1", "velocity", 0.2, [-0.2, 0.1, 0.3], None),
        ("f1", "fix", 0.3, [0.01, -0.06, 0.07], 0.02),
        ("f2", "fix", 0.4, [-0.01, -0.05, 0.1], 0.01),
    ]
    chronological = PositionReplay(0, [0, 0, 0])
    delayed = PositionReplay(0, [0, 0, 0])
    for event in events:
        chronological.add(*event)
    for i in [0, 2, 4, 1, 3]:
        delayed.add(*events[i])
        delayed.at(0.4)
    assert delayed.at(0.4) == chronological.at(0.4)
    assert delayed.at(0.4)["accepted"]
    assert not delayed.at(1)["accepted"]
    with pytest.raises(ValueError):
        delayed.add(*events[0])
    with pytest.raises(ValueError):
        delayed.add("negative", "fix", -0.1, [0, 0, 0], 0.01)


def test_map_velocity_integrates_in_declared_axes():
    replay = PositionReplay(0, [1, 2, 3])
    replay.add("v", "velocity", 0, [-2, 3, 0.5])
    np.testing.assert_allclose(replay.at(0.2)["position_map_m"], [0.6, 2.6, 3.1])
    assert not replay.at(0.2)["accepted"]


def test_joint_nonplanar_tag_pixels(tmp_path):
    localizer, image, camera, _, _ = scene(tmp_path, nonplanar=True)
    result = localizer.estimate(image, 1, 1, 1)
    assert result["accepted"], result
    np.testing.assert_allclose(result["T_map_camera"], camera, atol=0.025)


def test_innovation_rejection_preserves_last_accepted_fix():
    replay = PositionReplay(0, [0, 0, 0])
    replay.add("velocity", "velocity", 0, [1, 0, 0])
    replay.add("good", "fix", 0.1, [0.1, 0, 0], 0.001)
    replay.add("bad", "fix", 0.15, [100, 0, 0], 0.001)
    result = replay.at(0.2)
    assert result["rejected_fix_times"] == [0.15]
    assert result["fix_age_s"] == pytest.approx(0.1)
    assert result["position_map_m"][0] == pytest.approx(0.2)
    assert replay.at(0.7)["confidence"] == "amber"
    assert replay.at(2.2)["confidence"] == "red"


def test_missing_velocity_does_not_extrapolate_indefinitely():
    replay = PositionReplay(0, [0, 0, 0])
    replay.add("v", "velocity", 0, [1, 0, 0])
    replay.add("fix", "fix", 0, [0, 0, 0], 0.01)
    result = replay.at(10)
    assert result["position_map_m"] == [0.2, 0, 0]
    assert not result["accepted"]


def test_changed_map_content_with_same_version_is_rejected(tmp_path):
    _, _, _, _, config = scene(tmp_path)
    manifest_path = Path(config["bundle"]) / "manifest.yaml"
    manifest = json.loads(manifest_path.read_text())
    manifest["created_at"] = "2026-09-01T00:00:00Z"
    manifest_path.write_text(json.dumps(manifest))
    seal_manifest(config["bundle"])
    with pytest.raises(ValueError, match="map content hash"):
        TagLocalizer(**config)


def test_cli_runs_real_image_and_rejects_wrong_artifact(tmp_path, capsys, monkeypatch):
    from tools.tag_localize import main

    _, image, camera, _, config = scene(tmp_path)
    image_path = tmp_path / "frame.png"
    cv2.imwrite(str(image_path), image)
    config_path = tmp_path / "run.json"
    config_path.write_text(
        json.dumps(dict(localizer=config, timing=dict(capture_time=1, decode_time=1.1, now=1.2)))
    )
    monkeypatch.setattr("sys.argv", ["tag_localize", str(config_path), str(image_path)])
    assert main() == 0
    report = json.loads(capsys.readouterr().out)
    np.testing.assert_allclose(report["T_map_camera"], camera, atol=0.02)
    config["map_sha256"] = "0" * 64
    config_path.write_text(json.dumps(dict(localizer=config, timing={})))
    assert main() == 1
    assert not json.loads(capsys.readouterr().out)["accepted"]


def test_claimed_calibration_fit_requires_quality_evidence(tmp_path):
    _, _, _, _, config = scene(tmp_path)
    path = Path(config["calibration_path"])
    calibration = json.loads(path.read_text())
    calibration["evidence_kind"] = "recorded_live"
    path.write_text(json.dumps(calibration))
    config["calibration_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="quality evidence"):
        TagLocalizer(**config)
