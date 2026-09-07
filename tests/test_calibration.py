from __future__ import annotations

import json
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

from calibration.cli import main
from calibration.intrinsics import CalibrationRequest, calibrate
from calibration.latency import summarize_latency

_CAMERA_MATRIX = np.array([[920.0, 0.0, 640.0], [0.0, 900.0, 360.0], [0.0, 0.0, 1.0]])
_FISHEYE_IMAGE_SIZE = (640, 480)
_FISHEYE_CAMERA_MATRIX = np.array([[305.0, 0.0, 320.0], [0.0, 300.0, 240.0], [0.0, 0.0, 1.0]])
_FISHEYE_DISTORTION = np.array([[-0.08], [0.006], [0.0], [0.0]])


def _pipeline() -> dict[str, object]:
    return {
        "resolution_px": [1280, 720],
        "codec": "h264",
        "decoder_path": "test-decoder",
        "camera_mode": "fpv",
        "android_device_id": "not_applicable",
        "network_id": "not_applicable",
        "fov_bounds_deg": {"horizontal": [65, 75], "vertical": [40, 48]},
    }


def _write_varied_boards(directory: Path) -> None:
    square_px = 100
    board = np.full((7 * square_px, 10 * square_px), 255, dtype=np.uint8)
    for row in range(7):
        for column in range(10):
            if (row + column) % 2 == 0:
                board[
                    row * square_px : (row + 1) * square_px,
                    column * square_px : (column + 1) * square_px,
                ] = 0
    source = np.float32([[0, 0], [999, 0], [999, 699], [0, 699]])
    board_corners = np.float32([[0, 0, 0], [0.24, 0, 0], [0.24, 0.168, 0], [0, 0.168, 0]])
    for index in range(24):
        rvec = np.array([0.12 + index * 0.009, -0.18 + (index % 6) * 0.06, index * 0.017])
        tvec = np.array(
            [-0.12 + (index % 5) * 0.04, -0.08 + (index % 4) * 0.035, 0.72 + index * 0.012]
        )
        destination, _ = cv2.projectPoints(board_corners, rvec, tvec, _CAMERA_MATRIX, None)
        image = cv2.warpPerspective(
            board,
            cv2.getPerspectiveTransform(source, destination.reshape(-1, 2).astype(np.float32)),
            (1280, 720),
            borderValue=255,
        )
        assert cv2.imwrite(str(directory / f"board-{index}.png"), image)


def _request(directory: Path, pipeline: dict[str, object] | None = None) -> CalibrationRequest:
    return CalibrationRequest(
        images_dir=directory,
        inner_corners=(9, 6),
        square_size_m=0.024,
        camera_serial="fixture-camera",
        pipeline=pipeline or _pipeline(),
        evidence_kind="synthetic",
    )


def _fisheye_pipeline() -> dict[str, object]:
    return {
        "resolution_px": list(_FISHEYE_IMAGE_SIZE),
        "codec": "h264",
        "decoder_path": "test-decoder",
        "camera_mode": "downwardfisheye",
        "android_device_id": "not_applicable",
        "network_id": "not_applicable",
        "fov_bounds_deg": {"horizontal": [170, 250], "vertical": [170, 250]},
    }


def _fisheye_request(
    directory: Path, pipeline: dict[str, object] | None = None
) -> CalibrationRequest:
    return CalibrationRequest(
        images_dir=directory,
        inner_corners=(9, 6),
        square_size_m=0.024,
        camera_serial="fixture-fisheye-camera",
        pipeline=pipeline or _fisheye_pipeline(),
        evidence_kind="synthetic",
        model="fisheye",
    )


def _write_fisheye_boards(
    directory: Path, *, repeated_pose: bool = False, mismatched_lens: bool = False
) -> None:
    width, height = _FISHEYE_IMAGE_SIZE
    y_coordinates, x_coordinates = np.indices((height, width), dtype=np.float64)
    pixels = np.stack([x_coordinates.ravel(), y_coordinates.ravel()], axis=-1).reshape(-1, 1, 2)

    for index in range(20):
        distortion = _FISHEYE_DISTORTION
        rays = np.column_stack(
            [
                cv2.fisheye.undistortPoints(pixels, _FISHEYE_CAMERA_MATRIX, distortion).reshape(
                    -1, 2
                ),
                np.ones(width * height),
            ]
        )
        pose_index = 0 if repeated_pose else index
        rotation_vector = np.array(
            [
                -0.3 + (pose_index % 5) * 0.15,
                -0.25 + (pose_index // 5) * 0.18,
                -0.12 + (pose_index % 4) * 0.08,
            ]
        )
        translation = np.array(
            [
                -0.11 + (pose_index % 5) * 0.045,
                -0.08 + (pose_index % 4) * 0.043,
                0.55 + (pose_index % 4) * 0.07,
            ]
        )
        rotation, _ = cv2.Rodrigues(rotation_vector)
        normal = rotation[:, 2]
        scale = (normal @ translation) / (rays @ normal)
        board_points = (rays * scale[:, None] - translation) @ rotation
        visible = (
            (scale > 0)
            & (board_points[:, 0] >= 0)
            & (board_points[:, 0] < 0.24)
            & (board_points[:, 1] >= 0)
            & (board_points[:, 1] < 0.168)
        )
        squares = np.floor(board_points[:, :2] / 0.024).astype(int)
        image = np.full(width * height, 255, dtype=np.uint8)
        image[visible & ((squares[:, 0] + squares[:, 1]) % 2 == 0)] = 0
        image = image.reshape(height, width)
        if mismatched_lens and index >= 10:
            distorted_x = x_coordinates + 0.0023 * (x_coordinates - width / 2) ** 2 * np.sign(
                x_coordinates - width / 2
            )
            distorted_y = y_coordinates + 0.0023 * (y_coordinates - height / 2) ** 2 * np.sign(
                y_coordinates - height / 2
            )
            image = cv2.remap(
                image,
                distorted_x.astype(np.float32),
                distorted_y.astype(np.float32),
                cv2.INTER_LINEAR,
                borderValue=255,
            )
        if repeated_pose:
            image[0, index] = index
        assert cv2.imwrite(str(directory / f"fisheye-{index}.png"), image)


def test_calibrate_recovers_known_intrinsics_from_decoded_varied_images(tmp_path: Path) -> None:
    _write_varied_boards(tmp_path)

    result = calibrate(_request(tmp_path))

    matrix = np.asarray(result["camera_matrix"])
    assert result["schema_version"] == 1
    assert "model" not in result
    assert result["accepted_image_count"] == 24
    assert result["checkerboard"] == {"inner_corners": [9, 6], "square_size_m": 0.024}
    assert len(result["image_sha256"]) == 24
    assert result["rms_reprojection_error_px"] < 0.5
    assert matrix[0, 0] == pytest.approx(920.0, abs=15.0)
    assert matrix[1, 1] == pytest.approx(900.0, abs=15.0)
    assert matrix[0, 2] == pytest.approx(640.0, abs=15.0)
    assert matrix[1, 2] == pytest.approx(360.0, abs=15.0)
    assert all(0 < value <= 0.05 for value in result["relative_focal_stddev"])
    assert len(result["focal_stddev_px"]) == 2
    assert 65 <= result["pinhole_fov_deg"]["horizontal"] <= 75
    assert 40 <= result["pinhole_fov_deg"]["vertical"] <= 48
    assert result["pipeline"]["fov_bounds_deg"] == _pipeline()["fov_bounds_deg"]


def test_fisheye_calibration_uses_detected_rendered_checkerboards(tmp_path: Path) -> None:
    _write_fisheye_boards(tmp_path)

    result = calibrate(_fisheye_request(tmp_path))

    matrix = np.asarray(result["camera_matrix"])
    assert result["schema_version"] == 2
    assert result["model"] == "fisheye"
    assert result["accepted_image_count"] == 20
    assert len(result["distortion_coefficients"]) == 4
    assert result["rms_reprojection_error_px"] < 0.5
    assert matrix[0, 1] == 0
    assert matrix[0, 0] == pytest.approx(305.0, abs=25.0)
    assert matrix[1, 1] == pytest.approx(300.0, abs=25.0)
    assert result["pipeline"]["fov_bounds_deg"] == _fisheye_pipeline()["fov_bounds_deg"]
    quality = result["quality"]
    assert quality["accepted_image_count"] == quality["minimum_accepted_image_count"] == 20
    assert quality["pose_constraint_ratio"] >= quality["minimum_pose_constraint_ratio"] == 0.005
    assert quality["rms_reprojection_error_px"] == result["rms_reprojection_error_px"]
    assert quality["maximum_rms_reprojection_error_px"] == 0.5
    assert quality["opencv_check_cond"] is True
    assert "pinhole_fov_deg" not in result
    assert "focal_stddev_px" not in result
    assert "relative_focal_stddev" not in result


def test_fisheye_calibration_rejects_repeated_board_pose(tmp_path: Path) -> None:
    _write_fisheye_boards(tmp_path, repeated_pose=True)

    with pytest.raises(
        ValueError, match="insufficiently varied|ill-conditioned|fisheye radial mapping folds"
    ):
        calibrate(_fisheye_request(tmp_path))


def test_fisheye_calibration_rejects_pipeline_resolution_mismatch(tmp_path: Path) -> None:
    _write_fisheye_boards(tmp_path)
    pipeline = _fisheye_pipeline()
    pipeline["resolution_px"] = [1280, 720]

    with pytest.raises(ValueError, match="does not match decoded image size"):
        calibrate(_fisheye_request(tmp_path, pipeline))


def test_calibrate_rejects_insufficient_detected_boards(tmp_path: Path) -> None:
    _write_varied_boards(tmp_path)
    for image in sorted(tmp_path.glob("*.png"))[19:]:
        image.unlink()

    with pytest.raises(ValueError, match="at least 20"):
        calibrate(_request(tmp_path))


def test_calibrate_rejects_pipeline_resolution_mismatch(tmp_path: Path) -> None:
    _write_varied_boards(tmp_path)
    pipeline = _pipeline()
    pipeline["resolution_px"] = [960, 720]

    with pytest.raises(ValueError, match="does not match decoded image size"):
        calibrate(_request(tmp_path, pipeline))


def test_calibrate_rejects_duplicate_decoded_images(tmp_path: Path) -> None:
    _write_varied_boards(tmp_path)
    source = tmp_path / "board-0.png"
    for index in range(1, 24):
        (tmp_path / f"board-{index}.png").write_bytes(source.read_bytes())

    with pytest.raises(ValueError, match="distinct"):
        calibrate(_request(tmp_path))


def test_calibrate_rejects_invalid_inner_corner_api_input(tmp_path: Path) -> None:
    _write_varied_boards(tmp_path)
    request = _request(tmp_path)
    request = CalibrationRequest(
        images_dir=request.images_dir,
        inner_corners=(1, 6),
        square_size_m=request.square_size_m,
        camera_serial=request.camera_serial,
        pipeline=request.pipeline,
        evidence_kind=request.evidence_kind,
    )

    with pytest.raises(ValueError, match="inner corners"):
        calibrate(request)


def test_intrinsics_cli_writes_json_compatible_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_varied_boards(tmp_path)
    pipeline_path = tmp_path / "pipeline.json"
    pipeline_path.write_text(json.dumps(_pipeline()))
    output = tmp_path / "intrinsics_fixture-camera.yaml"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "calibration",
            "intrinsics",
            "--images",
            str(tmp_path),
            "--inner-corners",
            "9x6",
            "--square-size-m",
            "0.024",
            "--camera-serial",
            "fixture-camera",
            "--pipeline",
            str(pipeline_path),
            "--evidence-kind",
            "synthetic",
            "--output",
            str(output),
        ],
    )

    main()

    assert json.loads(output.read_text())["camera_serial"] == "fixture-camera"


def test_fisheye_cli_rejects_poor_fit_without_writing_an_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_fisheye_boards(tmp_path, mismatched_lens=True)
    pipeline_path = tmp_path / "pipeline.json"
    pipeline_path.write_text(json.dumps(_fisheye_pipeline()))
    output = tmp_path / "intrinsics_fixture-fisheye-camera.yaml"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "calibration",
            "intrinsics",
            "--images",
            str(tmp_path),
            "--inner-corners",
            "9x6",
            "--square-size-m",
            "0.024",
            "--camera-serial",
            "fixture-fisheye-camera",
            "--model",
            "fisheye",
            "--pipeline",
            str(pipeline_path),
            "--evidence-kind",
            "synthetic",
            "--output",
            str(output),
        ],
    )

    with pytest.raises(SystemExit, match="RMS reprojection error|fisheye radial mapping folds"):
        main()

    assert not output.exists()


def test_intrinsics_cli_reports_invalid_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_varied_boards(tmp_path)
    pipeline_path = tmp_path / "pipeline.json"
    pipeline = _pipeline()
    pipeline["resolution_px"] = [960, 720]
    pipeline_path.write_text(json.dumps(pipeline))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "calibration",
            "intrinsics",
            "--images",
            str(tmp_path),
            "--inner-corners",
            "9x6",
            "--square-size-m",
            "0.024",
            "--camera-serial",
            "fixture-camera",
            "--pipeline",
            str(pipeline_path),
            "--evidence-kind",
            "synthetic",
            "--output",
            str(tmp_path / "unused.yaml"),
        ],
    )

    with pytest.raises(SystemExit, match="does not match decoded image size"):
        main()


def test_latency_summary_preserves_explicit_measurements_and_provenance() -> None:
    samples = {"duration_ms": 60_000, "samples_ms": [100, 200, 300, 400]}
    result = summarize_latency(
        camera_serial="fixture-camera",
        pipeline=_pipeline(),
        evidence_kind="recorded_live",
        samples=samples,
    )

    assert result["p50_ms"] == 250.0
    assert result["p95_ms"] == 385.0
    assert result["samples_ms"] == samples["samples_ms"]
    assert result["meets_60_second_capture_minimum"] is False


def test_latency_summary_rejects_missing_explicit_duration() -> None:
    with pytest.raises(ValueError, match="duration_ms"):
        summarize_latency(
            camera_serial="fixture-camera",
            pipeline=_pipeline(),
            evidence_kind="synthetic",
            samples={"samples_ms": [100]},
        )
