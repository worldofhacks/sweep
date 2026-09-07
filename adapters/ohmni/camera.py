"""Verified V4L capture and RTSP publishing for an Ohmni camera."""

from __future__ import annotations

import hashlib
import hmac
import subprocess
import threading
import time
from dataclasses import dataclass
from urllib.parse import quote

MAX_DEVICE_ID = 64
FRAME_FRESHNESS_S = 3.0


@dataclass(frozen=True)
class V4LSource:
    device: str
    input_format: str
    input_fps: int | None
    width: int
    height: int

    def __post_init__(self) -> None:
        if (
            not self.device.startswith("/dev/video")
            or not self.device.removeprefix("/dev/video").isdigit()
        ):
            raise ValueError("camera device must be a V4L video node")
        if self.input_format not in {"uyvy422", "mjpeg"}:
            raise ValueError("camera input format is not approved")
        if self.input_fps is not None and not 1 <= self.input_fps <= 120:
            raise ValueError("camera input FPS must be from 1 through 120 or native")
        if not 1 <= self.width <= 4_096 or not 1 <= self.height <= 4_096:
            raise ValueError("camera dimensions are invalid")


def stream_name(device_id: int) -> str:
    if (
        not isinstance(device_id, int)
        or isinstance(device_id, bool)
        or not 1 <= device_id <= MAX_DEVICE_ID
    ):
        raise ValueError(f"device ID must be an integer from 1 through {MAX_DEVICE_ID}")
    return f"drone{device_id}"


def publish_url(host: str, device_id: int, key: str) -> str:
    if not host or any(character in host for character in "/@?#\r\n"):
        raise ValueError("media host must be a hostname[:port]")
    path = stream_name(device_id)
    password = hmac.new(
        key.encode(), f"sweep-media-publish-v1:{path}".encode(), hashlib.sha256
    ).hexdigest()
    origin = host if ":" in host else f"{host}:8554"
    return f"rtsp://{path}:{quote(password, safe='')}@{origin}/{path}"


def command(ffmpeg: str, url: str, source: V4LSource) -> list[str]:
    values = [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-progress",
        "pipe:2",
        "-stats_period",
        "0.5",
        "-nostats",
        "-f",
        "v4l2",
        "-input_format",
        source.input_format,
        "-video_size",
        f"{source.width}x{source.height}",
    ]
    if source.input_fps is not None:
        values.extend(("-framerate", str(source.input_fps)))
    values.extend(
        (
            "-i",
            source.device,
            "-r",
            "15",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-tune",
            "zerolatency",
            "-pix_fmt",
            "yuv420p",
            "-b:v",
            "800k",
            "-g",
            "15",
            "-rtsp_transport",
            "tcp",
            "-f",
            "rtsp",
            url,
        )
    )
    return values


class Camera:
    def __init__(
        self,
        host: str,
        device_id: int,
        key: str,
        ffmpeg: str,
        source: V4LSource,
        *,
        monotonic=time.monotonic,
    ) -> None:
        self._command = command(ffmpeg, publish_url(host, device_id, key), source)
        self._monotonic = monotonic
        self._process: subprocess.Popen[bytes] | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._frames_seen = 0
        self._last_frame_at: float | None = None
        self._thread = threading.Thread(target=self._run, name="ohmni-camera", daemon=True)
        self._state = "stopped"

    @property
    def state(self) -> str:
        with self._lock:
            process = self._process
            if self._stop.is_set():
                return "stopped"
            if process is None or process.poll() is not None:
                return self._state
            if self._frames_seen == 0:
                return "connecting"
            if (
                self._last_frame_at is not None
                and self._monotonic() - self._last_frame_at <= FRAME_FRESHNESS_S
            ):
                return "publishing"
            return "failed"

    def start(self) -> None:
        self._thread.start()

    def _observe_progress(self, process: subprocess.Popen[bytes]) -> None:
        if process.stderr is None:
            return
        for line in process.stderr:
            if not line.startswith(b"frame="):
                continue
            try:
                count = int(line.removeprefix(b"frame=").strip())
            except ValueError:
                continue
            if count <= 0:
                continue
            with self._lock:
                if count > self._frames_seen:
                    self._frames_seen = count
                    self._last_frame_at = self._monotonic()

    def _stop_process(self, process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                process = subprocess.Popen(
                    self._command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                )
            except OSError:
                with self._lock:
                    self._state = "failed"
                self._stop.wait(2.0)
                continue

            with self._lock:
                self._process = process
                self._frames_seen = 0
                self._last_frame_at = None
                self._state = "connecting"
            observer = threading.Thread(
                target=self._observe_progress,
                args=(process,),
                name="ohmni-camera-progress",
                daemon=True,
            )
            observer.start()
            while not self._stop.wait(0.5) and process.poll() is None:
                pass
            self._stop_process(process)
            observer.join(timeout=1)
            with self._lock:
                self._process = None
                self._state = "failed"
            self._stop.wait(2.0)
        with self._lock:
            self._state = "stopped"

    def close(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=3)
