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


def _pin(root: Path, name: str, value: bytes) -> dict[str, str]:
    (root / name).write_bytes(value)
    return {"path": name, "sha256": hashlib.sha256(value).hexdigest()}


def _calibration() -> bytes:
    hashes = {str(index): hashlib.sha256(str(index).encode()).hexdigest() for index in range(20)}
    return json.dumps(
        {
            "schema_version": 1,
            "status": "offline",
            "evidence_kind": "recorded_live",
            "camera_serial": "camera-1",
            "pipeline": {"resolution_px": [640, 480]},
            "image_size_px": [640, 480],
            "camera_matrix": [[500, 0, 320], [0, 500, 240], [0, 0, 1]],
            "distortion_coefficients": [0, 0, 0, 0, 0],
            "rms_reprojection_error_px": 0.1,
            "accepted_image_count": 20,
            "image_sha256": hashes,
        }
    ).encode()


def _motion(chain_index: int, before_state: str, after_state: str) -> bytes:
    def stage(state_id: str, x: float, yaw: float) -> dict[str, object]:
        return {
            "pose": {"x_m": x, "y_m": 0.0, "yaw_deg": yaw},
            "encoder": {"left": int(x * 1000), "right": int(x * 1000)},
            "neck_position": 368,
            "stationary": True,
            "state_id": state_id,
        }

    return json.dumps(
        {
            "schema_version": 1,
            "kind": "ohmni_fixed_head_motion",
            "device_id": 11,
            "boot_id": "boot-1",
            "camera_serial": "camera-1",
            "motion_chain_id": "chain-1",
            "chain_index": chain_index,
            "stages": {
                "before": stage(before_state, 0.0, 0.0),
                "after": stage(after_state, 0.05, 10.0),
            },
        }
    ).encode()


def test_recovers_mount_from_pixel_geometry() -> None:
    K = np.array([[500, 0, 320], [0, 500, 240], [0, 0, 1]], dtype=float)
    camera = _transform(0.18, -0.06)
    camera[2, 3] = 0.6
    camera[:3, :3] = cv2.Rodrigues(np.array([np.pi - 0.04, -0.03, 0.02]))[0]
    tag = _transform(1.2, 0.2, 0.4)
    bodies = [
        _transform(),
        _transform(0, 0, np.deg2rad(20)),
        _transform(0.16, 0, np.deg2rad(20)),
        _transform(0.25, 0.04, np.deg2rad(35)),
    ]
    observations = []
    for body in bodies:
        camera_tag = np.linalg.inv(body @ camera) @ tag
        corners, _ = cv2.projectPoints(
            tag_corners(TAG_SIZE_M),
            cv2.Rodrigues(camera_tag[:3, :3])[0],
            camera_tag[:3, 3],
            K,
            np.zeros(5),
        )
        observations.append((body, corners.reshape(4, 2), 0))

    parameters, rank, condition, residual = _fit(observations, 1, K, np.zeros(5))
    fitted = np.eye(4)
    fitted[:3, :3] = cv2.Rodrigues(parameters[:3])[0]
    fitted[:3, 3] = parameters[3:6]

    np.testing.assert_allclose(fitted, camera, atol=1e-4)
    assert rank == len(parameters)
    assert condition < 1e8
    assert np.max(np.abs(residual)) < 1e-4


def test_refuses_disconnected_motion_endpoint_chain(tmp_path: Path) -> None:
    first = _pin(tmp_path, "motion-0.json", _motion(0, "state-0", "state-1"))
    second = _pin(tmp_path, "motion-1.json", _motion(1, "other-state", "state-2"))
    intrinsics = _pin(tmp_path, "intrinsics.json", _calibration())
    request = {
        "schema_version": 1,
        "kind": "ohmni_fixed_head_mount_request",
        "device_id": 11,
        "boot_id": "boot-1",
        "camera_serial": "camera-1",
        "motion_chain_id": "chain-1",
        "intrinsics": intrinsics,
        "floor": {"z_m": 0.0, "normal": [0.0, 0.0, 1.0], "tag_size_m": TAG_SIZE_M},
        "tag_ids": [7],
        "motions": [
            {"evidence": first, "before_stage": "before", "after_stage": "after"},
            {"evidence": second, "before_stage": "before", "after_stage": "after"},
        ],
        "captures": [
            {"chain_index": index, "frame": first, "manifest": first}
            for index in range(3)
        ],
    }
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request))
    with pytest.raises(ValueError, match="endpoint chain"):
        build(path, tmp_path, tmp_path / "out")


def test_refuses_legacy_unbound_motion_evidence(tmp_path: Path) -> None:
    request = tmp_path / "request.json"
    request.write_text("{}")
    with pytest.raises(ValueError, match="schema"):
        build(request, tmp_path, tmp_path / "out")
