import hashlib
import json
from pathlib import Path

import cv2
import numpy as np
import pytest

from perception.tag_localization import tag_corners
from tools.ohmni_fixed_head_camera_mount import TAG_SIZE_M, _fit, build


def _transform(x=0.0, y=0.0, yaw=0.0):
    result = np.eye(4)
    c, s = np.cos(yaw), np.sin(yaw)
    result[:2, :2] = [[c, -s], [s, c]]
    result[:2, 3] = [x, y]
    return result


def _calibration() -> dict[str, object]:
    hashes = {str(index): hashlib.sha256(str(index).encode()).hexdigest() for index in range(20)}
    return {
        "schema_version": 1,
        "status": "offline",
        "evidence_kind": "recorded_live",
        "camera_serial": "test",
        "pipeline": {"resolution_px": [640, 480]},
        "image_size_px": [640, 480],
        "camera_matrix": [[500, 0, 320], [0, 500, 240], [0, 0, 1]],
        "distortion_coefficients": [0, 0, 0, 0, 0],
        "rms_reprojection_error_px": 0.1,
        "accepted_image_count": 20,
        "image_sha256": hashes,
    }


def _frame(camera_tag: np.ndarray) -> bytes:
    image = np.full((480, 640, 3), 255, np.uint8)
    marker = cv2.aruco.generateImageMarker(
        cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_APRILTAG_36h11), 7, 200
    )
    K = np.array(_calibration()["camera_matrix"], float)
    pixels, _ = cv2.projectPoints(
        tag_corners(TAG_SIZE_M), cv2.Rodrigues(camera_tag[:3, :3])[0], camera_tag[:3, 3], K, None
    )
    source = np.array([[0, 0], [199, 0], [199, 199], [0, 199]], np.float32)
    warp = cv2.warpPerspective(
        marker,
        cv2.getPerspectiveTransform(source, pixels.reshape(4, 2).astype(np.float32)),
        (640, 480),
    )
    image[warp < 128] = 0
    ok, encoded = cv2.imencode(".png", image)
    assert ok
    return encoded.tobytes()


def _pin(root: Path, name: str, value: bytes) -> dict[str, str]:
    (root / name).write_bytes(value)
    return {"path": name, "sha256": hashlib.sha256(value).hexdigest()}


def _motion(before: tuple[float, float, float], after: tuple[float, float, float]) -> bytes:
    return json.dumps(
        {
            "stages": {
                "before": {"pose": dict(zip(("x_m", "y_m", "yaw_deg"), before, strict=True))},
                "after": {"pose": dict(zip(("x_m", "y_m", "yaw_deg"), after, strict=True))},
            }
        }
    ).encode()


def test_recovers_mount_from_rendered_tag_geometry() -> None:
    camera = _transform(0.18, -0.06)
    camera[2, 3] = 0.6
    camera[:3, :3] = cv2.Rodrigues(np.array([0.04, -0.03, 0.02]))[0]
    tag = _transform(1.2, 0.2, 0.4)
    bodies = [_transform(), _transform(0, 0, np.deg2rad(20)), _transform(0.16, 0, np.deg2rad(20))]
    observations = [(body, np.linalg.inv(body @ camera) @ tag, 0) for body in bodies]

    parameters, rank, condition, residual = _fit(observations, 1)
    fitted = np.eye(4)
    fitted[:3, :3] = cv2.Rodrigues(parameters[:3])[0]
    fitted[:3, 3] = parameters[3:6]

    np.testing.assert_allclose(fitted, camera, atol=1e-5)
    assert rank >= len(parameters) - 1
    assert np.max(np.abs(residual)) < 1e-7


def test_refuses_translation_only_motion(tmp_path: Path) -> None:
    request = tmp_path / "request.json"
    request.write_text("{}")
    with pytest.raises(ValueError, match="schema"):
        build(request, tmp_path, tmp_path / "out")
