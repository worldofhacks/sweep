"""Explicit V4L2 publishers; progress is local output evidence, not playback proof.

No device is opened until start(). Credentials never enter diagnostics. Each feed
owns its process, input and output; a missing second input has no primary fallback.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import stat
import subprocess
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields
from urllib.parse import quote, urlsplit

FRESH_SECONDS = 2.0
STARTUP_SECONDS = 10.0
MAX_COUNTER = 2**53 - 1
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}\Z")
_CAMERA_ID = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")
_DEVICE = re.compile(r"/dev/(?:video[0-9]+|v4l/by-id/[A-Za-z0-9_:+.-]{1,180})\Z")


@dataclass(frozen=True, slots=True)
class CameraConfig:
    camera_id: str
    device: str
    stream: str
    publisher_user: str
    input_format: str
    width: int
    height: int
    capture_fps: int
    output_fps: int
    bitrate_kbps: int

    def __post_init__(self) -> None:
        for value, pattern in (
            (self.camera_id, _CAMERA_ID),
            (self.device, _DEVICE),
            (self.stream, _NAME),
            (self.publisher_user, _NAME),
        ):
            if not isinstance(value, str) or pattern.fullmatch(value) is None:
                raise ValueError("camera identity, device or stream is invalid")
        if self.device.endswith(("/.", "/..")):
            raise ValueError("camera device must identify a V4L2 input")
        if self.input_format not in ("uyvy422", "yuyv422", "mjpeg"):
            raise ValueError("camera input format must be explicitly supported")
        for value, low, high in (
            (self.width, 2, 4096),
            (self.height, 2, 2160),
            (self.capture_fps, 1, 60),
            (self.output_fps, 1, 60),
            (self.bitrate_kbps, 64, 20000),
        ):
            if type(value) is not int or not low <= value <= high:
                raise ValueError("camera dimensions, rate or bitrate are invalid")
        if self.width % 2 or self.height % 2 or self.output_fps > self.capture_fps:
            raise ValueError("H.264 requires even dimensions and output fps <= capture fps")


def publish_url(host: str, unit: int, key: str, *, stream: str, publisher_user: str) -> str:
    """Preserve the deployed per-stream HMAC domain without inferring a stream."""
    if type(unit) is not int or not 1 <= unit <= 64:
        raise ValueError("device unit must be 1..64")
    if (
        not isinstance(host, str)
        or not host
        or len(host) > 253
        or any(c.isspace() or c in "/@?#\\" for c in host)
        or not isinstance(key, str)
        or not key
        or not isinstance(stream, str)
        or _NAME.fullmatch(stream) is None
        or not isinstance(publisher_user, str)
        or _NAME.fullmatch(publisher_user) is None
    ):
        raise ValueError("explicit media host, key, stream and publisher are required")
    try:
        parsed = urlsplit(f"rtsp://{host}")
        if not parsed.hostname or parsed.username or parsed.password:
            raise ValueError
        port = parsed.port
        if port is not None and not 1 <= port <= 65535:
            raise ValueError
    except ValueError:
        raise ValueError("media host must be a hostname[:port]") from None
    origin = host if port is not None else f"{host}:8554"
    password = hmac.new(
        key.encode(), f"sweep-media-publish-v1:{stream}".encode(), hashlib.sha256
    ).hexdigest()
    return f"rtsp://{quote(publisher_user, safe='')}:{password}@{origin}/{stream}"


def command(ffmpeg: str, url: str, camera: CameraConfig) -> list[str]:
    if (
        not isinstance(ffmpeg, str)
        or not ffmpeg
        or len(ffmpeg) > 512
        or any(c in ffmpeg for c in "\0\r\n")
    ):
        raise ValueError("ffmpeg executable is invalid")
    return [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostats",
        "-progress",
        "pipe:1",
        "-stats_period",
        "0.5",
        "-f",
        "v4l2",
        "-input_format",
        camera.input_format,
        "-video_size",
        f"{camera.width}x{camera.height}",
        "-framerate",
        str(camera.capture_fps),
        "-i",
        camera.device,
        "-r",
        str(camera.output_fps),
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-tune",
        "zerolatency",
        "-pix_fmt",
        "yuv420p",
        "-b:v",
        f"{camera.bitrate_kbps}k",
        "-g",
        str(camera.output_fps),
        "-rtsp_transport",
        "tcp",
        "-f",
        "rtsp",
        url,
    ]


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate camera configuration key")
        result[key] = value
    return result


class _Feed:
    def __init__(self, camera: CameraConfig, args: list[str], clock) -> None:
        self.camera = camera
        self.args = args
        self.clock = clock
        self.lock = threading.Lock()
        self.generation = 0
        self.phase = "stopped"
        self.error: str | None = None
        self.frame: int | None = None
        self.out_us: int | None = None
        self.last_progress: float | None = None
        self.process = None
        self.thread: threading.Thread | None = None

    def begin(self) -> int:
        with self.lock:
            self.generation += 1
            self.phase, self.error = "connecting", None
            self.frame = self.out_us = self.last_progress = None
            return self.generation

    def finish(self, phase: str, error: str | None = None) -> None:
        with self.lock:
            self.generation += 1
            self.phase, self.error = phase, error

    def progress(self, generation: int, frame: int, out_us: int) -> None:
        with self.lock:
            if (
                generation != self.generation
                or self.phase not in ("connecting", "publishing")
                or not 0 < frame <= MAX_COUNTER
                or not 0 < out_us <= MAX_COUNTER
                or (self.frame is not None and frame <= self.frame)
                or (self.out_us is not None and out_us <= self.out_us)
            ):
                return
            self.frame, self.out_us = frame, out_us
            self.last_progress = self.clock()
            self.phase = "publishing"

    def telemetry(self) -> dict:
        with self.lock:
            age = (
                max(0.0, self.clock() - self.last_progress)
                if self.last_progress is not None
                else None
            )
            fresh = self.phase == "publishing" and age is not None and age <= FRESH_SECONDS
            phase = self.phase
            error = self.error
            if phase == "publishing" and not fresh:
                phase, error = "failed", "progress_stale"
            return {
                "camera_id": self.camera.camera_id,
                "stream": self.camera.stream,
                "device": self.camera.device,
                "publisher_state": phase,
                "fresh": fresh,
                "last_frame_age_ms": min(MAX_COUNTER, int(age * 1000)) if age is not None else None,
                "frames": self.frame,
                "output_time_ms": self.out_us // 1000 if self.out_us is not None else None,
                "evidence": "ffmpeg_progress",
                "error": error,
            }


class Camera:
    """Zero implicit inputs; up to two independent publishers on a configured node."""

    def __init__(
        self,
        host: str,
        unit: int,
        key: str,
        ffmpeg: str,
        cameras: Sequence[CameraConfig],
        *,
        clock=time.monotonic,
    ) -> None:
        if not 1 <= len(cameras) <= 2 or any(not isinstance(c, CameraConfig) for c in cameras):
            raise ValueError("configure one or two explicit cameras")
        for field in ("camera_id", "device", "stream"):
            if len({getattr(c, field) for c in cameras}) != len(cameras):
                raise ValueError("camera IDs, devices and streams must be independent")
        self._clock = clock
        self._feeds = [
            _Feed(
                c,
                command(
                    ffmpeg,
                    publish_url(host, unit, key, stream=c.stream, publisher_user=c.publisher_user),
                    c,
                ),
                clock,
            )
            for c in cameras
        ]
        self._stop = threading.Event()
        self._lifecycle = threading.Lock()
        self._claims_lock = threading.Lock()
        self._claims: set[int] = set()
        self._started = False

    @classmethod
    def from_environment(cls, environment: Mapping[str, str], *, key: str) -> Camera | None:
        raw = environment.get("SWEEP_GROUND_CAMERAS_JSON")
        if raw is None:
            return None
        if not isinstance(raw, str) or len(raw.encode()) > 16384:
            raise ValueError("camera configuration exceeds its bound")
        try:
            rows = json.loads(raw, object_pairs_hook=_unique_object)
        except (ValueError, RecursionError):
            raise ValueError("camera configuration must be valid JSON") from None
        names = {field.name for field in fields(CameraConfig)}
        if (
            not isinstance(rows, list)
            or len(rows) > 2
            or any(not isinstance(row, dict) or set(row) != names for row in rows)
        ):
            raise ValueError("camera configuration must contain zero to two exact feed entries")
        if not rows:
            return None
        try:
            unit = int(environment["SWEEP_DEVICE_UNIT"])
            host = environment["SWEEP_MEDIA_HOST"]
        except (KeyError, TypeError, ValueError):
            raise ValueError("configured cameras require media host and device unit") from None
        return cls(
            host,
            unit,
            key,
            environment.get("SWEEP_FFMPEG", "/data/local/sweep/ffmpeg"),
            [CameraConfig(**row) for row in rows],
        )

    @property
    def state(self) -> str:
        states = [row["publisher_state"] for row in self.telemetry()]
        if all(state == "publishing" for state in states):
            return "publishing"
        if "failed" in states:
            return "failed"
        if "connecting" in states or "publishing" in states:
            return "connecting"
        return "stopped"

    def telemetry(self) -> list[dict]:
        return [feed.telemetry() for feed in self._feeds]

    def start(self) -> None:
        with self._lifecycle:
            if self._started or self._stop.is_set():
                return
            self._started = True
            for feed in self._feeds:
                feed.thread = threading.Thread(
                    target=self._run,
                    args=(feed,),
                    name=f"ohmni-camera-{feed.camera.camera_id}",
                    daemon=True,
                )
                feed.thread.start()

    def _claim(self, device: str) -> int:
        info = os.stat(device)
        if not stat.S_ISCHR(info.st_mode):
            raise ValueError("device_unavailable")
        with self._claims_lock:
            if info.st_rdev in self._claims:
                raise ValueError("device_alias")
            self._claims.add(info.st_rdev)
        return info.st_rdev

    def _read_progress(self, feed: _Feed, process, generation: int) -> None:
        frame = out_us = None
        try:
            while not self._stop.is_set():
                line = process.stdout.readline(512)
                if not line:
                    break
                if len(line) >= 512 or not line.endswith(b"\n"):
                    continue
                name, separator, value = line.strip().partition(b"=")
                if not separator:
                    continue
                if name in (b"frame", b"out_time_us"):
                    try:
                        number = int(value)
                    except ValueError:
                        number = 0
                    if name == b"frame":
                        frame = number
                    else:
                        out_us = number
                elif name == b"progress":
                    if frame is not None and out_us is not None:
                        feed.progress(generation, frame, out_us)
                    frame = out_us = None
        except (OSError, ValueError):
            pass

    @staticmethod
    def _terminate(process) -> None:
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)

    def _run(self, feed: _Feed) -> None:
        while not self._stop.is_set():
            generation = feed.begin()
            identity = None
            process = reader = None
            error = "publisher_failed"
            try:
                try:
                    identity = self._claim(feed.camera.device)
                except OSError:
                    raise ValueError("device_unavailable") from None
                # Error text may contain RTSP credentials. Only machine progress is read.
                process = subprocess.Popen(
                    feed.args,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                )
                feed.process = process
                reader = threading.Thread(
                    target=self._read_progress, args=(feed, process, generation), daemon=True
                )
                reader.start()
                started = self._clock()
                while not self._stop.wait(0.25):
                    if process.poll() is not None:
                        error = "publisher_exited"
                        break
                    row = feed.telemetry()
                    if row["publisher_state"] == "failed":
                        error = "progress_stale"
                        break
                    if row["frames"] is None and self._clock() - started > STARTUP_SECONDS:
                        error = "progress_missing"
                        break
            except ValueError as exc:
                error = "device_alias" if str(exc) == "device_alias" else "device_unavailable"
            except OSError:
                error = "publisher_start_failed"
            finally:
                feed.finish(
                    "stopped" if self._stop.is_set() else "failed",
                    None if self._stop.is_set() else error,
                )
                try:
                    self._terminate(process)
                except (OSError, subprocess.TimeoutExpired):
                    feed.finish("failed", "publisher_cleanup_failed")
                    # Never launch a replacement if its predecessor may still own the input.
                    return
                if reader is not None:
                    reader.join(timeout=1)
                if process is not None and process.stdout is not None:
                    process.stdout.close()
                feed.process = None
                if identity is not None:
                    with self._claims_lock:
                        self._claims.discard(identity)
            self._stop.wait(2)

    def close(self) -> None:
        with self._lifecycle:
            self._stop.set()
        deadline = time.monotonic() + 6
        for feed in self._feeds:
            if feed.thread is not None:
                feed.thread.join(timeout=max(0, deadline - time.monotonic()))
