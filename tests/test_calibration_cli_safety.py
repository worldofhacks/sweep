import json
import os
import sys
from dataclasses import replace

import pytest

from calibration.cli import main
from calibration.intrinsics import calibrate
from calibration.latency import summarize_latency
from tests.test_calibration import _pipeline, _request


@pytest.mark.parametrize(
    "target_name", ["samples.json", "pipeline.json", "image.png", "artifact.yaml"]
)
@pytest.mark.parametrize("alias", ["direct", "symlink", "hardlink"])
def test_cli_preserves_existing_inputs_and_artifacts(tmp_path, monkeypatch, target_name, alias):
    samples = tmp_path / "samples.json"
    samples.write_text(json.dumps({"duration_ms": 60000, "samples_ms": [123]}))
    pipeline = tmp_path / "pipeline.json"
    pipeline.write_text(json.dumps(_pipeline()))
    target = tmp_path / target_name
    if not target.exists():
        target.write_bytes(b"original data")
    before = target.read_bytes()
    output = target
    if alias != "direct":
        output = tmp_path / "alias.yaml"
        if alias == "symlink":
            output.symlink_to(target)
        else:
            os.link(target, output)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "calibration",
            "latency",
            "--samples",
            str(samples),
            "--pipeline",
            str(pipeline),
            "--camera-serial",
            "fixture",
            "--evidence-kind",
            "synthetic",
            "--output",
            str(output),
        ],
    )
    with pytest.raises(SystemExit, match="File exists"):
        main()
    assert target.read_bytes() == before


@pytest.mark.parametrize("corners", [(2, 6), (9, 2), (2, 2)])
def test_two_corner_dimensions_are_rejected_before_image_processing(tmp_path, corners):
    with pytest.raises(ValueError, match="at least three"):
        calibrate(replace(_request(tmp_path), inner_corners=corners))


@pytest.mark.parametrize("corners", ["2x6", "9x2", "2x2"])
def test_cli_reports_small_boards_as_usage_errors(tmp_path, monkeypatch, capsys, corners):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "calibration",
            "intrinsics",
            "--images",
            str(tmp_path),
            "--inner-corners",
            corners,
            "--square-size-m",
            "0.024",
            "--pipeline",
            str(tmp_path / "pipeline.json"),
            "--camera-serial",
            "fixture",
            "--evidence-kind",
            "synthetic",
            "--output",
            str(tmp_path / "out.json"),
        ],
    )
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    assert "at least 3x3" in capsys.readouterr().err


@pytest.mark.parametrize(
    "field,value",
    [
        ("fps", 60),
        ("encoder", "replacement-encoder"),
        ("phone", {"model": "replacement-phone"}),
        ("network_topology", {"path": ["controller", "phone", "router", "computer"]}),
    ],
)
def test_pipeline_changes_survive_artifact_creation(field, value):
    pipeline = _pipeline()
    pipeline[field] = value
    artifact = summarize_latency(
        camera_serial="fixture",
        pipeline=pipeline,
        evidence_kind="synthetic",
        samples={"duration_ms": 60000, "samples_ms": [123]},
    )
    assert artifact["pipeline"] == pipeline
    assert artifact["pipeline"][field] == value
