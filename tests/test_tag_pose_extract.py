import copy
import json
import math
import subprocess
import sys
from pathlib import Path

import pytest

from tools.map_common import read_document
from tools.tag_pose_extract import extract_tags, write_preview

FIXTURE = Path(__file__).parent / "fixtures" / "mapping"


@pytest.fixture
def survey():
    return read_document(FIXTURE / "survey.json")


def test_three_independent_known_centers_and_full_axes(survey):
    expected = read_document(FIXTURE / "expected_tags.json")
    tags = extract_tags(survey)["tags"]
    for i, tag in enumerate(tags):
        assert [tag["x"], tag["y"], tag["z"]] == pytest.approx(expected["centers"][i])
        assert tag["normal"] == pytest.approx(expected["normals"][i])
        assert [r[0] for r in tag["T_map_tag"][:3]] == pytest.approx(expected["right_axes"][i])
    assert tags[2]["yaw"] == pytest.approx(math.pi / 2)


def test_registration_rotates_scan_back_to_tag_zero_building_axes(survey):
    for tag in survey["tags"]:
        tag["corners"] = [[point[1] + 10, -point[0], point[2]] for point in tag["corners"]]
        x, y, z = tag["front_normal_scan"]
        tag["front_normal_scan"] = [y, -x, z]
    survey["T_map_scan"] = [[0, -1, 0, 0], [1, 0, 0, -10], [0, 0, 1, 0], [0, 0, 0, 1]]
    result = extract_tags(survey)
    assert result["tags"][0]["yaw"] == pytest.approx(0)
    assert [result["tags"][1][k] for k in ("x", "y", "z")] == pytest.approx([2, 1, 0.5])
    assert result["T_map_scan"] == survey["T_map_scan"]
    assert result["source"] == survey["source"]
    scan_pose = result["tags"][1]["T_scan_tag"]
    assert [row[3] for row in scan_pose[:3]] == pytest.approx([11, -2, 0.5])


@pytest.mark.parametrize(
    "kind",
    [
        "duplicate",
        "nan",
        "infinite",
        "bowtie",
        "reverse",
        "size",
        "missing",
        "confirmation",
        "collinear",
        "nonplanar",
        "floor",
    ],
)
def test_invalid_corner_inputs_are_refused(survey, kind):
    tag = survey["tags"][0]
    if kind == "duplicate":
        survey["tags"].append(copy.deepcopy(tag))
    elif kind in ("nan", "infinite"):
        tag["corners"][0][0] = float("nan" if kind == "nan" else "inf")
    elif kind == "bowtie":
        tag["corners"][1], tag["corners"][2] = tag["corners"][2], tag["corners"][1]
    elif kind == "reverse":
        tag["corners"] = list(reversed(tag["corners"]))
    elif kind == "size":
        tag["size"] = 0.3
    elif kind == "missing":
        del tag["corners"]
    elif kind == "confirmation":
        tag["orientation_confirmed"] = False
    elif kind == "collinear":
        tag["corners"] = [[i, 0, 0] for i in range(4)]
    elif kind == "nonplanar":
        tag["corners"][0][2] = 0.05
    elif kind == "floor":
        del tag["floor_id"]
    with pytest.raises(ValueError):
        extract_tags(survey)


@pytest.mark.parametrize(
    "transform",
    [
        [[-1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]],
        [[2, 0, 0, 0], [0, 2, 0, 0], [0, 0, 2, 0], [0, 0, 0, 1]],
        [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [1, 0, 0, 1]],
        [[1, 0, 0, 1], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]],
    ],
)
def test_bad_registration_and_nonzero_origin_are_refused(survey, transform):
    survey["T_map_scan"] = transform
    with pytest.raises(ValueError):
        extract_tags(survey)


def test_preview_contains_every_tag_and_orientation(survey, tmp_path):
    path = tmp_path / "preview.html"
    write_preview(path, extract_tags(survey))
    text = path.read_text()
    assert "Elevation: X / Z" in text
    assert "2 (mezzanine)" in text
    assert text.count("<path d=") == 18


def test_extractor_cli_produces_validator_input(tmp_path):
    from shutil import copytree

    from tools.map_validate import seal_manifest, validate_bundle

    bundle = tmp_path / "bundle"
    copytree(FIXTURE, bundle)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tools.tag_pose_extract",
            str(bundle / "survey.json"),
            str(bundle / "tags.yaml"),
            "--preview",
            str(bundle / "preview.html"),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["tags"] == 3
    manifest = seal_manifest(bundle)
    accepted = {manifest["bundle_version"]: manifest["content_sha256"]}
    assert validate_bundle(bundle, accepted)["bundle_version"] == ("synthetic-three-tags-v1")


def test_tilted_tag_preserves_full_orientation(survey):
    q = math.sqrt(0.5)
    tag = survey["tags"][1]
    tag["corners"] = [
        [2 + sx * 0.12 * q, 1 + sy * 0.12, 0.5 + sx * 0.12 * q]
        for sx, sy in [(-1, 1), (1, 1), (1, -1), (-1, -1)]
    ]
    tag["front_normal_scan"] = [-q, 0, q]
    result = extract_tags(survey)["tags"][1]
    assert result["normal"] == pytest.approx([-q, 0, q])
    assert [r[0] for r in result["T_map_tag"][:3]] == pytest.approx([q, 0, q])
    assert [r[1] for r in result["T_map_tag"][:3]] == pytest.approx([0, 1, 0])
    assert [result[k] for k in ("x", "y", "z")] == pytest.approx([2, 1, 0.5])


def test_survey_boolean_schema_is_invalid(survey):
    survey["schema_version"] = True
    with pytest.raises(ValueError):
        extract_tags(survey)
