from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import cv2
import numpy as np
import pytest

from calibration.intrinsics import CalibrationRequest, calibrate


@pytest.mark.parametrize("tilt", [(0.0, 0.0), (0.2, -0.15)])
def test_translated_scaled_boards_with_parallel_normals_cannot_certify_intrinsics(
    tmp_path: Path, tilt: tuple[float, float]
) -> None:
    camera = np.array([[920.0, 0.0, 640.0], [0.0, 900.0, 360.0], [0.0, 0.0, 1.0]])
    board = np.full((700, 1000), 255, dtype=np.uint8)
    for row in range(7):
        for column in range(10):
            if (row + column) % 2 == 0:
                board[row * 100 : (row + 1) * 100, column * 100 : (column + 1) * 100] = 0
    source = np.float32([[0, 0], [999, 0], [999, 699], [0, 699]])
    corners = np.float32([[0, 0, 0], [0.24, 0, 0], [0.24, 0.168, 0], [0, 0.168, 0]])
    tilt_matrix, _ = cv2.Rodrigues(np.array([*tilt, 0.0]))
    for index in range(24):
        in_plane_rotation, _ = cv2.Rodrigues(np.array([0.0, 0.0, index * 0.017]))
        rotation, _ = cv2.Rodrigues(tilt_matrix @ in_plane_rotation)
        translation = np.array(
            [-0.12 + (index % 5) * 0.04, -0.08 + (index % 4) * 0.035, 0.72 + index * 0.012]
        )
        destination, _ = cv2.projectPoints(corners, rotation, translation, camera, None)
        image = cv2.warpPerspective(
            board,
            cv2.getPerspectiveTransform(source, destination.reshape(-1, 2).astype(np.float32)),
            (1280, 720),
            borderValue=255,
        )
        assert cv2.imwrite(str(tmp_path / f"board-{index}.png"), image)
    assert len({sha256(path.read_bytes()).hexdigest() for path in tmp_path.glob("*.png")}) == 24
    request = CalibrationRequest(
        images_dir=tmp_path,
        inner_corners=(9, 6),
        square_size_m=0.024,
        camera_serial="fixture-camera",
        pipeline={
            "resolution_px": [1280, 720],
            "codec": "h264",
            "decoder_path": "test-decoder",
            "camera_mode": "fpv",
            "android_device_id": "not_applicable",
            "network_id": "not_applicable",
            "fov_bounds_deg": {"horizontal": [65, 75], "vertical": [40, 48]},
        },
        evidence_kind="synthetic",
    )

    with pytest.raises(ValueError, match="poses are insufficiently varied"):
        calibrate(request)


def _quality_request(directory: Path) -> CalibrationRequest:
    return CalibrationRequest(
        images_dir=directory,
        inner_corners=(9, 6),
        square_size_m=0.024,
        camera_serial="fixture-camera",
        pipeline={
            "resolution_px": [1280, 720],
            "codec": "h264",
            "decoder_path": "test-decoder",
            "camera_mode": "fpv",
            "android_device_id": "not_applicable",
            "network_id": "not_applicable",
            "fov_bounds_deg": {"horizontal": [1, 179], "vertical": [1, 179]},
        },
        evidence_kind="synthetic",
    )


@pytest.mark.parametrize("seed", [1, 3, 4, 7, 138])
def test_noisy_nearparallel_detections_reject_uncertain_focal_even_with_broad_fov(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seed: int
) -> None:
    camera = np.array([[920.0, 0.0, 640.0], [0.0, 900.0, 360.0], [0.0, 0.0, 1.0]])
    objects = np.zeros((54, 3), np.float32)
    objects[:, :2] = np.mgrid[0:9, 0:6].T.reshape(-1, 2) * 0.024
    rng = np.random.default_rng(seed)
    detections = []
    for index in range(20):
        if seed == 138:
            rotation = np.array(
                [0.12 + 0.042 * rng.normal(), -0.08 + 0.042 * rng.normal(), rng.uniform(-0.8, 0.8)]
            )
            translation = np.array(
                [rng.uniform(-0.18, 0.18), rng.uniform(-0.12, 0.12), rng.uniform(0.6, 1.15)]
            )
        else:
            rotation = rng.normal(0, 0.042, 3)
            translation = np.array(
                [-0.12 + rng.uniform(0, 0.16), -0.08 + rng.uniform(0, 0.1), rng.uniform(0.72, 1.05)]
            )
        corners, _ = cv2.projectPoints(objects, rotation, translation, camera, None)
        corners += rng.normal(0, 0.36, corners.shape).astype(np.float32)
        detections.append(corners)
        assert cv2.imwrite(
            str(tmp_path / f"frame-{index:02}.png"), np.full((720, 1280), index, np.uint8)
        )
    remaining = iter(detections)
    monkeypatch.setattr(cv2, "findChessboardCornersSB", lambda *_: (True, next(remaining)))

    with pytest.raises(ValueError, match="focal length uncertainty exceeds"):
        calibrate(_quality_request(tmp_path))


@pytest.mark.parametrize(
    "bounds",
    [
        None,
        {},
        {"horizontal": [65, 75]},
        {"horizontal": [0, 75], "vertical": [40, 48]},
        {"horizontal": [75, 65], "vertical": [40, 48]},
        {"horizontal": [65, float("nan")], "vertical": [40, 48]},
        {"horizontal": [True, 75], "vertical": [40, 48]},
    ],
)
def test_intrinsics_requires_valid_declared_fov_before_decoding(
    tmp_path: Path, bounds: object
) -> None:
    request = _quality_request(tmp_path)
    request.pipeline["fov_bounds_deg"] = bounds
    with pytest.raises(ValueError, match="fov_bounds_deg"):
        calibrate(request)


@pytest.mark.parametrize("axis", ["horizontal", "vertical"])
def test_varied_boards_must_match_independently_declared_fov(tmp_path: Path, axis: str) -> None:
    from tests.test_calibration import _write_varied_boards

    _write_varied_boards(tmp_path)
    request = _quality_request(tmp_path)
    request.pipeline["fov_bounds_deg"][axis] = [20, 30]
    with pytest.raises(ValueError, match="outside declared fov_bounds_deg"):
        calibrate(request)
