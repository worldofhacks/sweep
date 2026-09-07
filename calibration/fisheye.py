"""Validated rectification over the rays consumed by an unchanged camera matrix."""

import cv2
import numpy as np


def rectification_maps(camera_matrix, distortion, image_size):
    """Return float32 remaps; refuse radial folds within the output image's ray domain."""
    width, height = image_size
    k = np.asarray(camera_matrix, dtype=float)
    d = np.asarray(distortion, dtype=float).reshape(-1)
    if k.shape != (3, 3) or d.shape != (4,) or not np.isfinite(k).all() or not np.isfinite(d).all():
        raise ValueError("fisheye rectification requires finite K and four distortion coefficients")
    pixels = np.array(
        [[0, 0, 1], [width - 1, 0, 1], [0, height - 1, 1], [width - 1, height - 1, 1]], dtype=float
    )
    try:
        with np.errstate(over="raise", invalid="raise", divide="raise"):
            rays = np.linalg.solve(k, pixels.T).T
            radius = np.max(np.linalg.norm(rays[:, :2] / rays[:, 2, None], axis=1))
            maximum = float(np.arctan(radius) ** 2)
            derivative = np.array([1, 3 * d[0], 5 * d[1], 7 * d[2], 9 * d[3]])
            critical = np.roots([36 * d[3], 21 * d[2], 10 * d[1], 3 * d[0]])
            candidates = [0.0, maximum]
            candidates.extend(
                float(root.real)
                for root in critical
                if abs(root.imag) <= 1e-10 and 0 < root.real < maximum
            )
            slopes = np.polynomial.polynomial.polyval(candidates, derivative)
            if not np.isfinite(slopes).all() or np.min(slopes) <= 1e-8:
                raise ValueError("fisheye radial mapping folds within the rectified image")
    except (FloatingPointError, np.linalg.LinAlgError) as error:
        raise ValueError("fisheye radial mapping is numerically unstable") from error
    maps = cv2.fisheye.initUndistortRectifyMap(k, d, np.eye(3), k, (width, height), cv2.CV_32FC1)
    if any(not np.isfinite(mapping).all() for mapping in maps):
        raise ValueError("fisheye rectification contains nonfinite coordinates")
    return maps
