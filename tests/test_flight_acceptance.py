from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys

import pytest

from evals.flight_acceptance import EvidenceError, evaluate


def _sample(sample_id, timestamp, position, source_id, clock_id="room-clock"):
    return {
        "id": sample_id,
        "timestamp_s": timestamp,
        "position_map_m": position,
        "source_id": source_id,
        "clock_id": clock_id,
        "status": "available",
    }


def _run(index, error=0.25):
    estimator = f"fused-localizer-{index}"
    reference = f"survey-total-station-{index}"
    start = index * 10.0
    end = start + 1.0
    points = tuple(start + offset for offset in (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0))
    return {
        "run_id": f"run-{index}",
        "route_id": "approved-lobby-route",
        "manifest": {
            "map_id": "map-sha-1",
            "geometry_id": "geometry-sha-1",
            "aircraft_id": "aircraft-serial-1",
            "camera_calibration_id": "camera-calibration-sha-1",
            "body_extrinsics_id": "body-extrinsics-sha-1",
            "clock_id": "room-clock",
            "session_id": "mapping-session-1",
            "estimator_source_id": estimator,
            "reference": {
                "source_id": reference,
                "method": "surveyed total station",
                "calibration_id": "total-station-calibration-2026-09",
                "calibration_bound_m": 0.01,
                "independence_claimed": True,
            },
        },
        "interval": {"start_s": start, "end_s": end},
        "estimates": [
            _sample(f"estimate-{point}", point, [float(index), 0.0, 0.0], estimator)
            for point in points
        ],
        "references": [
            _sample(f"reference-{point}", point, [float(index) + error, 0.0, 0.0], reference)
            for point in points
        ],
        "localization_updates": [
            {
                "id": f"update-{point}",
                "timestamp_s": point,
                "source_id": estimator,
                "clock_id": "room-clock",
                "status": "available",
            }
            for point in (start, start + 0.5, end)
        ],
    }


def _evidence(error=0.25):
    return {
        "schema_version": 1,
        "criteria": {"max_pairing_age_s": 0.1},
        "runs": [_run(index, error=error) for index in range(5)],
    }


def _hashes():
    return {"evidence": "a" * 64}


def test_evaluator_runs_five_literal_independent_rehearsals_at_the_position_boundary():
    report = evaluate(_evidence(0.25), input_file_hashes=_hashes())

    assert report["software_evidence_checks_passed"] is True
    assert report["aggregate_error_distribution"]["p95_m"] == pytest.approx(0.25)
    assert report["aggregate_max_localization_update_gap_s"] == pytest.approx(0.5)
    assert report["aggregate_max_paired_reference_coverage_gap_s"] == pytest.approx(0.1)
    assert report["aggregate_sample_counts"]["localization_updates"]["available"] == 15
    assert report["run_count"] == 5
    assert report["distinct_route_count"] == 1
    assert "flight_approved" not in report


def test_p95_error_above_the_fixed_limit_fails_the_actual_evaluator():
    report = evaluate(_evidence(0.250001), input_file_hashes=_hashes())

    assert report["software_evidence_checks_passed"] is False
    assert report["aggregate_error_distribution"]["p95_m"] == pytest.approx(0.250001)
    assert len(report["aggregate_errors_over_position_error_limit"]) == 50


def test_missing_a_route_rehearsal_is_rejected():
    evidence = _evidence()
    evidence["runs"].pop()

    with pytest.raises(EvidenceError, match="exactly five"):
        evaluate(evidence, input_file_hashes=_hashes())


def test_duplicate_run_and_self_referential_reference_are_rejected():
    evidence = _evidence()
    evidence["runs"][1]["run_id"] = evidence["runs"][0]["run_id"]
    with pytest.raises(EvidenceError, match="duplicate run"):
        evaluate(evidence, input_file_hashes=_hashes())

    evidence = _evidence()
    evidence["runs"][0]["manifest"]["reference"]["source_id"] = evidence["runs"][0]["manifest"][
        "estimator_source_id"
    ]
    with pytest.raises(EvidenceError, match="source IDs must differ"):
        evaluate(evidence, input_file_hashes=_hashes())


def test_bad_run_p95_cannot_be_hidden_by_a_passing_aggregate_distribution():
    evidence = _evidence(error=0.0)
    evidence["runs"][0]["references"][-1]["position_map_m"] = [0.251, 0.0, 0.0]

    report = evaluate(evidence, input_file_hashes=_hashes())

    assert report["aggregate_error_distribution"]["p95_m"] == pytest.approx(0.0)
    assert report["runs"][0]["error_distribution"]["p95_m"] == pytest.approx(0.251)
    assert report["software_evidence_checks_passed"] is False


def test_rehearsals_bind_one_deployment_and_unique_recording_content():
    evidence = _evidence()
    evidence["runs"][1]["manifest"]["map_id"] = "other-map"
    with pytest.raises(EvidenceError, match="mixed deployment"):
        evaluate(evidence, input_file_hashes=_hashes())

    evidence = _evidence()
    copied = copy.deepcopy(evidence["runs"][0])
    copied["run_id"] = "renamed-run"
    copied["route_id"] = "renamed-route"
    copied["manifest"]["session_id"] = "renamed-session"
    copied["manifest"]["estimator_source_id"] = "renamed-estimator"
    copied["manifest"]["reference"]["source_id"] = "renamed-reference"
    for series in ("estimates", "references", "localization_updates"):
        for index, sample in enumerate(copied[series]):
            sample["id"] = f"renamed-{series}-{index}"
            sample["source_id"] = (
                "renamed-reference" if series == "references" else "renamed-estimator"
            )
    copied["operator_note"] = "different label does not make a new recording"
    evidence["runs"][1] = copied
    with pytest.raises(EvidenceError, match="duplicate recording content"):
        evaluate(evidence, input_file_hashes=_hashes())


def test_schema_version_and_pairing_age_are_bounded_by_the_actual_evaluator():
    evidence = _evidence()
    evidence["schema_version"] = True
    with pytest.raises(EvidenceError, match="schema_version"):
        evaluate(evidence, input_file_hashes=_hashes())

    evidence = _evidence()
    evidence["criteria"]["max_pairing_age_s"] = 0.500001
    with pytest.raises(EvidenceError, match="no more than 0.5"):
        evaluate(evidence, input_file_hashes=_hashes())


def test_start_to_end_gap_over_500ms_fails_even_when_samples_are_finite():
    evidence = _evidence()
    evidence["runs"][0]["localization_updates"] = [
        {
            "id": "only-update",
            "timestamp_s": 0.49,
            "source_id": evidence["runs"][0]["manifest"]["estimator_source_id"],
            "clock_id": "room-clock",
            "status": "available",
        }
    ]

    report = evaluate(evidence, input_file_hashes=_hashes())

    assert report["software_evidence_checks_passed"] is False
    assert report["runs"][0]["max_localization_update_gap_s"] == pytest.approx(0.51)


def test_sparse_paired_reference_coverage_fails_across_the_run_boundaries():
    evidence = _evidence()
    evidence["runs"][0]["estimates"] = evidence["runs"][0]["estimates"][:1]
    evidence["runs"][0]["references"] = evidence["runs"][0]["references"][:1]

    report = evaluate(evidence, input_file_hashes=_hashes())

    assert report["software_evidence_checks_passed"] is False
    assert report["runs"][0]["max_paired_reference_coverage_gap_s"] == pytest.approx(0.9)


def test_unavailable_recorded_sample_is_counted_and_fails_the_software_check():
    evidence = _evidence()
    estimate = evidence["runs"][0]["estimates"][0]
    estimate.pop("position_map_m")
    estimate.update(status="unavailable", reason="tag occluded")

    report = evaluate(evidence, input_file_hashes=_hashes())

    assert report["software_evidence_checks_passed"] is False
    assert report["runs"][0]["sample_counts"]["estimates"]["unavailable"] == 1


@pytest.mark.parametrize(
    "mutation, message",
    [
        (
            lambda evidence: evidence["runs"][0]["estimates"][0].update(clock_id="wrong-clock"),
            "wrong clock",
        ),
        (
            lambda evidence: evidence["runs"][0]["references"][0].update(
                position_map_m=[0, float("nan"), 0]
            ),
            "finite",
        ),
        (
            lambda evidence: evidence["runs"][0]["estimates"].append(
                _sample(
                    "estimate-2",
                    0.1,
                    [0, 0, 0],
                    evidence["runs"][0]["manifest"]["estimator_source_id"],
                )
            ),
            "strictly increasing",
        ),
    ],
)
def test_malformed_measurement_evidence_is_refused(mutation, message):
    evidence = _evidence()
    mutation(evidence)

    with pytest.raises(EvidenceError, match=message):
        evaluate(evidence, input_file_hashes=_hashes())


def test_cli_hash_binds_the_real_input_and_writes_a_failure_report(tmp_path):
    evidence = _evidence(0.251)
    evidence_path = tmp_path / "rehearsals.json"
    output_path = tmp_path / "report.json"
    payload = json.dumps(evidence).encode()
    evidence_path.write_bytes(payload)

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "evals.flight_acceptance",
            str(evidence_path),
            "--output",
            str(output_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    report = json.loads(output_path.read_text())
    assert completed.returncode == 1
    assert report["input_file_hashes"] == {"evidence": hashlib.sha256(payload).hexdigest()}
    assert report["software_evidence_checks_passed"] is False
    assert len(report["report_sha256"]) == 64


def test_cli_refuses_duplicate_json_keys_before_evidence_evaluation(tmp_path):
    evidence_path = tmp_path / "duplicate.json"
    evidence_path.write_text('{"schema_version": 1, "schema_version": 1}')

    completed = subprocess.run(
        [sys.executable, "-m", "evals.flight_acceptance", str(evidence_path)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 1
    assert "duplicate JSON key" in completed.stderr
