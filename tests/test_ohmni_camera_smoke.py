import copy
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import cv2
import numpy as np
import pytest
from tools.ohmni_camera_frames import open_capture
from tools.ohmni_head_capture import record_head

from calibration.intrinsics import calibrate
from perception.camera_tags import CameraTagDetector
from tests.test_calibration import (
    _FISHEYE_CAMERA_MATRIX,
    _FISHEYE_DISTORTION,
    _fisheye_request,
    _write_fisheye_boards,
)
from tests.test_camera_tags import rendered_tag


def test_calibrated_fisheye_artifact_recovers_pose_from_an_independent_print(tmp_path):
    _write_fisheye_boards(tmp_path)
    calibration = calibrate(_fisheye_request(tmp_path))
    _, image, expected = rendered_tag(
        "fisheye", intrinsics=(_FISHEYE_CAMERA_MATRIX, _FISHEYE_DISTORTION)
    )
    detector = CameraTagDetector(
        calibration,
        camera_serial="fixture-fisheye-camera",
        tag_sizes_m={7: 0.3},
        allow_synthetic=True,
    )
    result = detector.detect(image)[0]
    assert result["pose_accepted"], result
    np.testing.assert_allclose(result["T_camera_tag"], expected, atol=0.03)


def test_head_recording_reader_and_detector_preserve_one_source_frame(tmp_path, monkeypatch):
    calibration, image, expected = rendered_tag()

    class Stream:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def read(self, _):
            return cv2.cvtColor(image, cv2.COLOR_GRAY2BGR), time.monotonic()

    monkeypatch.setattr("tools.ohmni_head_capture.WebcamStream", Stream)
    path = tmp_path / "head"
    record_head(
        "rtsp://localhost/ground1",
        path,
        run_id="run",
        device_id="7",
        camera_id="ohmni-test",
        max_bytes=307200,
    )
    detector = CameraTagDetector(
        calibration, camera_serial="ohmni-test", tag_sizes_m={7: 0.3}, allow_synthetic=True
    )
    with open_capture(path) as capture:
        frame = next(capture.frames())
        assert (
            frame.received_monotonic_ns
            == capture.metadata["capture"]["first_received_monotonic_ns"]
        )
        result = detector.detect(np.frombuffer(frame.gray8, np.uint8).reshape(480, 640))[0]
    assert result["pose_accepted"]
    np.testing.assert_allclose(result["T_camera_tag"], expected, atol=0.025)


def _recorded_tag_capture(tmp_path, monkeypatch, image):
    class Stream:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def read(self, _):
            return cv2.cvtColor(image, cv2.COLOR_GRAY2BGR), time.monotonic()

    monkeypatch.setattr("tools.ohmni_head_capture.WebcamStream", Stream)
    capture = tmp_path / "head"
    record_head(
        "rtsp://localhost/ground1",
        capture,
        run_id="smoke-run",
        device_id="7",
        camera_id="ohmni-test",
        max_bytes=307200,
    )
    return capture


def _smoke_config(**changes):
    from tools.ohmni_camera_smoke import SmokeConfig

    values = {
        "session": "ground-session",
        "device_id": 7,
        "connection_epoch": 1,
        "source_id": "ohmni-head",
        "camera_serial": "ohmni-test",
        "camera_frame": "camera",
        "tag_sizes_m": {7: 0.3},
        "allow_synthetic_calibration": True,
    }
    values.update(changes)
    return SmokeConfig(**values)


def test_camera_smoke_configuration_copies_tag_sizes_and_bounds_identifiers():
    sizes = {7: 0.3}
    config = _smoke_config(tag_sizes_m=sizes)
    sizes[7] = 0.4

    assert config.tag_sizes_m[7] == 0.3
    with pytest.raises(TypeError):
        config.tag_sizes_m[7] = 0.4
    for changes in (
        {"session": "x" * 513},
        {"source_id": "bad\nsource"},
        {"camera_serial": "x" * 129},
        {"camera_frame": "world"},
        {"connection_epoch": 2**63},
    ):
        with pytest.raises(ValueError):
            _smoke_config(**changes)


def test_camera_smoke_quaternion_is_stable_at_zero_and_half_turn():
    from tools.ohmni_camera_smoke import _quaternion

    assert _quaternion(np.eye(3)) == pytest.approx((0, 0, 0, 1))
    half_turn = np.diag((-1.0, 1.0, -1.0))
    qx, qy, qz, qw = _quaternion(half_turn)

    assert (qx, qz, qw) == pytest.approx((0, 0, 0), abs=1e-6)
    assert abs(qy) == pytest.approx(1.0)
    with pytest.raises(ValueError, match="rotation"):
        _quaternion(np.diag((2.0, 1.0, 1.0)))


def test_camera_smoke_hashes_the_raw_and_receipt_index_identities():
    from tools.ohmni_camera_smoke import _run_hash

    first = {
        "run_id": "run",
        "recording": {"frames_sha256": "a" * 64, "frame_index_sha256": "b" * 64},
    }
    second = copy.deepcopy(first)
    second["recording"]["frame_index_sha256"] = "c" * 64

    assert _run_hash(first) != _run_hash(second)


@pytest.mark.parametrize("model", ["pinhole", "fisheye"])
def test_camera_smoke_writes_canonical_local_submissions_and_diagnostics(
    tmp_path, monkeypatch, model
):
    from relay.observations import (
        FrameDeclaration,
        FrameRegistry,
        SourceBinding,
        TimingPolicy,
        decode_submission,
        ingest,
    )
    from tools.ohmni_camera_smoke import run_smoke

    calibration, image, _ = rendered_tag(model)
    capture = _recorded_tag_capture(tmp_path, monkeypatch, image)
    calibration_path = tmp_path / "calibration.json"
    calibration_path.write_text(json.dumps(calibration))
    digest = hashlib.sha256(calibration_path.read_bytes()).hexdigest()
    output = tmp_path / "smoke"

    report = run_smoke(capture, calibration_path, digest, output, _smoke_config())

    rows = [
        decode_submission(line)
        for line in (output / "observations.jsonl").read_bytes().splitlines()
    ]
    assert len(rows) == 2
    camera, tag = rows
    assert camera.payload["kind"] == "camera_frame"
    assert camera.t_capture is None
    assert camera.confidence == 0
    assert camera.t_source_receipt.clock_id.startswith("capture-receipt:")
    assert tag.payload["kind"] == "tag_observation"
    assert tag.payload["pose_accepted"] is True
    assert tag.payload["covariance_m2"] is None
    assert tag.payload["reason"] == "pose"
    assert tag.to_mapping().get("t_ingest") is None
    binding = SourceBinding(
        "ground-session",
        7,
        1,
        "ohmni-head",
        "ground",
        ("camera", "tag:7"),
        ("tag_observation",),
    )
    frames = FrameRegistry(
        (
            FrameDeclaration(
                "camera", "camera", "right_down_forward", "m", "ground-session", 7, 1, "ohmni-head"
            ),
            FrameDeclaration(
                "tag:7", "tag", "right_up_outward", "m", "ground-session", 7, 1, "ohmni-head"
            ),
        )
    )
    assert ingest(
        tag, t_ingest=1, frames=frames, binding=binding, mappings={}, timing=TimingPolicy(0)
    )
    assert report["detections"]["decoded_ids"] == [{"id": 7, "count": 1}]
    assert report["detections"]["pose_admission"]["flight_approved"] is False
    assert report["measurements"]["latency"]["status"] == "unavailable"


def test_camera_smoke_keeps_an_unconfigured_decoded_tag_and_fails_closed(tmp_path, monkeypatch):
    from tools.ohmni_camera_smoke import REPORT_RESERVE_BYTES, run_smoke

    calibration, image, _ = rendered_tag()
    capture = _recorded_tag_capture(tmp_path, monkeypatch, image)
    calibration_path = tmp_path / "calibration.json"
    calibration_path.write_text(json.dumps(calibration))
    digest = hashlib.sha256(calibration_path.read_bytes()).hexdigest()
    output = tmp_path / "unconfigured"

    run_smoke(capture, calibration_path, digest, output, _smoke_config(tag_sizes_m={8: 0.3}))
    tag = json.loads((output / "observations.jsonl").read_bytes().splitlines()[1])
    assert tag["payload"]["tag_id"] == 7
    assert tag["payload"]["reason"] == "unconfigured_tag"
    assert tag["payload"]["tag_pose"] is None

    for name, changed in (
        ("hash", (digest[:-1] + ("0" if digest[-1] != "0" else "1"), _smoke_config())),
        ("camera", (digest, _smoke_config(camera_serial="wrong-camera"))),
        ("bytes", (digest, _smoke_config(max_output_bytes=REPORT_RESERVE_BYTES))),
    ):
        failed = tmp_path / name
        with pytest.raises(ValueError):
            run_smoke(capture, calibration_path, changed[0], failed, changed[1])
        assert not failed.exists()

    changed_calibration = copy.deepcopy(calibration)
    changed_calibration["image_size_px"] = [640, 479]
    changed_calibration["pipeline"]["resolution_px"] = [640, 479]
    changed_path = tmp_path / "wrong-resolution.json"
    changed_path.write_text(json.dumps(changed_calibration))
    changed_digest = hashlib.sha256(changed_path.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="resolutions"):
        run_smoke(capture, changed_path, changed_digest, tmp_path / "resolution", _smoke_config())
    assert not (tmp_path / "resolution").exists()


def test_camera_smoke_module_cli_processes_a_verified_recording(tmp_path, monkeypatch):
    calibration, image, _ = rendered_tag()
    capture = _recorded_tag_capture(tmp_path, monkeypatch, image)
    calibration_path = tmp_path / "calibration.json"
    calibration_path.write_text(json.dumps(calibration))
    digest = hashlib.sha256(calibration_path.read_bytes()).hexdigest()
    output = tmp_path / "subprocess-output"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tools.ohmni_camera_smoke",
            "--capture-dir",
            str(capture),
            "--calibration",
            str(calibration_path),
            "--calibration-sha256",
            digest,
            "--output",
            str(output),
            "--session",
            "ground-session",
            "--device-id",
            "7",
            "--connection-epoch",
            "1",
            "--source-id",
            "ohmni-head",
            "--camera-serial",
            "ohmni-test",
            "--camera-frame",
            "camera",
            "--tag-size",
            "7:0.3",
            "--allow-synthetic-calibration",
        ],
        cwd=Path(__file__).parents[1],
        env={"PATH": os.defpath},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["observations"] == 2
    assert (output / "report.json").is_file()
