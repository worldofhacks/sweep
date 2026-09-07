import hashlib
import json
import math
import subprocess
import sys

import pytest

from tools.ohmni_world_registration import (
    MAX_RMS_RESIDUAL_M,
    apply_transform,
    compose_transforms,
    invert_transform,
    planar_transform,
    register_documents,
)


def _digest(label):
    return hashlib.sha256(label.encode()).hexdigest()


def _document(frame, name, tags):
    return {
        "schema_version": 1,
        "frame": frame,
        "provenance": {"name": name, "sha256": _digest(name)},
        "tags": [{"tag_id": tag_id, "xy_m": list(point)} for tag_id, point in tags.items()],
    }


def _valid_documents():
    observed = {4: (0, 0), 18: (2, 0), 42: (0, 3), 99: (2, 3)}
    known = {4: (10, -4), 18: (10, -2), 42: (7, -4), 99: (7, -2)}
    return (
        _document("ohmni_slam", "ohmni-scan", observed),
        _document("world", "tape-measured-tags", known),
    )


def test_known_proper_rigid_transform_is_recovered_from_independent_points():
    observed, known = _valid_documents()

    candidate = register_documents(observed, known)

    assert candidate["kind"] == "ohmni_world_registration_candidate"
    assert candidate["approval_status"] == "unapproved"
    assert candidate["source"] == {"frame": "ohmni_slam", **observed["provenance"]}
    assert candidate["target"] == {"frame": "world", **known["provenance"]}
    assert candidate["T_target_source"] == pytest.approx(
        {"dx_m": 10, "dy_m": -4, "yaw_rad": math.pi / 2}
    )
    assert candidate["fit_tag_ids"] == [4, 18, 42, 99]
    assert candidate["max_residual_m"] == pytest.approx(0)
    assert candidate["rms_residual_m"] == pytest.approx(0)
    assert all(row["residual_m"] == pytest.approx(0) for row in candidate["residuals"])


def test_transform_compose_and_inverse_round_trip_with_independent_expected_values():
    quarter_turn = planar_transform(1, 2, math.pi / 2)
    second_quarter_turn = planar_transform(3, 4, math.pi / 2)

    composed = compose_transforms(quarter_turn, second_quarter_turn)

    assert composed == pytest.approx({"dx_m": -3, "dy_m": 5, "yaw_rad": math.pi})
    assert apply_transform(composed, (2, -1)) == pytest.approx((-5, 6))
    assert apply_transform(invert_transform(composed), (-5, 6)) == pytest.approx((2, -1))


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda observed, known: observed["tags"].__delitem__(slice(-2, None)),
            "at least 3 matched fit tags",
        ),
        (
            lambda observed, known: [
                tag.update(xy_m=[index, 0]) for index, tag in enumerate(observed["tags"])
            ],
            "noncollinear",
        ),
        (
            lambda observed, known: [
                tag.update(xy_m=[index * 100, 0.001 if index == 2 else 0])
                for index, tag in enumerate(observed["tags"])
            ],
            "poorly conditioned",
        ),
    ],
)
def test_insufficient_degenerate_and_poorly_conditioned_fit_tags_are_refused(mutate, message):
    observed, known = _valid_documents()
    mutate(observed, known)

    with pytest.raises(ValueError, match=message):
        register_documents(observed, known)


def test_reflected_ties_are_refused_instead_of_estimating_a_reflection():
    observed, known = _valid_documents()
    known["tags"] = [
        {"tag_id": 4, "xy_m": [0, 0]},
        {"tag_id": 18, "xy_m": [-2, 0]},
        {"tag_id": 42, "xy_m": [0, 3]},
        {"tag_id": 99, "xy_m": [-2, 3]},
    ]

    with pytest.raises(ValueError, match="reflection"):
        register_documents(observed, known)


def test_single_outlier_is_refused_before_a_candidate_is_created():
    observed, known = _valid_documents()
    known["tags"][-1]["xy_m"] = [7, -1]

    with pytest.raises(ValueError, match="outlier tag 99"):
        register_documents(observed, known)


def test_rms_threshold_refuses_a_consistently_inexact_fit():
    observed = _document("ohmni_slam", "scan", {1: (0, 0), 2: (2, 0), 3: (0, 2), 4: (2, 2)})
    known = _document("world", "tape", {1: (0.08, 0), 2: (1.92, 0), 3: (0.08, 2), 4: (1.92, 2)})

    with pytest.raises(ValueError, match=f"RMS residual .*{MAX_RMS_RESIDUAL_M:.3f}"):
        register_documents(observed, known)


def test_held_out_tie_is_not_fitted_and_must_meet_its_own_bound():
    observed, known = _valid_documents()
    known["tags"][-1]["xy_m"] = [7, -1]

    with pytest.raises(ValueError, match="held-out tag 99"):
        register_documents(observed, known, held_out_tag_ids=[99])


def test_duplicate_nonfinite_and_nonindependent_inputs_are_refused():
    observed, known = _valid_documents()
    observed["tags"].append({"tag_id": 4, "xy_m": [1, 1]})
    with pytest.raises(ValueError, match="duplicate observed"):
        register_documents(observed, known)

    observed, known = _valid_documents()
    observed["tags"][0]["xy_m"] = [math.nan, 0]
    with pytest.raises(ValueError, match="finite"):
        register_documents(observed, known)

    observed, known = _valid_documents()
    known["provenance"]["sha256"] = observed["provenance"]["sha256"]
    with pytest.raises(ValueError, match="independent provenance"):
        register_documents(observed, known)


def test_cli_records_input_hashes_and_never_promotes_the_candidate(tmp_path):
    observed, known = _valid_documents()
    observed_path = tmp_path / "observed.json"
    known_path = tmp_path / "known.json"
    output_path = tmp_path / "candidate.json"
    observed_path.write_text(json.dumps(observed))
    known_path.write_text(json.dumps(known))

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tools.ohmni_world_registration",
            str(observed_path),
            str(known_path),
            str(output_path),
            "--held-out-tag-id",
            "99",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout
    candidate = json.loads(output_path.read_text())
    assert candidate["approval_status"] == "unapproved"
    assert candidate["fit_tag_ids"] == [4, 18, 42]
    assert candidate["held_out_tag_ids"] == [99]
    assert candidate["input_provenance"] == {
        "observed_document_sha256": hashlib.sha256(observed_path.read_bytes()).hexdigest(),
        "known_document_sha256": hashlib.sha256(known_path.read_bytes()).hexdigest(),
    }
