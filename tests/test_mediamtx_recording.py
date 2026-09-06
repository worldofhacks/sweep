from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest

IMAGE = "bluenviron/mediamtx:1.20.1"
ROOT = Path(__file__).resolve().parents[1]


def _command(*args: str, timeout: float = 30, **kwargs: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args, text=True, capture_output=True, check=True, timeout=timeout, **kwargs
    )


def _port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _wait_for_port(port: int) -> None:
    for _ in range(100):
        with socket.socket() as connection:
            connection.settimeout(0.1)
            if connection.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.05)
    raise AssertionError(f"MediaMTX did not listen on {port}")


def _required_or_skip(condition: bool, message: str) -> None:
    if condition:
        return
    if os.environ.get("SWEEP_REQUIRE_MEDIA_RECORDING_TEST") == "1":
        pytest.fail(message)
    pytest.skip(message)


def _recording_environment() -> dict[str, str]:
    default = json.loads(
        _command(
            "docker", "compose", "-f", "docker-compose.yml", "config", "--format", "json", cwd=ROOT
        ).stdout
    )
    resolved = json.loads(
        _command(
            "docker",
            "compose",
            "-f",
            "docker-compose.yml",
            "-f",
            "docker-compose.recording.yml",
            "config",
            "--format",
            "json",
            cwd=ROOT,
        ).stdout
    )
    assert "MTX_PATHDEFAULTS_RECORD" not in default["services"]["mediamtx"]["environment"]
    service = resolved["services"]["mediamtx"]
    assert service["image"] == IMAGE
    assert any(volume["target"] == "/recordings" for volume in service["volumes"])
    environment = service["environment"]
    assert (
        environment
        | {
            "MTX_PATHDEFAULTS_RECORD": "true",
            "MTX_PATHDEFAULTS_RECORDPATH": "/recordings/%path/%Y-%m-%d_%H-%M-%S-%f",
            "MTX_PATHDEFAULTS_RECORDFORMAT": "fmp4",
            "MTX_PATHDEFAULTS_RECORDPARTDURATION": "1s",
            "MTX_PATHDEFAULTS_RECORDSEGMENTDURATION": "1m",
            "MTX_PATHDEFAULTS_RECORDDELETEAFTER": "24h",
        }
        == environment
    )
    return environment


def test_pinned_mediamtx_records_an_fmp4_segment_when_docker_is_available(tmp_path: Path) -> None:
    _required_or_skip(
        all(shutil.which(command) for command in ("docker", "ffmpeg", "ffprobe")),
        "Docker, ffmpeg, and ffprobe are required for the MediaMTX smoke test",
    )
    inspected = subprocess.run(
        ["docker", "image", "inspect", IMAGE],
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    _required_or_skip(inspected.returncode == 0, f"pinned MediaMTX image is unavailable: {IMAGE}")
    environment = _recording_environment()
    recordings = tmp_path / "recordings"
    recordings.mkdir()
    port = _port()
    name = f"sweep-recording-test-{port}"
    command = [
        "docker",
        "run",
        "-d",
        "--rm",
        "--name",
        name,
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "-p",
        f"127.0.0.1:{port}:8554",
        "-v",
        f"{ROOT / 'media' / 'mediamtx.yml'}:/mediamtx.yml:ro",
        "-v",
        f"{recordings}:/recordings",
    ]
    environment["MTX_AUTHINTERNALUSERS_0_PASS"] = "recording-test-password"
    for key, value in environment.items():
        command.extend(("-e", f"{key}={value}"))
    command.append(IMAGE)
    _command(*command)
    try:
        _wait_for_port(port)
        unauthenticated = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "testsrc=size=160x90:rate=10",
                "-t",
                "0.2",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-g",
                "10",
                "-f",
                "rtsp",
                "-rtsp_transport",
                "tcp",
                f"rtsp://127.0.0.1:{port}/drone1",
            ],
            text=True,
            capture_output=True,
            check=False,
            timeout=15,
        )
        assert unauthenticated.returncode != 0
        assert "401 Unauthorized" in unauthenticated.stderr
        _command(
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-re",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=160x90:rate=10",
            "-t",
            "3",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-g",
            "10",
            "-f",
            "rtsp",
            "-rtsp_transport",
            "tcp",
            f"rtsp://drone1:recording-test-password@127.0.0.1:{port}/drone1",
            timeout=15,
        )
        for _ in range(100):
            segments = list(recordings.rglob("*.mp4"))
            if segments:
                break
            time.sleep(0.05)
        else:
            raise AssertionError("MediaMTX did not create an fMP4 recording")
        probe = _command(
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=format_name,duration",
            "-of",
            "default=noprint_wrappers=1",
            str(segments[0]),
        )
        assert "format_name=mov,mp4,m4a,3gp,3g2,mj2" in probe.stdout
        assert float(probe.stdout.split("duration=", 1)[1].splitlines()[0]) > 0
    finally:
        subprocess.run(
            ["docker", "rm", "-f", name],
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
