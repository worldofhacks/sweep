"""See3CAM UVC capture and software H.264, publishing RTSP over TCP to MediaMTX."""

from __future__ import annotations

import hashlib
import hmac
import subprocess
import threading
from urllib.parse import quote


def publish_url(host: str, unit: int, key: str) -> str:
    if not 1 <= unit <= 4 or not host or any(c in host for c in "/@?#\r\n"):
        raise ValueError("media host must be a hostname[:port]; unit must be 1..4")
    path = f"ground{unit}"
    password = hmac.new(
        key.encode(), f"sweep-media-publish-v1:{path}".encode(), hashlib.sha256
    ).hexdigest()
    origin = host if ":" in host else f"{host}:8554"
    return f"rtsp://{path}:{quote(password, safe='')}@{origin}/{path}"


def command(ffmpeg: str, url: str) -> list[str]:
    return [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "v4l2",
        "-input_format",
        "uyvy422",
        "-video_size",
        "640x480",
        "-framerate",
        "30",
        "-i",
        "/dev/video0",
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
    ]


class Camera:
    def __init__(self, host: str, unit: int, key: str, ffmpeg: str) -> None:
        self._command = command(ffmpeg, publish_url(host, unit, key))
        self._process: subprocess.Popen | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="ohmni-camera", daemon=True)
        self.state = "stopped"

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            self.state = "connecting"
            try:
                # ffmpeg error text may contain the credential-bearing URL; never log it.
                self._process = subprocess.Popen(
                    self._command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                while not self._stop.wait(0.5) and self._process.poll() is None:
                    self.state = "publishing"
            except OSError:
                pass
            self.state = "failed"
            if self._process is not None and self._process.poll() is None:
                self._process.terminate()
                try:
                    self._process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self._process.kill()
                    self._process.wait()
            self._process = None
            self._stop.wait(2.0)
        self.state = "stopped"

    def close(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=3)
