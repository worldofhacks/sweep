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


@pytest.mark.parametrize("failure", ["short_write", "write_error", "sync_error"])
def test_failed_artifact_write_leaves_no_final_file_and_allows_retry(
    tmp_path, monkeypatch, failure
):
    from calibration import cli

    output = tmp_path / "artifact.json"
    artifact = {"camera_serial": "fixture", "measurements": list(range(20))}
    original_temporary = cli.tempfile.NamedTemporaryFile

    def failing_temporary(*args, **kwargs):
        stream = original_temporary(*args, **kwargs)
        original_write = stream.write

        def partial_write(content):
            original_write(content[:17])
            if failure == "write_error":
                raise OSError("simulated write failure")
            return 17

        stream.write = partial_write
        return stream

    def fail_sync(fd):
        raise OSError("simulated sync failure")

    with monkeypatch.context() as patch:
        if failure == "sync_error":
            patch.setattr(cli.os, "fsync", fail_sync)
        else:
            patch.setattr(cli.tempfile, "NamedTemporaryFile", failing_temporary)
        with pytest.raises(OSError):
            cli._write_artifact(output, artifact)
    assert not output.exists()
    assert list(tmp_path.iterdir()) == []
    cli._write_artifact(output, artifact)
    assert json.loads(output.read_text()) == artifact
    assert list(tmp_path.iterdir()) == [output]


def test_destination_created_during_write_is_preserved(tmp_path, monkeypatch):
    from calibration import cli

    output = tmp_path / "artifact.json"
    original_link = cli.os.link

    def competing_link(source, destination):
        assert not output.exists()
        assert json.loads(source.read_text()) == {"camera_serial": "fixture"}
        output.write_text("other writer")
        original_link(source, destination)

    monkeypatch.setattr(cli.os, "link", competing_link)
    with pytest.raises(FileExistsError):
        cli._write_artifact(output, {"camera_serial": "fixture"})
    assert output.read_text() == "other writer"
    assert list(tmp_path.iterdir()) == [output]


@pytest.mark.parametrize(
    "operation", ["findChessboardCornersSB", "findHomography", "calibrateCameraExtended"]
)
def test_opencv_failures_are_normalized_by_cli(tmp_path, monkeypatch, operation):
    import cv2

    from tests.test_calibration import _write_varied_boards

    _write_varied_boards(tmp_path)
    pipeline = tmp_path / "pipeline.json"
    pipeline.write_text(json.dumps(_pipeline()))
    output = tmp_path / "artifact.json"

    def fail(*args, **kwargs):
        raise cv2.error("simulated OpenCV failure")

    monkeypatch.setattr(cv2, operation, fail)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "calibration",
            "intrinsics",
            "--images",
            str(tmp_path),
            "--inner-corners",
            "9x6",
            "--square-size-m",
            "0.024",
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
    with pytest.raises(SystemExit, match="error: simulated OpenCV failure"):
        main()
    assert not output.exists()
