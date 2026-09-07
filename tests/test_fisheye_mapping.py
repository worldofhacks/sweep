import numpy as np
import pytest

from calibration.fisheye import rectification_maps


def test_rectification_refuses_a_radial_fold_between_regular_samples():
    k = np.array([[300.0, 0, 320], [0, 300.0, 240], [0, 0, 1]])
    low, high = 0.19999, 0.20001
    d = np.array([-(low + high) / (low * high * 3), 1 / (low * high * 5), 0, 0])
    with pytest.raises(ValueError, match="folds"):
        rectification_maps(k, d, (640, 480))


def test_large_monotonic_coefficient_is_checked_over_the_actual_ray_domain():
    k = np.array([[300.0, 0, 320], [0, 300.0, 240], [0, 0, 1]])
    maps = rectification_maps(k, [20.0, 0, 0, 0], (640, 480))
    assert all(np.isfinite(item).all() and item.shape == (480, 640) for item in maps)
    assert maps[0][240, 320] == 320
    assert maps[1][240, 320] == 240


def test_rectification_refuses_overflowing_coefficients():
    k = np.array([[300.0, 0, 320], [0, 300.0, 240], [0, 0, 1]])
    with pytest.raises(ValueError, match="unstable"):
        rectification_maps(k, [0, 0, 0, 1e308], (640, 480))
