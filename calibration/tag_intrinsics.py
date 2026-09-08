"""Diagnostic pinhole-intrinsics candidates from recorded AprilTag corners."""

from __future__ import annotations

import json
from dataclasses import dataclass
from math import atan, degrees, isfinite
from pathlib import Path

import cv2
import numpy as np

from calibration.intrinsics import (
    _FISHEYE_CALIBRATION_FLAGS,
    _FISHEYE_CRITERIA,
    _MAXIMUM_RELATIVE_FOCAL_STDDEV,
    _MAXIMUM_RMS_REPROJECTION_ERROR_PX,
    _MINIMUM_POSE_CONSTRAINT_RATIO,
    _pipeline,
    _pose_constraint_ratio,
)
from calibration.tag_modules import extract_module_corners
from perception.tag_localization import tag_corners

_MINIMUM_VIEWS = 20
_MINIMUM_EDGE_PX = 60.0


@dataclass(frozen=True, slots=True)
class TagCandidateRequest:
    evidence: Path
    tag_size_m: float
    pipeline: dict[str, object]
    minimum_frame_gap: int = 8
    maximum_views: int = 30
    model: str = "pinhole"
    frames_dir: Path | None = None


def calibrate_tag_candidate(request: TagCandidateRequest) -> dict[str, object]:
    """Fit a diagnostic candidate; this never creates a usable calibration artifact."""
    if not isfinite(request.tag_size_m) or request.tag_size_m <= 0:
        raise ValueError("tag size must be a positive number of meters")
    if request.model not in {"pinhole", "fisheye"}:
        raise ValueError("model must be pinhole or fisheye")
    if request.minimum_frame_gap < 1 or request.maximum_views < _MINIMUM_VIEWS:
        raise ValueError("minimum frame gap must be positive and maximum views must be at least 20")
    pipeline = _pipeline(request.pipeline)
    try:
        document = json.loads(request.evidence.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read tag evidence: {error}") from error
    if not isinstance(document, dict) or not isinstance(document.get("frames"), list):
        raise ValueError("tag evidence must contain a frames array")

    image_size, observations = _observations(document["frames"])
    if pipeline["resolution_px"] != list(image_size):
        raise ValueError("pipeline resolution_px does not match tag evidence")
    selected = _select(observations, request.minimum_frame_gap, request.maximum_views)
    report: dict[str, object] = {
        "kind": "apriltag_intrinsics_candidate",
        "status": "rejected",
        "source_evidence": str(request.evidence),
        "pipeline": pipeline,
        "image_size_px": list(image_size),
        "tag_black_square_edge_m": request.tag_size_m,
        "model": request.model,
        "raw_observation_count": len(observations),
        "selected_observation_count": len(selected),
        "minimum_selected_observation_count": _MINIMUM_VIEWS,
        "minimum_shortest_edge_px": _MINIMUM_EDGE_PX,
        "selection": {
            "minimum_frame_gap": request.minimum_frame_gap,
            "maximum_views": request.maximum_views,
            "frames": [item[0] for item in selected],
            "tag_ids": [item[1] for item in selected],
        },
        "rejection_reasons": [],
    }
    reasons: list[str] = report["rejection_reasons"]  # type: ignore[assignment]
    if len(selected) < _MINIMUM_VIEWS:
        reasons.append("fewer than 20 separated raw four-corner observations")
        return report

    image_points = [pixels.astype(np.float32) for _, _, pixels in selected]
    object_points = [tag_corners(request.tag_size_m).astype(np.float32) for _ in selected]
    normalized = [
        (points - np.asarray(image_size, dtype=float) / 2) / max(image_size)
        for points in image_points
    ]
    ratio = _pose_constraint_ratio(object_points[0], normalized)
    report["pose_constraint_ratio"] = ratio
    report["minimum_pose_constraint_ratio"] = _MINIMUM_POSE_CONSTRAINT_RATIO
    if ratio < _MINIMUM_POSE_CONSTRAINT_RATIO:
        reasons.append("square homographies are insufficiently varied")
        return report

    if request.model == "fisheye":
        if request.frames_dir is None:
            reasons.append("fisheye fitting requires --frames-dir with the raw images")
            return report
        module_views = _module_views(selected, request.frames_dir, request.tag_size_m, image_size)
        if len(module_views) < _MINIMUM_VIEWS:
            reasons.append("fewer than 20 frames have six validated observed tag corners")
            return report
        objects = [view[0].reshape(-1, 1, 3).astype(np.float64) for view in module_views]
        pixels = [view[1].reshape(-1, 1, 2).astype(np.float64) for view in module_views]
        try:
            rms, camera_matrix, distortion, _, _ = cv2.fisheye.calibrate(
                objects, pixels, image_size, None, None, flags=_FISHEYE_CALIBRATION_FLAGS,
                criteria=_FISHEYE_CRITERIA,
            )
        except cv2.error:
            reasons.append("fisheye calibration is ill-conditioned")
            return report
        report.update(
            rms_reprojection_error_px=float(rms),
            camera_matrix=camera_matrix.tolist(),
            distortion_coefficients=distortion.reshape(-1).tolist(),
            validated_module_view_count=len(module_views),
        )
        if not isfinite(float(rms)) or rms >= _MAXIMUM_RMS_REPROJECTION_ERROR_PX:
            reasons.append("RMS reprojection error is at least 0.5 pixels")
        if not reasons:
            reasons.append(
                "fisheye fit is unqualified without held-out error, stability, and FOV validation"
            )
        return report
    rms, camera_matrix, distortion, _, _, stddev, _, _ = cv2.calibrateCameraExtended(
        object_points, image_points, image_size, None, None
    )
    report["rms_reprojection_error_px"] = float(rms)
    report["camera_matrix"] = camera_matrix.tolist()
    report["distortion_coefficients"] = distortion.reshape(-1).tolist()
    focal = np.array([camera_matrix[0, 0], camera_matrix[1, 1]])
    focal_stddev = stddev.reshape(-1)[:2]
    relative = focal_stddev / focal
    report["focal_stddev_px"] = focal_stddev.tolist()
    report["relative_focal_stddev"] = relative.tolist()
    fov = _fov(camera_matrix, image_size)
    report["pinhole_fov_deg"] = fov
    if not isfinite(float(rms)) or rms >= _MAXIMUM_RMS_REPROJECTION_ERROR_PX:
        reasons.append("RMS reprojection error is at least 0.5 pixels")
    if (
        focal_stddev.shape != (2,)
        or not np.isfinite(focal_stddev).all()
        or np.any(focal_stddev <= 0)
        or np.any(relative > _MAXIMUM_RELATIVE_FOCAL_STDDEV)
    ):
        reasons.append("focal length uncertainty exceeds 5 percent")
    bounds = pipeline.get("fov_bounds_deg")
    if not isinstance(bounds, dict):
        reasons.append("pipeline omits independent FOV bounds")
    else:
        for axis in ("horizontal", "vertical"):
            interval = bounds.get(axis)
            if not isinstance(interval, list) or not interval[0] <= fov[axis] <= interval[1]:
                reasons.append(f"estimated {axis} FOV is outside declared bounds")
    if not reasons:
        report["status"] = "candidate"
    return report


def _observations(
    frames: list[object],
) -> tuple[tuple[int, int], list[tuple[int, int, np.ndarray]]]:
    image_size: tuple[int, int] | None = None
    observations: list[tuple[int, int, np.ndarray]] = []
    for raw in frames:
        if not isinstance(raw, dict):
            continue
        shape, identifiers, corners = raw.get("shape_px"), raw.get("tag_ids"), raw.get("corners_px")
        index = raw.get("frame_index")
        if (
            not isinstance(shape, list)
            or len(shape) != 2
            or any(type(value) is not int or value <= 0 for value in shape)
            or type(index) is not int
            or not isinstance(identifiers, list)
            or not isinstance(corners, list)
            or len(identifiers) != len(corners)
        ):
            continue
        size = (shape[0], shape[1])
        if image_size is None:
            image_size = size
        elif size != image_size:
            raise ValueError("tag evidence contains more than one image resolution")
        for identifier, corner_set in zip(identifiers, corners, strict=True):
            pixels = np.asarray(corner_set, dtype=float)
            if (
                type(identifier) is not int
                or pixels.shape != (4, 2)
                or not np.isfinite(pixels).all()
            ):
                continue
            edges = np.linalg.norm(pixels - np.roll(pixels, 1, axis=0), axis=1)
            if np.min(edges) >= _MINIMUM_EDGE_PX:
                observations.append((index, identifier, pixels))
    if image_size is None:
        raise ValueError("tag evidence contains no decoded frames")
    return image_size, observations


def _select(
    observations: list[tuple[int, int, np.ndarray]], minimum_frame_gap: int, maximum_views: int
) -> list[tuple[int, int, np.ndarray]]:
    selected: list[tuple[int, int, np.ndarray]] = []
    for item in observations:
        if len(selected) == maximum_views:
            break
        if all(abs(item[0] - prior[0]) >= minimum_frame_gap for prior in selected):
            selected.append(item)
    return selected


def _module_views(
    selected: list[tuple[int, int, np.ndarray]], frames_dir: Path, tag_size_m: float,
    image_size: tuple[int, int],
) -> list[tuple[np.ndarray, np.ndarray]]:
    if not frames_dir.is_dir():
        raise ValueError("frames directory does not exist")
    views = []
    for index, identifier, corners in selected:
        path = frames_dir / f"frame-{index:06}.png"
        image = cv2.imread(str(path))
        if image is None or (image.shape[1], image.shape[0]) != image_size:
            raise ValueError(f"missing or mismatched frame image: {path}")
        module = extract_module_corners(image, identifier, corners, tag_size_m)
        if module is not None:
            views.append((module.object_points, module.image_points))
    return views


def _fov(camera_matrix: np.ndarray, image_size: tuple[int, int]) -> dict[str, float]:
    result = {}
    for index, axis in enumerate(("horizontal", "vertical")):
        focal, principal = camera_matrix[index, index], camera_matrix[index, 2]
        result[axis] = degrees(
            atan(principal / focal) + atan((image_size[index] - principal) / focal)
        )
    return result
