import json
from pathlib import Path

import cv2
import numpy as np
import pytest

from tools import ohmni_dual_calibration_capture as capture


def test_decode_main_converts_a_complete_uyvy_frame(tmp_path):
    raw = tmp_path / "main.uyvy"
    packed = np.zeros(capture.MAIN_SHAPE, dtype=np.uint8)
    packed[:, :, 0] = 128
    packed[:, :, 1] = 128
    raw.write_bytes(packed.tobytes())

    image = capture._decode(raw, "main")

    assert image.shape == (720, 1280, 3)
    assert image.dtype == np.uint8


def test_run_writes_decoded_camera_records_and_receipt_intervals(tmp_path, monkeypatch):
    lower = np.full((3, 4, 3), 100, dtype=np.uint8)
    lower_bytes = cv2.imencode(".jpg", lower)[1].tobytes()
    main_bytes = np.zeros(capture.MAIN_SHAPE, dtype=np.uint8).tobytes()
    commands = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        if "pull" in command:
            destination = Path(command[-1])
            destination.write_bytes(main_bytes if destination.suffix == ".uyvy" else lower_bytes)

    monkeypatch.setattr(capture.subprocess, "run", fake_run)
    monkeypatch.setattr(capture.subprocess, "check_output", lambda *_args, **_kwargs: "boot-1\n")
    monkeypatch.setattr(capture.time, "monotonic_ns", lambda: 100)

    output = tmp_path / "capture"
    capture.run("serial-1", output, count=1, duration_s=1)

    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["capture"]["pairing"] == "sequential main then lower; not simultaneous"
    assert not (output / "INCOMPLETE").exists()
    for camera, shape in (("main", [1280, 720]), ("lower", [4, 3])):
        record = json.loads((output / camera / "frame-000000.json").read_text())
        assert record["shape_px"] == shape
        assert record["capture_timing"] == {
            "status": "measured",
            "clock_domain": "capture_host_monotonic",
            "timestamp_meaning": "host receipt interval, not device capture or exposure",
            "host_receipt_started_monotonic_ns": 100,
            "host_receipt_ended_monotonic_ns": 100,
        }
        assert (output / camera / "frame-000000.png").exists()
    assert sum("pull" in command for command in commands) == 2


@pytest.mark.parametrize("count,duration", [(0, 1), (61, 1), (1, 0), (1, 31)])
def test_run_refuses_capture_bounds_without_starting_adb(tmp_path, monkeypatch, count, duration):
    monkeypatch.setattr(capture.subprocess, "check_output", pytest.fail)

    with pytest.raises(ValueError, match="count must be 1..60 and duration at most 30s"):
        capture.run("serial-1", tmp_path / "capture", count=count, duration_s=duration)
