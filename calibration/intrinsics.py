"""OpenCV checkerboard calibration from decoded image files."""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from math import atan, degrees, isfinite
from pathlib import Path

import cv2
import numpy as np

_IMAGE_SUFFIXES = {".bmp", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}
_MINIMUM_DETECTIONS = 20
_MAXIMUM_RMS_REPROJECTION_ERROR_PX = 0.5
_MINIMUM_POSE_CONSTRAINT_RATIO = 0.005
_MAXIMUM_RELATIVE_FOCAL_STDDEV = 0.05
_FISHEYE_CALIBRATION_FLAGS = (
    cv2.fisheye.CALIB_RECOMPUTE_EXTRINSIC
    | cv2.fisheye.CALIB_CHECK_COND
    | cv2.fisheye.CALIB_FIX_SKEW
)
_FISHEYE_CRITERIA = (
    cv2.TERM_CRITERIA_COUNT + cv2.TERM_CRITERIA_EPS,
    100,
    1e-6,
)


@dataclass(frozen=True, slots=True)
class CalibrationRequest:
    images_dir: Path
    inner_corners: tuple[int, int]
    square_size_m: float
    camera_serial: str
    pipeline: dict[str, object]
    evidence_kind: str
    model: str = "pinhole"


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
            _validate_pipeline_resolution(request.pipeline, image_size)
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

    if len(set(hashes.values())) != len(image_points):
        raise ValueError(
            "checkerboard inputs must be distinct; duplicate decoded images were supplied"
        )

    if request.model == "fisheye":
        return _calibrate_fisheye(request, object_template, image_points, image_size, hashes)

    _validate_pose_diversity(object_template, image_points, image_size)
    rms_error, camera_matrix, distortion, _, _, intrinsic_stddev, _, _ = (
        cv2.calibrateCameraExtended(object_points, image_points, image_size, None, None)
    )
    if not isfinite(float(rms_error)):
        raise ValueError("OpenCV produced a non-finite reprojection error")
    if rms_error >= _MAXIMUM_RMS_REPROJECTION_ERROR_PX:
        raise ValueError(
            f"RMS reprojection error {rms_error:.3f}px exceeds "
            f"{_MAXIMUM_RMS_REPROJECTION_ERROR_PX:.1f}px"
        )
    _validate_calibration_result(camera_matrix, distortion, image_size)
    focal_stddev = intrinsic_stddev.reshape(-1)[:2]
    focal = np.array([camera_matrix[0, 0], camera_matrix[1, 1]])
    relative_focal_stddev = focal_stddev / focal
    if (
        focal_stddev.shape != (2,)
        or not np.isfinite(focal_stddev).all()
        or np.any(focal_stddev <= 0)
        or np.any(relative_focal_stddev > _MAXIMUM_RELATIVE_FOCAL_STDDEV)
    ):
        raise ValueError("focal length uncertainty exceeds 5%; capture more varied, sharper boards")
    fov = {}
    for index, axis in enumerate(("horizontal", "vertical")):
        principal = float(camera_matrix[index, 2])
        fov[axis] = degrees(
            atan(principal / focal[index]) + atan((image_size[index] - principal) / focal[index])
        )
        lower, upper = request.pipeline["fov_bounds_deg"][axis]
        if not lower <= fov[axis] <= upper:
            raise ValueError(f"estimated {axis} FOV is outside declared fov_bounds_deg")

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
        "focal_stddev_px": focal_stddev.tolist(),
        "relative_focal_stddev": relative_focal_stddev.tolist(),
        "pinhole_fov_deg": fov,
        "accepted_image_count": len(image_points),
        "image_sha256": hashes,
    }


def _calibrate_fisheye(
    request: CalibrationRequest,
    object_template: np.ndarray,
    image_points: list[np.ndarray],
    image_size: tuple[int, int],
    hashes: dict[str, str],
) -> dict[str, object]:
    fisheye_object_template = object_template.reshape(-1, 1, 3).astype(np.float64)
    fisheye_object_points = [fisheye_object_template.copy() for _ in image_points]
    fisheye_image_points = [points.reshape(-1, 1, 2).astype(np.float64) for points in image_points]
    try:
        rms_error, camera_matrix, distortion, rotations, translations = cv2.fisheye.calibrate(
            fisheye_object_points,
            fisheye_image_points,
            image_size,
            None,
            None,
            flags=_FISHEYE_CALIBRATION_FLAGS,
            criteria=_FISHEYE_CRITERIA,
        )
    except cv2.error as error:
        raise ValueError(
            "fisheye calibration is ill-conditioned; capture boards tilted in different directions"
        ) from error

    if not isfinite(float(rms_error)):
        raise ValueError("OpenCV produced a non-finite reprojection error")
    _validate_calibration_result(camera_matrix, distortion, image_size, distortion_count=4)
    constraint_ratio = _validate_fisheye_pose_diversity(
        object_template, image_points, camera_matrix, distortion
    )
    reprojection_rms = _fisheye_reprojection_rms(
        fisheye_object_points,
        fisheye_image_points,
        rotations,
        translations,
        camera_matrix,
        distortion,
    )
    if not isfinite(reprojection_rms):
        raise ValueError("fisheye reprojection check produced a non-finite error")
    if reprojection_rms >= _MAXIMUM_RMS_REPROJECTION_ERROR_PX:
        raise ValueError(
            f"RMS reprojection error {reprojection_rms:.3f}px exceeds "
            f"{_MAXIMUM_RMS_REPROJECTION_ERROR_PX:.1f}px"
        )

    return {
        "schema_version": 2,
        "model": "fisheye",
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
        "rms_reprojection_error_px": reprojection_rms,
        "accepted_image_count": len(image_points),
        "image_sha256": hashes,
        "quality": {
            "accepted_image_count": len(image_points),
            "minimum_accepted_image_count": _MINIMUM_DETECTIONS,
            "pose_constraint_ratio": constraint_ratio,
            "minimum_pose_constraint_ratio": _MINIMUM_POSE_CONSTRAINT_RATIO,
            "rms_reprojection_error_px": reprojection_rms,
            "maximum_rms_reprojection_error_px": _MAXIMUM_RMS_REPROJECTION_ERROR_PX,
            "opencv_check_cond": True,
        },
    }


def _validate_pose_diversity(
    object_points: np.ndarray, image_points: list[np.ndarray], image_size: tuple[int, int]
) -> None:
    constraint_ratio = _pose_constraint_ratio(
        object_points,
        [
            (corners.reshape(-1, 2) - np.asarray(image_size, dtype=float) / 2) / max(image_size)
            for corners in image_points
        ],
    )
    if constraint_ratio < _MINIMUM_POSE_CONSTRAINT_RATIO:
        raise ValueError(
            "checkerboard poses are insufficiently varied; capture boards tilted "
            "in different directions, not only translated or rotated in the image plane"
        )


def _validate_fisheye_pose_diversity(
    object_points: np.ndarray,
    image_points: list[np.ndarray],
    camera_matrix: np.ndarray,
    distortion: np.ndarray,
) -> float:
    undistorted = [
        cv2.fisheye.undistortPoints(points.astype(np.float64), camera_matrix, distortion).reshape(
            -1, 2
        )
        for points in image_points
    ]
    constraint_ratio = _pose_constraint_ratio(object_points, undistorted)
    if constraint_ratio < _MINIMUM_POSE_CONSTRAINT_RATIO:
        raise ValueError(
            "fisheye checkerboard poses are insufficiently varied after undistortion; "
            "capture boards tilted in different directions"
        )
    return constraint_ratio


def _pose_constraint_ratio(object_points: np.ndarray, image_points: list[np.ndarray]) -> float:
    def constraint(a: np.ndarray, b: np.ndarray) -> np.ndarray:
        return np.array(
            [
                a[0] * b[0],
                a[0] * b[1] + a[1] * b[0],
                a[1] * b[1],
                a[2] * b[0] + a[0] * b[2],
                a[2] * b[1] + a[1] * b[2],
                a[2] * b[2],
            ]
        )

    constraints = []
    for corners in image_points:
        homography, _ = cv2.findHomography(object_points[:, :2], corners)
        if homography is None or not np.isfinite(homography).all():
            raise ValueError("checkerboard homography could not be estimated")

        # Orthogonal, equal-length board axes constrain the intrinsic conic.
        first, second = homography[:, 0], homography[:, 1]
        for row in (
            constraint(first, second),
            constraint(first, first) - constraint(second, second),
        ):
            norm = np.linalg.norm(row)
            if not np.isfinite(norm) or norm == 0:
                raise ValueError("checkerboard homography is degenerate")
            constraints.append(row / norm)

    singular_values = np.linalg.svd(constraints, compute_uv=False)
    # The conic has five degrees of freedom; its sixth singular value is the nullspace.
    return float(singular_values[-2] / singular_values[0])


def _validate_request(request: CalibrationRequest) -> None:
    if not request.images_dir.is_dir():
        raise ValueError(f"images directory does not exist: {request.images_dir}")
    if not request.camera_serial.strip():
        raise ValueError("camera serial must not be empty")
    if request.evidence_kind not in {"synthetic", "recorded_live"}:
        raise ValueError("evidence kind must be synthetic or recorded_live")
    if request.model not in {"pinhole", "fisheye"}:
        raise ValueError("model must be pinhole or fisheye")
    if not isfinite(request.square_size_m) or request.square_size_m <= 0:
        raise ValueError("square size must be a positive number of meters")
    if (
        not isinstance(request.inner_corners, tuple)
        or len(request.inner_corners) != 2
        or any(
            not isinstance(value, int) or isinstance(value, bool) or value < 3
            for value in request.inner_corners
        )
    ):
        raise ValueError("inner corners must be a two-integer tuple, each value at least three")
    bounds = request.pipeline.get("fov_bounds_deg")
    if not isinstance(bounds, dict) or set(bounds) != {"horizontal", "vertical"}:
        raise ValueError("pipeline fov_bounds_deg must declare horizontal and vertical bounds")
    maximum = 180 if request.model == "pinhole" else 360
    for axis, interval in bounds.items():
        if (
            not isinstance(interval, list)
            or len(interval) != 2
            or any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not isfinite(value)
                for value in interval
            )
            or not 0 < interval[0] < interval[1]
            or interval[1] > maximum
            or (request.model == "pinhole" and interval[1] == maximum)
        ):
            ending = ")" if request.model == "pinhole" else "]"
            raise ValueError(
                f"fov_bounds_deg {axis} must be [minimum, maximum] within (0, {maximum}{ending}"
            )


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
    try:
        return json.loads(json.dumps({**value, **result}, allow_nan=False))
    except (TypeError, ValueError) as error:
        raise ValueError("pipeline must contain finite JSON values") from error


def _validate_pipeline_resolution(pipeline: dict[str, object], image_size: tuple[int, int]) -> None:
    declared = _pipeline(pipeline)["resolution_px"]
    if declared != list(image_size):
        raise ValueError(
            f"pipeline resolution_px {declared} does not match decoded image size "
            f"{list(image_size)}"
        )


def _validate_calibration_result(
    camera_matrix: np.ndarray,
    distortion: np.ndarray,
    image_size: tuple[int, int],
    distortion_count: int | None = None,
) -> None:
    width, height = image_size
    if camera_matrix.shape != (3, 3) or not np.isfinite(camera_matrix).all():
        raise ValueError("OpenCV produced an invalid camera matrix")
    focal_x, focal_y = float(camera_matrix[0, 0]), float(camera_matrix[1, 1])
    principal_x, principal_y = float(camera_matrix[0, 2]), float(camera_matrix[1, 2])
    if (
        focal_x <= 0
        or focal_y <= 0
        or not (0 <= principal_x <= width and 0 <= principal_y <= height)
    ):
        raise ValueError("OpenCV produced implausible camera intrinsics")
    if (
        (distortion_count is not None and distortion.reshape(-1).shape != (distortion_count,))
        or not np.isfinite(distortion).all()
        or (distortion_count is None and np.max(np.abs(distortion)) > 10)
    ):
        raise ValueError("OpenCV produced implausible distortion coefficients")


def _fisheye_reprojection_rms(
    object_points: list[np.ndarray],
    image_points: list[np.ndarray],
    rotations: list[np.ndarray],
    translations: list[np.ndarray],
    camera_matrix: np.ndarray,
    distortion: np.ndarray,
) -> float:
    squared_error = 0.0
    point_count = 0
    for object_corners, detected_corners, rotation, translation in zip(
        object_points, image_points, rotations, translations, strict=True
    ):
        projected, _ = cv2.fisheye.projectPoints(
            object_corners, rotation, translation, camera_matrix, distortion
        )
        residual = projected.reshape(-1, 2) - detected_corners.reshape(-1, 2)
        squared_error += float(np.sum(residual * residual))
        point_count += len(residual)
    if point_count == 0:
        raise ValueError("OpenCV returned no fisheye reprojection points")
    return float(np.sqrt(squared_error / point_count))
