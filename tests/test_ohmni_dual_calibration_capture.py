import hashlib
import json
import shlex
import subprocess
from pathlib import Path

import cv2
import numpy as np
import pytest

from tools import ohmni_dual_calibration_capture as capture


def _probe(
    node: str,
    *,
    pixel_format: str | None = None,
    vendor_id: str | None = None,
) -> str:
    _, name, _, device_name, expected_format, shape, vendor, product = next(
        item for item in capture.CAMERAS if item[0] == node
    )
    pixel_format = expected_format if pixel_format is None else pixel_format
    return (
        f"usb_parent=/sys/devices/usb/{node}\n"
        f"id_vendor={vendor if vendor_id is None else vendor_id}\n"
        f"id_product={product}\n"
        f"name={device_name}\n"
        "Format Video Capture:\n"
        f"\tWidth/Height      : {shape[0]}/{shape[1]}\n"
        f"\tPixel Format      : '{pixel_format}'\n"
    )


def _check_output(command, **_kwargs):
    if "boot_id" in command[-1]:
        return "boot-1\n"
    return _probe("video0" if "video0" in command[-1] else "video1")


def _fingerprint(main: bytes, lower: bytes):
    def fingerprint(_serial, remote, _deadline_ns):
        content = main if remote.endswith(".uyvy") else lower
        return len(content), hashlib.sha256(content).hexdigest()

    return fingerprint


def test_decode_main_converts_a_complete_uyvy_frame(tmp_path):
    raw = tmp_path / "main.uyvy"
    packed = np.zeros(capture.MAIN_SHAPE, dtype=np.uint8)
    packed[:, :, 0] = 128
    packed[:, :, 1] = 128
    raw.write_bytes(packed.tobytes())

    image = capture._decode(raw, "main")

    assert image.shape == (720, 1280, 3)
    assert image.dtype == np.uint8


def test_run_writes_verified_pipeline_bound_camera_records(tmp_path, monkeypatch):
    lower = np.full((480, 640, 3), 100, dtype=np.uint8)
    lower_bytes = cv2.imencode(".jpg", lower)[1].tobytes()
    main_bytes = np.zeros(capture.MAIN_SHAPE, dtype=np.uint8).tobytes()
    commands = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        if "pull" in command:
            destination = Path(command[-1])
            destination.write_bytes(main_bytes if destination.suffix == ".uyvy" else lower_bytes)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(capture.subprocess, "run", fake_run)
    monkeypatch.setattr(capture.subprocess, "check_output", _check_output)
    monkeypatch.setattr(capture, "_remote_raw_fingerprint", _fingerprint(main_bytes, lower_bytes))
    monkeypatch.setattr(capture.time, "monotonic_ns", lambda: 100)

    output = tmp_path / "capture"
    capture.run("serial-1", output, expected_boot_id="boot-1", count=1, duration_s=1)

    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["capture"]["pairing"] == "sequential main then lower; not simultaneous"
    assert manifest["status"] == "complete"
    assert manifest["capture_pipeline_sha256"] == capture._pipeline_sha256(
        manifest["capture_pipeline"]
    )
    assert set(manifest["capture_pipeline"]) == {"main", "lower"}
    assert not (output / "INCOMPLETE").exists()
    for camera, shape in (("main", [1280, 720]), ("lower", [640, 480])):
        record = json.loads((output / camera / "frame-000000.json").read_text())
        assert record["shape_px"] == shape
        assert record["camera"] == camera
        assert record["raw_capture_collection"] == manifest["raw_capture_collection"]
        assert record["capture_pipeline_sha256"] == manifest["capture_pipeline_sha256"]
        source = output / camera / ("raw-000000.uyvy" if camera == "main" else "raw-000000.mjpg")
        assert record["source_file"] == source.name
        assert record["source_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
        assert record["capture_timing"] == {
            "status": "measured",
            "clock_domain": "capture_host_monotonic",
            "timestamp_meaning": (
                "host capture-command through transfer interval, not device capture or exposure"
            ),
            "host_capture_and_pull_started_monotonic_ns": 100,
            "host_capture_and_pull_ended_monotonic_ns": 100,
        }
        assert record["source_device_sha256"] == record["source_sha256"]
        assert record["source_device_size_bytes"] == source.stat().st_size
        assert (output / camera / "frame-000000.png").exists()
        stream_pipeline = json.loads((output / camera / "calibration-pipeline.json").read_text())
        assert stream_pipeline["capture_pipeline_sha256"] == manifest["capture_pipeline_sha256"]
        assert (
            stream_pipeline["calibration_pipeline"]
            == manifest["cameras"][camera]["calibration_pipeline"]
        )
        result = json.loads((output / camera / "result.json").read_text())
        assert result["recorded_live_provenance"]["stream_id"] == camera
        assert result["recorded_live_provenance"]["manifest_file"] == "manifest.json"
        assert (
            hashlib.sha256((output / camera / "manifest.json").read_bytes()).hexdigest()
            == result["recorded_live_provenance"]["manifest_sha256"]
        )
    assert sum("pull" in command for command in commands) == 2
    assert commands[-1][-1].startswith("rm -f /data/local/tmp/ohmni-cal-")


def test_run_records_only_the_selected_camera_in_its_manifest(tmp_path, monkeypatch):
    main_bytes = np.zeros(capture.MAIN_SHAPE, dtype=np.uint8).tobytes()
    commands = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        if "pull" in command:
            Path(command[-1]).write_bytes(main_bytes)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(capture.subprocess, "run", fake_run)
    monkeypatch.setattr(capture.subprocess, "check_output", _check_output)
    monkeypatch.setattr(capture, "_remote_raw_fingerprint", _fingerprint(main_bytes, b""))
    monkeypatch.setattr(capture.time, "monotonic_ns", lambda: 100)

    output = tmp_path / "main-only"
    capture.run(
        "serial-1",
        output,
        expected_boot_id="boot-1",
        count=1,
        duration_s=1,
        camera="main",
        warmup_frames=7,
    )

    manifest = json.loads((output / "manifest.json").read_text())
    assert set(manifest["cameras"]) == {"main"}
    assert (output / "main" / "frame-000000.png").exists()
    assert not (output / "lower").exists()
    capture_command = next(command[-1] for command in commands if "v4l2-ctl" in command[-1])
    assert "/dev/video0" in capture_command
    assert "--stream-skip=7" in capture_command


def test_run_refuses_identity_or_format_mismatch_before_streaming(tmp_path, monkeypatch):
    commands = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0)

    def mismatch(command, **_kwargs):
        if "boot_id" in command[-1]:
            return "boot-1\n"
        return _probe("video0", pixel_format="MJPG")

    monkeypatch.setattr(capture.subprocess, "run", fake_run)
    monkeypatch.setattr(capture, "_remote_raw_fingerprint", _fingerprint(b"", b""))
    monkeypatch.setattr(capture.subprocess, "check_output", mismatch)
    monkeypatch.setattr(capture.time, "monotonic_ns", lambda: 100)

    with pytest.raises(ValueError, match="identity or negotiated format"):
        capture.run("serial-1", tmp_path / "capture", expected_boot_id="boot-1", camera="main")

    failure = json.loads((tmp_path / "capture" / "failure-manifest.json").read_text())
    assert failure["failure_phase"] == "preflight"
    assert not any("v4l2-ctl" in command[-1] for command in commands)


def test_run_refuses_an_unexpected_usb_identity_before_streaming(tmp_path, monkeypatch):
    commands = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0)

    def mismatch(command, **_kwargs):
        if "boot_id" in command[-1]:
            return "boot-1\n"
        return _probe("video0", vendor_id="ffff")

    monkeypatch.setattr(capture.subprocess, "run", fake_run)
    monkeypatch.setattr(capture.subprocess, "check_output", mismatch)
    monkeypatch.setattr(capture.time, "monotonic_ns", lambda: 100)

    with pytest.raises(ValueError, match="identity or negotiated format"):
        capture.run("serial-1", tmp_path / "capture", expected_boot_id="boot-1", camera="main")

    assert not any("v4l2-ctl" in command[-1] for command in commands)


@pytest.mark.parametrize("count,duration", [(0, 1), (61, 1), (1, 0), (1, 31)])
def test_run_refuses_capture_bounds_without_starting_adb(tmp_path, monkeypatch, count, duration):
    monkeypatch.setattr(capture.subprocess, "check_output", pytest.fail)

    with pytest.raises(ValueError, match="count must be 1..60 and duration at most 30s"):
        capture.run(
            "serial-1",
            tmp_path / "capture",
            expected_boot_id="boot-1",
            count=count,
            duration_s=duration,
        )


def test_run_stops_before_a_capture_after_the_deadline(tmp_path, monkeypatch):
    commands = []
    moments = iter((0, 0, 950_000_000, 950_000_000))
    monkeypatch.setattr(capture.time, "monotonic_ns", lambda: next(moments))
    monkeypatch.setattr(capture.subprocess, "check_output", _check_output)
    monkeypatch.setattr(
        capture.subprocess,
        "run",
        lambda command, **_kwargs: (
            commands.append(command) or subprocess.CompletedProcess(command, 0)
        ),
    )

    with pytest.raises(TimeoutError, match="duration elapsed"):
        capture.run("serial-1", tmp_path / "capture", expected_boot_id="boot-1", duration_s=1)

    assert not any("v4l2-ctl" in command[-1] for command in commands)


def test_run_retains_remote_raw_and_recovery_metadata_when_pull_fails(tmp_path, monkeypatch):
    commands = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        if "pull" in command:
            raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(capture.time, "monotonic_ns", lambda: 100)
    monkeypatch.setattr(capture.subprocess, "check_output", _check_output)
    monkeypatch.setattr(capture.subprocess, "run", fake_run)
    monkeypatch.setattr(capture, "_remote_raw_fingerprint", _fingerprint(b"raw", b""))

    with pytest.raises(subprocess.CalledProcessError):
        capture.run(
            "serial-1", tmp_path / "capture", expected_boot_id="boot-1", count=1, duration_s=1
        )

    failure = json.loads((tmp_path / "capture" / "failure-manifest.json").read_text())
    assert failure["status"] == "failed"
    assert failure["failure_phase"] == "pull:0:main"
    assert len(failure["retained_remote_raw"]) == 1
    assert failure["capture_commands"]
    assert not any(command[-1].startswith("rm -f ") for command in commands)


def test_run_rejects_a_pulled_raw_file_that_differs_from_its_device_fingerprint(
    tmp_path, monkeypatch
):
    commands = []
    main_bytes = np.zeros(capture.MAIN_SHAPE, dtype=np.uint8).tobytes()

    def fake_run(command, **_kwargs):
        commands.append(command)
        if "pull" in command:
            Path(command[-1]).write_bytes(main_bytes)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(capture.time, "monotonic_ns", lambda: 100)
    monkeypatch.setattr(capture.subprocess, "check_output", _check_output)
    monkeypatch.setattr(capture.subprocess, "run", fake_run)
    monkeypatch.setattr(
        capture,
        "_remote_raw_fingerprint",
        lambda _serial, _remote, _deadline: (len(main_bytes), "0" * 64),
    )

    with pytest.raises(ValueError, match="does not match the device source"):
        capture.run(
            "serial-1", tmp_path / "capture", expected_boot_id="boot-1", count=1, camera="main"
        )

    failure = json.loads((tmp_path / "capture" / "failure-manifest.json").read_text())
    assert failure["failure_phase"] == "pull:0:main"
    assert failure["retained_remote_raw"]
    assert failure["error_type"] == "ValueError"
    assert not any(command[-1].startswith("rm -f ") for command in commands)


def test_remote_fingerprint_uses_a_single_quoted_remote_shell_command(monkeypatch):
    commands = []
    digest = "a" * 64

    def output(command, **_kwargs):
        commands.append(command)
        return f"sha256={digest}\nsize=42\n"

    monkeypatch.setattr(capture.subprocess, "check_output", output)
    monkeypatch.setattr(capture, "_remaining_timeout", lambda _deadline: 1)

    assert capture._remote_raw_fingerprint("serial-1", "/data/local/tmp/raw file", 1) == (
        42,
        digest,
    )

    remote_command = commands[0][-1]
    assert shlex.split(remote_command)[:4] == ["su", "0", "sh", "-c"]
    assert "toybox sha256sum" in shlex.split(remote_command)[4]


def test_run_keeps_a_completed_capture_when_cleanup_time_expires(tmp_path, monkeypatch):
    lower = np.full((480, 640, 3), 100, dtype=np.uint8)
    lower_bytes = cv2.imencode(".jpg", lower)[1].tobytes()
    main_bytes = np.zeros(capture.MAIN_SHAPE, dtype=np.uint8).tobytes()
    commands = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        if "pull" in command:
            destination = Path(command[-1])
            destination.write_bytes(main_bytes if destination.suffix == ".uyvy" else lower_bytes)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(capture.subprocess, "run", fake_run)
    monkeypatch.setattr(capture.subprocess, "check_output", _check_output)
    monkeypatch.setattr(capture, "_remote_raw_fingerprint", _fingerprint(main_bytes, lower_bytes))
    monkeypatch.setattr(capture.time, "monotonic_ns", lambda: 0)

    def timeout(deadline_ns):
        if deadline_ns == 1_000_000_000:
            raise TimeoutError("capture duration elapsed")
        return 1

    monkeypatch.setattr(capture, "_remaining_timeout", timeout)

    output = tmp_path / "capture"
    capture.run("serial-1", output, expected_boot_id="boot-1", count=1, duration_s=1)

    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["status"] == "complete"
    assert manifest["remote_raw_cleanup"]["status"] == "deadline_elapsed"
    assert not (output / "failure-manifest.json").exists()
    assert not any(command[-1].startswith("rm -f ") for command in commands)
