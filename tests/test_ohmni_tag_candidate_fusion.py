from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from relay.observations import (
    FrameDeclaration,
    FrameRegistry,
    Observation,
    SourceBinding,
    TimingPolicy,
    decode_observation,
    ingest,
)
from tools.ohmni_tag_candidate_fusion import (
    _calibration,
    _mount,
    _registration,
    fuse_observations,
    run,
)

IDENTITY = [
    [1.0, 0.0, 0.0, 0.0],
    [0.0, 1.0, 0.0, 0.0],
    [0.0, 0.0, 1.0, 0.0],
    [0.0, 0.0, 0.0, 1.0],
]
SCOPE = {"session": "survey-1", "device_id": 9, "connection_epoch": 4, "source_id": "ohmni-head"}
CALIBRATION_ID = "sha256:" + "c" * 64


def _digest(path: Path) -> dict[str, str]:
    return {"path": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def _calibration_document() -> dict[str, object]:
    return {
        "schema_version": 2,
        "model": "fisheye",
        "status": "offline",
        "evidence_kind": "recorded_live",
        "camera_serial": "ohmni-head-1",
        "pipeline": {"resolution_px": [640, 480]},
        "checkerboard": {"inner_corners": [9, 6], "square_size_m": 0.02},
        "image_size_px": [640, 480],
        "camera_matrix": [[300.0, 0.0, 320.0], [0.0, 300.0, 240.0], [0.0, 0.0, 1.0]],
        "distortion_coefficients": [0.0, 0.0, 0.0, 0.0],
        "rms_reprojection_error_px": 0.2,
        "accepted_image_count": 20,
        "image_sha256": {f"board-{index}": f"{index:064x}" for index in range(20)},
        "quality": {
            "accepted_image_count": 20,
            "minimum_accepted_image_count": 20,
            "pose_constraint_ratio": 0.01,
            "minimum_pose_constraint_ratio": 0.005,
            "rms_reprojection_error_px": 0.2,
            "maximum_rms_reprojection_error_px": 0.5,
            "opencv_check_cond": True,
        },
    }


def _mount_document() -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "ohmni_camera_mount",
        "measurement_kind": "measured_rigid_mount",
        "camera_serial": "ohmni-head-1",
        "body_frame": "body",
        "camera_frame": "camera",
        "T_body_camera": IDENTITY,
    }


def _registration_document() -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "ohmni_world_registration_candidate",
        "approval_status": "unapproved",
        "source": {"frame": "odom", **SCOPE, "name": "scan", "sha256": "a" * 64},
        "target": {
            "frame": "world",
            "map_id": "lab",
            "map_version": "v1",
            "physical_datum": "tag-0",
            "name": "tape",
            "sha256": "b" * 64,
        },
        "T_target_source": {"dx_m": 0.0, "dy_m": 0.0, "yaw_rad": 0.0},
    }


def _request() -> dict[str, object]:
    return {
        "schema_version": 2,
        "kind": "ohmni_tag_candidate_fusion_request",
        "source_scope": SCOPE,
        "odom_frame": "odom",
        "calibration_id": CALIBRATION_ID,
        "maximum_association_error_ns": 1_000,
        "maximum_translation_spread_m": 0.02,
        "maximum_rotation_spread_rad": 0.02,
        "minimum_observations_per_tag": 2,
        "observations": {"path": "observations.jsonl", "sha256": "0" * 64},
        "calibration": {"path": "calibration.json", "sha256": "0" * 64},
        "mount": {"path": "mount.json", "sha256": "0" * 64},
        "registration": {"path": "registration.json", "sha256": "0" * 64},
        "tape_checkpoint": {
            "schema_version": 1,
            "kind": "independent_tape_checkpoint",
            "measurement_kind": "XYZ_euclidean_distance",
            "tag_ids": [0, 1],
            "measured_distance_m": 1.0,
            "maximum_error_m": 0.05,
        },
    }


def _event(event_id: str, frame: str, capture_ns: int, payload: dict[str, object]) -> Observation:
    raw = {
        "v": 1,
        "type": "observation",
        "event_id": event_id,
        **SCOPE,
        "node_type": "ground",
        "frame": frame,
        "confidence": 0.5,
        "t_capture": {"clock_id": "capture", "unit": "ns", "value": capture_ns},
        "t_source_receipt": {"clock_id": "capture", "unit": "ns", "value": capture_ns + 1},
        "clock_mapping_id": None,
        "payload": payload,
        "t_ingest": capture_ns // 1_000_000,
    }
    return decode_observation(json.dumps(raw))


def _camera(event_id: str, capture_ns: int, image_id: str) -> Observation:
    return _event(
        event_id,
        "camera",
        capture_ns,
        {
            "kind": "camera_frame",
            "image_id": image_id,
            "sha256": "d" * 64,
            "width_px": 640,
            "height_px": 480,
            "calibration_id": CALIBRATION_ID,
        },
    )


def _body(event_id: str, capture_ns: int) -> Observation:
    return _event(
        event_id,
        "odom",
        capture_ns,
        {
            "kind": "pose",
            "pose": {
                "parent_frame": "odom",
                "child_frame": "body",
                "x_m": 0,
                "y_m": 0,
                "z_m": 0,
                "qx": 0,
                "qy": 0,
                "qz": 0,
                "qw": 1,
            },
        },
    )


def _tag(event_id: str, capture_ns: int, image_id: str, tag_id: int, x_m: float) -> Observation:
    return _event(
        event_id,
        "camera",
        capture_ns,
        {
            "kind": "tag_observation",
            "family": "tag36h11",
            "tag_id": tag_id,
            "image_id": image_id,
            "pose_accepted": True,
            "tag_pose": {
                "parent_frame": "camera",
                "child_frame": f"tag:{tag_id}",
                "x_m": x_m,
                "y_m": 0,
                "z_m": 1,
                "qx": 0,
                "qy": 0,
                "qz": 0,
                "qw": 1,
            },
            "covariance_m2": [0.01, 0, 0, 0, 0.01, 0, 0, 0, 0.02],
            "reason": "pose",
            "size_m": 0.16,
            "corners_px": [[100, 100], [120, 100], [120, 120], [100, 120]],
            "pixel_frame": "rectified_camera",
            "reprojection_rms_px": 0.2,
        },
    )


def _fuse(events: list[Observation]) -> dict[str, object]:
    request = _request()
    calibration = _calibration(
        _calibration_document(), {"path": "calibration.json", "sha256": "c" * 64}
    )
    mount = _mount(_mount_document(), calibration)
    registration = _registration(_registration_document(), SCOPE, "odom")
    return fuse_observations(
        events,
        request=request,
        calibration=calibration,
        mount=mount,
        registration=registration,
        input_pins={
            name: {"path": f"{name}.json", "sha256": name[0] * 64}
            for name in ("observations", "calibration", "mount", "registration")
        },
    )


def test_fusion_uses_typed_canonical_observations_and_weighted_pose_chain() -> None:
    events = []
    for tag_id, x_m in ((0, 1.0), (1, 2.0)):
        for index in range(2):
            capture = 1_000_000 + tag_id * 100_000 + index * 10_000
            image = f"image-{tag_id}-{index}"
            events.extend(
                [
                    _camera(f"camera-{tag_id}-{index}", capture, image),
                    _body(f"body-{tag_id}-{index}", capture),
                    _tag(f"tag-{tag_id}-{index}", capture, image, tag_id, x_m),
                ]
            )

    result = _fuse(events)

    assert [item["tag_id"] for item in result["candidates"]] == [0, 1]
    assert result["candidates"][0]["T_world_tag"][0][3] == pytest.approx(1)
    assert result["candidates"][1]["translation_weight_sum_m2_inverse"] == pytest.approx(50)
    assert result["checkpoint"]["status"] == "evaluated"
    assert result["checkpoint"]["passes"] is True


def test_actual_camera_smoke_submissions_with_receipt_only_stay_diagnostic() -> None:
    from tools.ohmni_camera_smoke import SmokeConfig, _submission

    config = SmokeConfig(
        session=SCOPE["session"],
        device_id=SCOPE["device_id"],
        connection_epoch=SCOPE["connection_epoch"],
        source_id=SCOPE["source_id"],
        camera_serial="ohmni-head-1",
        camera_frame="camera",
        tag_sizes_m={0: 0.16},
    )
    camera_submission = _submission(
        config,
        event_id="actual-smoke-camera",
        receipt_clock_id="receipt",
        receipt_ns=100,
        payload={
            "kind": "camera_frame",
            "image_id": "actual-smoke-image",
            "sha256": "d" * 64,
            "width_px": 640,
            "height_px": 480,
            "calibration_id": CALIBRATION_ID,
        },
    )
    tag_submission = _submission(
        config,
        event_id="actual-smoke-tag",
        receipt_clock_id="receipt",
        receipt_ns=100,
        payload={
            "kind": "tag_observation",
            "family": "tag36h11",
            "tag_id": 0,
            "image_id": "actual-smoke-image",
            "pose_accepted": True,
            "tag_pose": {
                "parent_frame": "camera",
                "child_frame": "tag:0",
                "x_m": 1,
                "y_m": 0,
                "z_m": 1,
                "qx": 0,
                "qy": 0,
                "qz": 0,
                "qw": 1,
            },
            "covariance_m2": [0.01, 0, 0, 0, 0.01, 0, 0, 0, 0.02],
            "reason": "pose",
            "size_m": 0.16,
            "corners_px": [[100, 100], [120, 100], [120, 120], [100, 120]],
            "pixel_frame": "rectified_camera",
            "reprojection_rms_px": 0.2,
        },
    )
    frames = FrameRegistry(
        (
            FrameDeclaration("camera", "camera", "right_down_forward", "m", **SCOPE),
            FrameDeclaration("tag:0", "tag", "right_up_outward", "m", **SCOPE),
        )
    )
    binding = SourceBinding(
        **SCOPE,
        node_type="ground",
        allowed_frames=("camera", "tag:0"),
        allowed_payload_kinds=("camera_frame", "tag_observation"),
    )
    events = [
        ingest(
            submission,
            t_ingest=100,
            frames=frames,
            binding=binding,
            mappings={},
            timing=TimingPolicy(0),
        )
        for submission in (camera_submission, tag_submission)
    ]

    result = _fuse(events)

    assert result["candidates"] == []
    assert result["checkpoint"]["status"] == "not_evaluated"
    assert result["diagnostics"] == [
        {"event_id": "actual-smoke-camera", "reason": "missing_capture_time"},
        {"event_id": "actual-smoke-tag", "reason": "missing_capture_time"},
        {"reason": "checkpoint_not_evaluated"},
    ]


def test_run_pins_inputs_and_writes_a_bounded_create_only_candidate(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    events = []
    for tag_id, x_m in ((0, 1.0), (1, 2.0)):
        for index in range(2):
            capture = 1_000_000 + tag_id * 100_000 + index * 10_000
            image = f"image-{tag_id}-{index}"
            events.extend(
                [
                    _camera(f"camera-{tag_id}-{index}", capture, image),
                    _body(f"body-{tag_id}-{index}", capture),
                    _tag(f"tag-{tag_id}-{index}", capture, image, tag_id, x_m),
                ]
            )
    observations = evidence / "observations.jsonl"
    observations.write_bytes(b"".join(event.encode() + b"\n" for event in events))
    calibration = evidence / "calibration.json"
    mount = evidence / "mount.json"
    registration = evidence / "registration.json"
    calibration.write_text(json.dumps(_calibration_document()))
    mount.write_text(json.dumps(_mount_document()))
    registration.write_text(json.dumps(_registration_document()))
    request = _request()
    for name, path in (
        ("observations", observations),
        ("calibration", calibration),
        ("mount", mount),
        ("registration", registration),
    ):
        request[name] = _digest(path)
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(request))
    output = tmp_path / "candidate.json"

    result = run(request_path, evidence, output)

    assert output.exists()
    assert result["checkpoint"]["passes"] is True
    with pytest.raises(FileExistsError):
        run(request_path, evidence, output)
