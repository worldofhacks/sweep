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


def test_calibrate_recovers_known_intrinsics_from_decoded_varied_images(tmp_path: Path) -> None:
    _write_varied_boards(tmp_path)

    result = calibrate(_request(tmp_path))

    matrix = np.asarray(result["camera_matrix"])
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
