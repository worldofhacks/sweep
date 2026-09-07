from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pytest

from tools.ohmni_lidar_self_calibration import fit_capture, predicted_raw_transform, run

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


@pytest.mark.parametrize(("offset_deg", "sign"), [(37.3, -1), (-72.4, 1)])
def test_fits_asymmetric_synthetic_capture_for_each_angle_handedness(
    offset_deg: float, sign: int
) -> None:
    result = fit_capture(_capture(offset_deg, sign))

    assert result["approval_status"] == "unapproved_candidate"
    assert result["candidate"]["angle_sign"] == sign
    assert result["candidate"]["offset_deg"] == pytest.approx(offset_deg, abs=1.0)
    assert result["metrics"]["offset_uncertainty_deg"] <= 2.0
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
