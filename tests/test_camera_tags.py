import copy
import hashlib
import json

import cv2
import numpy as np
import pytest

from perception.camera_tags import CameraTagDetector, read_calibration


def rendered_tag(model="pinhole", tilt=0.55, identifier=7, *, intrinsics=None):
    if intrinsics is None:
        k = np.array([[350.0, 0, 320], [0, 350, 240], [0, 0, 1]])
        d = np.array([-0.08, 0.015, 0.001, -0.001]) if model == "fisheye" else np.zeros(5)
    else:
        k, d = intrinsics
    calibration = dict(
        schema_version=2 if model == "fisheye" else 1,
        model=model,
        status="offline",
        evidence_kind="synthetic",
        camera_serial="ohmni-test",
        pipeline={"resolution_px": [640, 480]},
        image_size_px=[640, 480],
        camera_matrix=k.tolist(),
        distortion_coefficients=d.tolist(),
        accepted_image_count=20,
        rms_reprojection_error_px=0.1,
        image_sha256={str(i): hashlib.sha256(str(i).encode()).hexdigest() for i in range(20)},
    )
    if model == "fisheye":
        calibration["quality"] = dict(
            accepted_image_count=20,
            minimum_accepted_image_count=20,
            rms_reprojection_error_px=0.1,
            maximum_rms_reprojection_error_px=0.5,
            minimum_pose_constraint_ratio=0.005,
            pose_constraint_ratio=0.1,
            opencv_check_cond=True,
        )
    transform = np.eye(4)
    transform[:3, :3] = cv2.Rodrigues(np.array([tilt, 0.2, 0.35]))[0] @ np.diag([1.0, -1.0, -1.0])
    transform[:3, 3] = [0.15, 0.05, 0.9]
    physical_corners = np.array(
        [[-0.15, 0.15, 0], [0.15, 0.15, 0], [0.15, -0.15, 0], [-0.15, -0.15, 0]]
    )
    pixels = cv2.projectPoints(
        physical_corners, cv2.Rodrigues(transform[:3, :3])[0], transform[:3, 3], k, np.zeros(5)
    )[0].reshape(4, 2)
    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_APRILTAG_36h11)
    marker = cv2.aruco.generateImageMarker(dictionary, identifier, 300)
    warp = cv2.getPerspectiveTransform(
        np.float32([[0, 0], [299, 0], [299, 299], [0, 299]]), pixels.astype(np.float32)
    )
    image = cv2.warpPerspective(marker, warp, (640, 480), borderValue=255)
    if model == "fisheye":
        yy, xx = np.indices((480, 640), dtype=np.float32)
        raw_pixels = np.stack([xx, yy], axis=-1).reshape(-1, 1, 2)
        rectified = cv2.fisheye.undistortPoints(raw_pixels, k, d, P=k).reshape(480, 640, 2)
        image = cv2.remap(
            image, rectified[:, :, 0], rectified[:, :, 1], cv2.INTER_LINEAR, borderValue=255
        )
    return calibration, image, transform


@pytest.mark.parametrize("model", ["pinhole", "fisheye"])
def test_real_detector_recovers_camera_frame_from_rendered_rotated_print(model):
    calibration, image, expected = rendered_tag(model)
    detector = CameraTagDetector(
        calibration, camera_serial="ohmni-test", tag_sizes_m={7: 0.3}, allow_synthetic=True
    )
    observations = detector.detect(image)
    assert len(observations) == 1
    result = observations[0]
    assert result["tag_id"] == 7
    assert result["pose_accepted"], result
    np.testing.assert_allclose(result["T_camera_tag"], expected, atol=0.025)
    assert not result["verified_for_flight"]


def test_decoded_unconfigured_tag_keeps_id_without_inventing_scale():
    calibration, image, _ = rendered_tag()
    detector = CameraTagDetector(
        calibration, camera_serial="ohmni-test", tag_sizes_m={8: 0.3}, allow_synthetic=True
    )
    assert detector.detect(image)[0]["reason"] == "unconfigured_tag"
    assert detector.detect(image)[0]["T_camera_tag"] is None


def test_calibration_identity_quality_and_image_resolution_are_enforced(tmp_path):
    calibration, image, _ = rendered_tag()
    with pytest.raises(ValueError, match="synthetic"):
        CameraTagDetector(calibration, camera_serial="ohmni-test", tag_sizes_m={7: 0.3})
    for field, value in [
        ("camera_serial", "wrong"),
        ("model", "fisheye"),
        ("rms_reprojection_error_px", 3),
        ("accepted_image_count", 1),
    ]:
        changed = copy.deepcopy(calibration)
        changed[field] = value
        with pytest.raises(ValueError):
            CameraTagDetector(
                changed, camera_serial="ohmni-test", tag_sizes_m={7: 0.3}, allow_synthetic=True
            )
    detector = CameraTagDetector(
        calibration, camera_serial="ohmni-test", tag_sizes_m={7: 0.3}, allow_synthetic=True
    )
    with pytest.raises(ValueError, match="resolution"):
        detector.detect(image[:400])
    path = tmp_path / "calibration.json"
    path.write_text(json.dumps(calibration))
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    assert read_calibration(path, digest) == calibration
    with pytest.raises(ValueError, match="hash"):
        read_calibration(path, "0" * 64)
