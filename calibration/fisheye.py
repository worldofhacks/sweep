"""Validated rectification over the rays consumed by an unchanged camera matrix."""

import cv2
import numpy as np


def raw_sensor_radial_invertibility(camera_matrix, distortion, image_size):
    """Assess raw sensor-edge pixels against the initial invertible fisheye branch.

    `image_size` is `(width, height)` in pixel coordinates; invalid inputs raise `ValueError`.
    """
    k, d, width, height = _parameters(camera_matrix, distortion, image_size)
    try:
        radii = _raw_radii(
            k,
            np.array(
                [[0, 0], [width, 0], [0, height], [width, height]],
                dtype=float,
            ),
        )
        theta_limit = _first_monotonic_theta_limit(d)
        radius_limit = _forward_radius(theta_limit, d)
    except (FloatingPointError, np.linalg.LinAlgError) as error:
        raise ValueError("fisheye raw sensor inverse is numerically unstable") from error
    required = float(np.max(radii))
    return {
        "domain": "raw_sensor_pixel_domain",
        "maximum_raw_radius": required,
        "first_monotonic_theta_rad": theta_limit,
        "first_monotonic_max_radius": radius_limit,
        "passes": bool(np.isfinite(radius_limit) and required < radius_limit),
    }


def raw_sensor_rays(camera_matrix, distortion, pixels):
    """Return forward rays for raw sensor pixel coordinates on the initial inverse branch.

    Invalid inputs or pixels outside that branch raise `ValueError`.
    """
    k, d, _, _ = _parameters(camera_matrix, distortion, (1, 1))
    points = np.asarray(pixels, dtype=float).reshape(-1, 2)
    try:
        raw = _raw_coordinates(k, points)
        radii = np.linalg.norm(raw, axis=1)
        theta = np.array([_inverse_radius(radius, d) for radius in radii])
        scale = np.ones_like(radii)
        nonzero = radii > 0
        scale[nonzero] = np.tan(theta[nonzero]) / radii[nonzero]
        rays = np.column_stack((raw * scale[:, None], np.ones(len(raw))))
    except (FloatingPointError, np.linalg.LinAlgError) as error:
        raise ValueError("fisheye raw sensor inverse is numerically unstable") from error
    if not np.isfinite(rays).all():
        raise ValueError("fisheye raw sensor inverse is numerically unstable")
    return rays


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


def _parameters(camera_matrix, distortion, image_size):
    width, height = image_size
    k = np.asarray(camera_matrix, dtype=float)
    d = np.asarray(distortion, dtype=float).reshape(-1)
    if (
        k.shape != (3, 3)
        or d.shape != (4,)
        or not np.isfinite(k).all()
        or not np.isfinite(d).all()
        or type(width) is not int
        or type(height) is not int
        or width <= 0
        or height <= 0
    ):
        raise ValueError(
            "fisheye raw sensor inverse requires finite K, four coefficients, and an image"
        )
    return k, d, width, height


def _raw_coordinates(camera_matrix, pixels):
    homogeneous = np.column_stack((pixels, np.ones(len(pixels))))
    rays = np.linalg.solve(camera_matrix, homogeneous.T).T
    if np.any(np.abs(rays[:, 2]) <= np.finfo(float).eps):
        raise ValueError("fisheye raw sensor inverse has an invalid camera matrix")
    return rays[:, :2] / rays[:, 2, None]


def _raw_radii(camera_matrix, pixels):
    return np.linalg.norm(_raw_coordinates(camera_matrix, pixels), axis=1)


def _forward_radius(theta, distortion):
    squared = theta * theta
    return float(theta * np.polynomial.polynomial.polyval(squared, [1.0, *distortion]))


def _first_monotonic_theta_limit(distortion):
    derivative = np.array(
        [1.0, 3 * distortion[0], 5 * distortion[1], 7 * distortion[2], 9 * distortion[3]]
    )
    critical = np.polynomial.polynomial.polyroots(derivative)
    roots = [
        float(np.sqrt(root.real)) for root in critical if abs(root.imag) <= 1e-10 and root.real > 0
    ]
    return min([*roots, np.pi / 2])


def _inverse_radius(radius, distortion):
    if not np.isfinite(radius) or radius < 0:
        raise ValueError("fisheye raw sensor radius must be finite and nonnegative")
    if radius == 0:
        return 0.0
    limit = _first_monotonic_theta_limit(distortion)
    maximum = _forward_radius(limit, distortion)
    if not np.isfinite(maximum) or radius >= maximum:
        raise ValueError("fisheye raw sensor radius exceeds its initial monotonic inverse branch")
    lower, upper = 0.0, limit
    for _ in range(80):
        midpoint = (lower + upper) / 2
        if _forward_radius(midpoint, distortion) < radius:
            lower = midpoint
        else:
            upper = midpoint
    return (lower + upper) / 2
