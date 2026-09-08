import hashlib
import json
from pathlib import Path

import cv2
import numpy as np
import pytest

from perception.tag_localization import tag_corners
from tools.ohmni_fixed_head_camera_mount import TAG_SIZE_M, _fit, build, build_retained
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


def _absolute_pin(root: Path, name: str, value: bytes) -> dict[str, str]:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value)
    return {"path": str(path), "sha256": hashlib.sha256(value).hexdigest()}


def _calibration(model: str = "pinhole") -> bytes:
    hashes = {str(index): hashlib.sha256(str(index).encode()).hexdigest() for index in range(20)}
    calibration: dict[str, object] = {
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
    if model == "fisheye":
        calibration.update(
            {
                "schema_version": 2,
                "model": "fisheye",
                "distortion_coefficients": [-0.08, 0.015, 0.001, -0.001],
                "quality": {
                    "accepted_image_count": 20,
                    "minimum_accepted_image_count": 20,
                    "rms_reprojection_error_px": 0.1,
                    "maximum_rms_reprojection_error_px": 0.5,
                    "minimum_pose_constraint_ratio": 0.005,
                    "pose_constraint_ratio": 0.1,
                    "opencv_check_cond": True,
                },
            }
        )
    return json.dumps(calibration).encode()


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


def _render_frame(camera_tag: np.ndarray, model: str = "pinhole") -> bytes:
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
    if model == "fisheye":
        distortion = np.array([-0.08, 0.015, 0.001, -0.001])
        y, x = np.indices((480, 640), dtype=np.float32)
        rectified = cv2.fisheye.undistortPoints(
            np.stack([x, y], axis=-1).reshape(-1, 1, 2), K, distortion, P=K
        ).reshape(480, 640, 2)
        image = cv2.remap(
            image, rectified[:, :, 0], rectified[:, :, 1], cv2.INTER_LINEAR, borderValue=255
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


@pytest.mark.parametrize("model", ["pinhole", "fisheye"])
def test_build_recovers_known_mount_from_readable_raw_rasters(tmp_path: Path, model: str) -> None:
    intrinsics = _pin(tmp_path, "intrinsics.json", _calibration(model))
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
            tmp_path,
            f"frame-{index}.png",
            _render_frame(np.linalg.inv(body @ camera) @ tag, model),
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
    fitted = np.asarray(result["T_body_camera"])
    translation_error_m = np.linalg.norm(fitted[:3, 3] - camera[:3, 3])
    rotation_error_deg = np.rad2deg(
        np.arccos(np.clip((np.trace(fitted[:3, :3].T @ camera[:3, :3]) - 1) / 2, -1, 1))
    )
    assert translation_error_m < 0.12
    assert rotation_error_deg < 7


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


def _retained_capture(
    root: Path,
    index: int,
    frame: bytes,
    capture_time_ns: int,
    *,
    camera_serial: str = "camera-1",
    target: int = 66363,
    repeat_encoders: bool = False,
) -> dict[str, dict[str, str]]:
    prefix = f"capture-{index}"
    helper_path = root / "step-neck-lowest-reconnect.py"
    if not helper_path.exists():
        helper_path.write_text("target=int(-(args.target-480)*18961/32)\n")
    helper_payload = helper_path.read_bytes()
    helper = {"path": str(helper_path), "sha256": hashlib.sha256(helper_payload).hexdigest()}
    plan = {
        "boot_id": "boot-1",
        "base_actuation": "none",
        "head_angles": [280, 368],
        "source_sha256": {str(helper_path): helper["sha256"]},
    }
    plan_pin = _absolute_pin(root, f"{prefix}/plan.json", json.dumps(plan).encode())
    samples = [
        {"position": target + offset, "target": target, "flags": "NONE"} for offset in (12, 8, 10)
    ]
    step = {
        "boot_id": "boot-1",
        "status": "completed",
        "command": "neck_angle 368",
        "before": {"position": 70000, "target": 70000, "flags": "NONE"},
        "settled": {
            "position": target + 10,
            "target": target,
            "flags": "NONE",
            "request_ns": capture_time_ns - 20,
            "receipt_ns": capture_time_ns - 10,
        },
        "samples": samples,
    }
    step_pin = _absolute_pin(root, f"{prefix}/step-368.json", json.dumps(step).encode())

    def neck(receipt_ns: int) -> dict[str, object]:
        return {
            "boot_id": "boot-1",
            "position": target + 10,
            "target": target,
            "flags": "NONE",
            "request_ns": receipt_ns - 5,
            "receipt_ns": receipt_ns,
        }

    before_neck = _absolute_pin(
        root, f"{prefix}/neck-before.json", json.dumps(neck(capture_time_ns - 100)).encode()
    )
    after_neck = _absolute_pin(
        root, f"{prefix}/neck-after.json", json.dumps(neck(capture_time_ns)).encode()
    )

    def encoders(offset: int, receipt_offset: int) -> bytes:
        return json.dumps(
            {
                "boot_id": "boot-1",
                "clock": "linux_monotonic",
                "read_only": True,
                "samples": [
                    {
                        "poll_id": index * 10 + sample,
                        "left": 1000 + index * 100 + offset + sample % 2,
                        "right": 2000 + index * 100 + offset + sample % 2,
                        "left_receipt_ns": capture_time_ns + receipt_offset + sample,
                        "right_receipt_ns": capture_time_ns + receipt_offset + 10 + sample,
                    }
                    for sample in range(3)
                ],
            }
        ).encode()

    before_encoder_payload = encoders(0, -90)
    after_encoder_payload = before_encoder_payload if repeat_encoders else encoders(2, 20)
    before_encoder = _absolute_pin(root, f"{prefix}/encoders-before.json", before_encoder_payload)
    after_encoder = _absolute_pin(root, f"{prefix}/encoders-after.json", after_encoder_payload)
    image = cv2.imdecode(np.frombuffer(frame, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert image is not None
    raw = cv2.cvtColor(image, cv2.COLOR_BGR2YUV_UYVY)
    ok, retained_frame = cv2.imencode(".png", cv2.cvtColor(raw, cv2.COLOR_YUV2BGR_UYVY))
    assert ok
    frame_pin = _absolute_pin(root, f"{prefix}/frame-000000.png", retained_frame.tobytes())
    raw_pin = _absolute_pin(
        root,
        f"{prefix}/raw-000000.uyvy",
        raw.tobytes(),
    )
    manifest = {
        "schema_version": "ohmni-dual-calibration-capture/v2",
        "status": "complete",
        "boot_id": "boot-1",
        "capture_pipeline_sha256": "pipeline-1",
        "cameras": {
            "main": {"usb_serial": camera_serial, "pixel_format": "UYVY", "shape_px": [640, 480]}
        },
    }
    manifest_pin = _absolute_pin(root, f"{prefix}/manifest.json", json.dumps(manifest).encode())
    result = {
        "frames": [
            {
                "boot_id": "boot-1",
                "camera": "main",
                "image_file": "frame-000000.png",
                "image_sha256": frame_pin["sha256"],
                "source_file": "raw-000000.uyvy",
                "source_sha256": raw_pin["sha256"],
                "source_device_sha256": raw_pin["sha256"],
                "capture_pipeline_sha256": "pipeline-1",
                "shape_px": [640, 480],
            }
        ],
        "recorded_live_provenance": {"manifest_sha256": manifest_pin["sha256"]},
    }
    result_pin = _absolute_pin(root, f"{prefix}/result.json", json.dumps(result).encode())
    return {
        "plan": plan_pin,
        "neck_helper": helper,
        "neck_step": step_pin,
        "neck_before": before_neck,
        "neck_after": after_neck,
        "encoders_before": before_encoder,
        "encoders_after": after_encoder,
        "capture_manifest": manifest_pin,
        "capture_result": result_pin,
        "frame": frame_pin,
        "raw_frame": raw_pin,
    }


def _retained_motion_record(
    root: Path, index: int, delta: np.ndarray, start_s: float
) -> dict[str, object]:
    yaw_deg = np.rad2deg(np.arctan2(delta[1, 0], delta[0, 0]))
    before_encoder = {"left": 1002 + index * 100, "right": 2002 + index * 100}
    after_encoder = {"left": 1002 + (index + 1) * 100, "right": 2002 + (index + 1) * 100}
    document = {
        "schema_version": 1,
        "kind": "camera_calibration_poses",
        "device_id": 11,
        "boot_id": "boot-1",
        "camera_serial": "camera-1",
        "stages": {
            "before_motion": {
                "pose": {"x_m": 0.0, "y_m": 0.0, "yaw_deg": 0.0},
                "encoder": {
                    **before_encoder,
                    "left_receipt_ns": int((start_s - 0.15) * 1_000_000_000),
                    "right_receipt_ns": int((start_s - 0.15) * 1_000_000_000) + 1,
                },
                "stage_started_monotonic_s": start_s,
                "stage_completed_monotonic_s": start_s + 0.25,
            },
            "after_motion": {
                "pose": {"x_m": delta[0, 3], "y_m": delta[1, 3], "yaw_deg": yaw_deg},
                "encoder": {
                    **after_encoder,
                    "left_receipt_ns": int((start_s + 0.35) * 1_000_000_000),
                    "right_receipt_ns": int((start_s + 0.35) * 1_000_000_000) + 1,
                },
                "stage_started_monotonic_s": start_s + 0.5,
                "stage_completed_monotonic_s": start_s + 0.75,
            },
        },
    }
    return {
        "record": _absolute_pin(root, f"motion-{index}.json", json.dumps(document).encode()),
        "before_stage": "before_motion",
        "after_stage": "after_motion",
    }


def _retained_request(
    root: Path, *, repeat_encoders: bool = False, target: int = 66363
) -> tuple[Path, np.ndarray]:
    intrinsics = _absolute_pin(root, "intrinsics.json", _calibration())
    tag = _transform(0.2, 0.0, 0.4)
    initial_camera_tag = np.eye(4)
    initial_camera_tag[:3, :3] = cv2.Rodrigues(np.array([0.55, 0.2, 0.35]))[0] @ np.diag(
        [1.0, -1.0, -1.0]
    )
    initial_camera_tag[:3, 3] = [0.05, 0.03, 1.2]
    camera = tag @ np.linalg.inv(initial_camera_tag)
    poses = [
        _transform(0.0, 0.0, 0.0),
        _transform(0.0, 0.0, np.deg2rad(5)),
        _transform(0.04, 0.0, np.deg2rad(5)),
        _transform(0.08, 0.01, np.deg2rad(10)),
        _transform(0.12, 0.02, np.deg2rad(15)),
        _transform(0.16, 0.03, np.deg2rad(20)),
    ]
    captures = [
        _retained_capture(
            root,
            index,
            _render_frame(np.linalg.inv(body @ camera) @ tag),
            1_000_000_000 + index * 10_000_000_000,
            target=target,
            repeat_encoders=repeat_encoders and index == 0,
        )
        for index, body in enumerate(poses)
    ]
    motions = [
        _retained_motion_record(
            root,
            index,
            np.linalg.inv(before) @ after,
            2.0 + index * 10.0,
        )
        for index, (before, after) in enumerate(zip(poses, poses[1:], strict=False))
    ]
    request = {
        "schema_version": 1,
        "kind": "ohmni_retained_fixed_head_mount_request",
        "device_id": 11,
        "boot_id": "boot-1",
        "camera_serial": "camera-1",
        "intrinsics": intrinsics,
        "tag_ids": [7],
        "motions": motions,
        "captures": captures,
    }
    path = root / "retained-request.json"
    path.write_text(json.dumps(request))
    return path, camera


def test_retained_artifact_adapter_builds_from_readable_rasters(tmp_path: Path) -> None:
    request, camera = _retained_request(tmp_path)
    result = build_retained(request, tmp_path / "out")
    fitted = np.asarray(result["T_body_camera"])
    translation_error_m = np.linalg.norm(fitted[:3, 3] - camera[:3, 3])
    rotation_error_deg = np.rad2deg(
        np.arccos(np.clip((np.trace(fitted[:3, :3].T @ camera[:3, :3]) - 1) / 2, -1, 1))
    )
    assert translation_error_m < 0.01
    assert rotation_error_deg < 0.5
    assert result["retained_inputs"]["captures"][0]["raw_frame"]["sha256"]
    assert (tmp_path / "out/retained-inputs/captures/000/capture/main/raw-000000.uyvy").is_file()


def test_retained_artifact_adapter_refuses_reused_encoder_endpoint(tmp_path: Path) -> None:
    request, _ = _retained_request(tmp_path, repeat_encoders=True)
    with pytest.raises(ValueError, match="independently sampled"):
        build_retained(request, tmp_path / "out")


def test_retained_artifact_adapter_refuses_changed_raw_frame(tmp_path: Path) -> None:
    request, _ = _retained_request(tmp_path)
    document = json.loads(request.read_text())
    Path(document["captures"][0]["raw_frame"]["path"]).write_bytes(b"changed")
    with pytest.raises(ValueError, match="raw_frame pin does not match"):
        build_retained(request, tmp_path / "out")


def test_retained_artifact_adapter_refuses_raw_png_pixel_mismatch(tmp_path: Path) -> None:
    request, _ = _retained_request(tmp_path)
    document = json.loads(request.read_text())
    capture = document["captures"][0]
    raw_path = Path(capture["raw_frame"]["path"])
    raw = bytearray(raw_path.read_bytes())
    raw[1] = (raw[1] + 32) % 256
    raw_path.write_bytes(raw)
    raw_hash = hashlib.sha256(raw).hexdigest()
    capture["raw_frame"]["sha256"] = raw_hash
    result_path = Path(capture["capture_result"]["path"])
    result = json.loads(result_path.read_text())
    result["frames"][0]["source_sha256"] = raw_hash
    result["frames"][0]["source_device_sha256"] = raw_hash
    result_path.write_text(json.dumps(result))
    capture["capture_result"]["sha256"] = hashlib.sha256(result_path.read_bytes()).hexdigest()
    request.write_text(json.dumps(document))
    with pytest.raises(ValueError, match="PNG does not match"):
        build_retained(request, tmp_path / "out")


def test_retained_artifact_adapter_refuses_capture_camera_mismatch(tmp_path: Path) -> None:
    request, _ = _retained_request(tmp_path)
    document = json.loads(request.read_text())
    document["camera_serial"] = "camera-elsewhere"
    request.write_text(json.dumps(document))
    with pytest.raises(ValueError, match="identity"):
        build_retained(request, tmp_path / "out")


def test_retained_artifact_adapter_refuses_wrong_fixed_head_target(tmp_path: Path) -> None:
    request, _ = _retained_request(tmp_path, target=66364)
    with pytest.raises(ValueError, match="fixed head"):
        build_retained(request, tmp_path / "out")


def test_retained_artifact_adapter_refuses_boot_mismatch(tmp_path: Path) -> None:
    request, _ = _retained_request(tmp_path)
    document = json.loads(request.read_text())
    document["boot_id"] = "other-boot"
    request.write_text(json.dumps(document))
    with pytest.raises(ValueError, match="motion schema or identity"):
        build_retained(request, tmp_path / "out")


def test_retained_artifact_adapter_refuses_unbound_motion_encoder(tmp_path: Path) -> None:
    request, _ = _retained_request(tmp_path)
    document = json.loads(request.read_text())
    motion_pin = document["motions"][0]["record"]
    motion_path = Path(motion_pin["path"])
    motion = json.loads(motion_path.read_text())
    motion["stages"]["before_motion"]["encoder"]["left"] += 17
    motion_path.write_text(json.dumps(motion))
    motion_pin["sha256"] = hashlib.sha256(motion_path.read_bytes()).hexdigest()
    request.write_text(json.dumps(document))
    with pytest.raises(ValueError, match="do not bind"):
        build_retained(request, tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_retained_artifact_adapter_refuses_stale_motion_endpoint(tmp_path: Path) -> None:
    request, _ = _retained_request(tmp_path)
    document = json.loads(request.read_text())
    motion_pin = document["motions"][0]["record"]
    motion_path = Path(motion_pin["path"])
    motion = json.loads(motion_path.read_text())
    encoder = motion["stages"]["before_motion"]["encoder"]
    encoder["left_receipt_ns"] = 1_649_999_999
    encoder["right_receipt_ns"] = 1_650_000_000
    motion_path.write_text(json.dumps(motion))
    motion_pin["sha256"] = hashlib.sha256(motion_path.read_bytes()).hexdigest()
    request.write_text(json.dumps(document))
    with pytest.raises(ValueError, match="direct encoder endpoint is invalid"):
        build_retained(request, tmp_path / "out")


def test_retained_artifact_adapter_refuses_stale_paired_motion_sample(tmp_path: Path) -> None:
    request, _ = _retained_request(tmp_path)
    document = json.loads(request.read_text())
    motion_pin = document["motions"][0]["record"]
    motion_path = Path(motion_pin["path"])
    motion = json.loads(motion_path.read_text())
    stage = motion["stages"]["before_motion"]
    stage["paired_pose_samples"] = [
        {
            "poll_id": 1,
            "pose": stage["pose"],
            "encoder": {
                "left": 1002,
                "right": 2002,
                "left_receipt_ns": 1_649_999_999,
                "right_receipt_ns": 1_650_000_000,
            },
        }
    ]
    motion_path.write_text(json.dumps(motion))
    motion_pin["sha256"] = hashlib.sha256(motion_path.read_bytes()).hexdigest()
    request.write_text(json.dumps(document))
    with pytest.raises(ValueError, match="paired samples are invalid"):
        build_retained(request, tmp_path / "out")
