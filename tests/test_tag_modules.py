from __future__ import annotations

import cv2
import numpy as np

from calibration.tag_modules import extract_module_corners


def _render(identifier: int) -> tuple[np.ndarray, np.ndarray]:
    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_APRILTAG_36h11)
    tag = cv2.aruco.generateImageMarker(dictionary, identifier, 800)
    image = np.full((960, 1280), 255, np.uint8)
    corners = np.float32([[300, 160], [900, 210], [840, 800], [250, 750]])
    transform = cv2.getPerspectiveTransform(
        np.float32([[0, 0], [800, 0], [800, 800], [0, 800]]), corners
    )
    rendered = cv2.warpPerspective(
        tag, transform, (1280, 960), dst=image, borderMode=cv2.BORDER_TRANSPARENT
    )
    return rendered, corners


def test_module_intersections_are_observed_features_not_homography_points(monkeypatch) -> None:
    image, corners = _render(0)
    result = extract_module_corners(image, 0, corners, 0.199898)
    assert result is not None
    assert result.internal_count == 4
    assert len(result.object_points) == len(result.image_points) == 8
    assert result.pattern_match == 1.0

    monkeypatch.setattr(cv2, "goodFeaturesToTrack", lambda *_args, **_kwargs: None)
    assert extract_module_corners(image, 0, corners, 0.199898) is None


def test_module_intersections_reject_a_tag_with_wrong_payload() -> None:
    image, corners = _render(0)
    assert extract_module_corners(image, 1, corners, 0.199898) is None
