from __future__ import annotations

import json

import cv2
import numpy as np
import pytest

from calibration.intrinsics import CalibrationRequest, calibrate
from calibration.latency import summarize_latency


def _pipeline() -> dict[str, object]:
    return {
        "resolution_px": [960, 720],
        "codec": "h264",
        "decoder_path": "test-decoder",
        "camera_mode": "fpv",
        "android_device_id": "not_applicable",
        "network_id": "not_applicable",
    }


def _write_varied_boards(directory: pytest.TempPathFactory | object) -> None:
    directory = directory  # type: ignore[assignment]
    board = np.full((700, 1000), 255, dtype=np.uint8)
    square = 70
    for row in range(7):
        for column in range(10):
            if (row + column) % 2 == 0:
                board[
                    row * square : (row + 1) * square,
                    column * square : (column + 1) * square,
                ] = 0
    source = np.float32([[0, 0], [999, 0], [999, 699], [0, 699]])
    destinations = (
        [[120, 90], [845, 75], [875, 650], [90, 670]],
        [[50, 130], [880, 50], [920, 620], [110, 690]],
        [[170, 50], [790, 145], [850, 680], [80, 600]],
        [[75, 85], [910, 100], [830, 610], [130, 665]],
        [[180, 120], [820, 50], [900, 650], [65, 610]],
        [[100, 55], [860, 145], [800, 675], [145, 620]],
    )
    for index, destination in enumerate(destinations):
        image = cv2.warpPerspective(
            board,
            cv2.getPerspectiveTransform(source, np.float32(destination)),
            (960, 720),
            borderValue=255,
        )
        assert cv2.imwrite(str(directory / f"board-{index}.png"), image)


def test_calibrate_uses_decoded_varied_checkerboard_images(tmp_path) -> None:
    _write_varied_boards(tmp_path)

    result = calibrate(
        CalibrationRequest(
            images_dir=tmp_path,
            inner_corners=(9, 6),
            square_size_m=0.024,
            camera_serial="fixture-camera",
            pipeline=_pipeline(),
            evidence_kind="synthetic",
        )
    )

    assert result["status"] == "offline"
    assert result["accepted_image_count"] == 6
    assert result["checkerboard"] == {"inner_corners": [9, 6], "square_size_m": 0.024}
    assert len(result["image_sha256"]) == 6
    assert len(result["camera_matrix"]) == 3
    # These fixture images use arbitrary perspective warps, not a physical lens model.
    assert result["rms_reprojection_error_px"] < 4.0


def test_calibrate_rejects_insufficient_detected_boards(tmp_path) -> None:
    _write_varied_boards(tmp_path)
    for image in sorted(tmp_path.glob("*.png"))[4:]:
        image.unlink()

    with pytest.raises(ValueError, match="at least 5"):
        calibrate(
            CalibrationRequest(
                images_dir=tmp_path,
                inner_corners=(9, 6),
                square_size_m=0.024,
                camera_serial="fixture-camera",
                pipeline=_pipeline(),
                evidence_kind="synthetic",
            )
        )


def test_calibrate_rejects_pipeline_without_decoder_path(tmp_path) -> None:
    _write_varied_boards(tmp_path)
    pipeline = _pipeline()
    del pipeline["decoder_path"]

    with pytest.raises(ValueError, match="decoder_path"):
        calibrate(
            CalibrationRequest(
                images_dir=tmp_path,
                inner_corners=(9, 6),
                square_size_m=0.024,
                camera_serial="fixture-camera",
                pipeline=pipeline,
                evidence_kind="synthetic",
            )
        )


def test_latency_summary_uses_only_explicit_measurements() -> None:
    result = summarize_latency(
        camera_serial="fixture-camera",
        pipeline=_pipeline(),
        evidence_kind="recorded_live",
        samples={"duration_ms": 60_000, "samples_ms": [100, 200, 300, 400]},
    )

    assert result["p50_ms"] == 250.0
    assert result["p95_ms"] == 385.0
    assert result["meets_60_second_capture_minimum"] is True


def test_latency_summary_rejects_missing_explicit_duration() -> None:
    with pytest.raises(ValueError, match="duration_ms"):
        summarize_latency(
            camera_serial="fixture-camera",
            pipeline=_pipeline(),
            evidence_kind="synthetic",
            samples=json.loads('{"samples_ms": [100]}'),
        )
