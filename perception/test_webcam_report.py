import copy

import pytest

from perception.webcam_report import build_report


def evidence():
    rows = [
        {
            "timestamp": 100 + index / 10,
            "run_elapsed_s": index / 10,
            "map_sha256": "a" * 64,
            "calibration_sha256": "b" * 64,
            "latency_sha256": "c" * 64,
            "bundle_version": "room-map-v1",
            "camera_serial": "camera-1",
            "stream_path": "drone1",
            "timing_provenance": "decode_monotonic_minus_measured_p50",
            "capture_time_verified": False,
            "publisher_identity_verified": False,
            "confidence": "green",
            "accepted": True,
            "position_map_m": [index / 10, 0, 1],
            "fix_age_s": 0.02,
            "fix_age_with_p95_tail_s": 0.04,
            "synthetic": False,
            "flight_approved": False,
            "control_eligible": False,
            "spacing_certified": False,
        }
        for index in range(11)
    ]
    checkpoints = {
        "map_sha256": "a" * 64,
        "evidence_kind": "recorded_live",
        "independent_survey": True,
        "checkpoints": [
            {"id": str(index), "run_elapsed_s": index / 10, "position_map_m": [index / 10, 0, 1]}
            for index in range(1, 7)
        ],
    }
    return rows, checkpoints


def test_survey_checks_preserve_physical_acceptance_boundary():
    report = build_report(*evidence())
    assert report["software_checks_passed"]
    assert not report["flight_approved"]
    assert report["physical_acceptance"] == "pending_independent_verification"
    assert report["max_localization_gap_s"] == pytest.approx(0.1)


def test_missing_heartbeats_cannot_hide_localization_gap():
    rows, checkpoints = evidence()
    report = build_report(rows[:2] + rows[8:], checkpoints)
    assert report["max_localization_gap_s"] >= 0.7
    assert not report["software_checks_passed"]


@pytest.mark.parametrize("startup", [True, False])
def test_startup_and_trailing_time_count_toward_gap(startup):
    rows, checkpoints = evidence()
    for row in rows:
        missing = row["run_elapsed_s"] < 0.8 if startup else row["run_elapsed_s"] > 0.1
        if missing:
            row.update(accepted=False, confidence="red", position_map_m=None)
            row["fix_age_s"] = None if startup else row["run_elapsed_s"] - 0.1
            row["fix_age_with_p95_tail_s"] = row["fix_age_s"]
    report = build_report(rows, checkpoints)
    assert not report["coverage_within_500ms"]


def test_conservative_capture_age_counts_even_with_regular_observations():
    rows, checkpoints = evidence()
    for row in rows[2:]:
        row.update(accepted=False, confidence="amber", fix_age_s=0.7, fix_age_with_p95_tail_s=0.8)
    assert not build_report(rows, checkpoints)["coverage_within_500ms"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("map_sha256", "d" * 64),
        ("timestamp", float("nan")),
        ("run_elapsed_s", -1),
        ("flight_approved", True),
        ("spacing_certified", True),
        ("position_map_m", [0, float("inf"), 0]),
        ("fix_age_s", 10**400),
        ("capture_time_verified", True),
        ("publisher_identity_verified", True),
        ("stream_path", "drone2"),
    ],
)
def test_invalid_observation_fails_closed(field, value):
    rows, checkpoints = evidence()
    rows[3][field] = value
    with pytest.raises(ValueError):
        build_report(rows, checkpoints)


def test_unordered_rows_reject():
    rows, checkpoints = evidence()
    rows[2], rows[3] = rows[3], rows[2]
    with pytest.raises(ValueError):
        build_report(rows, checkpoints)


@pytest.mark.parametrize(
    "field,value",
    [
        ("independent_survey", False),
        ("evidence_kind", "synthetic"),
        ("map_sha256", "f" * 64),
        ("checkpoints", []),
    ],
)
def test_invalid_survey_rejects(field, value):
    rows, checkpoints = evidence()
    checkpoints[field] = value
    with pytest.raises(ValueError):
        build_report(rows, checkpoints)


def test_checkpoint_error_uses_independent_position():
    rows, checkpoints = evidence()
    checkpoints["checkpoints"][0]["position_map_m"][1] = 0.11
    report = build_report(rows, checkpoints)
    assert report["checkpoints"][0]["error_m"] == pytest.approx(0.11)
    assert not report["heldout_checkpoint_check_passed"]


def test_fewer_than_six_checkpoints_cannot_pass():
    rows, checkpoints = evidence()
    checkpoints["checkpoints"].pop()
    assert not build_report(rows, checkpoints)["heldout_checkpoint_check_passed"]


@pytest.mark.parametrize("duplicate_field", ["run_elapsed_s", "position_map_m"])
def test_heldout_checkpoints_must_be_distinct(duplicate_field):
    rows, checkpoints = evidence()
    checkpoints["checkpoints"][1][duplicate_field] = checkpoints["checkpoints"][0][duplicate_field]
    with pytest.raises(ValueError, match="distinct"):
        build_report(rows, checkpoints)


def test_one_observation_cannot_satisfy_multiple_checkpoints():
    rows, checkpoints = evidence()
    checkpoints["checkpoints"][0].update(run_elapsed_s=0.09, position_map_m=[0.1, 0, 1])
    checkpoints["checkpoints"][1].update(run_elapsed_s=0.11, position_map_m=[0.11, 0, 1])
    report = build_report(rows, checkpoints)
    assert report["checkpoints"][0]["error_m"] is not None
    assert report["checkpoints"][1]["error_m"] is None
    assert report["heldout_checkpoint_check_passed"] is False


def test_synthetic_observations_never_claim_physical_acceptance():
    rows, checkpoints = evidence()
    rows = copy.deepcopy(rows)
    rows[0]["synthetic"] = True
    report = build_report(rows, checkpoints)
    assert report["synthetic"]
    assert report["physical_acceptance"] == "pending_independent_verification"


def test_empty_observations_reject():
    with pytest.raises(ValueError):
        build_report([], evidence()[1])


def test_short_recording_without_any_fix_cannot_claim_coverage():
    rows, checkpoints = evidence()
    rows = rows[:3]
    for row in rows:
        row.update(
            accepted=False,
            confidence="red",
            position_map_m=None,
            fix_age_s=None,
            fix_age_with_p95_tail_s=None,
        )
    checkpoints["checkpoints"] = checkpoints["checkpoints"][:2]
    report = build_report(rows, checkpoints)
    assert report["coverage_within_500ms"] is False
    assert report["software_checks_passed"] is False
