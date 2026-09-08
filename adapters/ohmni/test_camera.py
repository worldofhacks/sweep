from __future__ import annotations

import io
import subprocess
import threading
from types import SimpleNamespace

import pytest

from .camera import Camera, V4LSource, command, from_environment, publish_url, stream_name

SOURCE_11 = V4LSource("/dev/video1", "mjpeg", None, 640, 480)


def test_global_device_id_derives_the_canonical_media_path() -> None:
    assert stream_name(11) == "drone11"
    assert "/drone11" in publish_url("media.example", 11, "node-key")
    assert "/ground11" not in publish_url("media.example", 11, "node-key")


@pytest.mark.parametrize("device_id", [0, 65, True])
def test_stream_name_refuses_out_of_range_or_boolean_device_ids(device_id: object) -> None:
    with pytest.raises(ValueError, match="device ID"):
        stream_name(device_id)  # type: ignore[arg-type]


def test_native_v4l_rate_does_not_force_a_framerate() -> None:
    values = command("ffmpeg", "rtsp://private/drone11", SOURCE_11)
    assert values[values.index("-input_format") + 1] == "mjpeg"
    assert values[values.index("-i") + 1] == "/dev/video1"
    assert "-framerate" not in values


def test_pts_sidecar_tees_one_passthrough_encoder_without_changing_the_camera_source() -> None:
    values = command("ffmpeg", "rtsp://private/drone11", SOURCE_11, pts_port=18555)

    assert values[values.index("-timestamps") + 1] == "default"
    assert "-copyts" in values
    assert "-r" not in values
    assert values[values.index("-fps_mode") + 1] == "passthrough"
    assert values[values.index("-map") + 1] == "0:v:0"
    assert values[-2] == "tee"
    output = values[-1]
    assert "f=rtsp" in output
    assert "f=nut" in output
    assert "onfail=ignore" not in output
    assert output.count("onfail=abort") == 2
    assert "avoid_negative_ts=disabled" in output
    assert "tcp\\://127.0.0.1\\:18555?tcp_nodelay=1" in output


@pytest.mark.parametrize("port", [0, 80, 65536, True])
def test_pts_sidecar_refuses_unusable_loopback_ports(port: object) -> None:
    with pytest.raises(ValueError, match="PTS sidecar port"):
        command("ffmpeg", "rtsp://private/drone11", SOURCE_11, pts_port=port)  # type: ignore[arg-type]


def test_camera_only_reports_publishing_after_current_frame_progress() -> None:
    now = [10.0]
    camera = Camera("media.example", 11, "node-key", "ffmpeg", SOURCE_11, monotonic=lambda: now[0])
    camera._process = SimpleNamespace(poll=lambda: None)  # type: ignore[assignment]
    camera._state = "connecting"

    assert camera.state == "connecting"
    camera._observe_progress(SimpleNamespace(stderr=io.BytesIO(b"frame=3\nprogress=continue\n")))  # type: ignore[arg-type]
    assert camera.state == "publishing"

    now[0] += 2.0
    camera._observe_progress(SimpleNamespace(stderr=io.BytesIO(b"frame=3\n")))  # type: ignore[arg-type]
    now[0] += 1.1
    assert camera.state == "failed"


def test_camera_environment_requires_an_explicit_verified_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SWEEP_DEVICE_UNIT", "11")
    monkeypatch.setenv("SWEEP_CAMERA_DEVICE", "/dev/video1")
    monkeypatch.setenv("SWEEP_CAMERA_INPUT_FORMAT", "mjpeg")
    monkeypatch.setenv("SWEEP_CAMERA_INPUT_FPS", "native")
    monkeypatch.setenv("SWEEP_CAMERA_WIDTH_PX", "640")
    monkeypatch.setenv("SWEEP_CAMERA_HEIGHT_PX", "480")

    camera = from_environment("media.example", "node-key")
    values = camera._command
    assert values[values.index("-i") + 1] == "/dev/video1"
    assert "/drone11" in values[-1]

    monkeypatch.delenv("SWEEP_CAMERA_INPUT_FORMAT")
    with pytest.raises(ValueError, match="SWEEP_CAMERA_INPUT_FORMAT"):
        from_environment("media.example", "node-key")


def test_camera_environment_enables_the_sidecar_only_when_explicitly_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SWEEP_DEVICE_UNIT", "11")
    monkeypatch.setenv("SWEEP_CAMERA_DEVICE", "/dev/video1")
    monkeypatch.setenv("SWEEP_CAMERA_INPUT_FORMAT", "mjpeg")
    monkeypatch.setenv("SWEEP_CAMERA_INPUT_FPS", "native")
    monkeypatch.setenv("SWEEP_CAMERA_WIDTH_PX", "640")
    monkeypatch.setenv("SWEEP_CAMERA_HEIGHT_PX", "480")
    monkeypatch.setenv("SWEEP_CAMERA_PTS_PORT", "18555")

    camera = from_environment("media.example", "node-key")

    assert "tee" in camera._command


def test_camera_cleanup_timeout_fails_without_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    launched = threading.Event()
    cleanup_attempted = threading.Event()
    processes: list[SimpleNamespace] = []

    def popen(*_: object, **__: object) -> SimpleNamespace:
        process = SimpleNamespace(
            stderr=io.BytesIO(),
            wait_timeouts=[],
            poll=lambda: None,
            terminate=lambda: None,
            kill=lambda: None,
        )

        def wait(*, timeout: int) -> None:
            process.wait_timeouts.append(timeout)
            if len(process.wait_timeouts) == 2:
                cleanup_attempted.set()
            raise subprocess.TimeoutExpired("ffmpeg", timeout)

        process.wait = wait
        processes.append(process)
        launched.set()
        return process

    monkeypatch.setattr("adapters.ohmni.camera.subprocess.Popen", popen)
    camera = Camera("media.example", 11, "node-key", "ffmpeg", SOURCE_11)
    camera.start()
    assert launched.wait(timeout=1)

    camera.request_stop()
    assert cleanup_attempted.wait(timeout=1)
    camera.close()

    assert len(processes) == 1
    assert processes[0].wait_timeouts == [2, 2]
    assert camera.state == "failed"
    assert not camera._thread.is_alive()
