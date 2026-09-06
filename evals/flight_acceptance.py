"""Build a bounded, hash-bound software localization measurement report."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import stat
import sys
import tempfile
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationError


class EvidenceError(ValueError):
    """Evidence cannot support a software localization measurement report."""


RUN_COUNT = 5
POSITION_ERROR_LIMIT_M = 0.25
LOCALIZATION_GAP_LIMIT_S = 0.5
ROUTE_SPEED_LIMIT_M_S = 0.5
MAX_CHECKPOINT_RADIUS_M = 0.1

MAX_INPUT_BYTES = 16 * 1024 * 1024
MAX_REPORT_BYTES = 32 * 1024 * 1024
MAX_JSON_DEPTH = 32
MAX_JSON_NODES = 300_000
MAX_JSON_STRING_CHARS = 2_048
MAX_SERIES_SAMPLES = 10_000
MAX_ROUTE_CHECKPOINTS = 64
MAX_RUN_DURATION_S = 3_600.0
MAX_ABS_TIMESTAMP_S = 1_000_000_000_000.0
MAX_ABS_COORDINATE_M = 10_000.0

REQUIRED_ROUTE_PHASES = (
    "launch",
    "lobby",
    "corridor",
    "kitchen_hold_start",
    "kitchen_hold_complete",
    "return",
    "land",
)

DEPLOYMENT_DIGEST_FIELDS = (
    "map_bundle_sha256",
    "geometry_sha256",
    "camera_calibration_sha256",
    "body_extrinsics_sha256",
    "latency_calibration_sha256",
)
STATUSES = ("available", "invalid", "unavailable")


def _clean_text(value: object, *, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("must be a nonempty string without surrounding whitespace")
    if len(value) > maximum:
        raise ValueError(f"cannot exceed {maximum} characters")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("cannot contain control characters")
    return value


def _text(value: object) -> str:
    return _clean_text(value)


def _reason(value: object) -> str:
    return _clean_text(value, maximum=1_024)


def _sha256(value: object) -> str:
    digest = _clean_text(value, maximum=64)
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ValueError("must be a lowercase SHA-256 digest")
    return digest


def _position(value: object) -> object:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError("must contain exactly three coordinates")
    return tuple(value)


Text = Annotated[str, BeforeValidator(_text)]
Reason = Annotated[str, BeforeValidator(_reason)]
Sha256 = Annotated[str, BeforeValidator(_sha256)]
Finite = Annotated[float, Field(allow_inf_nan=False)]
Timestamp = Annotated[Finite, Field(ge=-MAX_ABS_TIMESTAMP_S, le=MAX_ABS_TIMESTAMP_S)]
Coordinate = Annotated[Finite, Field(ge=-MAX_ABS_COORDINATE_M, le=MAX_ABS_COORDINATE_M)]
Position = Annotated[
    tuple[Coordinate, Coordinate, Coordinate],
    BeforeValidator(_position),
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class Checkpoint(StrictModel):
    checkpoint_id: Text
    phase: Literal[
        "launch",
        "lobby",
        "corridor",
        "kitchen_hold_start",
        "kitchen_hold_complete",
        "return",
        "land",
        "transit",
    ]
    position_map_m: Position
    radius_m: Annotated[Finite, Field(gt=0, le=MAX_CHECKPOINT_RADIUS_M)]


class RouteExpectation(StrictModel):
    route_id: Text
    route_sha256: Sha256
    minimum_duration_s: Annotated[Finite, Field(gt=0, le=MAX_RUN_DURATION_S)]
    maximum_duration_s: Annotated[Finite, Field(gt=0, le=MAX_RUN_DURATION_S)]
    minimum_hold_duration_s: Annotated[Finite, Field(gt=0, le=MAX_RUN_DURATION_S)]
    checkpoints: Annotated[
        list[Checkpoint],
        Field(min_length=len(REQUIRED_ROUTE_PHASES), max_length=MAX_ROUTE_CHECKPOINTS),
    ]


class DeploymentExpectation(StrictModel):
    aircraft_id: Text
    map_bundle_sha256: Sha256
    geometry_sha256: Sha256
    camera_calibration_sha256: Sha256
    body_extrinsics_sha256: Sha256
    latency_calibration_sha256: Sha256


class EstimatorExpectation(StrictModel):
    source_id: Text
    build_sha256: Sha256
    config_sha256: Sha256


class ReferenceExpectation(StrictModel):
    source_id: Text
    method: Text
    calibration_sha256: Sha256
    clock_alignment_sha256: Sha256
    maximum_calibration_bound_m: Annotated[Finite, Field(ge=0, lt=POSITION_ERROR_LIMIT_M)]
    maximum_clock_alignment_bound_s: Annotated[Finite, Field(ge=0, le=LOCALIZATION_GAP_LIMIT_S)]


class MeasurementLimits(StrictModel):
    minimum_estimate_samples_per_run: Annotated[int, Field(ge=1, le=MAX_SERIES_SAMPLES)]
    minimum_reference_samples_per_run: Annotated[int, Field(ge=1, le=MAX_SERIES_SAMPLES)]
    minimum_localization_updates_per_run: Annotated[int, Field(ge=1, le=MAX_SERIES_SAMPLES)]
    max_pairing_age_s: Annotated[Finite, Field(gt=0, le=LOCALIZATION_GAP_LIMIT_S)]


class EvaluationManifest(StrictModel):
    schema_version: Literal[1]
    manifest_kind: Literal["localization_software_measurement_manifest"]
    manifest_id: Text
    route: RouteExpectation
    deployment: DeploymentExpectation
    estimator: EstimatorExpectation
    reference: ReferenceExpectation
    expected_raw_run_evidence_sha256: Annotated[
        list[Sha256], Field(min_length=RUN_COUNT, max_length=RUN_COUNT)
    ]
    limits: MeasurementLimits


class RunReference(StrictModel):
    source_id: Text
    method: Text
    calibration_sha256: Sha256
    calibration_bound_m: Annotated[Finite, Field(ge=0, lt=POSITION_ERROR_LIMIT_M)]
    clock_alignment_sha256: Sha256
    clock_alignment_bound_s: Annotated[Finite, Field(ge=0, le=LOCALIZATION_GAP_LIMIT_S)]
    independence_claimed: Literal[True]


class RunManifest(StrictModel):
    raw_run_evidence_sha256: Sha256
    route_id: Text
    route_sha256: Sha256
    aircraft_id: Text
    map_bundle_sha256: Sha256
    geometry_sha256: Sha256
    camera_calibration_sha256: Sha256
    body_extrinsics_sha256: Sha256
    latency_calibration_sha256: Sha256
    session_id: Text
    clock_id: Text
    estimator_source_id: Text
    estimator_build_sha256: Sha256
    estimator_config_sha256: Sha256
    reference: RunReference


class RunInterval(StrictModel):
    start_s: Timestamp
    end_s: Timestamp


class SampleBase(StrictModel):
    id: Text
    timestamp_s: Timestamp
    source_id: Text
    clock_id: Text


class AvailablePositionSample(SampleBase):
    status: Literal["available"]
    position_map_m: Position


class MissingPositionSample(SampleBase):
    status: Literal["invalid", "unavailable"]
    reason: Reason


PositionSample = Annotated[
    AvailablePositionSample | MissingPositionSample,
    Field(discriminator="status"),
]


class AvailableUpdate(SampleBase):
    status: Literal["available"]


class MissingUpdate(SampleBase):
    status: Literal["invalid", "unavailable"]
    reason: Reason


LocalizationUpdate = Annotated[
    AvailableUpdate | MissingUpdate,
    Field(discriminator="status"),
]


class CheckpointCrossing(StrictModel):
    checkpoint_id: Text
    reference_id: Text


class RunEvidence(StrictModel):
    run_id: Text
    manifest: RunManifest
    interval: RunInterval
    estimates: Annotated[list[PositionSample], Field(max_length=MAX_SERIES_SAMPLES)]
    references: Annotated[list[PositionSample], Field(max_length=MAX_SERIES_SAMPLES)]
    localization_updates: Annotated[list[LocalizationUpdate], Field(max_length=MAX_SERIES_SAMPLES)]
    checkpoint_crossings: Annotated[
        list[CheckpointCrossing], Field(max_length=MAX_ROUTE_CHECKPOINTS)
    ]


class EvidenceDocument(StrictModel):
    schema_version: Literal[1]
    document_kind: Literal["localization_software_measurement_evidence"]
    evaluation_manifest_sha256: Sha256
    runs: Annotated[list[RunEvidence], Field(min_length=RUN_COUNT, max_length=RUN_COUNT)]


def _schema(model: type[BaseModel], raw: object, name: str) -> BaseModel:
    try:
        return model.model_validate(raw)
    except ValidationError as error:
        details = []
        for item in error.errors(include_input=False, include_url=False)[:5]:
            location = ".".join(map(str, item["loc"]))
            details.append(f"{location}: {item['msg']}")
        suffix = "" if error.error_count() <= 5 else f"; plus {error.error_count() - 5} more"
        raise EvidenceError(f"{name} schema refused: {'; '.join(details)}{suffix}") from error


def _distance(left: Position, right: Position, name: str) -> float:
    distance = math.dist(left, right)
    if not math.isfinite(distance):
        raise EvidenceError(f"{name} produced a nonfinite distance")
    return distance


def _duration(start: float, end: float, name: str) -> float:
    duration = end - start
    if not math.isfinite(duration) or duration <= 0:
        raise EvidenceError(f"{name} must have a finite end after its start")
    return duration


def _validate_manifest(manifest: EvaluationManifest) -> float:
    if len(set(manifest.expected_raw_run_evidence_sha256)) != RUN_COUNT:
        raise EvidenceError("expected raw-run evidence digests must be unique")
    checkpoint_ids = [checkpoint.checkpoint_id for checkpoint in manifest.route.checkpoints]
    if len(set(checkpoint_ids)) != len(checkpoint_ids):
        raise EvidenceError("route checkpoint IDs must be unique")
    phases = [checkpoint.phase for checkpoint in manifest.route.checkpoints]
    required_phases = [phase for phase in phases if phase != "transit"]
    if (
        required_phases != list(REQUIRED_ROUTE_PHASES)
        or phases[0] != "launch"
        or phases[-1] != "land"
    ):
        raise EvidenceError(
            "route checkpoints must cover launch, lobby, corridor, kitchen hold, "
            "return, and land in protocol order"
        )
    hold_start = manifest.route.checkpoints[phases.index("kitchen_hold_start")]
    hold_complete = manifest.route.checkpoints[phases.index("kitchen_hold_complete")]
    if (
        hold_start.position_map_m != hold_complete.position_map_m
        or hold_start.radius_m != hold_complete.radius_m
    ):
        raise EvidenceError("kitchen hold start and completion must use one pinned hold volume")
    launch = manifest.route.checkpoints[0]
    land = manifest.route.checkpoints[-1]
    if launch.position_map_m != land.position_map_m or launch.radius_m != land.radius_m:
        raise EvidenceError("land checkpoint must return to the pinned launch zone")
    route_length = sum(
        _distance(left.position_map_m, right.position_map_m, "route checkpoint path")
        for left, right in zip(
            manifest.route.checkpoints,
            manifest.route.checkpoints[1:],
            strict=False,
        )
    )
    if route_length < 1.0:
        raise EvidenceError("route checkpoint path must span at least 1 m")
    if manifest.route.maximum_duration_s < manifest.route.minimum_duration_s:
        raise EvidenceError("route maximum duration must be at least its minimum")
    minimum_route_duration = (
        route_length / ROUTE_SPEED_LIMIT_M_S + manifest.route.minimum_hold_duration_s
    )
    if manifest.route.minimum_duration_s < minimum_route_duration:
        raise EvidenceError(
            "route minimum duration is shorter than its checkpoint path and hold at 0.5 m/s"
        )
    minimum_gap_samples = max(
        1,
        math.ceil(manifest.route.minimum_duration_s / LOCALIZATION_GAP_LIMIT_S) - 1,
    )
    minimums = (
        manifest.limits.minimum_estimate_samples_per_run,
        manifest.limits.minimum_reference_samples_per_run,
        manifest.limits.minimum_localization_updates_per_run,
    )
    if any(minimum < minimum_gap_samples for minimum in minimums):
        raise EvidenceError(
            "manifest sample minimums cannot be lower than 500 ms coverage requires"
        )
    if manifest.reference.source_id == manifest.estimator.source_id:
        raise EvidenceError("reference and estimator source IDs must differ")
    return route_length


def _validate_run_identity(run: RunEvidence, expected: EvaluationManifest) -> None:
    manifest = run.manifest
    exact = {
        "route_id": expected.route.route_id,
        "route_sha256": expected.route.route_sha256,
        "aircraft_id": expected.deployment.aircraft_id,
        **{field: getattr(expected.deployment, field) for field in DEPLOYMENT_DIGEST_FIELDS},
        "estimator_source_id": expected.estimator.source_id,
        "estimator_build_sha256": expected.estimator.build_sha256,
        "estimator_config_sha256": expected.estimator.config_sha256,
    }
    mismatches = [field for field, value in exact.items() if getattr(manifest, field) != value]
    reference_exact = {
        "source_id": expected.reference.source_id,
        "method": expected.reference.method,
        "calibration_sha256": expected.reference.calibration_sha256,
        "clock_alignment_sha256": expected.reference.clock_alignment_sha256,
    }
    mismatches.extend(
        f"reference.{field}"
        for field, value in reference_exact.items()
        if getattr(manifest.reference, field) != value
    )
    if mismatches:
        raise EvidenceError(
            f"run {run.run_id!r} does not match pinned manifest fields: "
            f"{', '.join(sorted(mismatches))}"
        )
    if manifest.reference.source_id == manifest.estimator_source_id:
        raise EvidenceError(f"run {run.run_id!r} uses its estimator as its reference")


def _validate_series(
    samples: list[SampleBase],
    *,
    source_id: str,
    clock_id: str,
    start: float,
    end: float,
    name: str,
) -> None:
    ids: set[str] = set()
    previous: float | None = None
    for sample in samples:
        if sample.id in ids:
            raise EvidenceError(f"{name} has duplicate ID {sample.id!r}")
        ids.add(sample.id)
        if sample.timestamp_s < start or sample.timestamp_s > end:
            raise EvidenceError(f"{name} sample {sample.id!r} lies outside the run")
        if previous is not None and sample.timestamp_s <= previous:
            raise EvidenceError(f"{name} timestamps must be strictly increasing")
        previous = sample.timestamp_s
        if sample.source_id != source_id or sample.clock_id != clock_id:
            raise EvidenceError(f"{name} sample {sample.id!r} has the wrong source or clock")


def _pair_samples(
    estimates: list[PositionSample],
    references: list[PositionSample],
    max_age_s: float,
    *,
    calibration_bound_m: float,
    clock_alignment_bound_s: float,
) -> list[dict[str, object]]:
    """Create a maximum-cardinality, ordered pairing in linear time."""

    available_estimates = [sample for sample in estimates if sample.status == "available"]
    cursor = 0
    pairs = []
    for reference in references:
        if reference.status != "available":
            pairs.append(_missing_pair(reference))
            continue
        while (
            cursor < len(available_estimates)
            and available_estimates[cursor].timestamp_s < reference.timestamp_s - max_age_s
        ):
            cursor += 1
        estimate = (
            available_estimates[cursor]
            if cursor < len(available_estimates)
            and available_estimates[cursor].timestamp_s <= reference.timestamp_s + max_age_s
            else None
        )
        if estimate is None:
            pairs.append(_missing_pair(reference))
            continue
        cursor += 1
        age = abs(estimate.timestamp_s - reference.timestamp_s)
        measured = _distance(
            estimate.position_map_m,
            reference.position_map_m,
            "paired position samples",
        )
        upper = (
            measured + calibration_bound_m + ROUTE_SPEED_LIMIT_M_S * (age + clock_alignment_bound_s)
        )
        if not math.isfinite(age) or not math.isfinite(upper):
            raise EvidenceError("sample pairing produced a nonfinite derived value")
        pairs.append(
            {
                "reference_id": reference.id,
                "reference_timestamp_s": reference.timestamp_s,
                "estimate_id": estimate.id,
                "estimate_timestamp_s": estimate.timestamp_s,
                "pairing_age_s": age,
                "measured_error_m": measured,
                "position_error_upper_bound_m": upper,
            }
        )
    return pairs


def _missing_pair(reference: PositionSample) -> dict[str, object]:
    return {
        "reference_id": reference.id,
        "reference_timestamp_s": reference.timestamp_s,
        "estimate_id": None,
        "estimate_timestamp_s": None,
        "pairing_age_s": None,
        "measured_error_m": None,
        "position_error_upper_bound_m": None,
    }


def _gap_segments(times: list[float]) -> tuple[float, list[dict[str, float]]]:
    segments = []
    for earlier, later in zip(times, times[1:], strict=False):
        gap = later - earlier
        if not math.isfinite(gap) or gap < 0:
            raise EvidenceError("coverage timestamps produced an invalid gap")
        segments.append({"start_s": earlier, "end_s": later, "duration_s": gap})
    maximum = max((segment["duration_s"] for segment in segments), default=0.0)
    return maximum, [
        segment for segment in segments if segment["duration_s"] > LOCALIZATION_GAP_LIMIT_S
    ]


def _coverage(
    records: list[SampleBase] | list[dict[str, object]],
    *,
    start: float,
    end: float,
    pair_records: bool = False,
) -> tuple[float, list[dict[str, float]]]:
    if pair_records:
        timestamps = [
            float(record["reference_timestamp_s"])
            for record in records
            if record["position_error_upper_bound_m"] is not None
        ]
    else:
        timestamps = [record.timestamp_s for record in records if record.status == "available"]
    return _gap_segments([start, *timestamps, end])


def _checkpoint_report(
    run: RunEvidence,
    expected: EvaluationManifest,
    pairs: list[dict[str, object]],
) -> tuple[list[dict[str, object]], bool]:
    expected_ids = [checkpoint.checkpoint_id for checkpoint in expected.route.checkpoints]
    sequence_matches = [
        crossing.checkpoint_id for crossing in run.checkpoint_crossings
    ] == expected_ids
    crossing_reference_ids = [crossing.reference_id for crossing in run.checkpoint_crossings]
    unique_references = len(crossing_reference_ids) == len(set(crossing_reference_ids))
    references = {sample.id: sample for sample in run.references}
    pairs_by_reference = {str(pair["reference_id"]): pair for pair in pairs}
    details = []
    for index, crossing in enumerate(run.checkpoint_crossings):
        checkpoint = (
            expected.route.checkpoints[index] if index < len(expected.route.checkpoints) else None
        )
        reference = references.get(crossing.reference_id)
        checkpoint_error = None
        checkpoint_error_upper_bound = None
        within_radius = False
        if (
            checkpoint is not None
            and checkpoint.checkpoint_id == crossing.checkpoint_id
            and reference is not None
            and reference.status == "available"
        ):
            checkpoint_error = _distance(
                reference.position_map_m,
                checkpoint.position_map_m,
                "checkpoint crossing",
            )
            checkpoint_error_upper_bound = (
                checkpoint_error + run.manifest.reference.calibration_bound_m
            )
            within_radius = checkpoint_error_upper_bound <= checkpoint.radius_m
        pair = pairs_by_reference.get(crossing.reference_id)
        details.append(
            {
                "checkpoint_id": crossing.checkpoint_id,
                "phase": checkpoint.phase if checkpoint is not None else None,
                "reference_id": crossing.reference_id,
                "reference_timestamp_s": (reference.timestamp_s if reference is not None else None),
                "reference_checkpoint_error_m": checkpoint_error,
                "reference_checkpoint_error_upper_bound_m": checkpoint_error_upper_bound,
                "within_checkpoint_radius": within_radius,
                "paired_measurement_available": (
                    pair is not None and pair["position_error_upper_bound_m"] is not None
                ),
            }
        )
    timestamps = [
        float(detail["reference_timestamp_s"])
        for detail in details
        if detail["reference_timestamp_s"] is not None
    ]
    ordered = len(timestamps) == len(details) and all(
        later > earlier for earlier, later in zip(timestamps, timestamps[1:], strict=False)
    )
    can_check_segments = ordered and len(details) == len(expected.route.checkpoints)
    start_matches = can_check_segments and abs(timestamps[0] - run.interval.start_s) <= 1e-9
    end_matches = can_check_segments and abs(timestamps[-1] - run.interval.end_s) <= 1e-9
    boundary_matches = start_matches and end_matches
    segment_timing_passed = can_check_segments
    for index, detail in enumerate(details):
        if index == 0 or not can_check_segments:
            detail["elapsed_from_previous_s"] = None
            detail["minimum_elapsed_from_previous_s"] = None
            continue
        elapsed = timestamps[index] - timestamps[index - 1]
        previous_reference = references.get(run.checkpoint_crossings[index - 1].reference_id)
        current_reference = references.get(run.checkpoint_crossings[index].reference_id)
        if (
            previous_reference is None
            or previous_reference.status != "available"
            or current_reference is None
            or current_reference.status != "available"
        ):
            minimum_elapsed = None
            segment_timing_passed = False
        else:
            conservative_distance = max(
                0.0,
                _distance(
                    previous_reference.position_map_m,
                    current_reference.position_map_m,
                    "checkpoint segment",
                )
                - 2 * run.manifest.reference.calibration_bound_m,
            )
            minimum_elapsed = conservative_distance / ROUTE_SPEED_LIMIT_M_S
            segment_timing_passed = segment_timing_passed and elapsed >= minimum_elapsed
        detail["elapsed_from_previous_s"] = elapsed
        detail["minimum_elapsed_from_previous_s"] = minimum_elapsed
    hold_timing_passed = False
    hold_position_passed = False
    if can_check_segments:
        phases = [checkpoint.phase for checkpoint in expected.route.checkpoints]
        hold_start_index = phases.index("kitchen_hold_start")
        hold_complete_index = phases.index("kitchen_hold_complete")
        hold_start_s = timestamps[hold_start_index]
        hold_complete_s = timestamps[hold_complete_index]
        hold_elapsed_s = hold_complete_s - hold_start_s
        hold_timing_passed = hold_elapsed_s >= expected.route.minimum_hold_duration_s
        hold_checkpoint = expected.route.checkpoints[hold_start_index]
        hold_references = [
            reference
            for reference in run.references
            if reference.status == "available"
            and hold_start_s <= reference.timestamp_s <= hold_complete_s
        ]
        hold_position_passed = bool(hold_references) and all(
            _distance(
                reference.position_map_m,
                hold_checkpoint.position_map_m,
                "kitchen hold sample",
            )
            + run.manifest.reference.calibration_bound_m
            <= hold_checkpoint.radius_m
            for reference in hold_references
        )
        details[hold_complete_index]["hold_elapsed_s"] = hold_elapsed_s
        details[hold_complete_index]["minimum_hold_duration_s"] = (
            expected.route.minimum_hold_duration_s
        )
        details[hold_complete_index]["hold_samples_within_volume"] = hold_position_passed
    if details:
        details[0]["matches_run_start"] = start_matches
        details[-1]["matches_run_end"] = end_matches
    passed = (
        sequence_matches
        and unique_references
        and ordered
        and boundary_matches
        and segment_timing_passed
        and hold_timing_passed
        and hold_position_passed
        and all(
            detail["within_checkpoint_radius"] and detail["paired_measurement_available"]
            for detail in details
        )
    )
    return details, passed


def _distribution(values: list[float]) -> dict[str, float | int | None]:
    ordered = sorted(values)

    def percentile(proportion: float) -> float | None:
        return ordered[math.ceil(proportion * len(ordered)) - 1] if ordered else None

    return {
        "count": len(ordered),
        "minimum_m": ordered[0] if ordered else None,
        "p50_m": percentile(0.5),
        "p95_m": percentile(0.95),
        "maximum_m": ordered[-1] if ordered else None,
    }


def _status_counts(samples: list[SampleBase]) -> dict[str, int]:
    return {status: sum(sample.status == status for sample in samples) for status in STATUSES}


def _validated_hashes(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict) or set(raw) != {"evaluation_manifest", "evidence"}:
        raise EvidenceError("input_file_hashes must contain only evaluation_manifest and evidence")
    try:
        return {name: _sha256(raw[name]) for name in sorted(raw)}
    except ValueError as error:
        raise EvidenceError(f"input_file_hashes refused: {error}") from error


def _validate_json_value_limits(value: object) -> None:
    remaining = MAX_JSON_NODES

    def visit(item: object, depth: int) -> None:
        nonlocal remaining
        remaining -= 1
        if remaining < 0:
            raise EvidenceError(f"JSON input exceeds {MAX_JSON_NODES} values")
        if depth > MAX_JSON_DEPTH:
            raise EvidenceError(f"JSON input exceeds nesting depth {MAX_JSON_DEPTH}")
        if isinstance(item, str):
            if len(item) > MAX_JSON_STRING_CHARS:
                raise EvidenceError(f"JSON string exceeds {MAX_JSON_STRING_CHARS} characters")
        elif isinstance(item, list):
            for child in item:
                visit(child, depth + 1)
        elif isinstance(item, dict):
            for key, child in item.items():
                visit(key, depth + 1)
                visit(child, depth + 1)

    visit(value, 1)


def evaluate(
    evidence: object,
    *,
    evaluation_manifest: object,
    input_file_hashes: object,
) -> dict[str, object]:
    """Return a deterministic report that never represents physical acceptance."""

    _validate_json_value_limits(evidence)
    _validate_json_value_limits(evaluation_manifest)
    hashes = _validated_hashes(input_file_hashes)
    expected = _schema(EvaluationManifest, evaluation_manifest, "evaluation manifest")
    document = _schema(EvidenceDocument, evidence, "evidence")
    assert isinstance(expected, EvaluationManifest)
    assert isinstance(document, EvidenceDocument)
    route_length = _validate_manifest(expected)
    if document.evaluation_manifest_sha256 != hashes["evaluation_manifest"]:
        raise EvidenceError("evidence is not bound to the supplied evaluation manifest")

    run_ids: set[str] = set()
    raw_digests: set[str] = set()
    intervals: dict[tuple[str, str], list[tuple[float, float, str]]] = {}
    run_reports = []
    all_measured: list[float] = []
    all_upper: list[float] = []
    all_outliers = []
    all_runs_passed = True
    aggregate_counts = {
        series: {status: 0 for status in STATUSES}
        for series in ("estimates", "references", "localization_updates")
    }
    aggregate_gap_violations = []
    aggregate_reference_violations = []
    aggregate_max_gap = 0.0
    aggregate_max_reference_gap = 0.0
    aggregate_max_pairing_age: float | None = None

    for run in document.runs:
        if run.run_id in run_ids:
            raise EvidenceError(f"duplicate run ID {run.run_id!r}")
        run_ids.add(run.run_id)
        digest = run.manifest.raw_run_evidence_sha256
        if digest in raw_digests:
            raise EvidenceError(f"duplicate raw-run evidence digest {digest!r}")
        raw_digests.add(digest)
        _validate_run_identity(run, expected)

        start, end = run.interval.start_s, run.interval.end_s
        duration = _duration(start, end, f"run {run.run_id!r}")
        session_clock = (run.manifest.session_id, run.manifest.clock_id)
        for occupied_start, occupied_end, occupied_id in intervals.setdefault(session_clock, []):
            if start < occupied_end and occupied_start < end:
                raise EvidenceError(
                    f"run {run.run_id!r} overlaps {occupied_id!r} in one session clock"
                )
        intervals[session_clock].append((start, end, run.run_id))

        _validate_series(
            run.estimates,
            source_id=run.manifest.estimator_source_id,
            clock_id=run.manifest.clock_id,
            start=start,
            end=end,
            name=f"run {run.run_id!r} estimates",
        )
        _validate_series(
            run.references,
            source_id=run.manifest.reference.source_id,
            clock_id=run.manifest.clock_id,
            start=start,
            end=end,
            name=f"run {run.run_id!r} references",
        )
        _validate_series(
            run.localization_updates,
            source_id=run.manifest.estimator_source_id,
            clock_id=run.manifest.clock_id,
            start=start,
            end=end,
            name=f"run {run.run_id!r} localization updates",
        )

        pairs = _pair_samples(
            run.estimates,
            run.references,
            expected.limits.max_pairing_age_s,
            calibration_bound_m=run.manifest.reference.calibration_bound_m,
            clock_alignment_bound_s=run.manifest.reference.clock_alignment_bound_s,
        )
        crossings, checkpoint_passed = _checkpoint_report(run, expected, pairs)
        measured = [
            float(pair["measured_error_m"])
            for pair in pairs
            if pair["measured_error_m"] is not None
        ]
        upper = [
            float(pair["position_error_upper_bound_m"])
            for pair in pairs
            if pair["position_error_upper_bound_m"] is not None
        ]
        pairing_ages = [
            float(pair["pairing_age_s"]) for pair in pairs if pair["pairing_age_s"] is not None
        ]
        max_gap, gap_violations = _coverage(run.localization_updates, start=start, end=end)
        max_reference_gap, reference_violations = _coverage(
            pairs, start=start, end=end, pair_records=True
        )
        counts = {
            "estimates": _status_counts(run.estimates),
            "references": _status_counts(run.references),
            "localization_updates": _status_counts(run.localization_updates),
        }
        samples_available = all(
            values["invalid"] == 0 and values["unavailable"] == 0 for values in counts.values()
        )
        sample_count_passed = (
            len(run.estimates) >= expected.limits.minimum_estimate_samples_per_run
            and len(run.references) >= expected.limits.minimum_reference_samples_per_run
            and len(run.localization_updates)
            >= expected.limits.minimum_localization_updates_per_run
        )
        duration_passed = (
            expected.route.minimum_duration_s <= duration <= expected.route.maximum_duration_s
        )
        reference_quality_passed = (
            run.manifest.reference.calibration_bound_m
            <= expected.reference.maximum_calibration_bound_m
            and run.manifest.reference.clock_alignment_bound_s
            <= expected.reference.maximum_clock_alignment_bound_s
        )
        pairings_complete = bool(pairs) and all(
            pair["position_error_upper_bound_m"] is not None for pair in pairs
        )
        measured_distribution = _distribution(measured)
        upper_distribution = _distribution(upper)
        error_passed = bool(upper) and upper_distribution["p95_m"] <= POSITION_ERROR_LIMIT_M
        gap_passed = max_gap <= LOCALIZATION_GAP_LIMIT_S
        reference_coverage_passed = max_reference_gap <= LOCALIZATION_GAP_LIMIT_S
        passed = (
            samples_available
            and sample_count_passed
            and duration_passed
            and reference_quality_passed
            and pairings_complete
            and checkpoint_passed
            and error_passed
            and gap_passed
            and reference_coverage_passed
        )
        outliers = [
            pair
            for pair in pairs
            if pair["position_error_upper_bound_m"] is not None
            and pair["position_error_upper_bound_m"] > POSITION_ERROR_LIMIT_M
        ]
        max_pairing_age = max(pairing_ages, default=None)

        all_measured.extend(measured)
        all_upper.extend(upper)
        all_outliers.extend({"run_id": run.run_id} | pair for pair in outliers)
        all_runs_passed &= passed
        aggregate_max_gap = max(aggregate_max_gap, max_gap)
        aggregate_max_reference_gap = max(aggregate_max_reference_gap, max_reference_gap)
        if max_pairing_age is not None:
            aggregate_max_pairing_age = max(aggregate_max_pairing_age or 0.0, max_pairing_age)
        for series, values in counts.items():
            for status, count in values.items():
                aggregate_counts[series][status] += count
        aggregate_gap_violations.extend(
            {"run_id": run.run_id} | violation for violation in gap_violations
        )
        aggregate_reference_violations.extend(
            {"run_id": run.run_id} | violation for violation in reference_violations
        )
        run_reports.append(
            {
                "run_id": run.run_id,
                "raw_run_evidence_sha256": digest,
                "manifest": run.manifest.model_dump(mode="json"),
                "interval": {
                    "start_s": start,
                    "end_s": end,
                    "duration_s": duration,
                },
                "sample_counts": counts,
                "sample_count_passed": sample_count_passed,
                "duration_passed": duration_passed,
                "reference_quality_passed": reference_quality_passed,
                "checkpoint_crossings": crossings,
                "checkpoint_coverage_passed": checkpoint_passed,
                "paired_measurements": pairs,
                "pairings_complete": pairings_complete,
                "max_observed_pairing_age_s": max_pairing_age,
                "measured_error_distribution": measured_distribution,
                "position_error_upper_bound_distribution": upper_distribution,
                "errors_over_position_error_limit": outliers,
                "max_localization_update_gap_s": max_gap,
                "localization_gap_violations": gap_violations,
                "max_paired_reference_coverage_gap_s": max_reference_gap,
                "paired_reference_coverage_violations": reference_violations,
                "software_measurement_checks_passed": passed,
            }
        )

    expected_digests = set(expected.expected_raw_run_evidence_sha256)
    if raw_digests != expected_digests:
        raise EvidenceError("raw-run evidence digests do not match the evaluation manifest")
    aggregate_measured = _distribution(all_measured)
    aggregate_upper = _distribution(all_upper)
    aggregate_error_passed = bool(all_upper) and aggregate_upper["p95_m"] <= POSITION_ERROR_LIMIT_M
    report: dict[str, object] = {
        "schema_version": 1,
        "report_kind": "localization_software_measurement_report",
        "input_file_hashes": hashes,
        "evaluation_manifest": expected.model_dump(mode="json")
        | {"checkpoint_path_length_m": route_length},
        "criteria": {
            "required_recording_runs": RUN_COUNT,
            "required_route_phases": list(REQUIRED_ROUTE_PHASES),
            "route_speed_limit_m_s": ROUTE_SPEED_LIMIT_M_S,
            "position_error_upper_bound_p95_limit_m": POSITION_ERROR_LIMIT_M,
            "localization_update_gap_limit_s": LOCALIZATION_GAP_LIMIT_S,
            "paired_reference_coverage_gap_limit_s": LOCALIZATION_GAP_LIMIT_S,
            "maximum_series_samples_per_run": MAX_SERIES_SAMPLES,
            "pairing_method": (
                "maximum-cardinality ordered earliest-feasible estimate without reuse"
            ),
            "position_error_upper_bound_method": (
                "measured distance + calibration bound + "
                "0.5 m/s * (pairing age + clock alignment bound)"
            ),
        },
        "run_count": len(run_reports),
        "runs": run_reports,
        "aggregate_sample_counts": aggregate_counts,
        "aggregate_measured_error_distribution": aggregate_measured,
        "aggregate_position_error_upper_bound_distribution": aggregate_upper,
        "aggregate_errors_over_position_error_limit": all_outliers,
        "aggregate_max_localization_update_gap_s": aggregate_max_gap,
        "aggregate_localization_gap_violations": aggregate_gap_violations,
        "aggregate_max_paired_reference_coverage_gap_s": (aggregate_max_reference_gap),
        "aggregate_paired_reference_coverage_violations": (aggregate_reference_violations),
        "aggregate_max_observed_pairing_age_s": aggregate_max_pairing_age,
        "software_measurement_checks_passed": (all_runs_passed and aggregate_error_passed),
        "assurance_scope": {
            "software_measurement_only": True,
            "artifact_authenticity_evaluated": False,
            "physical_flight_acceptance_evaluated": False,
            "failure_drills_evaluated": False,
            "release_readiness_evaluated": False,
        },
        "material_limitations": [
            (
                "Hashes bind supplied bytes and declared artifacts but do not "
                "authenticate their recorder or approver."
            ),
            ("Checkpoint and source-independence claims require external signed physical records."),
            (
                "Command and audit streams, video, failure drills, RC intervention, "
                "physical flight acceptance, and release readiness are not evaluated."
            ),
            (
                "Synthetic evidence can exercise this evaluator but cannot satisfy "
                "physical flight acceptance."
            ),
        ],
    }
    body = _serialize(report, pretty=False)
    result = report | {"report_sha256": hashlib.sha256(body).hexdigest()}
    _serialize(result, pretty=False)
    return result


def _scan_depth(text: str, name: str) -> None:
    depth = 0
    quoted = False
    escaped = False
    for character in text:
        if quoted:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quoted = False
        elif character == '"':
            quoted = True
        elif character in "[{":
            depth += 1
            if depth > MAX_JSON_DEPTH:
                raise EvidenceError(f"{name} exceeds JSON depth {MAX_JSON_DEPTH}")
        elif character in "]}":
            depth -= 1


def _json_integer(value: str) -> int:
    if len(value.lstrip("-")) > 32:
        raise EvidenceError("JSON integer exceeds 32 digits")
    return int(value)


def _json_float(value: str) -> float:
    if len(value) > 64:
        raise EvidenceError("JSON number exceeds 64 characters")
    number = float(value)
    if not math.isfinite(number):
        raise EvidenceError(f"nonfinite JSON number {value!r}")
    return number


def _json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    document = {}
    for key, value in pairs:
        if key in document:
            raise EvidenceError(f"duplicate JSON key {key!r}")
        document[key] = value
    return document


def _nonfinite(value: str) -> object:
    raise EvidenceError(f"nonfinite JSON value {value!r}")


def _safe_path(path: Path, name: str) -> Path:
    absolute = Path(os.path.abspath(os.fspath(path)))
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current /= component
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            break
        if stat.S_ISLNK(metadata.st_mode):
            raise EvidenceError(f"{name} cannot contain a symbolic link")
    return absolute


def _read(path: Path, name: str) -> tuple[object, str]:
    absolute = _safe_path(path, name)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(absolute, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise EvidenceError(f"{name} must be a regular file")
        if metadata.st_size > MAX_INPUT_BYTES:
            raise EvidenceError(f"{name} exceeds {MAX_INPUT_BYTES} bytes")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            payload = stream.read(MAX_INPUT_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(payload) > MAX_INPUT_BYTES:
        raise EvidenceError(f"{name} exceeds {MAX_INPUT_BYTES} bytes")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise EvidenceError(f"{name} must be UTF-8 JSON") from error
    _scan_depth(text, name)
    try:
        document = json.loads(
            text,
            object_pairs_hook=_json_object,
            parse_constant=_nonfinite,
            parse_int=_json_integer,
            parse_float=_json_float,
        )
    except json.JSONDecodeError as error:
        raise EvidenceError(f"cannot parse {name} JSON: {error}") from error
    _validate_json_value_limits(document)
    return document, hashlib.sha256(payload).hexdigest()


def _serialize(document: object, *, pretty: bool) -> bytes:
    content = (
        json.dumps(
            document,
            allow_nan=False,
            indent=2 if pretty else None,
            separators=None if pretty else (",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode()
    if len(content) > MAX_REPORT_BYTES:
        raise EvidenceError(f"report exceeds {MAX_REPORT_BYTES} bytes")
    return content


def _write(path: Path, content: bytes) -> None:
    absolute = _safe_path(path, "output path")
    if os.path.lexists(absolute):
        raise EvidenceError("output path must not already exist")
    if not absolute.parent.is_dir():
        raise EvidenceError("output parent directory must already exist")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=absolute.parent,
            prefix=".localization-measurement-",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            if stream.write(content) != len(content):
                raise OSError("incomplete report write")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, absolute, follow_symlinks=False)
        directory = os.open(absolute.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--evaluation-manifest", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        manifest, manifest_hash = _read(args.evaluation_manifest, "evaluation manifest")
        evidence, evidence_hash = _read(args.evidence, "evidence")
        report = evaluate(
            evidence,
            evaluation_manifest=manifest,
            input_file_hashes={
                "evaluation_manifest": manifest_hash,
                "evidence": evidence_hash,
            },
        )
        content = _serialize(report, pretty=True)
        if args.output is None:
            sys.stdout.buffer.write(content)
            sys.stdout.buffer.flush()
        else:
            _write(args.output, content)
    except (
        EvidenceError,
        OSError,
        RecursionError,
        TypeError,
        UnicodeError,
        ValueError,
        OverflowError,
    ) as error:
        print(f"localization software evidence refused: {error}", file=sys.stderr)
        return 1
    return 0 if report["software_measurement_checks_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
