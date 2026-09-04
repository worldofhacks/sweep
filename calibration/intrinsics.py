"""OpenCV checkerboard calibration from decoded image files."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from math import isfinite
from pathlib import Path

import cv2
import numpy as np

_IMAGE_SUFFIXES = {".bmp", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}
_MINIMUM_DETECTIONS = 5


@dataclass(frozen=True, slots=True)
class CalibrationRequest:
    images_dir: Path
    inner_corners: tuple[int, int]
    square_size_m: float
    camera_serial: str
    pipeline: dict[str, object]
    evidence_kind: str


def calibrate(request: CalibrationRequest) -> dict[str, object]:
    _validate_request(request)
    image_paths = _image_paths(request.images_dir)
    object_template = _object_points(request.inner_corners, request.square_size_m)
    object_points: list[np.ndarray] = []
    image_points: list[np.ndarray] = []
    hashes: dict[str, str] = {}
    image_size: tuple[int, int] | None = None

    for path in image_paths:
        image = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
        if image is None:
            continue
        size = (int(image.shape[1]), int(image.shape[0]))
        if image_size is None:
            image_size = size
        if size != image_size:
            raise ValueError(f"image {path} has {size}, expected {image_size}")
        found, corners = cv2.findChessboardCornersSB(image, request.inner_corners)
        if not found or corners is None:
            continue
        object_points.append(object_template.copy())
        image_points.append(corners.astype(np.float32))
        hashes[path.name] = sha256(path.read_bytes()).hexdigest()

    if image_size is None:
        raise ValueError("no decodable image files found")
    if len(image_points) < _MINIMUM_DETECTIONS:
        raise ValueError(
            f"found {len(image_points)} checkerboards; at least {_MINIMUM_DETECTIONS} "
            "varied-pose images are required"
        )

    rms_error, camera_matrix, distortion, _, _ = cv2.calibrateCamera(
        object_points, image_points, image_size, None, None
    )
    if not isfinite(float(rms_error)):
        raise ValueError("OpenCV produced a non-finite reprojection error")

    return {
        "schema_version": 1,
        "status": "offline",
        "evidence_kind": request.evidence_kind,
        "camera_serial": request.camera_serial,
        "pipeline": _pipeline(request.pipeline),
        "checkerboard": {
            "inner_corners": list(request.inner_corners),
            "square_size_m": request.square_size_m,
        },
        "image_size_px": list(image_size),
        "camera_matrix": camera_matrix.tolist(),
        "distortion_coefficients": distortion.reshape(-1).tolist(),
        "rms_reprojection_error_px": float(rms_error),
        "accepted_image_count": len(image_points),
        "image_sha256": hashes,
    }


def _validate_request(request: CalibrationRequest) -> None:
    if not request.images_dir.is_dir():
        raise ValueError(f"images directory does not exist: {request.images_dir}")
    if not request.camera_serial.strip():
        raise ValueError("camera serial must not be empty")
    if request.evidence_kind not in {"synthetic", "recorded_live"}:
        raise ValueError("evidence kind must be synthetic or recorded_live")
    if not isfinite(request.square_size_m) or request.square_size_m <= 0:
        raise ValueError("square size must be a positive number of meters")


def _image_paths(directory: Path) -> list[Path]:
    paths = sorted(path for path in directory.iterdir() if path.suffix.lower() in _IMAGE_SUFFIXES)
    if not paths:
        raise ValueError(f"no supported image files found in {directory}")
    return paths


def _object_points(inner_corners: tuple[int, int], square_size_m: float) -> np.ndarray:
    columns, rows = inner_corners
    points = np.zeros((columns * rows, 3), np.float32)
    points[:, :2] = np.mgrid[0:columns, 0:rows].T.reshape(-1, 2) * square_size_m
    return points


def _pipeline(value: dict[str, object]) -> dict[str, object]:
    required = {
        "resolution_px",
        "codec",
        "decoder_path",
        "camera_mode",
        "android_device_id",
        "network_id",
    }
    missing = sorted(required - value.keys())
    if missing:
        raise ValueError(f"pipeline is missing: {', '.join(missing)}")
    resolution = value["resolution_px"]
    if (
        not isinstance(resolution, list)
        or len(resolution) != 2
        or any(
            not isinstance(item, int) or isinstance(item, bool) or item <= 0 for item in resolution
        )
    ):
        raise ValueError("pipeline resolution_px must be [positive_width, positive_height]")
    result: dict[str, object] = {"resolution_px": resolution}
    for key in required - {"resolution_px"}:
        item = value[key]
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"pipeline {key} must be a non-empty string")
        result[key] = item
    return result
