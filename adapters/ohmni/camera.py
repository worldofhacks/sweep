"""Verified V4L capture and RTSP publishing for an Ohmni camera."""

from __future__ import annotations

import hashlib
import hmac
import math
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from urllib.parse import quote

MAX_DEVICE_ID = 64
FRAME_FRESHNESS_S = 3.0
STREAM_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")


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


def publish_url(host: str, device_id: int, key: str, *, stream: str | None = None) -> str:
    if not host or any(character in host for character in "/@?#\r\n"):
        raise ValueError("media host must be a hostname[:port]")
    path = stream_name(device_id)
    if stream is not None:
        if not isinstance(stream, str) or STREAM_PATTERN.fullmatch(stream) is None:
            raise ValueError("camera stream must be a bounded flat MediaMTX path")
        path = stream
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


def from_environment(host: str, key: str) -> Camera:
    if not key:
        raise ValueError("camera publishing requires a node key")
    try:
        raw_fps = os.environ["SWEEP_CAMERA_INPUT_FPS"]
        source = V4LSource(
            device=os.environ["SWEEP_CAMERA_DEVICE"],
            input_format=os.environ["SWEEP_CAMERA_INPUT_FORMAT"],
            input_fps=None if raw_fps == "native" else int(raw_fps),
            width=int(os.environ["SWEEP_CAMERA_WIDTH_PX"]),
            height=int(os.environ["SWEEP_CAMERA_HEIGHT_PX"]),
        )
        device_id = int(os.environ["SWEEP_DEVICE_UNIT"])
    except KeyError as error:
        raise ValueError(f"camera publishing requires {error.args[0]}") from error
    except ValueError as error:
        raise ValueError("camera publishing configuration is invalid") from error
    return Camera(
        host,
        device_id,
        key,
        os.environ.get("SWEEP_FFMPEG", "/data/local/sweep/ffmpeg"),
        source,
        stream=os.environ.get("SWEEP_CAMERA_STREAM"),
    )


class Camera:
    def __init__(
        self,
        host: str,
        device_id: int,
        key: str,
        ffmpeg: str,
        source: V4LSource,
        *,
        stream: str | None = None,
        monotonic=time.monotonic,
    ) -> None:
        self._command = command(ffmpeg, publish_url(host, device_id, key, stream=stream), source)
        self._monotonic = monotonic
        self._process: subprocess.Popen[bytes] | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._frames_seen = 0
        self._progress_updates = 0
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
                    self._progress_updates += 1
                    self._last_frame_at = self._monotonic()

    def _stop_process(self, process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)

    def probe(self, timeout_s: float = 10.0) -> dict[str, object]:
        """One owned attempt, with bounded cleanup and credential-free diagnostics.

        Two advancing progress samples establish local producer output only. The
        caller must independently verify MediaMTX receipt and actual playback.
        """
        if (
            isinstance(timeout_s, bool)
            or not isinstance(timeout_s, (int, float))
            or not math.isfinite(timeout_s)
            or not 1 <= timeout_s <= 30
        ):
            raise ValueError("camera probe timeout must be 1 through 30 seconds")
        with self._lock:
            if self._thread.is_alive() or self._process is not None or self._stop.is_set():
                raise ValueError("camera probe requires an idle, open owner")
            self._frames_seen = self._progress_updates = 0
            self._last_frame_at = None
            self._state = "connecting"
        result: dict[str, object] = {
            "status": "failed",
            "reason": "publisher_start_failed",
            "frames_seen": 0,
            "progress_updates": 0,
            "cleanup_confirmed": True,
        }
        process = observer = None
        try:
            process = subprocess.Popen(
                self._command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            with self._lock:
                self._process = process
            observer = threading.Thread(target=self._observe_progress, args=(process,), daemon=True)
            observer.start()
            deadline = self._monotonic() + timeout_s
            while True:
                if self._stop.is_set():
                    result["reason"] = "interrupted"
                    break
                if process.poll() is not None:
                    result["reason"] = "publisher_exited"
                    break
                with self._lock:
                    updates = self._progress_updates
                if updates >= 2 and self.state == "publishing":
                    result.update(status="frames_observed", reason=None)
                    break
                if self._monotonic() >= deadline:
                    result["reason"] = "progress_timeout"
                    break
                self._stop.wait(0.05)
        except OSError:
            # The exception or ffmpeg stderr may contain a credential-bearing URL.
            result["reason"] = "publisher_start_failed"
        finally:
            if process is not None:
                try:
                    self._stop_process(process)
                except (OSError, subprocess.TimeoutExpired):
                    result.update(status="failed", reason="cleanup_failed", cleanup_confirmed=False)
                if result["cleanup_confirmed"]:
                    if observer is not None:
                        observer.join(timeout=1)
                    if process.stderr is not None and not (observer and observer.is_alive()):
                        process.stderr.close()
            with self._lock:
                result["frames_seen"] = self._frames_seen
                result["progress_updates"] = self._progress_updates
                self._state = "stopped" if result["cleanup_confirmed"] else "failed"
                if result["cleanup_confirmed"]:
                    self._process = None
        return result

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
                self._progress_updates = 0
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
            try:
                self._stop_process(process)
            except (OSError, subprocess.TimeoutExpired):
                with self._lock:
                    self._state = "failed"
                return  # Never replace a publisher whose cleanup is unconfirmed.
            observer.join(timeout=1)
            with self._lock:
                self._process = None
                self._state = "failed"
            self._stop.wait(2.0)
        with self._lock:
            self._state = "stopped"

    def request_stop(self) -> None:
        self._stop.set()

    def close(self) -> None:
        self.request_stop()
        if self._thread.is_alive():
            self._thread.join(timeout=6)
