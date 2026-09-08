from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pytest

import tools.ohmni_lidar_self_calibration as fitter
from tools.ohmni_lidar_self_calibration import (
    _local_segments,
    fit_capture,
    parse_capture,
    predicted_raw_transform,
    run,
)

MOUNT = (-0.395, 0.395, 0.18)


def _rotation(angle: float) -> np.ndarray:
    c, s = math.cos(angle), math.sin(angle)
    return np.array(((c, -s), (s, c)))


def _raw_points(
    world: np.ndarray, pose: tuple[float, float, float], offset_deg: float, sign: int
) -> list[dict[str, float]]:
    position = np.array(pose[:2])
    body = (world - position) @ _rotation(math.radians(pose[2]))
    raw = (body - np.array(MOUNT[:2])) @ _rotation(math.radians(offset_deg)) @ np.diag((1.0, sign))
    result = []
    for x, y in raw:
        angle = math.degrees(math.atan2(y, x)) % 360.0
        result.append({"angle_deg": angle, "distance_mm": math.hypot(x, y) * 1000, "quality": 1.0})
    return result


def _stage(
    pose: tuple[float, float, float],
    world: np.ndarray,
    offset_deg: float,
    sign: int,
    ticks: tuple[int, int],
    time: float,
) -> dict[str, object]:
    points = _raw_points(world, pose, offset_deg, sign)
    return {
        "pose": {"x_m": pose[0], "y_m": pose[1], "yaw_deg": pose[2], "quality": 0.9},
        "encoder": {
            "poll_id": int(time),
            "left": ticks[0],
            "right": ticks[1],
            "left_receipt_ns": int((time - 0.001) * 1e9),
            "right_receipt_ns": int(time * 1e9),
        },
        "revolutions": [
            {"monotonic_s": time - 0.09 + index * 0.01, "points": points} for index in range(10)
        ],
        "stage_started_monotonic_s": time - 0.1,
        "stage_completed_monotonic_s": time + 0.01,
        "max_translation_drift_m": 0.001,
        "max_yaw_drift_deg": 0.1,
        "max_encoder_revolution_delta_s": 0.09,
        "monotonic_clock": "linux_monotonic",
    }


def _capture(offset_deg: float = 37.3, sign: int = -1) -> dict[str, object]:
    rng = np.random.default_rng(9)
    # Three offset rectangles create non-collinear, asymmetric geometry.
    world = np.vstack(
        (
            np.column_stack((rng.uniform(-3, 3, 75), rng.uniform(2, 3, 75))),
            np.column_stack((rng.uniform(-4, -3, 60), rng.uniform(-2, 3, 60))),
            np.column_stack((rng.uniform(1, 4, 55), rng.uniform(-3, -2, 55))),
        )
    )
    baseline = (1.0, -0.5, 17.0)
    forward = (1.22, -0.5 + 0.22 * math.tan(math.radians(17.0)), 17.0)
    yaw = (1.0, -0.5, 38.0)
    return {
        "schema_version": 1,
        "kind": "ohmni_supervised_lidar_calibration_capture",
        "device_id": 11,
        "mount": {"x_m": MOUNT[0], "y_m": MOUNT[1], "z_m": MOUNT[2]},
        "wheel_diameter_mm": 152.4,
        "limits": {
            "wheel_diameter_mm": 152.4,
            "forward_speed_m_s": 0.04,
            "forward_distance_m": 0.08,
            "yaw_rate_deg_s": 10.0,
            "yaw_degrees": 10.0,
            "pulse_duration_s": 0.5,
            "max_wheel_travel_m": 0.18,
            "max_yaw_degrees": 15.0,
            "max_runtime_s": 60.0,
        },
        "boot_id": "test-boot-1",
        "executed_bundle_source_sha256": "a" * 64,
        "stages": {
            "baseline": _stage(baseline, world, offset_deg, sign, (100, 100), 10.0),
            "after_forward": _stage(forward, world, offset_deg, sign, (300, 302), 20.0),
            "after_yaw": _stage(yaw, world, offset_deg, sign, (130, 70), 30.0),
        },
    }


def _multistage_capture(offset_deg: float = 37.3, sign: int = -1) -> dict[str, object]:
    rng = np.random.default_rng(9)
    world = np.vstack(
        (
            np.column_stack((rng.uniform(-3, 3, 75), rng.uniform(2, 3, 75))),
            np.column_stack((rng.uniform(-4, -3, 60), rng.uniform(-2, 3, 60))),
            np.column_stack((rng.uniform(1, 4, 55), rng.uniform(-3, -2, 55))),
        )
    )
    baseline = (1.0, -0.5, 17.0)
    forward = (
        1.0 + 0.4 * math.cos(math.radians(17.0)),
        -0.5 + 0.4 * math.sin(math.radians(17.0)),
        17.0,
    )
    yaw = (1.0, -0.5, 77.0)
    cross_forward = (
        1.0 + 0.4 * math.cos(math.radians(77.0)),
        -0.5 + 0.4 * math.sin(math.radians(77.0)),
        77.0,
    )
    capture = _capture(offset_deg, sign)
    capture["limits"].update(
        {
            "forward_distance_m": 0.4,
            "yaw_degrees": 60.0,
            "max_wheel_travel_m": 1.05,
            "max_yaw_degrees": 70.0,
        }
    )
    capture["stages"] = {
        "baseline": _stage(baseline, world, offset_deg, sign, (100, 100), 10.0),
        "after_forward": _stage(forward, world, offset_deg, sign, (300, 302), 20.0),
        "after_yaw": _stage(yaw, world, offset_deg, sign, (130, 70), 30.0),
        "after_cross_forward": _stage(cross_forward, world, offset_deg, sign, (330, 270), 40.0),
    }
    return capture


@pytest.mark.parametrize(("offset_deg", "sign"), [(37.3, -1), (-72.4, 1)])
def test_fits_asymmetric_synthetic_capture_for_each_angle_handedness(
    offset_deg: float, sign: int
) -> None:
    result = fit_capture(_capture(offset_deg, sign))

    assert result["approval_status"] == "unapproved_candidate"
    assert result["candidate"]["angle_sign"] == sign
    assert result["candidate"]["offset_deg"] == pytest.approx(offset_deg, abs=1.0)
    assert result["metrics"]["offset_uncertainty_deg"] <= 2.0
    assert result["metrics"]["offset_uncertainty_method"] == "local_curvature_ratio"
    assert result["metrics"]["residual_retained_fraction"] == 0.8
    assert result["metrics"]["held_out_rms_m"] < 0.02


def test_predicted_transform_uses_the_raw_to_body_inverse_and_mount_arc() -> None:
    baseline = {"x_m": 1.0, "y_m": 2.0, "yaw_deg": 90.0}
    stage = {"x_m": 1.0, "y_m": 2.0, "yaw_deg": 180.0}

    rotation, translation = predicted_raw_transform(baseline, stage, (0.5, 0.0), -1, 0.0)

    assert rotation == pytest.approx(_rotation(-math.pi / 2))
    assert translation == pytest.approx(np.array((-0.5, -0.5)))


def test_refuses_a_single_wall_even_when_nearest_neighbor_residual_is_small() -> None:
    capture = _capture()
    wall = np.column_stack((np.linspace(-3, 3, 120), np.full(120, 3.0)))
    poses = (
        (1.0, -0.5, 17.0),
        (1.22, -0.5 + 0.22 * math.tan(math.radians(17.0)), 17.0),
        (1.0, -0.5, 38.0),
    )
    for (_name, stage), pose in zip(capture["stages"].items(), poses, strict=True):
        points = _raw_points(wall, pose, 37.3, -1)
        for scan in stage["revolutions"]:
            scan["points"] = points
    result = fit_capture(capture)

    assert result["approval_status"] == "refused"
    assert "single_surface_geometry" in result["refusal_reasons"]


def test_refuses_wrong_pose_with_a_large_residual() -> None:
    capture = _capture()
    capture["stages"]["after_forward"]["pose"]["x_m"] += 1.5

    result = fit_capture(capture)

    assert result["approval_status"] == "refused"
    assert "scan_overlap_or_residual_failure" in result["refusal_reasons"]


def test_refuses_zero_motion_before_any_candidate_is_published() -> None:
    capture = _capture()
    capture["stages"]["after_forward"]["pose"] = dict(capture["stages"]["baseline"]["pose"])
    capture["stages"]["after_yaw"]["pose"] = dict(capture["stages"]["baseline"]["pose"])
    for name in ("after_forward", "after_yaw"):
        capture["stages"][name]["encoder"] = dict(capture["stages"]["baseline"]["encoder"])
        capture["stages"][name]["encoder"]["right_receipt_ns"] = int(
            (20 if name == "after_forward" else 30) * 1e9
        )

    result = fit_capture(capture)

    assert result["approval_status"] == "refused"
    assert "insufficient_translation" in result["refusal_reasons"]
    assert "insufficient_yaw" in result["refusal_reasons"]
    assert "candidate" not in result
    assert result["metrics"]["registration_skipped"] is True


def test_run_creates_one_hashed_result_and_never_replaces_it(tmp_path: Path) -> None:
    capture_path = tmp_path / "capture.json"
    output_path = tmp_path / "candidate.json"
    capture_path.write_text(json.dumps(_capture()))

    result = run(capture_path, output_path)

    assert json.loads(output_path.read_text()) == result
    assert result["input_provenance"]["capture_sha256"]
    with pytest.raises(FileExistsError):
        run(capture_path, output_path)


def test_refuses_stale_timing_drift_and_missing_capture_provenance() -> None:
    capture = _capture()
    stage = capture["stages"]["after_forward"]
    stage["encoder"]["left_receipt_ns"] = int(5e9)
    stage["encoder"]["right_receipt_ns"] = int(5e9)
    stage["max_encoder_revolution_delta_s"] = 15.0
    stage["max_translation_drift_m"] = 0.02
    capture["boot_id"] = None
    capture["executed_bundle_source_sha256"] = None

    result = fit_capture(capture)

    assert result["approval_status"] == "refused"
    assert "encoder_revolution_time_stale" in result["refusal_reasons"]
    assert "stage_translation_drift" in result["refusal_reasons"]
    assert "missing_boot_id" in result["refusal_reasons"]
    assert "missing_executed_bundle_source_sha256" in result["refusal_reasons"]
    assert "candidate" not in result


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("max_translation_drift_m", 0.0011, "stage_translation_drift"),
        ("max_yaw_drift_deg", 0.11, "stage_yaw_drift"),
    ],
)
def test_refuses_stage_motion_beyond_the_settled_capture_bound(
    field: str, value: float, reason: str
) -> None:
    capture = _capture()
    capture["stages"]["after_yaw"][field] = value

    result = fit_capture(capture)

    assert result["approval_status"] == "refused"
    assert reason in result["refusal_reasons"]
    assert "candidate" not in result


def test_refuses_sparse_scan_support_without_attempting_geometry_eigendecomposition() -> None:
    capture = _capture()
    for stage in capture["stages"].values():
        for revolution in stage["revolutions"]:
            revolution["points"] = revolution["points"][:1]

    result = fit_capture(capture)

    assert result["approval_status"] == "refused"
    assert result["refusal_reasons"] == ["sparse_scan_support"]
    assert result["metrics"]["registration_skipped"] is True
    assert result["metrics"]["geometry_eigenvalues_m2"] == []


def test_refuses_sparse_held_out_scans_before_residual_scoring() -> None:
    capture = _capture()
    for stage in capture["stages"].values():
        for revolution in stage["revolutions"][-2:]:
            revolution["points"] = [{"angle_deg": 0.0, "distance_mm": 1_000.0, "quality": 0.0}]

    result = fit_capture(capture)

    assert result["approval_status"] == "refused"
    assert result["refusal_reasons"] == ["sparse_held_out_scan_support"]
    assert result["metrics"]["held_out_point_counts"] == {
        "baseline": 0,
        "after_forward": 0,
        "after_yaw": 0,
    }
    assert "candidate" not in result


def test_local_segments_exclude_a_short_angle_range_discontinuity() -> None:
    angles = np.radians((0.0, 1.0, 1.0, 2.0))
    radii = np.array((1.0, 1.0, 1.0, 4.0))
    points = np.column_stack((radii * np.cos(angles), radii * np.sin(angles)))

    starts, ends = _local_segments(points)

    assert starts == pytest.approx(points[:1])
    assert ends == pytest.approx(points[1:2])


def test_normalizes_an_equivalent_offset_at_the_wrap_boundary() -> None:
    result = fit_capture(_capture(179.6, -1))

    assert result["approval_status"] == "unapproved_candidate"
    assert result["candidate"]["offset_deg"] == pytest.approx(179.6, abs=1.0)
    assert -180.0 <= result["candidate"]["offset_deg"] < 180.0


def test_rejects_noncanonical_provenance_and_modified_fixed_limits() -> None:
    capture = _capture()
    capture["boot_id"] = "\x01"
    with pytest.raises(ValueError, match="boot_id"):
        fit_capture(capture)

    capture = _capture()
    capture["limits"]["max_runtime_s"] = 61.0
    with pytest.raises(ValueError, match="fixed capture bound"):
        fit_capture(capture)


def test_multistage_profile_requires_the_second_independent_translation_stage() -> None:
    capture = _capture()
    capture["limits"].update(
        {
            "forward_distance_m": 0.4,
            "yaw_degrees": 60.0,
            "max_wheel_travel_m": 1.05,
            "max_yaw_degrees": 70.0,
        }
    )

    with pytest.raises(ValueError, match="stages schema"):
        fit_capture(capture)


@pytest.mark.parametrize(("max_runtime_s", "max_yaw_degrees"), ((60.0, 70.0), (90.0, 85.0)))
def test_multistage_profiles_keep_the_legacy_and_extended_budgets_immutable(
    max_runtime_s: float, max_yaw_degrees: float
) -> None:
    capture = _multistage_capture()
    capture["limits"].update({"max_runtime_s": max_runtime_s, "max_yaw_degrees": max_yaw_degrees})

    assert parse_capture(capture)["stage_names"] == (
        "baseline",
        "after_forward",
        "after_yaw",
        "after_cross_forward",
    )


def test_unit12_capture_propagates_an_unqualified_mount_seed_without_changing_unit11() -> None:
    legacy = fit_capture(_capture())
    assert "mount_source" not in legacy
    assert "mount_initialization_only" not in legacy

    capture = _multistage_capture()
    capture["device_id"] = 12
    capture["mount_source"] = "unqualified_legacy_seed"
    result = fit_capture(capture)

    assert result["device_id"] == 12
    assert result["mount_source"] == "unqualified_legacy_seed"
    assert result["mount_initialization_only"] is True

    capture = _capture()
    capture["device_id"] = 12
    capture["mount_source"] = "unqualified_legacy_seed"
    result = fit_capture(capture)
    assert result["approval_status"] == "refused"
    assert result["refusal_reasons"] == ["unit12_requires_multistage_joint_fit"]
    assert "candidate" not in result

    capture = _capture()
    capture["mount_source"] = "unqualified_legacy_seed"
    with pytest.raises(ValueError, match="capture schema"):
        fit_capture(capture)


def _geometry() -> fitter._ScanGeometry:
    angles = np.linspace(0.0, 2 * math.pi, 72, endpoint=False)
    points = np.column_stack((2.0 * np.cos(angles), 2.0 * np.sin(angles)))
    return fitter._ScanGeometry(points, np.empty((0, 2)), np.empty((0, 2)), False)


def test_joint_seed_ties_never_compare_arrays_or_score_outside_the_mount_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_mounts: list[np.ndarray] = []

    def tied_score(*args: object) -> float:
        seen_mounts.append(np.asarray(args[3], dtype=float))
        return 0.0

    monkeypatch.setattr(fitter, "_score_offset", tied_score)
    geometry = _geometry()
    stage = {"pose": {"x_m": 0.0, "y_m": 0.0, "yaw_deg": 0.0}}
    sign, offset, seed = fitter._joint_coarse_seed(
        geometry, stage["pose"], [(stage, geometry)], (0.9, 0.9)
    )
    score, _, fitted_mount = fitter._refine_joint_mount(
        geometry, stage["pose"], [(stage, geometry)], sign, offset, seed
    )

    assert score == 0.0
    assert np.linalg.norm(seed) <= fitter.MAX_FITTED_MOUNT_RADIUS_M
    assert np.linalg.norm(fitted_mount) <= fitter.MAX_FITTED_MOUNT_RADIUS_M
    assert seen_mounts
    assert all(np.linalg.norm(mount) <= fitter.MAX_FITTED_MOUNT_RADIUS_M for mount in seen_mounts)


def test_joint_offset_uncertainty_refuses_the_actual_multistage_fitter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        fitter,
        "_joint_mount_metrics",
        lambda *_args: {
            "joint_hessian_eigenvalues": [1.0, 1.0, 1.0],
            "joint_parameter_uncertainty": {
                "offset_deg": fitter.MAX_OFFSET_UNCERTAINTY_DEG + 0.01,
                "mount_x_m": 0.01,
                "mount_y_m": 0.01,
            },
            "joint_identifiable": True,
        },
    )

    result = fit_capture(_multistage_capture())

    assert result["approval_status"] == "refused"
    assert "joint_offset_uncertainty_too_broad" in result["refusal_reasons"]
    assert "candidate" not in result


def test_joint_competing_basins_profile_distinct_mount_seeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = fitter._refine_joint_mount
    seeds: list[tuple[float, float]] = []

    def record_refinement(*args: object) -> tuple[float, float, np.ndarray]:
        mount = np.asarray(args[5], dtype=float)
        seeds.append((float(mount[0]), float(mount[1])))
        return original(*args)  # type: ignore[arg-type]

    monkeypatch.setattr(fitter, "_refine_joint_mount", record_refinement)

    result = fit_capture(_multistage_capture())

    assert result["metrics"]["joint_competing_basin_count"] >= 2
    assert len(seeds) >= 3
    assert len(set(seeds)) >= 2
    assert all(math.hypot(*seed) <= fitter.MAX_FITTED_MOUNT_RADIUS_M for seed in seeds)
