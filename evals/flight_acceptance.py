"""Evaluate recorded localization evidence for the software flight-acceptance checks."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import tempfile
from pathlib import Path


class EvidenceError(ValueError):
    """Evidence cannot support a measurement report."""


IDENTITY_FIELDS = (
    "map_id",
    "geometry_id",
    "aircraft_id",
    "camera_calibration_id",
    "body_extrinsics_id",
    "clock_id",
)
DEPLOYMENT_IDENTITY_FIELDS = IDENTITY_FIELDS[:-1]
VALID_STATUSES = frozenset({"available", "unavailable", "invalid"})
POSITION_ERROR_LIMIT_M = 0.25
LOCALIZATION_GAP_LIMIT_S = 0.5


def _object(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise EvidenceError(f"{name} must be an object")
    return value


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EvidenceError(f"{name} must be a nonempty string")
    return value


def _number(value: object, name: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(value):
        raise EvidenceError(f"{name} must be a finite number")
    return float(value)


def _position(value: object, name: str) -> tuple[float, float, float]:
    if not isinstance(value, list) or len(value) != 3:
        raise EvidenceError(f"{name} must contain three coordinates")
    return tuple(_number(component, f"{name}[{index}]") for index, component in enumerate(value))


def _percentile(values: list[float], proportion: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[math.ceil(proportion * len(ordered)) - 1]


def _distribution(errors: list[float]) -> dict[str, float | int | None]:
    return {
        "count": len(errors),
        "minimum_m": min(errors) if errors else None,
        "p50_m": _percentile(errors, 0.5),
        "p95_m": _percentile(errors, 0.95),
        "maximum_m": max(errors) if errors else None,
    }


def _count_statuses(samples: list[dict[str, object]]) -> dict[str, int]:
    return {
        status: sum(sample["status"] == status for sample in samples) for status in VALID_STATUSES
    }


def _parse_manifest(raw: object, run_label: str) -> dict[str, object]:
    manifest = _object(raw, f"{run_label}.manifest")
    identities = {
        field: _text(manifest.get(field), f"{run_label}.manifest.{field}")
        for field in IDENTITY_FIELDS
    }
    estimator_source_id = _text(
        manifest.get("estimator_source_id"), f"{run_label}.manifest.estimator_source_id"
    )
    reference = _object(manifest.get("reference"), f"{run_label}.manifest.reference")
    reference_source_id = _text(
        reference.get("source_id"), f"{run_label}.manifest.reference.source_id"
    )
    if reference_source_id == estimator_source_id:
        raise EvidenceError(f"{run_label} reference and estimator source IDs must differ")
    if reference.get("independence_claimed") is not True:
        raise EvidenceError(f"{run_label}.manifest.reference.independence_claimed must be true")
    reference_details = {
        "source_id": reference_source_id,
        "method": _text(reference.get("method"), f"{run_label}.manifest.reference.method"),
        "calibration_id": _text(
            reference.get("calibration_id"), f"{run_label}.manifest.reference.calibration_id"
        ),
        "calibration_bound_m": _number(
            reference.get("calibration_bound_m"),
            f"{run_label}.manifest.reference.calibration_bound_m",
        ),
        "independence_claimed": True,
    }
    if reference_details["calibration_bound_m"] < 0:
        raise EvidenceError(
            f"{run_label}.manifest.reference.calibration_bound_m must be nonnegative"
        )
    return identities | {
        "session_id": _text(manifest.get("session_id"), f"{run_label}.manifest.session_id"),
        "estimator_source_id": estimator_source_id,
        "reference": reference_details,
    }


def _parse_interval(raw: object, run_label: str) -> tuple[float, float]:
    interval = _object(raw, f"{run_label}.interval")
    start = _number(interval.get("start_s"), f"{run_label}.interval.start_s")
    end = _number(interval.get("end_s"), f"{run_label}.interval.end_s")
    if end <= start:
        raise EvidenceError(f"{run_label}.interval must have end_s after start_s")
    return start, end


def _parse_position_samples(
    raw: object,
    name: str,
    *,
    source_id: str,
    clock_id: str,
    start: float,
    end: float,
) -> list[dict[str, object]]:
    if not isinstance(raw, list) or not raw:
        raise EvidenceError(f"{name} must be a nonempty array")
    samples: list[dict[str, object]] = []
    ids: set[str] = set()
    previous_timestamp = None
    for index, raw_sample in enumerate(raw):
        label = f"{name}[{index}]"
        sample = _object(raw_sample, label)
        sample_id = _text(sample.get("id"), f"{label}.id")
        if sample_id in ids:
            raise EvidenceError(f"{name} has duplicate sample ID {sample_id!r}")
        ids.add(sample_id)
        timestamp = _number(sample.get("timestamp_s"), f"{label}.timestamp_s")
        if timestamp < start or timestamp > end:
            raise EvidenceError(f"{label}.timestamp_s lies outside the explicit run interval")
        if previous_timestamp is not None and timestamp <= previous_timestamp:
            raise EvidenceError(f"{name} timestamps must be strictly increasing")
        previous_timestamp = timestamp
        if _text(sample.get("clock_id"), f"{label}.clock_id") != clock_id:
            raise EvidenceError(f"{label} has the wrong clock ID")
        if _text(sample.get("source_id"), f"{label}.source_id") != source_id:
            raise EvidenceError(f"{label} has the wrong source ID")
        status = sample.get("status")
        if status not in VALID_STATUSES:
            raise EvidenceError(f"{label}.status must be available, unavailable, or invalid")
        result: dict[str, object] = {"id": sample_id, "timestamp_s": timestamp, "status": status}
        if status == "available":
            result["position_map_m"] = _position(
                sample.get("position_map_m"), f"{label}.position_map_m"
            )
        else:
            if "position_map_m" in sample:
                raise EvidenceError(f"{label} cannot include a position when status is {status}")
            result["reason"] = _text(sample.get("reason"), f"{label}.reason")
        samples.append(result)
    return samples


def _parse_updates(
    raw: object, run_label: str, *, source_id: str, clock_id: str, start: float, end: float
) -> list[dict[str, object]]:
    if not isinstance(raw, list) or not raw:
        raise EvidenceError(f"{run_label}.localization_updates must be a nonempty array")
    updates: list[dict[str, object]] = []
    ids: set[str] = set()
    previous_timestamp = None
    for index, raw_update in enumerate(raw):
        label = f"{run_label}.localization_updates[{index}]"
        update = _object(raw_update, label)
        update_id = _text(update.get("id"), f"{label}.id")
        if update_id in ids:
            raise EvidenceError(
                f"{run_label}.localization_updates has duplicate update ID {update_id!r}"
            )
        ids.add(update_id)
        timestamp = _number(update.get("timestamp_s"), f"{label}.timestamp_s")
        if timestamp < start or timestamp > end:
            raise EvidenceError(f"{label}.timestamp_s lies outside the explicit run interval")
        if previous_timestamp is not None and timestamp <= previous_timestamp:
            raise EvidenceError(
                f"{run_label}.localization_updates timestamps must be strictly increasing"
            )
        previous_timestamp = timestamp
        if _text(update.get("clock_id"), f"{label}.clock_id") != clock_id:
            raise EvidenceError(f"{label} has the wrong clock ID")
        if _text(update.get("source_id"), f"{label}.source_id") != source_id:
            raise EvidenceError(f"{label} has the wrong source ID")
        status = update.get("status")
        if status not in VALID_STATUSES:
            raise EvidenceError(f"{label}.status must be available, unavailable, or invalid")
        result: dict[str, object] = {"id": update_id, "timestamp_s": timestamp, "status": status}
        if status != "available":
            result["reason"] = _text(update.get("reason"), f"{label}.reason")
        updates.append(result)
    return updates


def _pair_samples(
    estimates: list[dict[str, object]], references: list[dict[str, object]], max_age_s: float
) -> list[dict[str, object]]:
    candidates = [sample for sample in estimates if sample["status"] == "available"]
    used_ids: set[str] = set()
    pairs: list[dict[str, object]] = []
    for reference in references:
        if reference["status"] != "available":
            continue
        available = [candidate for candidate in candidates if candidate["id"] not in used_ids]
        nearest = min(
            available,
            key=lambda candidate: abs(
                float(candidate["timestamp_s"]) - float(reference["timestamp_s"])
            ),
            default=None,
        )
        age = (
            None
            if nearest is None
            else abs(float(nearest["timestamp_s"]) - float(reference["timestamp_s"]))
        )
        pair: dict[str, object] = {
            "reference_id": reference["id"],
            "reference_timestamp_s": reference["timestamp_s"],
            "estimate_id": None if nearest is None else nearest["id"],
            "estimate_timestamp_s": None if nearest is None else nearest["timestamp_s"],
            "pairing_age_s": age,
            "error_m": None,
        }
        if nearest is not None and age is not None and age <= max_age_s:
            pair["error_m"] = math.dist(reference["position_map_m"], nearest["position_map_m"])
            used_ids.add(str(nearest["id"]))
        pairs.append(pair)
    return pairs


def _gap_report(
    updates: list[dict[str, object]], start: float, end: float
) -> tuple[float, list[dict[str, float]]]:
    return _gap_segments(
        [
            start,
            *(
                float(update["timestamp_s"])
                for update in updates
                if update["status"] == "available"
            ),
            end,
        ]
    )


def _gap_segments(times: list[float]) -> tuple[float, list[dict[str, float]]]:
    segments = [
        {"start_s": earlier, "end_s": later, "duration_s": later - earlier}
        for earlier, later in zip(times, times[1:], strict=False)
    ]
    maximum = max(segment["duration_s"] for segment in segments)
    return maximum, [
        segment for segment in segments if segment["duration_s"] > LOCALIZATION_GAP_LIMIT_S
    ]


def _paired_reference_coverage(
    pairs: list[dict[str, object]], start: float, end: float
) -> tuple[float, list[dict[str, float]]]:
    return _gap_segments(
        [
            start,
            *(
                float(pair["reference_timestamp_s"])
                for pair in pairs
                if pair["error_m"] is not None
            ),
            end,
        ]
    )


def _validate_hashes(input_file_hashes: object) -> dict[str, str]:
    hashes = _object(input_file_hashes, "input_file_hashes")
    if not hashes:
        raise EvidenceError("input_file_hashes must identify at least one input")
    result = {}
    for name, value in hashes.items():
        result[_text(name, "input file name")] = _text(value, f"input_file_hashes.{name}")
        if len(result[name]) != 64 or any(
            character not in "0123456789abcdef" for character in result[name]
        ):
            raise EvidenceError(f"input_file_hashes.{name} must be a lowercase SHA-256 digest")
    return dict(sorted(result.items()))


def _recording_content_fingerprint(
    estimates: list[dict[str, object]],
    references: list[dict[str, object]],
    updates: list[dict[str, object]],
    start: float,
    end: float,
) -> str:
    def position_series(samples: list[dict[str, object]]) -> list[dict[str, object]]:
        return [
            {
                "timestamp_s": sample["timestamp_s"],
                "status": sample["status"],
                **(
                    {"position_map_m": sample["position_map_m"]}
                    if sample["status"] == "available"
                    else {"reason": sample["reason"]}
                ),
            }
            for sample in samples
        ]

    recording = {
        "interval": {"start_s": start, "end_s": end},
        "estimates": position_series(estimates),
        "references": position_series(references),
        "localization_updates": [
            {"timestamp_s": sample["timestamp_s"], "status": sample["status"]}
            | ({} if sample["status"] == "available" else {"reason": sample["reason"]})
            for sample in updates
        ],
    }
    payload = json.dumps(recording, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def evaluate(evidence: object, *, input_file_hashes: object) -> dict[str, object]:
    """Return a deterministic software measurement report or reject malformed evidence."""
    document = _object(evidence, "evidence")
    if type(document.get("schema_version")) is not int or document["schema_version"] != 1:
        raise EvidenceError("schema_version must be 1")
    criteria = _object(document.get("criteria"), "criteria")
    max_pairing_age_s = _number(criteria.get("max_pairing_age_s"), "criteria.max_pairing_age_s")
    if max_pairing_age_s <= 0 or max_pairing_age_s > LOCALIZATION_GAP_LIMIT_S:
        raise EvidenceError(
            "criteria.max_pairing_age_s must be positive and no more than 0.5 seconds"
        )
    runs = document.get("runs")
    if not isinstance(runs, list) or len(runs) != 5:
        raise EvidenceError("exactly five mapped-route rehearsals are required")
    run_ids: set[str] = set()
    route_ids: set[str] = set()
    recording_fingerprints: set[str] = set()
    deployment_identity: dict[str, str] | None = None
    run_reports = []
    all_errors: list[float] = []
    all_outliers: list[dict[str, object]] = []
    all_samples_available = True
    all_pairings_complete = True
    all_pairing_coverage_within_limit = True
    all_runs_passed = True
    all_gaps_within_limit = True
    aggregate_sample_counts = {
        series: {status: 0 for status in VALID_STATUSES}
        for series in ("estimates", "references", "localization_updates")
    }
    aggregate_gap_violations: list[dict[str, object]] = []
    aggregate_max_gap = 0.0
    aggregate_max_pairing_coverage_gap = 0.0
    aggregate_max_observed_pairing_age: float | None = None
    aggregate_pairing_coverage_violations: list[dict[str, object]] = []
    for index, raw_run in enumerate(runs):
        label = f"runs[{index}]"
        run = _object(raw_run, label)
        run_id = _text(run.get("run_id"), f"{label}.run_id")
        route_id = _text(run.get("route_id"), f"{label}.route_id")
        if run_id in run_ids:
            raise EvidenceError(f"duplicate run ID {run_id!r}")
        run_ids.add(run_id)
        route_ids.add(route_id)
        manifest = _parse_manifest(run.get("manifest"), label)
        run_deployment_identity = {field: manifest[field] for field in DEPLOYMENT_IDENTITY_FIELDS}
        if deployment_identity is None:
            deployment_identity = run_deployment_identity
        elif run_deployment_identity != deployment_identity:
            raise EvidenceError("mixed deployment identities across mapped-route rehearsals")
        start, end = _parse_interval(run.get("interval"), label)
        estimates = _parse_position_samples(
            run.get("estimates"),
            f"{label}.estimates",
            source_id=manifest["estimator_source_id"],
            clock_id=manifest["clock_id"],
            start=start,
            end=end,
        )
        references = _parse_position_samples(
            run.get("references"),
            f"{label}.references",
            source_id=manifest["reference"]["source_id"],
            clock_id=manifest["clock_id"],
            start=start,
            end=end,
        )
        updates = _parse_updates(
            run.get("localization_updates"),
            label,
            source_id=manifest["estimator_source_id"],
            clock_id=manifest["clock_id"],
            start=start,
            end=end,
        )
        fingerprint = _recording_content_fingerprint(estimates, references, updates, start, end)
        if fingerprint in recording_fingerprints:
            raise EvidenceError("duplicate recording content fingerprint")
        recording_fingerprints.add(fingerprint)
        pairs = _pair_samples(estimates, references, max_pairing_age_s)
        errors = [float(pair["error_m"]) for pair in pairs if pair["error_m"] is not None]
        pairing_ages = [
            float(pair["pairing_age_s"]) for pair in pairs if pair["pairing_age_s"] is not None
        ]
        max_observed_pairing_age = max(pairing_ages, default=None)
        max_gap, gap_violations = _gap_report(updates, start, end)
        max_pairing_coverage_gap, pairing_coverage_violations = _paired_reference_coverage(
            pairs, start, end
        )
        status_counts = {
            "estimates": _count_statuses(estimates),
            "references": _count_statuses(references),
            "localization_updates": _count_statuses(updates),
        }
        samples_available = all(
            counts["unavailable"] == 0 and counts["invalid"] == 0
            for counts in status_counts.values()
        )
        pairings_complete = len(pairs) > 0 and all(pair["error_m"] is not None for pair in pairs)
        error_distribution = _distribution(errors)
        error_limit_passed = bool(errors) and error_distribution["p95_m"] <= POSITION_ERROR_LIMIT_M
        gap_limit_passed = max_gap <= LOCALIZATION_GAP_LIMIT_S
        pairing_coverage_passed = max_pairing_coverage_gap <= LOCALIZATION_GAP_LIMIT_S
        run_passed = (
            samples_available
            and pairings_complete
            and error_limit_passed
            and gap_limit_passed
            and pairing_coverage_passed
        )
        outliers = [
            pair
            for pair in pairs
            if pair["error_m"] is not None and pair["error_m"] > POSITION_ERROR_LIMIT_M
        ]
        all_errors.extend(errors)
        all_outliers.extend({"run_id": run_id} | pair for pair in outliers)
        all_samples_available &= samples_available
        all_pairings_complete &= pairings_complete
        all_gaps_within_limit &= gap_limit_passed
        all_pairing_coverage_within_limit &= pairing_coverage_passed
        all_runs_passed &= run_passed
        aggregate_max_gap = max(aggregate_max_gap, max_gap)
        aggregate_max_pairing_coverage_gap = max(
            aggregate_max_pairing_coverage_gap, max_pairing_coverage_gap
        )
        if max_observed_pairing_age is not None:
            aggregate_max_observed_pairing_age = max(
                aggregate_max_observed_pairing_age or 0.0, max_observed_pairing_age
            )
        for series, counts in status_counts.items():
            for status, count in counts.items():
                aggregate_sample_counts[series][status] += count
        aggregate_gap_violations.extend({"run_id": run_id} | segment for segment in gap_violations)
        aggregate_pairing_coverage_violations.extend(
            {"run_id": run_id} | segment for segment in pairing_coverage_violations
        )
        run_reports.append(
            {
                "run_id": run_id,
                "route_id": route_id,
                "recording_content_sha256": fingerprint,
                "manifest": manifest,
                "interval": {"start_s": start, "end_s": end},
                "sample_counts": status_counts,
                "paired_measurements": pairs,
                "max_observed_pairing_age_s": max_observed_pairing_age,
                "error_distribution": error_distribution,
                "errors_over_position_error_limit": outliers,
                "max_localization_update_gap_s": max_gap,
                "localization_gap_violations": gap_violations,
                "max_paired_reference_coverage_gap_s": max_pairing_coverage_gap,
                "paired_reference_coverage_violations": pairing_coverage_violations,
                "software_evidence_checks_passed": run_passed,
            }
        )
    aggregate_distribution = _distribution(all_errors)
    aggregate_error_limit_passed = (
        bool(all_errors) and aggregate_distribution["p95_m"] <= POSITION_ERROR_LIMIT_M
    )
    report: dict[str, object] = {
        "schema_version": 1,
        "report_kind": "flight_acceptance_software_measurement",
        "input_file_hashes": _validate_hashes(input_file_hashes),
        "criteria": {
            "required_recording_runs": 5,
            "route_id_policy": (
                "recorded for provenance; repeated approved-route rehearsals allowed"
            ),
            "max_pairing_age_s": max_pairing_age_s,
            "pairing_method": "nearest recorded estimate without reuse or extrapolation",
            "position_error_p95_limit_m": POSITION_ERROR_LIMIT_M,
            "localization_update_gap_limit_s": LOCALIZATION_GAP_LIMIT_S,
            "paired_reference_coverage_gap_limit_s": LOCALIZATION_GAP_LIMIT_S,
        },
        "run_count": len(run_reports),
        "distinct_route_count": len(route_ids),
        "deployment_identity": deployment_identity,
        "runs": run_reports,
        "aggregate_sample_counts": aggregate_sample_counts,
        "aggregate_error_distribution": aggregate_distribution,
        "aggregate_errors_over_position_error_limit": all_outliers,
        "aggregate_max_localization_update_gap_s": aggregate_max_gap,
        "aggregate_localization_gap_violations": aggregate_gap_violations,
        "aggregate_max_paired_reference_coverage_gap_s": aggregate_max_pairing_coverage_gap,
        "aggregate_paired_reference_coverage_violations": aggregate_pairing_coverage_violations,
        "aggregate_max_observed_pairing_age_s": aggregate_max_observed_pairing_age,
        "software_evidence_checks_passed": (
            all_samples_available
            and all_pairings_complete
            and all_gaps_within_limit
            and all_pairing_coverage_within_limit
            and aggregate_error_limit_passed
            and all_runs_passed
        ),
        "material_limitations": [
            (
                "Reference quality is limited to the declared source, method, "
                "calibration identity, "
                "and calibration bound."
            ),
            "Physical failure and RC drills require separate signed acceptance measurements.",
        ],
    }
    digest = hashlib.sha256(
        json.dumps(report, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()
    return report | {"report_sha256": digest}


def _read_evidence(path: Path) -> tuple[object, str]:
    payload = path.read_bytes()
    try:
        return (
            json.loads(payload, object_pairs_hook=_json_object, parse_constant=_reject_nonfinite),
            hashlib.sha256(payload).hexdigest(),
        )
    except json.JSONDecodeError as error:
        raise EvidenceError(f"cannot parse JSON evidence from {path}: {error}") from error


def _json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    document: dict[str, object] = {}
    for key, value in pairs:
        if key in document:
            raise EvidenceError(f"duplicate JSON key {key!r}")
        document[key] = value
    return document


def _reject_nonfinite(value: str) -> object:
    raise EvidenceError(f"nonfinite JSON value {value!r}")


def _write_report(path: Path, report: dict[str, object]) -> None:
    content = json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=".flight-evidence-", delete=False
        ) as stream:
            temporary = Path(stream.name)
            if stream.write(content) != len(content):
                raise OSError("incomplete report write")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "evidence", type=Path, help="one JSON document containing all five rehearsals"
    )
    parser.add_argument(
        "--output", type=Path, help="new JSON report path; otherwise write to stdout"
    )
    args = parser.parse_args()
    try:
        evidence, digest = _read_evidence(args.evidence)
        report = evaluate(evidence, input_file_hashes={"evidence": digest})
        if args.output is None:
            print(json.dumps(report, indent=2, sort_keys=True, allow_nan=False))
        else:
            _write_report(args.output, report)
    except (EvidenceError, OSError, TypeError, OverflowError) as error:
        raise SystemExit(f"flight acceptance evidence failed: {error}") from error
    if not report["software_evidence_checks_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
