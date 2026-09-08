import numpy as np
import pytest

from calibration.fisheye import raw_sensor_radial_invertibility, rectification_maps
from calibration.tag_intrinsics import _fisheye_fov, _unknown_fov_qualification


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


def test_raw_sensor_inverse_refuses_the_unit11_candidate_before_its_first_radial_fold(tmp_path):
    k = np.array([[642.6894077, 0, 595.5427391], [0, 651.1459971, 362.7115196], [0, 0, 1]])
    d = np.array([-0.0734643443, 0.0453555594, -0.0494220576, 0.0015622331])

    qualification = raw_sensor_radial_invertibility(k, d, (1280, 720))

    assert qualification == {
        "domain": "raw_sensor_pixel_domain",
        "maximum_raw_radius": pytest.approx(1.20186964),
        "first_monotonic_theta_rad": pytest.approx(1.24411637),
        "first_monotonic_max_radius": pytest.approx(1.02098393),
        "passes": False,
    }
    assert all(np.isfinite(item).all() for item in rectification_maps(k, d, (1280, 720)))
    with pytest.raises(ValueError, match="initial monotonic inverse branch"):
        _fisheye_fov(k, d, (1280, 720))
    unknown_fov = _unknown_fov_qualification(
        {},
        tmp_path,
        [],
        [],
        [np.array([[[100.0, 100.0]], [[200.0, 100.0]], [[200.0, 200.0]], [[100.0, 200.0]]])],
        k,
        d,
        (1280, 720),
        0.2,
    )
    assert unknown_fov["passes"] is False
    assert unknown_fov["raw_sensor_radial_invertibility"]["passes"] is False


def test_raw_sensor_inverse_accepts_a_monotonic_fisheye_fit():
    k = np.array([[600.0, 0, 640], [0, 595.0, 360], [0, 0, 1]])
    d = np.array([-0.08, 0.01, -0.001, 0.0001])

    qualification = raw_sensor_radial_invertibility(k, d, (1280, 720))

    assert qualification["passes"] is True
    fov = _fisheye_fov(k, d, (1280, 720))
    assert 0 < fov["horizontal"] < 180
    assert 0 < fov["vertical"] < 180
