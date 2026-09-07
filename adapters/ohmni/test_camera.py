from __future__ import annotations

import hashlib
import hmac
import io
import json
import subprocess
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
    monkeypatch.delenv("SWEEP_CAMERA_STREAM", raising=False)
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


def test_explicit_local_stream_changes_account_path_and_hmac_without_changing_device_id():
    url = publish_url("media.example", 11, "node-key", stream="ground1")
    password = hmac.new(b"node-key", b"sweep-media-publish-v1:ground1", hashlib.sha256).hexdigest()
    assert url == f"rtsp://ground1:{password}@media.example:8554/ground1"
    assert stream_name(11) == "drone11"
    assert url != publish_url("media.example", 11, "node-key")


@pytest.mark.parametrize(
    "stream",
    ["", "../ground1", "ground1/second", "user@ground1", "ground1?x", "ground1\n", "a" * 65, True],
)
def test_explicit_stream_rejects_unbounded_or_nonflat_paths(stream):
    with pytest.raises(ValueError, match="stream"):
        publish_url("media.example", 11, "node-key", stream=stream)


def test_stream_override_cannot_bypass_device_identity_bound():
    with pytest.raises(ValueError, match="device ID"):
        publish_url("media.example", 65, "node-key", stream="ground1")


def test_stream_override_accepts_exact_relay_length_bound():
    stream = "a" * 64
    assert publish_url("media.example", 11, "node-key", stream=stream).endswith("/" + stream)


def test_factory_preserves_native_rate_with_explicit_local_stream(monkeypatch):
    for key, value in {
        "SWEEP_DEVICE_UNIT": "11",
        "SWEEP_CAMERA_STREAM": "ground1",
        "SWEEP_CAMERA_DEVICE": "/dev/video1",
        "SWEEP_CAMERA_INPUT_FORMAT": "mjpeg",
        "SWEEP_CAMERA_INPUT_FPS": "native",
        "SWEEP_CAMERA_WIDTH_PX": "640",
        "SWEEP_CAMERA_HEIGHT_PX": "480",
    }.items():
        monkeypatch.setenv(key, value)
    camera = from_environment("media.example", "node-key")
    assert camera._command[-1].endswith("/ground1")
    assert "-framerate" not in camera._command
    assert camera._command[camera._command.index("-i") + 1] == "/dev/video1"


class Process:
    def __init__(self, progress=b"", *, exit_code=None, stubborn=False):
        self.stderr = io.BytesIO(progress)
        self.returncode = exit_code
        self.stubborn = stubborn
        self.terminated = False
        self.killed = False
        self.wait_timeouts = []

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        if not self.stubborn:
            self.returncode = 0

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        self.wait_timeouts.append(timeout)
        if self.stubborn:
            raise subprocess.TimeoutExpired("private command", timeout)
        return self.returncode


def publisher(*, clock=lambda: 10.0):
    return Camera(
        "media.example", 11, "node-key", "ffmpeg", SOURCE_11, stream="ground1", monotonic=clock
    )


def install_process(monkeypatch, process):
    calls = []

    def launch(*args, **kwargs):
        calls.append((args, kwargs))
        return process

    monkeypatch.setattr(subprocess, "Popen", launch)
    return calls


def test_probe_observes_advancing_frames_and_cleans_only_its_single_owned_process(monkeypatch):
    process = Process(b"frame=2\nprogress=continue\nframe=3\nprogress=continue\n")
    calls = install_process(monkeypatch, process)
    camera = publisher()
    result = camera.probe()
    assert result == {
        "status": "frames_observed",
        "reason": None,
        "frames_seen": 3,
        "progress_updates": 2,
        "cleanup_confirmed": True,
    }
    assert len(calls) == 1
    assert process.terminated
    assert process.wait_timeouts == [2]
    assert camera.state == "stopped"
    assert camera._process is None
    assert calls[0][1]["stderr"] == subprocess.PIPE
    assert "node-key" not in json.dumps(result)
    assert "rtsp:" not in json.dumps(result)


@pytest.mark.parametrize(
    "progress", [b"", b"frame=3\nframe=3\n", b"frame=0\nframe=-1\n", b"frame=bad\n"]
)
def test_probe_does_not_succeed_on_process_liveness_or_repeated_frames(monkeypatch, progress):
    process = Process(progress)
    calls = install_process(monkeypatch, process)
    times = iter([0.0, 2.0, 4.0, 6.0, 8.0])
    camera = publisher(clock=lambda: next(times))
    result = camera.probe(1)
    assert result["status"] == "failed"
    assert result["reason"] == "progress_timeout"
    assert result["cleanup_confirmed"] is True
    assert process.terminated and len(calls) == 1


def test_probe_exits_once_on_capture_or_publish_failure(monkeypatch):
    process = Process(b"private ffmpeg error containing rtsp://credential@host/path\n", exit_code=1)
    calls = install_process(monkeypatch, process)
    result = publisher().probe()
    assert result["reason"] == "publisher_exited"
    assert result["cleanup_confirmed"] is True
    assert len(calls) == 1
    assert not process.terminated
    assert "credential" not in json.dumps(result)


def test_probe_start_error_is_sanitized_and_not_retried(monkeypatch):
    calls = []

    def fail(*args, **_kwargs):
        calls.append(args)
        raise OSError("private credential-bearing URL")

    monkeypatch.setattr(subprocess, "Popen", fail)
    result = publisher().probe()
    assert result["reason"] == "publisher_start_failed"
    assert result["cleanup_confirmed"] is True
    assert len(calls) == 1
    assert "credential" not in json.dumps(result)


def test_probe_reports_unconfirmed_cleanup_and_never_replaces_process(monkeypatch):
    process = Process(b"frame=1\nframe=2\n", stubborn=True)
    calls = install_process(monkeypatch, process)
    camera = publisher()
    result = camera.probe()
    assert result["status"] == "failed" and result["reason"] == "cleanup_failed"
    assert result["cleanup_confirmed"] is False
    assert process.terminated and process.killed
    assert process.wait_timeouts == [2, 2]
    assert camera._process is process
    with pytest.raises(ValueError, match="idle"):
        camera.probe()
    assert len(calls) == 1


def test_probe_interrupt_cleans_owned_child(monkeypatch):
    process = Process()
    camera = publisher()

    def launch(*_args, **_kwargs):
        camera.request_stop()
        return process

    monkeypatch.setattr(subprocess, "Popen", launch)
    result = camera.probe()
    assert result["reason"] == "interrupted"
    assert result["cleanup_confirmed"] is True and process.terminated


@pytest.mark.parametrize("timeout", [0, 31, float("nan"), float("inf"), True])
def test_probe_rejects_invalid_deadline_before_launch(monkeypatch, timeout):
    monkeypatch.setattr(subprocess, "Popen", lambda *_a, **_k: pytest.fail("unexpected launch"))
    with pytest.raises(ValueError, match="timeout"):
        publisher().probe(timeout)


@pytest.mark.parametrize("successful", [True, False])
def test_runner_probe_prints_only_diagnostic_json_and_returns_result_status(
    monkeypatch, capsys, successful
):
    from . import camera_runner

    result = {
        "status": "frames_observed" if successful else "failed",
        "reason": None if successful else "progress_timeout",
        "frames_seen": 3,
        "progress_updates": 2,
        "cleanup_confirmed": True,
    }
    closed = []
    camera = SimpleNamespace(
        probe=lambda deadline: result, close=lambda: closed.append(True), request_stop=lambda: None
    )
    monkeypatch.setenv("SWEEP_MEDIA_HOST", "media.example")
    monkeypatch.setattr(camera_runner, "from_environment", lambda *_args: camera)
    monkeypatch.setattr(camera_runner.signal, "signal", lambda *_args: None)
    with pytest.raises(SystemExit) as status:
        camera_runner.main(["--probe", "--timeout", "10"])
    assert status.value.code == (0 if successful else 1)
    assert json.loads(capsys.readouterr().out) == result
    assert closed == [True]
