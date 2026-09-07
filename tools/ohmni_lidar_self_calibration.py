"""Fit an unapproved Ohmni lidar mounting candidate from three settled captures."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from collections.abc import Mapping, Sequence
from pathlib import Path

import numpy as np

from tools.map_common import finite_number, parse_document

SCHEMA_VERSION = 1
KIND = "ohmni_supervised_lidar_calibration_capture"
DEVICE_ID = 11
WHEEL_DIAMETER_MM = 152.4
STAGE_NAMES = ("baseline", "after_forward", "after_yaw")
SCANS_PER_STAGE = 10
MAX_POINTS_PER_SCAN = 2_000
MAX_STAGE_POINTS = 120
MIN_STAGE_POINTS = 80
MIN_TRANSLATION_M = 0.04
MIN_YAW_RAD = 0.1
MAX_ENCODER_REVOLUTION_DELTA_S = 2.0
MAX_RMS_M = 0.12
MAX_HELD_OUT_RMS_M = 0.15
MAX_OFFSET_UNCERTAINTY_DEG = 5.0
MIN_GEOMETRY_RANK = 0.03
MAX_INPUT_BYTES = 8 * 1024 * 1024
MAX_OUTPUT_BYTES = 512 * 1024
EXPECTED_LIMITS = {
    "wheel_diameter_mm": 152.4,
    "forward_speed_m_s": 0.04,
    "forward_distance_m": 0.08,
    "yaw_rate_deg_s": 10.0,
    "yaw_degrees": 10.0,
    "pulse_duration_s": 0.5,
    "max_wheel_travel_m": 0.18,
    "max_yaw_degrees": 15.0,
    "max_runtime_s": 60.0,
}


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _number(value: object, name: str, *, minimum: float | None = None) -> float:
    result = finite_number(value, name)
    if abs(result) > 1_000_000:
        raise ValueError(f"{name} exceeds the metric bound")
    if minimum is not None and result < minimum:
        raise ValueError(f"{name} is below its lower bound")
    return result


def _integer(value: object, name: str, *, minimum: int = 0, maximum: int = 2**63 - 1) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer in range")
    return value


def _exact(value: object, keys: set[str], name: str) -> Mapping[str, object]:
    _require(isinstance(value, Mapping) and set(value) == keys, f"{name} schema is invalid")
    return value


def _pose(value: object, name: str) -> dict[str, float]:
    raw = _exact(value, {"x_m", "y_m", "yaw_deg", "quality"}, name)
    result = {key: _number(raw[key], f"{name}.{key}") for key in ("x_m", "y_m", "yaw_deg")}
    result["quality"] = _number(raw["quality"], f"{name}.quality", minimum=0.0)
    _require(result["quality"] <= 1.0, f"{name}.quality must be at most one")
    return result


def _pin(value: object, name: str) -> str | None:
    if value is None:
        return None
    if (
        type(value) is not str
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise ValueError(f"{name} must be a lowercase SHA-256 or null")
    return value


def _boot_id(value: object) -> str | None:
    if value is None:
        return None
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or not value.isprintable()
        or len(value) > 128
    ):
        raise ValueError("boot_id must be canonical text or null")
    return value


def _point(value: object, name: str) -> tuple[float, float] | None:
    raw = _exact(value, {"angle_deg", "distance_mm", "quality"}, name)
    angle = _number(raw["angle_deg"], f"{name}.angle_deg")
    distance = _number(raw["distance_mm"], f"{name}.distance_mm", minimum=0.0)
    quality = _number(raw["quality"], f"{name}.quality", minimum=0.0)
    _require(0.0 <= angle < 360.0, f"{name}.angle_deg is outside the RPLIDAR domain")
    if quality == 0.0 or not 150.0 <= distance <= 12_000.0:
        return None
    radians = math.radians(angle)
    return distance * math.cos(radians) / 1_000.0, distance * math.sin(radians) / 1_000.0


def _stage(value: object, name: str) -> dict[str, object]:
    raw = _exact(
        value,
        {
            "pose",
            "encoder",
            "revolutions",
            "stage_started_monotonic_s",
            "stage_completed_monotonic_s",
            "max_translation_drift_m",
            "max_yaw_drift_deg",
            "max_encoder_revolution_delta_s",
            "monotonic_clock",
        },
        name,
    )
    pose = _pose(raw["pose"], f"{name}.pose")
    encoder = _exact(
        raw["encoder"],
        {"poll_id", "left", "right", "left_receipt_ns", "right_receipt_ns"},
        f"{name}.encoder",
    )
    parsed_encoder = {
        "poll_id": _integer(encoder["poll_id"], f"{name}.encoder.poll_id", minimum=1),
        "left": _integer(encoder["left"], f"{name}.encoder.left", maximum=16383),
        "right": _integer(encoder["right"], f"{name}.encoder.right", maximum=16383),
        "left_receipt_ns": _integer(
            encoder["left_receipt_ns"], f"{name}.encoder.left_receipt_ns", minimum=1
        ),
        "right_receipt_ns": _integer(
            encoder["right_receipt_ns"], f"{name}.encoder.right_receipt_ns", minimum=1
        ),
    }
    _require(
        parsed_encoder["left_receipt_ns"] <= parsed_encoder["right_receipt_ns"],
        f"{name}.encoder receipts are not ordered",
    )
    started = _number(
        raw["stage_started_monotonic_s"], f"{name}.stage_started_monotonic_s", minimum=0.0
    )
    completed = _number(
        raw["stage_completed_monotonic_s"], f"{name}.stage_completed_monotonic_s", minimum=0.0
    )
    _require(completed >= started, f"{name} completion precedes start")
    _require(raw["monotonic_clock"] == "linux_monotonic", f"{name} needs linux_monotonic")
    revolutions = raw["revolutions"]
    _require(
        isinstance(revolutions, list) and len(revolutions) == SCANS_PER_STAGE,
        f"{name} requires ten revolutions",
    )
    parsed_revolutions: list[dict[str, object]] = []
    previous = -math.inf
    for index, revolution in enumerate(revolutions):
        item = _exact(revolution, {"monotonic_s", "points"}, f"{name}.revolutions[{index}]")
        monotonic_s = _number(
            item["monotonic_s"], f"{name}.revolutions[{index}].monotonic_s", minimum=0.0
        )
        _require(
            started <= monotonic_s <= completed and monotonic_s > previous,
            f"{name} revolution timing is invalid",
        )
        previous = monotonic_s
        points = item["points"]
        _require(
            isinstance(points, list) and 1 <= len(points) <= MAX_POINTS_PER_SCAN,
            f"{name} revolution point count is invalid",
        )
        parsed_points = [
            _point(point, f"{name}.revolutions[{index}].points[{point_index}]")
            for point_index, point in enumerate(points)
        ]
        parsed_revolutions.append(
            {
                "monotonic_s": monotonic_s,
                "points": np.asarray(
                    [point for point in parsed_points if point is not None], dtype=float
                ).reshape(-1, 2),
            }
        )
    declared_age = _number(
        raw["max_encoder_revolution_delta_s"],
        f"{name}.max_encoder_revolution_delta_s",
        minimum=0.0,
    )
    encoder_time_s = parsed_encoder["right_receipt_ns"] / 1_000_000_000.0
    measured_age = max(
        abs(float(item["monotonic_s"]) - encoder_time_s) for item in parsed_revolutions
    )
    _require(
        abs(measured_age - declared_age) <= 1e-6,
        f"{name} encoder-revolution delta does not match timestamps",
    )
    quality_refusals = []
    if pose["quality"] <= 0.0:
        quality_refusals.append("odometry_quality_unavailable")
    if declared_age > MAX_ENCODER_REVOLUTION_DELTA_S:
        quality_refusals.append("encoder_revolution_time_stale")
    if (
        _number(raw["max_translation_drift_m"], f"{name}.max_translation_drift_m", minimum=0.0)
        > 0.01
    ):
        quality_refusals.append("stage_translation_drift")
    if _number(raw["max_yaw_drift_deg"], f"{name}.max_yaw_drift_deg", minimum=0.0) > 2.0:
        quality_refusals.append("stage_yaw_drift")
    return {
        "pose": pose,
        "encoder": parsed_encoder,
        "revolutions": parsed_revolutions,
        "timing": {
            "stage_started_monotonic_s": started,
            "stage_completed_monotonic_s": completed,
            "max_translation_drift_m": _number(
                raw["max_translation_drift_m"], f"{name}.max_translation_drift_m", minimum=0.0
            ),
            "max_yaw_drift_deg": _number(
                raw["max_yaw_drift_deg"], f"{name}.max_yaw_drift_deg", minimum=0.0
            ),
            "max_encoder_revolution_delta_s": declared_age,
        },
        "quality_refusals": quality_refusals,
    }


def _limits(value: object) -> dict[str, float]:
    raw = _exact(
        value,
        {
            "wheel_diameter_mm",
            "forward_speed_m_s",
            "forward_distance_m",
            "yaw_rate_deg_s",
            "yaw_degrees",
            "pulse_duration_s",
            "max_wheel_travel_m",
            "max_yaw_degrees",
            "max_runtime_s",
        },
        "limits",
    )
    result = {key: _number(raw[key], f"limits.{key}", minimum=0.0) for key in raw}
    for name, expected in EXPECTED_LIMITS.items():
        _require(
            abs(result[name] - expected) < 1e-12, f"limits.{name} is not the fixed capture bound"
        )
    return result


def parse_capture(value: object) -> dict[str, object]:
    raw = _exact(
        value,
        {
            "schema_version",
            "kind",
            "device_id",
            "mount",
            "wheel_diameter_mm",
            "limits",
            "boot_id",
            "executed_bundle_source_sha256",
            "stages",
        },
        "capture",
    )
    _require(raw["schema_version"] == SCHEMA_VERSION, "capture schema_version is unsupported")
    _require(raw["kind"] == KIND, "capture kind is unsupported")
    _require(raw["device_id"] == DEVICE_ID, "capture device_id must be 11")
    _require(
        abs(_number(raw["wheel_diameter_mm"], "wheel_diameter_mm") - WHEEL_DIAMETER_MM) < 1e-6,
        "wheel diameter mismatches Ohmni 11",
    )
    mount_raw = _exact(raw["mount"], {"x_m", "y_m", "z_m"}, "mount")
    mount = {key: _number(mount_raw[key], f"mount.{key}") for key in mount_raw}
    _require(
        math.hypot(mount["x_m"], mount["y_m"]) <= 1.0 and abs(mount["z_m"]) <= 1.0,
        "mount exceeds the body envelope",
    )
    stages_raw = _exact(raw["stages"], set(STAGE_NAMES), "stages")
    return {
        "mount": mount,
        "limits": _limits(raw["limits"]),
        "boot_id": _boot_id(raw["boot_id"]),
        "executed_bundle_source_sha256": _pin(
            raw["executed_bundle_source_sha256"], "executed_bundle_source_sha256"
        ),
        "stages": {name: _stage(stages_raw[name], f"stages.{name}") for name in STAGE_NAMES},
    }


def _rotation(angle: float) -> np.ndarray:
    cosine, sine = math.cos(angle), math.sin(angle)
    return np.array(((cosine, -sine), (sine, cosine)), dtype=float)


def _normalized_offset(offset_deg: float) -> float:
    return (offset_deg + 180.0) % 360.0 - 180.0


def _offset_distance(left_deg: float, right_deg: float) -> float:
    return abs(_normalized_offset(left_deg - right_deg))


def predicted_raw_transform(
    baseline_pose: Mapping[str, float],
    stage_pose: Mapping[str, float],
    mount_xy: Sequence[float],
    sign: int,
    offset_deg: float,
) -> tuple[np.ndarray, np.ndarray]:
    """Return the raw-stage to raw-baseline transform implied by body-frame odometry."""
    _require(sign in (-1, 1), "angle sign must be -1 or 1")
    yaw0 = math.radians(baseline_pose["yaw_deg"])
    delta = math.radians(stage_pose["yaw_deg"] - baseline_pose["yaw_deg"])
    displacement = _rotation(-yaw0) @ np.array(
        [stage_pose["x_m"] - baseline_pose["x_m"], stage_pose["y_m"] - baseline_pose["y_m"]],
        dtype=float,
    )
    mount = np.asarray(mount_xy, dtype=float)
    body_translation = displacement + (_rotation(delta) - np.eye(2)) @ mount
    reflection = np.diag((1.0, float(sign)))
    offset = math.radians(offset_deg)
    return _rotation(sign * delta), reflection @ _rotation(-offset) @ body_translation


def _bounded_points(scans: Sequence[Mapping[str, object]], *, held_out: bool) -> np.ndarray:
    selected = scans[-2:] if held_out else scans[:-2]
    points = np.concatenate([np.asarray(scan["points"], dtype=float) for scan in selected])
    if len(points) > MAX_STAGE_POINTS:
        points = points[np.linspace(0, len(points) - 1, MAX_STAGE_POINTS, dtype=int)]
    return points


def _nearest_squared(source: np.ndarray, target: np.ndarray) -> np.ndarray:
    results: list[np.ndarray] = []
    for start in range(0, len(source), 128):
        chunk = source[start : start + 128]
        squared = np.sum((chunk[:, None, :] - target[None, :, :]) ** 2, axis=2)
        results.append(np.min(squared, axis=1))
    return np.concatenate(results)


def _robust_mean_squared(source: np.ndarray, target: np.ndarray) -> float:
    distances = np.concatenate((_nearest_squared(source, target), _nearest_squared(target, source)))
    keep = max(1, int(len(distances) * 0.8))
    return float(np.mean(np.partition(distances, keep - 1)[:keep]))


def _score_offset(
    baseline: Mapping[str, object],
    stages: Sequence[Mapping[str, object]],
    mount: Sequence[float],
    sign: int,
    offset_deg: float,
    *,
    held_out: bool,
) -> float:
    base = _bounded_points(baseline["revolutions"], held_out=held_out)  # type: ignore[arg-type]
    scores = []
    for stage in stages:
        rotation, translation = predicted_raw_transform(
            baseline["pose"],
            stage["pose"],
            mount,
            sign,
            offset_deg,  # type: ignore[arg-type]
        )
        raw = _bounded_points(stage["revolutions"], held_out=held_out)  # type: ignore[arg-type]
        scores.append(_robust_mean_squared(raw @ rotation.T + translation, base))
    return float(np.mean(scores))


def _geometry_rank(points: np.ndarray) -> tuple[float, list[float], list[float]]:
    covariance = np.cov(points.T)
    eigenvalues, eigenvectors = np.linalg.eigh(covariance)
    return (
        float(eigenvalues[0] / max(eigenvalues[-1], 1e-12)),
        [float(item) for item in eigenvalues],
        [float(item) for item in eigenvectors[:, 0]],
    )


def _motion_refusals(stages: Mapping[str, Mapping[str, object]]) -> list[str]:
    baseline = stages["baseline"]
    result: list[str] = []
    for name in ("after_forward", "after_yaw"):
        stage = stages[name]
        pose0, pose = baseline["pose"], stage["pose"]  # type: ignore[assignment]
        translation = math.hypot(pose["x_m"] - pose0["x_m"], pose["y_m"] - pose0["y_m"])
        yaw = abs(math.radians(pose["yaw_deg"] - pose0["yaw_deg"]))
        if name == "after_forward" and translation < MIN_TRANSLATION_M:
            result.append("insufficient_translation")
        if name == "after_yaw" and yaw < MIN_YAW_RAD:
            result.append("insufficient_yaw")
        if (
            stage["encoder"]["left"] == baseline["encoder"]["left"]  # type: ignore[index]
            and stage["encoder"]["right"] == baseline["encoder"]["right"]  # type: ignore[index]
        ):
            result.append(f"{name}_encoder_did_not_change")
    return result


def _candidate(
    stages: Mapping[str, Mapping[str, object]], mount: Mapping[str, float]
) -> dict[str, object]:
    baseline = stages["baseline"]
    changed = [stages["after_forward"], stages["after_yaw"]]
    base_fit = _bounded_points(baseline["revolutions"], held_out=False)  # type: ignore[arg-type]
    rank, eigenvalues, weak_geometry_normal = _geometry_rank(base_fit)
    refusals = _motion_refusals(stages)
    refusals.extend(reason for stage in stages.values() for reason in stage["quality_refusals"])
    if len(base_fit) < MIN_STAGE_POINTS or any(
        len(_bounded_points(stage["revolutions"], held_out=False)) < MIN_STAGE_POINTS
        for stage in changed
    ):  # type: ignore[arg-type]
        refusals.append("sparse_scan_support")
    if rank < MIN_GEOMETRY_RANK:
        refusals.append("single_surface_geometry")
    initial_metrics = {
        "geometry_eigenvalues_m2": eigenvalues,
        "geometry_rank": rank,
        "weak_geometry_normal": weak_geometry_normal,
        "stage_timing": {name: stages[name]["timing"] for name in STAGE_NAMES},
        "point_counts": {
            name: len(_bounded_points(stages[name]["revolutions"], held_out=False))
            for name in STAGE_NAMES
        },  # type: ignore[arg-type]
    }
    if refusals:
        return {
            "refusal_reasons": sorted(set(refusals)),
            "metrics": {"registration_skipped": True, **initial_metrics},
        }
    mount_xy = (mount["x_m"], mount["y_m"])
    coarse: list[tuple[float, int, float]] = []
    for sign in (-1, 1):
        for offset in np.arange(-180.0, 180.0, 1.0):
            coarse.append(
                (
                    _score_offset(baseline, changed, mount_xy, sign, float(offset), held_out=False),
                    sign,
                    float(offset),
                )
            )
    coarse.sort()
    best_coarse = coarse[0]
    refined: list[tuple[float, int, float]] = []
    for offset in np.arange(best_coarse[2] - 1.0, best_coarse[2] + 1.0001, 0.1):
        refined.append(
            (
                _score_offset(
                    baseline, changed, mount_xy, best_coarse[1], float(offset), held_out=False
                ),
                best_coarse[1],
                float(offset),
            )
        )
    refined.sort()
    fit_score, sign, offset = refined[0]
    held_out_score = _score_offset(baseline, changed, mount_xy, sign, offset, held_out=True)
    best_rms = math.sqrt(fit_score)
    held_out_rms = math.sqrt(held_out_score)
    step = 0.25
    nearby = [
        _score_offset(baseline, changed, mount_xy, sign, offset + delta, held_out=False)
        for delta in (-step, step)
    ]
    curvature = max((nearby[0] + nearby[1] - 2.0 * fit_score) / (step * step), 1e-12)
    uncertainty = math.sqrt(max(fit_score, 1e-12) / curvature)
    competing = next(
        item for item in coarse if item[1] != sign or _offset_distance(item[2], offset) > 10.0
    )
    if best_rms > MAX_RMS_M:
        refusals.append("scan_overlap_or_residual_failure")
    if held_out_rms > MAX_HELD_OUT_RMS_M:
        refusals.append("held_out_residual_failure")
    if uncertainty > MAX_OFFSET_UNCERTAINTY_DEG:
        refusals.append("offset_uncertainty_too_broad")
    if competing[0] <= fit_score * 1.10:
        refusals.append("competing_offset_basin")
    per_stage_offsets: list[float] = []
    for stage in changed:
        local = min(
            (
                _score_offset(baseline, [stage], mount_xy, sign, candidate_offset, held_out=False),
                candidate_offset,
            )
            for candidate_offset in np.arange(offset - 5.0, offset + 5.001, 0.25)
        )
        per_stage_offsets.append(float(local[1]))
    if _offset_distance(per_stage_offsets[0], per_stage_offsets[1]) > 3.0:
        refusals.append("per_stage_offset_disagreement")
    return {
        "refusal_reasons": sorted(set(refusals)),
        "candidate": {"offset_deg": _normalized_offset(offset), "angle_sign": sign},
        "metrics": {
            "fit_rms_m": best_rms,
            "held_out_rms_m": held_out_rms,
            "fit_mean_squared_m2": fit_score,
            "held_out_mean_squared_m2": held_out_score,
            "competing_basin_mean_squared_m2": competing[0],
            "per_stage_offset_deg": dict(
                zip(
                    ("after_forward", "after_yaw"),
                    [_normalized_offset(value) for value in per_stage_offsets],
                    strict=True,
                )
            ),
            **initial_metrics,
            "registration_skipped": False,
            "offset_hessian_m2_per_deg2": [[curvature]],
            "offset_hessian_eigenvalues_m2_per_deg2": [curvature],
            "offset_uncertainty_deg": uncertainty,
        },
    }


def fit_capture(value: object, *, source_sha256: str | None = None) -> dict[str, object]:
    parsed = parse_capture(value)
    fit = _candidate(parsed["stages"], parsed["mount"])  # type: ignore[arg-type]
    if parsed["boot_id"] is None:
        fit["refusal_reasons"].append("missing_boot_id")
    if parsed["executed_bundle_source_sha256"] is None:
        fit["refusal_reasons"].append("missing_executed_bundle_source_sha256")
    fit["refusal_reasons"] = sorted(set(fit["refusal_reasons"]))
    approved = not fit["refusal_reasons"]
    result = {
        "schema_version": 1,
        "kind": "ohmni_supervised_lidar_calibration_candidate",
        "device_id": DEVICE_ID,
        "approval_status": "unapproved_candidate" if approved else "refused",
        "input_provenance": {
            "capture_sha256": source_sha256,
            "boot_id": parsed["boot_id"],
            "executed_bundle_source_sha256": parsed["executed_bundle_source_sha256"],
        },
        "refusal_reasons": fit["refusal_reasons"],
        "metrics": fit["metrics"],
    }
    if approved:
        result["candidate"] = fit["candidate"]
    return result


def run(input_path: Path, output_path: Path) -> dict[str, object]:
    payload = input_path.read_bytes()
    _require(len(payload) <= MAX_INPUT_BYTES, "capture exceeds the input byte limit")
    document = parse_document(payload, str(input_path))
    result = fit_capture(document, source_sha256=hashlib.sha256(payload).hexdigest())
    encoded = json.dumps(result, allow_nan=False, sort_keys=True, indent=2).encode() + b"\n"
    _require(len(encoded) <= MAX_OUTPUT_BYTES, "candidate exceeds the output byte limit")
    descriptor = os.open(output_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as output:
            descriptor = -1
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    print(json.dumps(run(args.capture, args.output), sort_keys=True))


if __name__ == "__main__":
    main()
