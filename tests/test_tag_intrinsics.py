from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np
import pytest

from calibration.tag_intrinsics import (
    TagCandidateRequest,
    calibrate_tag_candidate,
    export_tag_calibration,
)
from perception.camera_tags import CameraTagDetector
from perception.tag_localization import tag_corners


def _pipeline() -> dict[str, object]:
    return {
        "resolution_px": [1280, 720],
        "codec": "h264",
        "decoder_path": "fixture",
        "camera_mode": "fpv",
        "android_device_id": "fixture",
        "network_id": "fixture",
        "fov_bounds_deg": {"horizontal": [40, 100], "vertical": [30, 80]},
    }


def _evidence(path: Path, rotations: list[np.ndarray]) -> None:
    camera = np.array([[850.0, 0.0, 640.0], [0.0, 830.0, 360.0], [0.0, 0.0, 1.0]])
    frames = []
    for index, rotation in enumerate(rotations):
        pixels, _ = cv2.projectPoints(
            tag_corners(0.199898),
            rotation,
            np.array([(index % 5 - 2) * 0.08, (index % 4 - 1.5) * 0.06, 1.4 + index * 0.02]),
            camera,
            None,
        )
        frames.append(
            {
                "frame_index": index * 10,
                "shape_px": [1280, 720],
                "tag_ids": [index],
                "corners_px": [pixels.reshape(4, 2).tolist()],
            }
        )
    path.write_text(json.dumps({"frames": frames}))


def test_tag_candidate_recovers_varied_square_intrinsics(tmp_path: Path) -> None:
    evidence = tmp_path / "corners.json"
    rotations = [
        np.array([0.35 * np.sin(index), 0.35 * np.cos(index), 0.1 * index])
        for index in range(25)
    ]
    _evidence(evidence, rotations)

    result = calibrate_tag_candidate(
        TagCandidateRequest(evidence=evidence, tag_size_m=0.199898, pipeline=_pipeline())
    )

    assert result["status"] == "candidate"
    assert result["selected_observation_count"] == 25
    assert result["camera_matrix"][0][0] == pytest.approx(850.0, rel=0.01)
    assert result["camera_matrix"][1][1] == pytest.approx(830.0, rel=0.01)


def test_tag_candidate_rejects_parallel_square_views(tmp_path: Path) -> None:
    evidence = tmp_path / "corners.json"
    _evidence(evidence, [np.zeros(3) for _ in range(25)])

    result = calibrate_tag_candidate(
        TagCandidateRequest(evidence=evidence, tag_size_m=0.199898, pipeline=_pipeline())
    )

    assert result["status"] == "rejected"
    assert "square homographies are insufficiently varied" in result["rejection_reasons"]


def test_tag_candidate_refuses_single_square_fisheye_fit(tmp_path: Path) -> None:
    evidence = tmp_path / "corners.json"
    _evidence(
        evidence,
        [
            np.array([0.35 * np.sin(index), 0.35 * np.cos(index), 0.1 * index])
            for index in range(25)
        ],
    )

    result = calibrate_tag_candidate(
        TagCandidateRequest(
            evidence=evidence, tag_size_m=0.199898, pipeline=_pipeline(), model="fisheye"
        )
    )

    assert result["status"] == "rejected"
    assert "fisheye fitting requires --frames-dir" in result["rejection_reasons"][0]

    with pytest.raises(ValueError, match="missing or mismatched frame image"):
        calibrate_tag_candidate(
            TagCandidateRequest(
                evidence=evidence,
                tag_size_m=0.199898,
                pipeline=_pipeline(),
                model="fisheye",
                frames_dir=tmp_path,
            )
        )


def test_fisheye_heldout_reprojection_calls_opencv_and_rejects_perturbed_corners() -> None:
    from calibration.tag_intrinsics import _fisheye_heldout_rms

    camera = np.array([[500.0, 0.0, 640.0], [0.0, 505.0, 360.0], [0.0, 0.0, 1.0]])
    distortion = np.array([[-0.08], [0.01], [0.0], [0.0]])
    objects = np.array(
        [
            [0.0, 0.0, 0.0],
            [0.1, 0.0, 0.0],
            [0.2, 0.0, 0.0],
            [0.0, 0.1, 0.0],
            [0.1, 0.1, 0.0],
            [0.2, 0.1, 0.0],
        ]
    ).reshape(-1, 1, 3)
    pixels, _ = cv2.fisheye.projectPoints(
        objects,
        np.array([[0.25], [-0.2], [0.1]]),
        np.array([[0.02], [-0.03], [0.8]]),
        camera,
        distortion,
    )

    accurate = _fisheye_heldout_rms([objects], [pixels], camera, distortion)
    perturbed = pixels.copy()
    perturbed[0, 0] += [8.0, -7.0]
    corrupted = _fisheye_heldout_rms([objects], [perturbed], camera, distortion)

    assert accurate < 1e-4
    assert corrupted > 0.5


def test_exported_apriltag_pinhole_calibration_loads_and_detects(tmp_path: Path) -> None:
    evidence = tmp_path / "corners.json"
    rotations = [
        np.array([0.35 * np.sin(index), 0.35 * np.cos(index), 0.1 * index])
        for index in range(25)
    ]
    _evidence(evidence, rotations)
    frames = tmp_path / "frames"
    frames.mkdir()
    for index in range(25):
        source = np.full((720, 1280, 3), 255, np.uint8)
        source[0, 0, 0] = index
        assert cv2.imwrite(str(frames / f"frame-{index * 10:06}.png"), source)
    artifact = export_tag_calibration(
        TagCandidateRequest(
            evidence=evidence, tag_size_m=0.199898, pipeline=_pipeline(), frames_dir=frames
        ),
        camera_serial="fixture-camera",
        evidence_kind="synthetic",
        allow_synthetic=True,
    )
    detector = CameraTagDetector(
        artifact, camera_serial="fixture-camera", tag_sizes_m={7: 0.199898}, allow_synthetic=True
    )
    marker = cv2.aruco.generateImageMarker(
        cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_APRILTAG_36h11), 7, 300
    )
    image = np.full((720, 1280), 255, np.uint8)
    image[210:510, 490:790] = marker

    observations = detector.detect(image)

    assert observations[0]["tag_id"] == 7
