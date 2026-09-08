from __future__ import annotations

import hashlib
import hmac
import io
import json
import stat
import subprocess
import threading
import time
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest

from adapters.ohmni.camera import FRESH_SECONDS, Camera, CameraConfig, command, publish_url


def config(**changes):
    return replace(
        CameraConfig(
            camera_id="front",
            device="/dev/video0",
            stream="ground11-front",
            publisher_user="ground11-front",
            input_format="uyvy422",
            width=640,
            height=480,
            capture_fps=30,
            output_fps=15,
            bitrate_kbps=800,
        ),
        **changes,
    )


def environment(*rows):
    return {
        "SWEEP_GROUND_CAMERAS_JSON": json.dumps([asdict(row) for row in rows]),
        "SWEEP_MEDIA_HOST": "media.local",
        "SWEEP_DEVICE_UNIT": "11",
    }


def camera(*rows, clock=time.monotonic):
    return Camera("media.local", 11, "test-key", "ffmpeg", rows or [config()], clock=clock)


def test_no_implicit_camera_or_second_feed(monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", lambda *_a, **_kw: pytest.fail("opened process"))
    assert Camera.from_environment({"SWEEP_MEDIA_HOST": "media.local"}, key="test-key") is None
    assert Camera.from_environment(environment(), key="test-key") is None
    publisher = Camera.from_environment(environment(config()), key="test-key")
    assert len(publisher.telemetry()) == 1
    assert publisher.state == "stopped"
    assert publisher.telemetry()[0]["frames"] is None
    publisher.close()
    publisher.start()  # A closed owner never opens a process.


@pytest.mark.parametrize("unit", [1, 5, 11, 64])
def test_explicit_units_beyond_four_preserve_per_stream_credentials(unit):
    stream = f"ground{unit}-front"
    url = publish_url("media.local", unit, "test-key", stream=stream, publisher_user=stream)
    expected = hmac.new(b"test-key", f"sweep-media-publish-v1:{stream}".encode(), hashlib.sha256)
    assert url == f"rtsp://{stream}:{expected.hexdigest()}@media.local:8554/{stream}"


@pytest.mark.parametrize("unit", [0, 65, True, "11"])
def test_invalid_units(unit):
    with pytest.raises(ValueError):
        publish_url("media.local", unit, "key", stream="ground11", publisher_user="ground11")


@pytest.mark.parametrize(
    "host", ["user@media", "media/path", "media?x", "media\nx", "media:0", "media:99999"]
)
def test_invalid_hosts(host):
    with pytest.raises(ValueError):
        publish_url(host, 11, "key", stream="ground11", publisher_user="ground11")


@pytest.mark.parametrize(
    "changes",
    [
        {"device": "/dev/video0/../video1"},
        {"device": "/dev/v4l/by-id/.."},
        {"stream": "ground11/second"},
        {"camera_id": "Front"},
        {"width": True},
        {"height": 481},
        {"output_fps": 31},
        {"capture_fps": 0},
        {"input_format": "invented"},
        {"bitrate_kbps": 0},
    ],
)
def test_invalid_explicit_mode(changes):
    with pytest.raises(ValueError):
        config(**changes)


@pytest.mark.parametrize("field", ["camera_id", "device", "stream"])
def test_duplicate_feed_inputs_or_outputs_rejected(field):
    first = config()
    second = config(camera_id="rear", device="/dev/video1", stream="ground11-rear")
    second = replace(second, **{field: getattr(first, field)})
    with pytest.raises(ValueError):
        camera(first, second)


@pytest.mark.parametrize(
    "raw", ["", "{}", "null", "[{}]", '[{"camera_id":"front","camera_id":"rear"}]', "[" * 2000]
)
def test_malformed_configuration(raw):
    with pytest.raises(ValueError):
        Camera.from_environment({"SWEEP_GROUND_CAMERAS_JSON": raw}, key="key")


@pytest.mark.parametrize("field", ["SWEEP_MEDIA_HOST", "SWEEP_DEVICE_UNIT"])
def test_nonempty_configuration_requires_identity(field):
    env = environment(config())
    del env[field]
    with pytest.raises(ValueError):
        Camera.from_environment(env, key="key")


def test_two_feeds_have_distinct_explicit_capture_and_stream_commands():
    first = config()
    second = config(
        camera_id="rear",
        device="/dev/video1",
        stream="ground11-rear",
        publisher_user="ground11-rear",
        input_format="mjpeg",
        width=1280,
        height=720,
        capture_fps=15,
        output_fps=10,
    )
    publisher = camera(first, second)
    for feed, expected in zip(publisher._feeds, (first, second), strict=True):
        args = feed.args
        for flag, value in (
            ("-i", expected.device),
            ("-input_format", expected.input_format),
            ("-video_size", f"{expected.width}x{expected.height}"),
            ("-framerate", str(expected.capture_fps)),
            ("-r", str(expected.output_fps)),
            ("-rtsp_transport", "tcp"),
            ("-c:v", "libx264"),
            ("-progress", "pipe:1"),
        ):
            assert args[args.index(flag) + 1] == value
        assert args[-1].endswith("/" + expected.stream)
    assert publisher._feeds[0].args[-1] != publisher._feeds[1].args[-1]
    assert "test-key" not in json.dumps(publisher.telemetry())


def test_freshness_requires_progress_per_feed_and_expires_without_new_frames():
    now = [10.0]
    publisher = camera(
        config(),
        config(camera_id="rear", device="/dev/video1", stream="rear"),
        clock=lambda: now[0],
    )
    first, second = publisher._feeds
    first_generation, second_generation = first.begin(), second.begin()
    assert publisher.state == "connecting"
    first.progress(first_generation, 1, 1000)
    assert publisher.state == "connecting"
    assert [row["fresh"] for row in publisher.telemetry()] == [True, False]
    second.progress(second_generation, 1, 1000)
    assert publisher.state == "publishing"
    now[0] += FRESH_SECONDS + 0.01
    first.progress(first_generation, 1, 1000)  # Repeated progress cannot refresh.
    second.progress(second_generation, 2, 2000)
    assert publisher.state == "failed"
    assert [row["fresh"] for row in publisher.telemetry()] == [False, True]
    assert publisher.telemetry()[0]["error"] == "progress_stale"


def test_failed_stopped_or_restarted_feed_cannot_accept_old_progress():
    publisher = camera()
    feed = publisher._feeds[0]
    generation = feed.begin()
    feed.progress(generation, 1, 1000)
    feed.finish("failed", "publisher_exited")
    feed.progress(generation, 2, 2000)
    assert publisher.state == "failed"
    assert publisher.telemetry()[0]["fresh"] is False
    new_generation = feed.begin()
    feed.progress(generation, 3, 3000)
    assert publisher.state == "connecting"
    feed.progress(new_generation, 1, 1000)
    feed.finish("stopped")
    assert publisher.state == "stopped"
    assert publisher.telemetry()[0]["fresh"] is False


def test_progress_parser_requires_both_positive_advancing_counters():
    publisher = camera()
    feed = publisher._feeds[0]
    generation = feed.begin()
    output = io.BytesIO(
        b"frame=1\nprogress=continue\nframe=0\nout_time_us=100\nprogress=continue\n"
        b"frame=2\nout_time_us=N/A\nprogress=continue\n"
    )
    publisher._read_progress(feed, SimpleNamespace(stdout=output), generation)
    assert publisher.state == "connecting"
    output = io.BytesIO(b"frame=2\nout_time_us=100000\nprogress=continue\n")
    publisher._read_progress(feed, SimpleNamespace(stdout=output), generation)
    assert publisher.telemetry()[0]["output_time_ms"] == 100
    assert publisher.state == "publishing"


class Process:
    def __init__(self, output=b""):
        self.stdout = io.BytesIO(output)
        self.returncode = None
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def wait(self, timeout=None):
        assert self.returncode is not None
        return self.returncode


def wait_for(predicate):
    deadline = time.monotonic() + 2
    while not predicate() and time.monotonic() < deadline:
        threading.Event().wait(0.01)
    assert predicate()


def test_owned_process_cleanup_and_alive_without_progress_is_not_publishing(monkeypatch):
    process = Process()
    monkeypatch.setattr(
        "adapters.ohmni.camera.os.stat", lambda _p: SimpleNamespace(st_mode=stat.S_IFCHR, st_rdev=1)
    )
    launches = []

    def launch(args, **kwargs):
        launches.append((args, kwargs))
        return process

    monkeypatch.setattr(subprocess, "Popen", launch)
    publisher = camera()
    try:
        publisher.start()
        publisher.start()
        wait_for(lambda: len(launches) == 1)
        assert publisher.state == "connecting"
        assert publisher.telemetry()[0]["fresh"] is False
        assert launches[0][1]["stderr"] == subprocess.DEVNULL
    finally:
        publisher.close()
    assert process.terminated
    assert publisher.state == "stopped"


@pytest.mark.parametrize("second_state", ["missing", "alias"])
def test_second_feed_failure_never_duplicates_primary_capture(monkeypatch, second_state):
    def device_stat(path):
        if path == "/dev/video1" and second_state == "missing":
            raise FileNotFoundError
        return SimpleNamespace(st_mode=stat.S_IFCHR, st_rdev=1)

    monkeypatch.setattr("adapters.ohmni.camera.os.stat", device_stat)
    launches = []

    def launch(args, **_kwargs):
        process = Process(b"frame=1\nout_time_us=100000\nprogress=continue\n")
        launches.append((args, process))
        return process

    monkeypatch.setattr(subprocess, "Popen", launch)
    publisher = camera(config(), config(camera_id="rear", device="/dev/video1", stream="rear"))
    try:
        publisher.start()
        wait_for(lambda: publisher.state == "failed" and len(launches) == 1)
        wait_for(lambda: any(row["fresh"] for row in publisher.telemetry()))
        rows = publisher.telemetry()
        assert sorted(row["publisher_state"] for row in rows) == ["failed", "publishing"]
        assert {row["error"] for row in rows} == {
            None,
            "device_unavailable" if second_state == "missing" else "device_alias",
        }
        assert len(launches) == 1
    finally:
        publisher.close()
    assert all(process.terminated for _, process in launches)


def test_command_does_not_accept_invalid_executable():
    with pytest.raises(ValueError):
        command("ffmpeg\0", "unused", config())
