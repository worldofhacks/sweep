"""Extract independently observed AprilTag module intersections from image pixels."""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from perception.tag_localization import tag_corners

_CELLS = 8
_CANONICAL_SIZE = 800
_MINIMUM_PATTERN_MATCH = 0.95
_MAXIMUM_OUTER_REFINEMENT_PX = 2.0


@dataclass(frozen=True, slots=True)
class ModuleCorners:
    object_points: np.ndarray
    image_points: np.ndarray
    internal_count: int
    pattern_match: float


def extract_module_corners(
    image: np.ndarray, identifier: int, outer_corners: np.ndarray, tag_size_m: float
) -> ModuleCorners | None:
    """Return outer corners plus observed alternating module intersections, if validated."""
    if (
        image.dtype != np.uint8
        or image.ndim not in (2, 3)
        or outer_corners.shape != (4, 2)
        or not np.isfinite(outer_corners).all()
        or not 0 < tag_size_m
    ):
        raise ValueError("invalid tag image, corners, or size")
    gray = image if image.ndim == 2 else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_APRILTAG_36h11)
    if not 0 <= identifier < dictionary.bytesList.shape[0]:
        raise ValueError("invalid tag36h11 identifier")
    observed_outer = _refine_outer_corners(gray, outer_corners)
    if observed_outer is None:
        return None
    source = np.float32(
        [[0, 0], [_CANONICAL_SIZE, 0], [_CANONICAL_SIZE, _CANONICAL_SIZE], [0, _CANONICAL_SIZE]]
    )
    homography = cv2.getPerspectiveTransform(source, observed_outer)
    warped = cv2.warpPerspective(
        gray, homography, (_CANONICAL_SIZE, _CANONICAL_SIZE), flags=cv2.WARP_INVERSE_MAP
    )
    expected = _cells(cv2.aruco.generateImageMarker(dictionary, identifier, _CANONICAL_SIZE))
    actual = _cells(warped)
    pattern_match = float(np.mean(actual == expected))
    if pattern_match < _MINIMUM_PATTERN_MATCH:
        return None

    module_px = (
        min(np.linalg.norm(observed_outer - np.roll(observed_outer, 1, axis=0), axis=1)) / _CELLS
    )
    candidates = cv2.goodFeaturesToTrack(
        gray, maxCorners=4096, qualityLevel=0.0001, minDistance=1, blockSize=3
    )
    if candidates is None:
        return None
    candidates = candidates.reshape(-1, 2).astype(np.float32)
    coordinates = _alternating_coordinates(expected)
    initial = cv2.perspectiveTransform(coordinates.reshape(-1, 1, 2), homography).reshape(-1, 2)
    tolerance = max(2.0, module_px * 0.55)
    observed = []
    object_points = []
    for canonical, estimate in zip(coordinates, initial, strict=True):
        distance = np.linalg.norm(candidates - estimate, axis=1)
        nearest = candidates[np.argmin(distance)]
        if float(np.min(distance)) > tolerance:
            continue
        refined = cv2.cornerSubPix(
            gray,
            nearest.reshape(1, 1, 2).copy(),
            (2, 2),
            (-1, -1),
            (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_COUNT, 30, 0.01),
        ).reshape(2)
        observed.append(refined)
        object_points.append(_object_point(canonical, tag_size_m))
    if len(observed) < 2:
        return None
    return ModuleCorners(
        object_points=np.vstack([tag_corners(tag_size_m), np.asarray(object_points)]).astype(float),
        image_points=np.vstack([observed_outer, np.asarray(observed)]).astype(float),
        internal_count=len(observed),
        pattern_match=pattern_match,
    )


def _refine_outer_corners(gray: np.ndarray, corners: np.ndarray) -> np.ndarray | None:
    try:
        refined = cv2.cornerSubPix(
            gray,
            corners.astype(np.float32).reshape(4, 1, 2).copy(),
            (3, 3),
            (-1, -1),
            (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_COUNT, 40, 0.005),
        ).reshape(4, 2)
    except cv2.error:
        return None
    max_shift = np.max(np.linalg.norm(refined - corners, axis=1))
    if not np.isfinite(refined).all() or max_shift > _MAXIMUM_OUTER_REFINEMENT_PX:
        return None
    return refined


def _cells(image: np.ndarray) -> np.ndarray:
    step = _CANONICAL_SIZE // _CELLS
    return np.array(
        [
            [
                image[
                    y * step + step // 4 : (y + 1) * step - step // 4,
                    x * step + step // 4 : (x + 1) * step - step // 4,
                ].mean()
                < 128
                for x in range(_CELLS)
            ]
            for y in range(_CELLS)
        ]
    )


def _alternating_coordinates(cells: np.ndarray) -> np.ndarray:
    coordinates = []
    step = _CANONICAL_SIZE / _CELLS
    for y in range(1, _CELLS):
        for x in range(1, _CELLS):
            quadrants = cells[y - 1 : y + 1, x - 1 : x + 1]
            if (
                quadrants[0, 0] == quadrants[1, 1]
                and quadrants[0, 1] == quadrants[1, 0]
                and quadrants[0, 0] != quadrants[0, 1]
            ):
                coordinates.append([x * step, y * step])
    return np.asarray(coordinates, dtype=np.float32)


def _object_point(canonical: np.ndarray, tag_size_m: float) -> np.ndarray:
    x, y = canonical / _CANONICAL_SIZE
    return np.array([(x - 0.5) * tag_size_m, (0.5 - y) * tag_size_m, 0.0])
