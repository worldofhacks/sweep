import hashlib
import json

import pytest

from adapters.ohmni.calibration import CalibrationError
from tests.test_ohmni_camera_inspection import _producer, _state
from tests.test_ohmni_camera_positioning import (
    _half_response_sleep,
    _simulated_capture_runner,
)
from tests.test_ohmni_camera_positioning import capture as positioning
from tools import ohmni_camera_inspection as inspection


@pytest.mark.parametrize("fault", [None, "unknown_scan", "head", "source", "lease"])
def test_capture_producer_review_request_and_owner_enforce_live_admission(
    tmp_path, monkeypatch, fault
):
    runner, simulation = _simulated_capture_runner(
        monkeypatch, tmp_path, sleep_factory=_half_response_sleep
    )
    runner.mode = "inspected-forward"
    runner.boot_id = "boot-1"
    runner.executed_bundle_source_sha256 = positioning.camera_positioning_source_sha256()
    original_sleep = runner.sleep
    original_drive = simulation.device.calibration_drive_velocity
    pulses = []
    submitted = False

    def drive(*args, **kwargs):
        pulses.append(args)
        return original_drive(*args, **kwargs)

    def sleep(delay):
        nonlocal submitted
        original_sleep(delay)
        challenge_path = tmp_path / "capture.json.inspection-challenge.json"
        if submitted or not challenge_path.exists():
            return
        challenge = inspection.parse_challenge(json.loads(challenge_path.read_bytes()))
        output = _producer(tmp_path, monkeypatch, challenge)
        inspection.main(
            [
                "--challenge",
                str(challenge_path),
                "--frame-record",
                str(output / "main" / "frame-000000.json"),
                "--manifest",
                str(output / "manifest.json"),
                "--operator-id",
                "operator-1",
                "--review-notes",
                "Forward view is clear",
                "--accept",
            ]
        )
        submitted = True
        if fault == "unknown_scan":
            simulation.device.lidar.scan.ranges_cm[:] = [0] * 360
        elif fault == "head":
            monkeypatch.setattr(
                positioning,
                "_neck_status",
                lambda: {"position": -46000, "target": -46000, "flags": "NONE"},
            )
        elif fault == "source":
            runner.executed_bundle_source_sha256 = "b" * 64
        elif fault == "lease":
            monkeypatch.setattr(simulation.lease, "reason", lambda now: "host_lease_expired")

    simulation.device.calibration_drive_velocity = drive
    runner.sleep = sleep
    if fault is None:
        runner.run()
        result = json.loads(runner.output.read_bytes())
        assert len(pulses) == 1
        assert pulses[0] == (0.04, 0.0, 0.5)
        assert 0 < result["motion"]["measured_distance_m"] < 0.02
        assert result["inspection_approval"]["consumed"] is True
        assert result["stages"]["after_inspected_forward"]["revolutions"]
    else:
        with pytest.raises((CalibrationError, inspection.InspectionError, RuntimeError)):
            runner.run()
        assert not runner.output.exists()
        assert runner.output.with_name(runner.output.name + ".failed.json").is_file()
        assert simulation.started_moving is None
    assert submitted
    assert simulation.device.motion is None
    assert not simulation.device.enabled


@pytest.mark.parametrize("fault", ["capture_bundle", "camera_pipeline"])
def test_self_consistent_files_require_the_capture_bundle_and_camera_metadata(
    tmp_path, monkeypatch, fault
):
    authority = inspection.InspectionAuthority()
    challenge = authority.issue_challenge(
        _state(), 1_000, inspection.capture_bundle_source_sha256()
    )
    output = _producer(tmp_path, monkeypatch, challenge)
    manifest_path = output / "manifest.json"
    frame_path = output / "main" / "frame-000000.json"
    manifest = json.loads(manifest_path.read_bytes())
    frame = json.loads(frame_path.read_bytes())
    if fault == "capture_bundle":
        manifest["capture_tool_sha256"] = "0" * 64
    else:
        manifest["capture_pipeline"] = {"camera": "main"}
        manifest["cameras"] = manifest["capture_pipeline"]
        pipeline_hash = hashlib.sha256(
            json.dumps(manifest["capture_pipeline"], sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        manifest["capture_pipeline_sha256"] = frame["capture_pipeline_sha256"] = pipeline_hash
    manifest_path.write_text(json.dumps(manifest))
    frame_path.write_text(json.dumps(frame))
    with pytest.raises(inspection.InspectionError, match="capture bundle|camera pipeline"):
        inspection.FrameEvidence.load(frame_path, manifest_path)
