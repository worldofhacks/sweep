from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys

import pytest

import evals.flight_acceptance as acceptance
from evals.flight_acceptance import (
    AvailablePositionSample,
    EvidenceError,
    _pair_samples,
    evaluate,
)


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _manifest() -> dict:
    return {
        "schema_version": 1,
        "manifest_kind": "localization_software_measurement_manifest",
        "manifest_id": "owner-reviewed-route-evaluation-v1",
        "route": {
            "route_id": "approved-lobby-kitchen-return-v1",
            "route_sha256": _digest("route"),
            "minimum_duration_s": 8.0,
            "maximum_duration_s": 20.0,
            "checkpoints": [
                {
                    "checkpoint_id": "launch",
                    "position_map_m": [0.0, 0.0, 1.0],
                    "radius_m": 0.1,
                },
                {
                    "checkpoint_id": "kitchen-hold",
                    "position_map_m": [2.0, 0.0, 1.0],
                    "radius_m": 0.1,
                },
                {
                    "checkpoint_id": "return",
                    "position_map_m": [0.0, 0.0, 1.0],
                    "radius_m": 0.1,
                },
            ],
        },
        "deployment": {
            "aircraft_id": "mini3-serial-1",
            "map_bundle_sha256": _digest("map"),
            "geometry_sha256": _digest("geometry"),
            "camera_calibration_sha256": _digest("camera"),
            "body_extrinsics_sha256": _digest("extrinsics"),
            "latency_calibration_sha256": _digest("latency"),
        },
        "estimator": {
            "source_id": "fused-localizer",
            "build_sha256": _digest("build"),
            "config_sha256": _digest("config"),
        },
        "reference": {
            "source_id": "survey-total-station",
            "method": "surveyed total station",
            "calibration_sha256": _digest("reference-calibration"),
            "clock_alignment_sha256": _digest("clock-alignment"),
            "maximum_calibration_bound_m": 0.02,
            "maximum_clock_alignment_bound_s": 0.01,
        },
        "expected_raw_run_evidence_sha256": [_digest(f"raw-run-{index}") for index in range(5)],
        "limits": {
            "minimum_estimate_samples_per_run": 20,
            "minimum_reference_samples_per_run": 20,
            "minimum_localization_updates_per_run": 20,
            "max_pairing_age_s": 0.05,
        },
    }


def _position(step: int) -> list[float]:
    x = step * 0.2 if step <= 10 else (20 - step) * 0.2
    return [x, 0.0, 1.0]


def _run(index: int, *, measured_error_m: float = 0.1) -> dict:
    start = index * 20.0
    timestamps = [start + step * 0.5 for step in range(21)]
    return {
        "run_id": f"run-{index}",
        "manifest": {
            "raw_run_evidence_sha256": _digest(f"raw-run-{index}"),
            "route_id": "approved-lobby-kitchen-return-v1",
            "route_sha256": _digest("route"),
            "aircraft_id": "mini3-serial-1",
            "map_bundle_sha256": _digest("map"),
            "geometry_sha256": _digest("geometry"),
            "camera_calibration_sha256": _digest("camera"),
            "body_extrinsics_sha256": _digest("extrinsics"),
            "latency_calibration_sha256": _digest("latency"),
            "session_id": "physical-session-1",
            "clock_id": "aligned-room-clock",
            "estimator_source_id": "fused-localizer",
            "estimator_build_sha256": _digest("build"),
            "estimator_config_sha256": _digest("config"),
            "reference": {
                "source_id": "survey-total-station",
                "method": "surveyed total station",
                "calibration_sha256": _digest("reference-calibration"),
                "calibration_bound_m": 0.01,
                "clock_alignment_sha256": _digest("clock-alignment"),
                "clock_alignment_bound_s": 0.01,
                "independence_claimed": True,
            },
        },
        "interval": {"start_s": start, "end_s": start + 10.0},
        "estimates": [
            {
                "id": f"estimate-{step}",
                "timestamp_s": timestamp,
                "position_map_m": [
                    _position(step)[0],
                    measured_error_m,
                    1.0,
                ],
                "source_id": "fused-localizer",
                "clock_id": "aligned-room-clock",
                "status": "available",
            }
            for step, timestamp in enumerate(timestamps)
        ],
        "references": [
            {
                "id": f"reference-{step}",
                "timestamp_s": timestamp,
                "position_map_m": _position(step),
                "source_id": "survey-total-station",
                "clock_id": "aligned-room-clock",
                "status": "available",
            }
            for step, timestamp in enumerate(timestamps)
        ],
        "localization_updates": [
            {
                "id": f"update-{step}",
                "timestamp_s": timestamp,
                "source_id": "fused-localizer",
                "clock_id": "aligned-room-clock",
                "status": "available",
            }
            for step, timestamp in enumerate(timestamps)
        ],
        "checkpoint_crossings": [
            {"checkpoint_id": "launch", "reference_id": "reference-0"},
            {"checkpoint_id": "kitchen-hold", "reference_id": "reference-10"},
            {"checkpoint_id": "return", "reference_id": "reference-20"},
        ],
    }


def _evidence() -> dict:
    return {
        "schema_version": 1,
        "document_kind": "localization_software_measurement_evidence",
        "evaluation_manifest_sha256": "",
        "runs": [_run(index) for index in range(5)],
    }


def _json_bytes(document: object) -> bytes:
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode()


def _evaluate(evidence: dict | None = None, manifest: dict | None = None) -> dict:
    evidence = copy.deepcopy(evidence or _evidence())
    manifest = copy.deepcopy(manifest or _manifest())
    manifest_payload = _json_bytes(manifest)
    evidence["evaluation_manifest_sha256"] = hashlib.sha256(manifest_payload).hexdigest()
    return evaluate(
        evidence,
        evaluation_manifest=manifest,
        input_file_hashes={
            "evaluation_manifest": hashlib.sha256(manifest_payload).hexdigest(),
            "evidence": hashlib.sha256(_json_bytes(evidence)).hexdigest(),
        },
    )


def _write_inputs(tmp_path, evidence: dict, manifest: dict):
    manifest_path = tmp_path / "manifest.json"
    evidence_path = tmp_path / "evidence.json"
    manifest_payload = _json_bytes(manifest)
    manifest_path.write_bytes(manifest_payload)
    evidence["evaluation_manifest_sha256"] = hashlib.sha256(manifest_payload).hexdigest()
    evidence_path.write_bytes(_json_bytes(evidence))
    return evidence_path, manifest_path


def _cli(evidence_path, manifest_path, *extra):
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "evals.flight_acceptance",
            str(evidence_path),
            "--evaluation-manifest",
            str(manifest_path),
            *map(str, extra),
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def test_valid_measurements_are_hash_bound_and_explicitly_software_only():
    report = _evaluate()

    assert report["software_measurement_checks_passed"] is True
    assert report["run_count"] == 5
    assert report["evaluation_manifest"]["checkpoint_path_length_m"] == 4.0
    assert report["aggregate_position_error_upper_bound_distribution"]["p95_m"] == pytest.approx(
        0.115
    )
    assert report["assurance_scope"] == {
        "software_measurement_only": True,
        "artifact_authenticity_evaluated": False,
        "physical_flight_acceptance_evaluated": False,
        "failure_drills_evaluated": False,
        "release_readiness_evaluated": False,
    }
    assert "flight_approved" not in report
    assert len(report["report_sha256"]) == 64


@pytest.mark.parametrize(
    ("path", "replacement", "message"),
    [
        (("runs", 0, "manifest", "route_id"), "other-route", "route_id"),
        (
            ("runs", 0, "manifest", "map_bundle_sha256"),
            _digest("other-map"),
            "map_bundle_sha256",
        ),
        (
            ("runs", 0, "manifest", "latency_calibration_sha256"),
            _digest("other-latency"),
            "latency_calibration_sha256",
        ),
    ],
)
def test_run_identity_must_match_the_external_manifest(path, replacement, message):
    evidence = _evidence()
    target = evidence
    for component in path[:-1]:
        target = target[component]
    target[path[-1]] = replacement

    with pytest.raises(EvidenceError, match=message):
        _evaluate(evidence)


def test_evidence_must_bind_the_exact_manifest_bytes():
    evidence = _evidence()
    evidence["evaluation_manifest_sha256"] = _digest("other-manifest")

    with pytest.raises(EvidenceError, match="not bound"):
        evaluate(
            evidence,
            evaluation_manifest=_manifest(),
            input_file_hashes={
                "evaluation_manifest": _digest("manifest"),
                "evidence": _digest("evidence"),
            },
        )


def test_duplicate_raw_recording_cannot_be_relabelled_or_time_shifted():
    evidence = _evidence()
    evidence["runs"][1]["manifest"]["raw_run_evidence_sha256"] = evidence["runs"][0]["manifest"][
        "raw_run_evidence_sha256"
    ]

    with pytest.raises(EvidenceError, match="duplicate raw-run"):
        _evaluate(evidence)


def test_manifest_requires_five_unique_expected_recording_digests():
    manifest = _manifest()
    manifest["expected_raw_run_evidence_sha256"][1] = manifest["expected_raw_run_evidence_sha256"][
        0
    ]

    with pytest.raises(EvidenceError, match="must be unique"):
        _evaluate(manifest=manifest)


def test_missing_or_unexpected_recording_digest_is_rejected():
    evidence = _evidence()
    evidence["runs"][0]["manifest"]["raw_run_evidence_sha256"] = _digest("unexpected")

    with pytest.raises(EvidenceError, match="do not match"):
        _evaluate(evidence)


def test_short_or_undersampled_runs_cannot_pass():
    manifest = _manifest()
    manifest["route"]["minimum_duration_s"] = 12.0
    manifest["limits"] = {
        **manifest["limits"],
        "minimum_estimate_samples_per_run": 23,
        "minimum_reference_samples_per_run": 23,
        "minimum_localization_updates_per_run": 23,
    }

    report = _evaluate(manifest=manifest)

    assert report["software_measurement_checks_passed"] is False
    assert report["runs"][0]["duration_passed"] is False
    assert report["runs"][0]["sample_count_passed"] is False


def test_route_duration_floor_is_derived_from_checkpoint_distance_and_speed():
    manifest = _manifest()
    manifest["route"]["minimum_duration_s"] = 7.999

    with pytest.raises(EvidenceError, match="shorter than its checkpoint path"):
        _evaluate(manifest=manifest)


def test_checkpoint_sequence_and_positions_must_cover_the_pinned_route():
    evidence = _evidence()
    evidence["runs"][0]["checkpoint_crossings"][1]["checkpoint_id"] = "not-kitchen"
    report = _evaluate(evidence)
    assert report["runs"][0]["checkpoint_coverage_passed"] is False

    evidence = _evidence()
    evidence["runs"][0]["references"][10]["position_map_m"] = [0.0, 0.0, 1.0]
    report = _evaluate(evidence)
    assert report["runs"][0]["checkpoint_coverage_passed"] is False


def test_checkpoint_labels_cannot_hide_faster_than_allowed_travel():
    evidence = _evidence()
    run = evidence["runs"][0]
    run["checkpoint_crossings"] = [
        {"checkpoint_id": "launch", "reference_id": "reference-0"},
        {"checkpoint_id": "kitchen-hold", "reference_id": "reference-1"},
        {"checkpoint_id": "return", "reference_id": "reference-2"},
    ]
    run["references"][1]["position_map_m"] = [2.0, 0.0, 1.0]
    run["references"][2]["position_map_m"] = [0.0, 0.0, 1.0]
    run["estimates"][1]["position_map_m"] = [2.0, 0.1, 1.0]
    run["estimates"][2]["position_map_m"] = [0.0, 0.1, 1.0]

    report = _evaluate(evidence)

    crossing = report["runs"][0]["checkpoint_crossings"][1]
    assert crossing["elapsed_from_previous_s"] == 0.5
    assert crossing["minimum_elapsed_from_previous_s"] == 4.0
    assert report["runs"][0]["checkpoint_coverage_passed"] is False
    assert report["software_measurement_checks_passed"] is False


def test_reference_and_clock_bounds_are_included_in_the_error_gate():
    evidence = _evidence()
    for estimate in evidence["runs"][0]["estimates"]:
        estimate["position_map_m"][1] = 0.24

    report = _evaluate(evidence)

    assert report["runs"][0]["measured_error_distribution"]["p95_m"] == pytest.approx(0.24)
    assert report["runs"][0]["position_error_upper_bound_distribution"]["p95_m"] == pytest.approx(
        0.255
    )
    assert report["software_measurement_checks_passed"] is False


def test_a_bad_run_cannot_be_hidden_by_a_passing_aggregate_p95():
    evidence = _evidence()
    for estimate in evidence["runs"][0]["estimates"][1:3]:
        estimate["position_map_m"][1] = 0.24

    report = _evaluate(evidence)

    assert report["runs"][0]["position_error_upper_bound_distribution"]["p95_m"] == pytest.approx(
        0.255
    )
    assert report["aggregate_position_error_upper_bound_distribution"]["p95_m"] == pytest.approx(
        0.115
    )
    assert report["software_measurement_checks_passed"] is False


def test_reference_quality_cannot_exceed_the_pinned_maximum():
    evidence = _evidence()
    evidence["runs"][0]["manifest"]["reference"]["calibration_bound_m"] = 0.021

    report = _evaluate(evidence)

    assert report["runs"][0]["reference_quality_passed"] is False
    assert report["software_measurement_checks_passed"] is False


def test_update_and_reference_gaps_include_run_boundaries():
    evidence = _evidence()
    evidence["runs"][0]["localization_updates"] = evidence["runs"][0]["localization_updates"][1:]

    report = _evaluate(evidence)

    assert report["runs"][0]["max_localization_update_gap_s"] == 0.5
    assert report["runs"][0]["max_paired_reference_coverage_gap_s"] == 0.5
    assert report["software_measurement_checks_passed"] is True

    evidence["runs"][0]["localization_updates"] = evidence["runs"][0]["localization_updates"][1:]
    report = _evaluate(evidence)
    assert report["runs"][0]["max_localization_update_gap_s"] == 1.0
    assert report["software_measurement_checks_passed"] is False


def test_unavailable_samples_are_counted_and_fail_the_measurement():
    evidence = _evidence()
    sample = evidence["runs"][0]["estimates"][4]
    sample.pop("position_map_m")
    sample.update(status="unavailable", reason="tag occluded")

    report = _evaluate(evidence)

    assert report["runs"][0]["sample_counts"]["estimates"]["unavailable"] == 1
    assert report["software_measurement_checks_passed"] is False


def test_ordered_pairing_avoids_the_local_nearest_trap():
    def sample(sample_id, timestamp):
        return AvailablePositionSample(
            id=sample_id,
            timestamp_s=timestamp,
            source_id="source",
            clock_id="clock",
            status="available",
            position_map_m=[0.0, 0.0, 0.0],
        )

    pairs = _pair_samples(
        [sample("e0", 0.0), sample("e1", 0.1)],
        [sample("r0", 0.09), sample("r1", 0.11)],
        0.1,
        calibration_bound_m=0.0,
        clock_alignment_bound_s=0.0,
    )

    assert [pair["estimate_id"] for pair in pairs] == ["e0", "e1"]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda evidence: evidence["runs"][0].update(extra="ignored"),
        lambda evidence: evidence["runs"][0]["estimates"][0].update(reason="contradictory"),
        lambda evidence: evidence["runs"][0]["estimates"][0].update(timestamp_s=True),
        lambda evidence: evidence["runs"][0]["estimates"][0].update(
            position_map_m=[1e308, 0.0, 0.0]
        ),
    ],
)
def test_unknown_fields_booleans_and_extreme_coordinates_are_typed_refusals(mutation):
    evidence = _evidence()
    mutation(evidence)

    with pytest.raises(EvidenceError, match="schema refused"):
        _evaluate(evidence)


def test_overlapping_runs_in_one_session_clock_are_rejected():
    evidence = _evidence()
    shift = evidence["runs"][1]["interval"]["start_s"]
    evidence["runs"][1]["interval"] = {"start_s": 0.0, "end_s": 10.0}
    for series in ("estimates", "references", "localization_updates"):
        for sample in evidence["runs"][1][series]:
            sample["timestamp_s"] -= shift

    with pytest.raises(EvidenceError, match="overlaps"):
        _evaluate(evidence)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda evidence: evidence["runs"][0]["estimates"][1].update(id="estimate-0"),
            "duplicate ID",
        ),
        (
            lambda evidence: evidence["runs"][0]["estimates"][1].update(timestamp_s=0.0),
            "strictly increasing",
        ),
        (
            lambda evidence: evidence["runs"][0]["references"][0].update(
                clock_id="unaligned-clock"
            ),
            "wrong source or clock",
        ),
    ],
)
def test_sample_identity_time_and_clock_are_strict(mutation, message):
    evidence = _evidence()
    mutation(evidence)

    with pytest.raises(EvidenceError, match=message):
        _evaluate(evidence)


def test_cli_hashes_both_inputs_and_uses_stable_exit_codes(tmp_path):
    evidence_path, manifest_path = _write_inputs(tmp_path, _evidence(), _manifest())
    completed = _cli(evidence_path, manifest_path)

    assert completed.returncode == 0
    report = json.loads(completed.stdout)
    assert report["input_file_hashes"] == {
        "evaluation_manifest": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "evidence": hashlib.sha256(evidence_path.read_bytes()).hexdigest(),
    }

    failed = _evidence()
    for estimate in failed["runs"][0]["estimates"]:
        estimate["position_map_m"][1] = 0.3
    evidence_path, manifest_path = _write_inputs(tmp_path, failed, _manifest())
    output = tmp_path / "failure-report.json"
    completed = _cli(evidence_path, manifest_path, "--output", output)
    assert completed.returncode == 1
    assert json.loads(output.read_text())["software_measurement_checks_passed"] is False


def test_cli_rejects_duplicate_keys_and_extreme_numbers_without_a_traceback(tmp_path):
    manifest_path = tmp_path / "manifest.json"
    evidence_path = tmp_path / "evidence.json"
    manifest_path.write_bytes(_json_bytes(_manifest()))
    evidence_path.write_text('{"schema_version": 1, "schema_version": 1e999}')

    completed = _cli(evidence_path, manifest_path)

    assert completed.returncode == 1
    assert "localization software evidence refused:" in completed.stderr
    assert "Traceback" not in completed.stderr


def test_cli_rejects_symlinked_inputs_and_output_parents(tmp_path):
    evidence_path, manifest_path = _write_inputs(tmp_path, _evidence(), _manifest())
    evidence_link = tmp_path / "evidence-link.json"
    evidence_link.symlink_to(evidence_path)

    completed = _cli(evidence_link, manifest_path)
    assert completed.returncode == 1
    assert "symbolic link" in completed.stderr

    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    completed = _cli(
        evidence_path,
        manifest_path,
        "--output",
        linked_parent / "report.json",
    )
    assert completed.returncode == 1
    assert not (real_parent / "report.json").exists()


def test_existing_output_is_never_overwritten(tmp_path):
    evidence_path, manifest_path = _write_inputs(tmp_path, _evidence(), _manifest())
    output = tmp_path / "report.json"
    output.write_text("sentinel")

    completed = _cli(evidence_path, manifest_path, "--output", output)

    assert completed.returncode == 1
    assert output.read_text() == "sentinel"


def test_file_depth_and_report_size_limits_are_controlled(tmp_path, monkeypatch):
    nested = tmp_path / "nested.json"
    nested.write_text("[" * (acceptance.MAX_JSON_DEPTH + 1) + "0" + "]" * 33)
    with pytest.raises(EvidenceError, match="depth"):
        acceptance._read(nested, "nested")

    small = tmp_path / "small.json"
    small.write_text("{}")
    monkeypatch.setattr(acceptance, "MAX_INPUT_BYTES", 1)
    with pytest.raises(EvidenceError, match="exceeds"):
        acceptance._read(small, "small")

    monkeypatch.setattr(acceptance, "MAX_REPORT_BYTES", 100)
    with pytest.raises(EvidenceError, match="report exceeds"):
        _evaluate()
