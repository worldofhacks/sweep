import hashlib
import json
from pathlib import Path

import cv2
import numpy as np
import pytest

from perception.tag_localization import tag_corners
from tools.ohmni_fixed_head_camera_mount import TAG_SIZE_M, _fit, build
from tools.ohmni_fixed_head_camera_mount import _capture_manifest as validate_capture_manifest


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
            "camera_matrix": [[350, 0, 320], [0, 350, 240], [0, 0, 1]],
            "distortion_coefficients": [0, 0, 0, 0, 0],
            "rms_reprojection_error_px": 0.1,
            "accepted_image_count": 20,
            "image_sha256": hashes,
        }
    ).encode()


def _motion(
    chain_index: int,
    before_state: str,
    after_state: str,
    before=(0.0, 0.0, 0.0),
    after=(0.05, 0.0, 10.0),
) -> bytes:
    def stage(state_id: str, x: float, yaw: float) -> dict[str, object]:
        return {
            "pose": {"x_m": x, "y_m": 0.0, "yaw_deg": yaw},
            "encoder": {"left": int(x * 1000), "right": int(x * 1000)},
            "neck_position": 368,
            "stationary": True,
            "state_id": state_id,
            "started_monotonic_s": chain_index * 10.0,
            "completed_monotonic_s": chain_index * 10.0 + 1.0,
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
                "before": stage(before_state, before[0], before[2]),
                "after": stage(after_state, after[0], after[2]),
            },
        }
    ).encode()


def test_recovers_mount_from_pixel_geometry() -> None:
    K = np.array([[350, 0, 320], [0, 350, 240], [0, 0, 1]], dtype=float)
    camera = _transform(0.18, -0.06)
    camera[2, 3] = 0.6
    camera[:3, :3] = cv2.Rodrigues(np.array([np.pi - 0.04, -0.03, 0.02]))[0]
    tag = _transform(0.35, 0.0, 0.4)
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


def test_refuses_unbound_raw_evidence(tmp_path: Path) -> None:
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
            {"chain_index": index, "frame": first, "manifest": first} for index in range(3)
        ],
    }
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request))
    with pytest.raises(ValueError, match="raw frame manifest schema"):
        build(path, tmp_path, tmp_path / "out")


def test_refuses_legacy_unbound_motion_evidence(tmp_path: Path) -> None:
    request = tmp_path / "request.json"
    request.write_text("{}")
    with pytest.raises(ValueError, match="schema"):
        build(request, tmp_path, tmp_path / "out")


def _render_frame(camera_tag: np.ndarray) -> bytes:
    K = np.array([[350, 0, 320], [0, 350, 240], [0, 0, 1]], dtype=float)
    pixels, _ = cv2.projectPoints(
        tag_corners(TAG_SIZE_M),
        cv2.Rodrigues(camera_tag[:3, :3])[0],
        camera_tag[:3, 3],
        K,
        np.zeros(5),
    )
    marker = cv2.aruco.generateImageMarker(
        cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_APRILTAG_36h11), 7, 300
    )
    image = cv2.warpPerspective(
        marker,
        cv2.getPerspectiveTransform(
            np.float32([[0, 0], [299, 0], [299, 299], [0, 299]]),
            pixels.reshape(4, 2).astype(np.float32),
        ),
        (640, 480),
        borderValue=255,
    )
    ok, encoded = cv2.imencode(".png", image)
    assert ok
    return encoded.tobytes()


def _capture_manifest(
    frame: dict[str, str], stage: str, state_id: str, encoder: dict[str, int]
) -> bytes:
    return json.dumps(
        {
            "schema_version": 1,
            "kind": "ohmni_fixed_head_camera_capture",
            "device_id": 11,
            "boot_id": "boot-1",
            "camera_serial": "camera-1",
            "motion_chain_id": "chain-1",
            "stage": stage,
            "state_id": state_id,
            "neck_position": 368,
            "stationary": True,
            "encoder": encoder,
            "frame": frame,
        }
    ).encode()


def test_build_recovers_known_mount_from_readable_raw_rasters(tmp_path: Path) -> None:
    intrinsics = _pin(tmp_path, "intrinsics.json", _calibration())
    tag = _transform(0.2, 0.0, 0.4)
    initial_camera_tag = np.eye(4)
    initial_camera_tag[:3, :3] = cv2.Rodrigues(np.array([0.55, 0.2, 0.35]))[0] @ np.diag(
        [1.0, -1.0, -1.0]
    )
    initial_camera_tag[:3, 3] = [0.05, 0.03, 1.2]
    camera = tag @ np.linalg.inv(initial_camera_tag)
    poses = [
        (0.0, 0.0, 0.0),
        (0.0, 0.0, 5.0),
        (0.04, 0.0, 5.0),
        (0.08, 0.01, 10.0),
        (0.12, 0.02, 15.0),
        (0.16, 0.03, 20.0),
    ]
    motions = []
    for index, (before, after) in enumerate(zip(poses, poses[1:], strict=False)):
        motions.append(
            _pin(
                tmp_path,
                f"motion-{index}.json",
                _motion(index, f"state-{index}", f"state-{index + 1}", before, after),
            )
        )
    captures = []
    for index, pose in enumerate(poses):
        body = _transform(pose[0], pose[1], np.deg2rad(pose[2]))
        frame = _pin(
            tmp_path, f"frame-{index}.png", _render_frame(np.linalg.inv(body @ camera) @ tag)
        )
        encoder = {"left": int(pose[0] * 1000), "right": int(pose[0] * 1000)}
        stage = "before" if index == 0 else "after"
        manifest = _pin(
            tmp_path,
            f"frame-{index}.json",
            _capture_manifest(frame, stage, f"state-{index}", encoder),
        )
        captures.append({"chain_index": index, "frame": frame, "manifest": manifest})
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
            {"evidence": motion, "before_stage": "before", "after_stage": "after"}
            for motion in motions
        ],
        "captures": captures,
    }
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request))
    result = build(path, tmp_path, tmp_path / "out")
    np.testing.assert_allclose(result["T_body_camera"], camera, atol=0.12)


def test_refuses_raw_capture_manifest_with_another_camera_identity(tmp_path: Path) -> None:
    frame = _pin(tmp_path, "frame.png", b"raw-frame")
    document = json.loads(_capture_manifest(frame, "before", "state-0", {"left": 1, "right": 1}))
    document["camera_serial"] = "camera-elsewhere"
    manifest = _pin(tmp_path, "capture.json", json.dumps(document).encode())
    with pytest.raises(ValueError, match="does not bind"):
        validate_capture_manifest(
            tmp_path,
            manifest,
            frame_pin=frame,
            identity={
                "device_id": 11,
                "boot_id": "boot-1",
                "camera_serial": "camera-1",
                "motion_chain_id": "chain-1",
            },
            expected_stage="before",
            stage={"encoder": {"left": 1, "right": 1}, "state_id": "state-0"},
        )
