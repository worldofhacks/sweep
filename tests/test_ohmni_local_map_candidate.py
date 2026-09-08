import hashlib
import importlib
import json
import sys
from pathlib import Path

import pytest

from tools import ohmni_local_map_candidate
from tools.ohmni_local_map_candidate import build

sys.path.insert(0, str(Path(__file__).parent))
fusion_fixture = importlib.import_module("test_ohmni_tag_candidate_fusion")


def _write_candidate_inputs(tmp_path: Path) -> tuple[Path, Path, Path, Path, Path]:
    archive = tmp_path / "archive"
    evidence = tmp_path / "evidence"
    archive.mkdir()
    evidence.mkdir()
    calibration = evidence / "calibration.json"
    mount = evidence / "mount.json"
    calibration.write_text(json.dumps(fusion_fixture._calibration_document()))
    mount.write_text(json.dumps(fusion_fixture._mount_document()))
    calibration_id = f"sha256:{hashlib.sha256(calibration.read_bytes()).hexdigest()}"
    tag_scope = {**fusion_fixture.TAG_SCOPE, "source_id": "ohmni-tag"}
    events = []
    for index in range(2):
        captured = 1_000_000 + index * 100
        image_id = f"image-{index}"
        events.extend(
            (
                fusion_fixture._camera(
                    f"camera-{index}", captured, image_id, calibration_id=calibration_id
                ),
                fusion_fixture._body(f"body-{index}", captured),
                fusion_fixture._event(
                    f"tag-{index}",
                    "camera",
                    captured,
                    {
                        "kind": "tag_observation",
                        "family": "tag36h11",
                        "tag_id": 7,
                        "image_id": image_id,
                        "pose_accepted": True,
                        "tag_pose": {
                            "parent_frame": "camera",
                            "child_frame": "tag:7",
                            "x_m": 1.5,
                            "y_m": 0.0,
                            "z_m": 1.0,
                            "qx": 1.0,
                            "qy": 0.0,
                            "qz": 0.0,
                            "qw": 0.0,
                        },
                        "covariance_m2": [0.01, 0.0, 0.0, 0.0, 0.01, 0.0, 0.0, 0.0, 0.02],
                        "reason": "pose",
                        "size_m": 0.16,
                        "corners_px": [[100, 100], [120, 100], [120, 120], [100, 120]],
                        "pixel_frame": "rectified_camera",
                        "reprojection_rms_px": 0.2,
                    },
                    tag_scope,
                ),
            )
        )
    events.append(
        fusion_fixture._event(
            "scan",
            "lidar",
            1_000_500,
            {
                "kind": "range_scan",
                "mount_id": "lidar-measured",
                "sensor_pose": {
                    "parent_frame": "odom",
                    "child_frame": "lidar",
                    "x_m": 0.0,
                    "y_m": 0.0,
                    "z_m": 0.0,
                    "qx": 0.0,
                    "qy": 0.0,
                    "qz": 0.0,
                    "qw": 1.0,
                },
                "angle_min_rad": 0.0,
                "angle_increment_rad": 1.0,
                "range_min_m": 0.1,
                "range_max_m": 8.0,
                "ranges_m": [2.0],
            },
            {**fusion_fixture.POSE_SCOPE, "source_id": "ohmni-lidar"},
        )
    )
    observations = b"".join(event.encode() + b"\n" for event in events)
    (archive / "observations.jsonl").write_bytes(observations)
    kinds = {
        kind: sum(event.submission.payload["kind"] == kind for event in events)
        for kind in ("camera_frame", "tag_observation", "pose", "range_scan")
    }
    (archive / "manifest.json").write_text(
        json.dumps(
            {
                "format": "ohmni.accepted-mapping-observation-archive.v1",
                "scope": {
                    key: fusion_fixture.POSE_SCOPE[key]
                    for key in ("session", "device_id", "connection_epoch")
                },
                "sources": {
                    "camera": "ohmni-camera",
                    "tag": "ohmni-tag",
                    "pose": "ohmni-pose",
                    "lidar": "ohmni-lidar",
                },
                "frames": {"odom": "odom", "body": "body", "camera": "camera", "lidar": "lidar"},
                "observations": {
                    "path": "observations.jsonl",
                    "count": len(events),
                    "bytes": len(observations),
                    "sha256": hashlib.sha256(observations).hexdigest(),
                    "kinds": kinds,
                    "stop_reason": "input_exhausted",
                },
                "limits": {"max_records": 100, "max_bytes": 100_000, "duration_s": 1.0},
            }
        )
    )
    request = fusion_fixture._local_odom_request()
    request.update(
        {
            "source_scopes": {
                "pose": fusion_fixture.POSE_SCOPE,
                "camera": fusion_fixture.CAMERA_SCOPE,
                "tag": tag_scope,
            },
            "calibration_id": calibration_id,
            "observations": {
                "path": "observations.jsonl",
                "sha256": hashlib.sha256(observations).hexdigest(),
            },
            "calibration": {
                "path": "calibration.json",
                "sha256": hashlib.sha256(calibration.read_bytes()).hexdigest(),
            },
            "mount": {
                "path": "mount.json",
                "sha256": hashlib.sha256(mount.read_bytes()).hexdigest(),
            },
        }
    )
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(request))
    config = tmp_path / "lidar.json"
    config.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "ohmni_local_map_lidar_config",
                "source_scope": {**fusion_fixture.POSE_SCOPE, "source_id": "ohmni-lidar"},
                "odom_frame": "odom",
                "lidar_frame": "lidar",
                "mount_id": "lidar-measured",
                "recording": {"max_records": 10, "max_bytes": 65536, "duration_s": 1.0},
                "grid": {"resolution_m": 0.1, "max_cells": 10000, "bounds": [-1, -1, 3, 1]},
            }
        )
    )
    return archive, config, request_path, evidence, tmp_path / "candidate"


def test_builds_a_local_occupancy_and_tag_candidate_from_one_accepted_snapshot(
    tmp_path: Path,
) -> None:
    archive, config, request, evidence, output = _write_candidate_inputs(tmp_path)

    result = build(archive, config, "lidar-measured", request, evidence, output)

    assert result["approval_status"] == "unapproved"
    assert result["candidate_frame"] == "odom"
    assert result["tag_count"] == 1
    assert (output / "occupancy" / "occupancy.png").is_file()
    assert (output / "inputs" / "lidar-config.json").read_bytes() == config.read_bytes()
    assert (
        result["inputs"]["lidar_config"]["sha256"]
        == hashlib.sha256(config.read_bytes()).hexdigest()
    )
    tags = json.loads((output / "tag_candidates.json").read_text())
    assert tags["candidate_mode"] == "local_odom"
    assert tags["candidates"][0]["T_odom_tag"][0][3] == pytest.approx(1.5)


def test_refuses_a_tampered_archive_before_publishing(tmp_path: Path) -> None:
    archive, config, request, evidence, output = _write_candidate_inputs(tmp_path)
    with (archive / "observations.jsonl").open("ab") as stream:
        stream.write(b" ")

    with pytest.raises(ValueError, match="digest or byte count"):
        build(archive, config, "lidar-measured", request, evidence, output)

    assert not output.exists()


def test_publishes_the_validated_archive_manifest_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive, config, request, evidence, output = _write_candidate_inputs(tmp_path)
    validated = (archive / "manifest.json").read_bytes()
    original = ohmni_local_map_candidate.build_grid

    def render_then_mutate(*args: object, **kwargs: object) -> dict[str, object]:
        result = original(*args, **kwargs)  # type: ignore[arg-type]
        (archive / "manifest.json").write_text('{"changed":true}')
        return result

    monkeypatch.setattr(ohmni_local_map_candidate, "build_grid", render_then_mutate)

    result = build(archive, config, "lidar-measured", request, evidence, output)

    assert (output / "inputs" / "archive-manifest.json").read_bytes() == validated
    assert result["archive"]["manifest"]["sha256"] == hashlib.sha256(validated).hexdigest()


@pytest.mark.parametrize("artifact", ("grid", "tags"))
def test_refuses_generated_artifacts_in_another_frame(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, artifact: str
) -> None:
    archive, config, request, evidence, output = _write_candidate_inputs(tmp_path)
    name = "build_grid" if artifact == "grid" else "run"
    original = getattr(ohmni_local_map_candidate, name)

    def produce_then_change_frame(*args: object, **kwargs: object) -> dict[str, object]:
        result = original(*args, **kwargs)  # type: ignore[arg-type]
        if artifact == "grid":
            return {**result, "grid": {**result["grid"], "frame": "other_odom"}}
        return {**result, "candidate_frame": "other_odom"}

    monkeypatch.setattr(ohmni_local_map_candidate, name, produce_then_change_frame)

    with pytest.raises(ValueError, match="do not match archive odometry frame"):
        build(archive, config, "lidar-measured", request, evidence, output)

    assert not output.exists()


def test_refuses_a_fusion_scope_from_another_epoch(tmp_path: Path) -> None:
    archive, config, request_path, evidence, output = _write_candidate_inputs(tmp_path)
    request = json.loads(request_path.read_text())
    request["source_scopes"]["pose"]["connection_epoch"] += 1
    request_path.write_text(json.dumps(request))

    with pytest.raises(ValueError, match="source scopes"):
        build(archive, config, "lidar-measured", request_path, evidence, output)

    assert not output.exists()
