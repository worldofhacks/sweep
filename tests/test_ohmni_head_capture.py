import hashlib
import json
import multiprocessing as mp
import time

import numpy as np
import pytest

from perception.webcam_stream import WebcamStream, _Mailbox
from tools.ohmni_head_capture import record_head


def _burn_cpu() -> None:
    deadline = time.process_time() + 0.1
    value = 0
    while time.process_time() < deadline:
        value += 1


def test_head_resolution_mailbox_preserves_pixels_and_default_stays_aircraft():
    mailbox = _Mailbox(mp.get_context("spawn"), (640, 480))
    frame = np.full((480, 640, 3), 23, np.uint8)
    mailbox.put(frame, 2.5)
    result = mailbox.get(0)
    np.testing.assert_array_equal(result[0], frame)
    assert result[1] == 2.5
    assert WebcamStream("rtsp://localhost/drone1")._mailbox._shape == (720, 1280, 3)
    for dimensions in [(0, 480), (True, 480), (640, 4097), (640,), [640, 480]]:
        with pytest.raises(ValueError, match="resolution"):
            WebcamStream("rtsp://localhost/ground1", resolution=dimensions)


def test_record_head_writes_bounded_frames_index_and_receipt_provenance(tmp_path, monkeypatch):
    class Stream:
        def __init__(self, url, *, resolution):
            assert resolution == (640, 480)

        def __enter__(self):
            return self

        def __exit__(self, *_):
            self.closed = True

        def read(self, _):
            return np.full((480, 640, 3), 45, np.uint8), time.monotonic()

    monkeypatch.setattr("tools.ohmni_head_capture.WebcamStream", Stream)
    output = tmp_path / "head"
    manifest = record_head(
        "rtsp://user:secret@example/ground1",
        output,
        run_id="run",
        device_id="7",
        camera_id="head",
        max_bytes=307200,
    )
    assert manifest["recording"]["frame_count"] == 1
    assert (
        manifest["recording"]["frames_sha256"]
        == hashlib.sha256((output / "frames.gray").read_bytes()).hexdigest()
    )
    assert (output / "frames.gray").read_bytes() == bytes([45]) * 307200
    row = json.loads((output / "frames.jsonl").read_text())
    assert row["index"] == row["offset_bytes"] == 0
    assert (
        manifest["capture"]["started_monotonic_ns"]
        <= row["received_monotonic_ns"]
        <= manifest["capture"]["ended_monotonic_ns"]
    )
    assert manifest["measurements"]["latency"]["status"] == "unavailable"
    cpu = manifest["measurements"]["cpu"]
    assert type(cpu["host_process_cpu_ns"]) is int and cpu["host_process_cpu_ns"] >= 0
    assert type(cpu["wall_duration_ns"]) is int and cpu["wall_duration_ns"] > 0
    assert cpu["child_process_cpu_status"] in {"measured", "unavailable"}
    assert "secret" not in (output / "manifest.json").read_text()
    assert not (output / "INCOMPLETE").exists()
    with pytest.raises(FileExistsError):
        record_head(
            "rtsp://localhost/ground1", output, run_id="run", device_id="7", camera_id="head"
        )


def test_failed_decoder_leaves_no_complete_manifest(tmp_path, monkeypatch):
    def fail(_):
        raise RuntimeError("decoder failed")

    monkeypatch.setattr(WebcamStream, "__enter__", fail)
    output = tmp_path / "failed"
    with pytest.raises(RuntimeError):
        record_head(
            "rtsp://localhost/ground1", output, run_id="run", device_id="7", camera_id="head"
        )
    assert (output / "INCOMPLETE").exists()
    assert not (output / "manifest.json").exists()


def test_record_head_accounts_for_a_reaped_decoder_child(tmp_path, monkeypatch):
    class Stream:
        def __init__(self, _url, *, resolution):
            assert resolution == (640, 480)
            self.child = None

        def __enter__(self):
            self.child = mp.get_context("spawn").Process(target=_burn_cpu)
            self.child.start()
            return self

        def __exit__(self, *_):
            self.child.join()

        def read(self, _):
            return np.zeros((480, 640, 3), np.uint8), time.monotonic()

    monkeypatch.setattr("tools.ohmni_head_capture.WebcamStream", Stream)
    manifest = record_head(
        "rtsp://localhost/ground1",
        tmp_path / "child-cpu",
        run_id="run",
        device_id="7",
        camera_id="head",
        max_bytes=307200,
    )
    cpu = manifest["measurements"]["cpu"]
    if cpu["child_process_cpu_status"] == "measured":
        assert cpu["child_process_cpu_ns"] > 0
        assert cpu["aggregate_process_cpu_ns"] >= cpu["child_process_cpu_ns"]
    else:
        assert cpu["child_process_cpu_ns"] is None


def test_record_head_marks_child_cpu_unavailable_when_the_platform_cannot_measure_it(
    tmp_path, monkeypatch
):
    class Stream:
        def __init__(self, _url, *, resolution):
            assert resolution == (640, 480)

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def read(self, _):
            return np.zeros((480, 640, 3), np.uint8), time.monotonic()

    monkeypatch.setattr("tools.ohmni_head_capture.WebcamStream", Stream)
    monkeypatch.setattr("tools.ohmni_head_capture._reaped_children_cpu_ns", lambda: None)
    manifest = record_head(
        "rtsp://localhost/ground1",
        tmp_path / "no-child-cpu",
        run_id="run",
        device_id="7",
        camera_id="head",
        max_bytes=307200,
    )
    cpu = manifest["measurements"]["cpu"]
    assert cpu["child_process_cpu_status"] == "unavailable"
    assert cpu["child_process_cpu_ns"] is None
    assert cpu["aggregate_process_cpu_ns"] is None
