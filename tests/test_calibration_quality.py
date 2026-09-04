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
        },
        evidence_kind="synthetic",
    )

    with pytest.raises(ValueError, match="poses are insufficiently varied"):
        calibrate(request)
