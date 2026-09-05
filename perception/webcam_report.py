"""Check recorded webcam observations against independently surveyed checkpoints."""

import argparse
import json
import math
from pathlib import Path

PINS = ("map_sha256", "calibration_sha256", "latency_sha256")


def _number(value):
    try:
        valid = type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError("expected a finite number")
    return value


def _position(value):
    if not isinstance(value, list) or len(value) != 3:
        raise ValueError("position must contain three coordinates")
    return [_number(component) for component in value]


def build_report(rows, checkpoints):
    """Raise ValueError for malformed evidence; coverage spans start through the last row."""
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
        raise ValueError("observations must be nonempty")
    pins = {key: rows[0].get(key) for key in PINS}
    if any(
        not isinstance(value, str)
        or len(value) != 64
        or any(c not in "0123456789abcdef" for c in value)
        for value in pins.values()
    ):
        raise ValueError("artifact pins must be SHA-256 digests")
    previous_elapsed = previous_timestamp = None
    origin = None
    fixes = [0.0]
    max_gap = 0.0
    candidates = []
    synthetic = False
    for row in rows:
        if not isinstance(row, dict) or any(row.get(key) != pins[key] for key in PINS):
            raise ValueError("inconsistent artifact pins")
        elapsed = _number(row.get("run_elapsed_s"))
        timestamp = _number(row.get("timestamp"))
        if elapsed < 0 or timestamp < 0:
            raise ValueError("negative observation time")
        if previous_elapsed is not None and (
            elapsed <= previous_elapsed or timestamp <= previous_timestamp
        ):
            raise ValueError("observations must be strictly time ordered")
        if origin is None:
            origin = timestamp - elapsed
        if abs(timestamp - elapsed - origin) > 1e-6:
            raise ValueError("observation clocks disagree")
        max_gap = max(max_gap, elapsed - (previous_elapsed or 0))
        previous_elapsed, previous_timestamp = elapsed, timestamp
        if (
            type(row.get("synthetic")) is not bool
            or type(row.get("accepted")) is not bool
            or row.get("confidence") not in ("green", "amber", "red")
            or row.get("flight_approved") is not False
            or row.get("control_eligible") is not False
        ):
            raise ValueError("invalid observation status")
        synthetic |= row["synthetic"]
        age = row.get("fix_age_with_p95_tail_s")
        raw_age = row.get("fix_age_s")
        position = row.get("position_map_m")
        if position is not None:
            _position(position)
        if raw_age is not None and _number(raw_age) < 0:
            raise ValueError("negative fix age")
        if age is not None:
            age = _number(age)
            if age < 0 or (raw_age is not None and age < raw_age):
                raise ValueError("invalid conservative fix age")
            fixes.append(max(0.0, elapsed - age))
            max_gap = max(max_gap, min(elapsed, age))
        if row["accepted"]:
            if row["confidence"] != "green" or age is None or age >= 0.5 or position is None:
                raise ValueError("accepted observation lacks a timely position")
            candidates.append(row)
    end = rows[-1]["run_elapsed_s"]
    fixes.append(end)
    fixes = sorted(set(fixes))
    max_gap = max(
        max_gap, max((b - a for a, b in zip(fixes, fixes[1:], strict=False)), default=end)
    )
    if (
        not isinstance(checkpoints, dict)
        or checkpoints.get("map_sha256") != pins["map_sha256"]
        or checkpoints.get("independent_survey") is not True
        or checkpoints.get("evidence_kind") != "recorded_live"
        or not isinstance(checkpoints.get("checkpoints"), list)
        or not checkpoints["checkpoints"]
    ):
        raise ValueError(
            "checkpoints require independent recorded survey evidence and matching map"
        )
    results = []
    ids = set()
    for checkpoint in checkpoints["checkpoints"]:
        if not isinstance(checkpoint, dict):
            raise ValueError("checkpoint must be an object")
        identifier = checkpoint.get("id")
        if not isinstance(identifier, str) or not identifier or identifier in ids:
            raise ValueError("checkpoint IDs must be unique nonempty strings")
        ids.add(identifier)
        elapsed = _number(checkpoint.get("run_elapsed_s"))
        if elapsed < 0 or elapsed > end:
            raise ValueError("checkpoint lies outside recorded run")
        position = _position(checkpoint.get("position_map_m"))
        nearest = min(candidates, key=lambda row: abs(row["run_elapsed_s"] - elapsed), default=None)
        offset = None if nearest is None else abs(nearest["run_elapsed_s"] - elapsed)
        error = (
            math.dist(position, nearest["position_map_m"])
            if offset is not None and offset <= 0.1 + 1e-9
            else None
        )
        if error is not None:
            _number(error)
        results.append({"id": identifier, "time_offset_s": offset, "error_m": error})
    coverage_pass = bool(candidates) and end > 0 and max_gap <= 0.5 + 1e-9
    checkpoint_pass = len(results) >= 6 and all(
        result["error_m"] is not None and result["error_m"] <= 0.10 for result in results
    )
    return pins | {
        "status": "software_evidence_checks_only",
        "capture_timing": "estimated_from_decode_time_and_measured_latency",
        "recorded_duration_s": end,
        "max_localization_gap_s": max_gap,
        "coverage_within_500ms": coverage_pass,
        "checkpoint_tolerance_m": 0.10,
        "checkpoint_count": len(results),
        "checkpoints": results,
        "heldout_checkpoint_check_passed": checkpoint_pass,
        "software_checks_passed": coverage_pass and checkpoint_pass,
        "synthetic": synthetic,
        "physical_acceptance": "pending_independent_verification",
        "flight_approved": False,
        "control_eligible": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("observations", type=Path)
    parser.add_argument("--checkpoints", type=Path, required=True)
    args = parser.parse_args()
    try:
        rows = [
            json.loads(line) for line in args.observations.read_text().splitlines() if line.strip()
        ]
        report = build_report(rows, json.loads(args.checkpoints.read_text()))
        print(json.dumps(report, indent=2, allow_nan=False))
    except (ValueError, OSError, KeyError, TypeError, AttributeError, OverflowError):
        raise SystemExit(
            "webcam report failed: invalid observations or checkpoint evidence"
        ) from None


if __name__ == "__main__":
    main()
