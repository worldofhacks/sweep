import copy
import json
import os
import sys
from pathlib import Path

import pytest

from tools import tag_pose_extract
from tools.map_common import read_document


@pytest.fixture
def survey():
    document = read_document(Path(__file__).parent / "fixtures/mapping/survey.json")
    document["source"] = {"path": "scene.ply", "sha256": "a" * 64}
    return document


@pytest.mark.parametrize(
    "rotation",
    [
        [[0, -1, 0, 0], [1, 0, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]],
        [[1, 0, 0, 0], [0, -1, 0, 0], [0, 0, -1, 0], [0, 0, 0, 1]],
    ],
)
def test_tag_zero_origin_cannot_hide_wrong_yaw_or_downward_normal(survey, rotation):
    survey["T_map_scan"] = rotation
    with pytest.raises(ValueError, match="Tag 0.*yaw zero"):
        tag_pose_extract.extract_tags(survey)


@pytest.mark.parametrize(
    "source",
    [
        None,
        {},
        {"path": "scene.ply"},
        {"path": "../scene.ply", "sha256": "a" * 64},
        {"path": "scene.ply", "sha256": "wrong"},
    ],
)
def test_extraction_cannot_drop_missing_or_malformed_source_provenance(survey, source):
    survey["source"] = source
    with pytest.raises(ValueError, match="source"):
        tag_pose_extract.extract_tags(survey)


def test_extraction_carries_scan_pose_and_exact_registration_for_independent_validation(survey):
    extracted = tag_pose_extract.extract_tags(survey)
    assert extracted["source"] == survey["source"]
    assert extracted["T_map_scan"] == survey["T_map_scan"]
    assert extracted["tags"][1]["T_scan_tag"] == extracted["tags"][1]["T_map_tag"]
    extracted["source"]["sha256"] = "b" * 64
    assert survey["source"]["sha256"] == "a" * 64


@pytest.mark.parametrize("pair", ["survey-output", "survey-preview", "output-preview"])
@pytest.mark.parametrize("alias", ["same", "symlink", "hardlink"])
def test_aliasing_destinations_never_overwrite_survey_or_other_output(
    tmp_path, monkeypatch, survey, pair, alias
):
    source = tmp_path / "survey.json"
    output = tmp_path / "tags.json"
    preview = tmp_path / "preview.html"
    source.write_text(json.dumps(survey))
    output.write_text("existing tags")
    preview.write_text("existing preview")
    paths = {"survey": source, "output": output, "preview": preview}
    left, right = pair.split("-")
    if alias == "same":
        paths[right] = paths[left]
    else:
        paths[right].unlink()
        if alias == "symlink":
            paths[right].symlink_to(paths[left])
        else:
            os.link(paths[left], paths[right])
    originals = {path: path.read_bytes() for path in paths.values()}
    monkeypatch.setattr(
        sys,
        "argv",
        ["extract", str(paths["survey"]), str(paths["output"]), "--preview", str(paths["preview"])],
    )
    assert tag_pose_extract.main() == 1
    for path, data in originals.items():
        assert path.read_bytes() == data


def test_preview_write_failure_preserves_both_existing_outputs_and_cleans_staging(
    tmp_path, monkeypatch, survey
):
    source = tmp_path / "survey.json"
    output = tmp_path / "tags.json"
    preview = tmp_path / "preview.html"
    source.write_text(json.dumps(survey))
    output.write_text("existing tags")
    preview.write_text("existing preview")

    def fail_preview(path, document):
        Path(path).write_text("partial")
        raise OSError("disk write failed")

    monkeypatch.setattr(tag_pose_extract, "write_preview", fail_preview)
    monkeypatch.setattr(
        sys, "argv", ["extract", str(source), str(output), "--preview", str(preview)]
    )
    assert tag_pose_extract.main() == 1
    assert output.read_text() == "existing tags"
    assert preview.read_text() == "existing preview"
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "preview.html",
        "survey.json",
        "tags.json",
    ]


def test_distinct_existing_outputs_can_be_intentionally_replaced(tmp_path, monkeypatch, survey):
    source = tmp_path / "survey.json"
    output = tmp_path / "tags.json"
    preview = tmp_path / "preview.html"
    source.write_text(json.dumps(survey))
    output.write_text("old")
    preview.write_text("old")
    monkeypatch.setattr(
        sys, "argv", ["extract", str(source), str(output), "--preview", str(preview)]
    )
    assert tag_pose_extract.main() == 0
    assert json.loads(output.read_text())["tags"][0]["yaw"] == 0
    assert "Survey tag axes" in preview.read_text()
    assert json.loads(source.read_text()) == survey


def test_malformed_missing_tag_fields_have_structured_cli_errors(
    tmp_path, monkeypatch, survey, capsys
):
    malformed = copy.deepcopy(survey)
    del malformed["tags"][0]["size"]
    source = tmp_path / "survey.json"
    source.write_text(json.dumps(malformed))
    monkeypatch.setattr(sys, "argv", ["extract", str(source), str(tmp_path / "tags.json")])
    assert tag_pose_extract.main() == 1
    assert json.loads(capsys.readouterr().out)["valid"] is False


@pytest.mark.parametrize("existing", [True, False])
def test_second_replace_failure_restores_first_output_and_removes_temporary_files(
    tmp_path, monkeypatch, survey, existing
):
    source = tmp_path / "survey.json"
    output = tmp_path / "tags.json"
    preview = tmp_path / "preview.html"
    source.write_text(json.dumps(survey))
    if existing:
        output.write_text("existing tags")
        preview.write_text("existing preview")
    replace = os.replace

    def fail_second(source_path, destination):
        if destination == preview:
            raise OSError("second replacement failed")
        replace(source_path, destination)

    monkeypatch.setattr(os, "replace", fail_second)
    monkeypatch.setattr(
        sys, "argv", ["extract", str(source), str(output), "--preview", str(preview)]
    )
    assert tag_pose_extract.main() == 1
    if existing:
        assert output.read_text() == "existing tags"
        assert preview.read_text() == "existing preview"
        assert len(list(tmp_path.iterdir())) == 3
    else:
        assert not output.exists()
        assert not preview.exists()
        assert list(tmp_path.iterdir()) == [source]


def test_absent_case_alias_outputs_cannot_overwrite_json_on_case_insensitive_disk(
    tmp_path, monkeypatch, survey
):
    source = tmp_path / "survey.json"
    output = tmp_path / "tags.json"
    preview = tmp_path / "TAGS.JSON"
    source.write_text(json.dumps(survey))
    replace = os.replace

    def case_insensitive_replace(src, dst):
        destination = Path(dst)
        if destination.name.casefold() == "tags.json":
            destination = output
        return replace(src, destination)

    monkeypatch.setattr(tag_pose_extract.os, "replace", case_insensitive_replace)
    monkeypatch.setattr(
        sys, "argv", ["extract", str(source), str(output), "--preview", str(preview)]
    )
    assert not output.exists() and not preview.exists()
    assert tag_pose_extract.main() == 1
    assert not output.exists() and not preview.exists()
    assert json.loads(source.read_text()) == survey
