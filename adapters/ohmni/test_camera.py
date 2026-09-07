from __future__ import annotations

import io
from types import SimpleNamespace

import pytest

from .camera import Camera, V4LSource, command, publish_url, stream_name
from .device import _camera_from_environment

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

    camera = _camera_from_environment("media.example", "node-key")
    values = camera._command
    assert values[values.index("-i") + 1] == "/dev/video1"
    assert "/drone11" in values[-1]

    monkeypatch.delenv("SWEEP_CAMERA_INPUT_FORMAT")
    with pytest.raises(ValueError, match="SWEEP_CAMERA_INPUT_FORMAT"):
        _camera_from_environment("media.example", "node-key")
