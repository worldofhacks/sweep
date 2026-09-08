import hashlib
import json
import subprocess
from pathlib import Path

import numpy as np
import pytest

from tools import ohmni_camera_inspection as inspection
from tools import ohmni_dual_calibration_capture as capture


def _state(**changes):
    values = dict(
        device_id=12,
        boot_id="boot-1",
        positioning_source_sha256="a" * 64,
        x_m=1.0,
        y_m=2.0,
        yaw_deg=3.0,
        pose_quality=0.6,
        head_position=-47000,
        head_target=-47000,
        head_flags="NONE",
    )
    values.update(changes)
    return inspection.LiveState(**values)


def _producer(tmp_path, monkeypatch, challenge):
    main = np.zeros(capture.MAIN_SHAPE, dtype=np.uint8).tobytes()

    def check_output(command, **_kwargs):
        if "boot_id" in command[-1]:
            return "boot-1\n"
        return (
            "usb_parent=/sys/devices/usb/video0\n"
            "id_vendor=2560\n"
            "id_product=c1d1\nname=See3CAM_CU135\nFormat Video Capture:\n"
            "\tWidth/Height      : 1280/720\n\tPixel Format      : 'UYVY'\n"
        )

    def run(command, **_kwargs):
        if "pull" in command:
            Path(command[-1]).write_bytes(main)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(capture.subprocess, "check_output", check_output)
    monkeypatch.setattr(capture.subprocess, "run", run)
    monkeypatch.setattr(
        capture, "_remote_raw_fingerprint", lambda *_: (len(main), hashlib.sha256(main).hexdigest())
    )
    monkeypatch.setattr(capture.time, "monotonic_ns", lambda: 1)
    output = tmp_path / "capture"
    capture.run(
        "adb-serial",
        output,
        expected_boot_id="boot-1",
        count=1,
        duration_s=1,
        camera="main",
        inspection_challenge=challenge.to_mapping(),
        device_id=12,
    )
    return output


def _approval(tmp_path, monkeypatch):
    state = _state()
    authority = inspection.InspectionAuthority()
    challenge = authority.issue_challenge(state, 1_000, capture._capture_tool_sha256())
    output = _producer(tmp_path, monkeypatch, challenge)
    evidence = inspection.FrameEvidence.load(
        output / "main" / "frame-000000.json", output / "manifest.json"
    )
    pulse = inspection.ForwardPulse(0.04, 0.0, 0.5)
    approval = authority.approve(
        evidence,
        pulse,
        state,
        1_100,
        operator_id="spotter-a",
        accepted=True,
        review_notes="path clear in inspected capsule",
    )
    return authority, approval, pulse, state, output


def test_actual_capture_producer_retains_challenge_and_authorizes_one_pulse(tmp_path, monkeypatch):
    authority, approval, pulse, state, output = _approval(tmp_path, monkeypatch)
    frame = json.loads((output / "main" / "frame-000000.json").read_text())
    manifest = json.loads((output / "manifest.json").read_text())
    assert frame["inspection_challenge"] == manifest["inspection_challenge"]
    assert authority.consume(approval, pulse, state, 1_200) is None
    assert authority.approval_record(approval)["review_notes"] == "path clear in inspected capsule"
    assert authority.consume(approval, pulse, state, 1_200) == "camera_inspection_approval_spent"


def test_frame_hash_change_refuses_before_operator_approval(tmp_path, monkeypatch):
    state = _state()
    authority = inspection.InspectionAuthority()
    challenge = authority.issue_challenge(state, 1_000, capture._capture_tool_sha256())
    output = _producer(tmp_path, monkeypatch, challenge)
    path = output / "main" / "frame-000000.json"
    record = json.loads(path.read_text())
    record["image_sha256"] = "0" * 64
    path.write_text(json.dumps(record))
    with pytest.raises(inspection.InspectionError, match="frame bytes differ"):
        inspection.FrameEvidence.load(path, output / "manifest.json")


@pytest.mark.parametrize(
    "change",
    [
        {"device_id": 13},
        {"boot_id": "later-boot"},
        {"positioning_source_sha256": "b" * 64},
        {"x_m": 1.01},
        {"head_target": -46900},
    ],
)
def test_live_identity_pose_or_head_change_refuses_consumption(tmp_path, monkeypatch, change):
    authority, approval, pulse, state, _ = _approval(tmp_path, monkeypatch)
    changed = _state(**change)
    assert (
        authority.consume(approval, pulse, changed, 1_200) == "camera_inspection_live_state_changed"
    )


def test_stale_challenge_and_oversized_or_reversed_pulses_refuse(tmp_path, monkeypatch):
    state = _state()
    authority = inspection.InspectionAuthority()
    challenge = authority.issue_challenge(state, 1_000, capture._capture_tool_sha256(), ttl_ns=1)
    output = _producer(tmp_path, monkeypatch, challenge)
    evidence = inspection.FrameEvidence.load(
        output / "main" / "frame-000000.json", output / "manifest.json"
    )
    with pytest.raises(inspection.InspectionError, match="challenge expired"):
        authority.approve(
            evidence,
            inspection.ForwardPulse(0.04, 0, 0.5),
            state,
            1_002,
            operator_id="spotter-a",
            accepted=True,
            review_notes="clear",
        )
    for values in ((0.05, 0, 0.5), (0.04, 0, 0.51), (-0.04, 0, 0.5), (0.04, 1, 0.5)):
        with pytest.raises(inspection.InspectionError, match="pulse exceeds"):
            inspection.ForwardPulse(*values)


def test_challenge_cannot_rebind_a_moved_body_or_authorize_twice(tmp_path, monkeypatch):
    state = _state()
    authority = inspection.InspectionAuthority()
    challenge = authority.issue_challenge(state, 1_000, capture._capture_tool_sha256())
    output = _producer(tmp_path, monkeypatch, challenge)
    evidence = inspection.FrameEvidence.load(
        output / "main" / "frame-000000.json", output / "manifest.json"
    )
    pulse = inspection.ForwardPulse(0.04, 0, 0.5)
    with pytest.raises(inspection.InspectionError, match="live identity differs"):
        authority.approve(
            evidence,
            pulse,
            _state(x_m=1.01),
            1_100,
            operator_id="spotter-a",
            accepted=True,
            review_notes="clear",
        )
    authority.approve(
        evidence, pulse, state, 1_100, operator_id="spotter-a", accepted=True, review_notes="clear"
    )
    with pytest.raises(inspection.InspectionError, match="challenge expired or unknown"):
        authority.approve(
            evidence,
            pulse,
            state,
            1_100,
            operator_id="spotter-a",
            accepted=True,
            review_notes="clear",
        )


def test_incomplete_manifest_refuses_capture_evidence(tmp_path, monkeypatch):
    _, _, _, _, output = _approval(tmp_path, monkeypatch)
    manifest_path = output / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["status"] = "failed"
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(inspection.InspectionError, match="manifest challenge differs"):
        inspection.FrameEvidence.load(output / "main" / "frame-000000.json", manifest_path)
