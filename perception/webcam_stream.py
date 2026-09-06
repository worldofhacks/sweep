"""Bounded RTSP decoding for the offline localization preview."""

from __future__ import annotations

import math
import multiprocessing as mp
import os
import time
from typing import Any

import cv2
import numpy as np

_STATUSES = ("starting", "connecting", "live", "reconnecting", "invalid_frame", "stopped")


class _Mailbox:
    def __init__(self, context: Any) -> None:
        self._pixels = context.RawArray("B", 720 * 1280 * 3)
        self._timestamp = context.RawValue("d", 0)
        self._lock = context.Lock()
        self._available = context.Event()

    def put(self, frame: np.ndarray, timestamp: float) -> None:
        if not self._lock.acquire(timeout=0.01):
            return
        try:
            np.copyto(np.frombuffer(self._pixels, dtype=np.uint8).reshape(720, 1280, 3), frame)
            self._timestamp.value = timestamp
            self._available.set()
        finally:
            self._lock.release()

    def get(self, timeout: float) -> tuple[np.ndarray, float] | None:
        deadline = time.monotonic() + timeout
        if not self._available.wait(timeout):
            return None
        if not self._lock.acquire(timeout=max(0, deadline - time.monotonic())):
            return None
        try:
            frame = np.frombuffer(self._pixels, dtype=np.uint8).reshape(720, 1280, 3).copy()
            timestamp = self._timestamp.value
            self._available.clear()
            return frame, timestamp
        finally:
            self._lock.release()


def _decode(url: str, mailbox: Any, stop: Any, state: Any) -> None:
    # FFmpeg error messages can include the authenticated source URL.
    with open(os.devnull, "w") as sink:
        os.dup2(sink.fileno(), 2)
    while not stop.is_set():
        capture = None
        state.value = 1
        try:
            capture = cv2.VideoCapture(
                url,
                cv2.CAP_FFMPEG,
                [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 1000, cv2.CAP_PROP_READ_TIMEOUT_MSEC, 1000],
            )
            if capture.isOpened():
                while not stop.is_set():
                    ok, frame = capture.read()
                    decoded_at = time.monotonic()
                    if not ok:
                        break
                    if (
                        not isinstance(frame, np.ndarray)
                        or frame.shape != (720, 1280, 3)
                        or frame.dtype != np.uint8
                    ):
                        state.value = 4
                        continue
                    mailbox.put(frame, decoded_at)
                    state.value = 2
        except Exception:
            pass
        finally:
            if capture is not None:
                capture.release()
        state.value = 3
        stop.wait(0.5)
    state.value = 5


class WebcamStream:
    def __init__(self, url: str) -> None:
        if not isinstance(url, str) or not url.startswith(("rtsp://", "rtsps://")):
            raise ValueError("webcam source must be an RTSP URL")
        self._url = url
        context = mp.get_context("spawn")
        self._mailbox = _Mailbox(context)
        self._stop = context.Event()
        self._state = context.RawValue("i", 0)
        self._process = context.Process(
            target=_decode,
            args=(url, self._mailbox, self._stop, self._state),
            daemon=True,
        )
        self._started = False
        self._closed = False

    @property
    def status(self) -> str:
        if self._closed or (self._started and not self._process.is_alive()):
            return "stopped"
        return _STATUSES[self._state.value]

    def start(self) -> WebcamStream:
        if self._closed:
            raise RuntimeError("webcam stream is closed")
        if not self._started:
            try:
                self._process.start()
            except Exception:
                self.close()
                raise RuntimeError("cannot start webcam decoder") from None
            self._started = True
        return self

    def read(self, timeout: float = 0.1) -> tuple[np.ndarray, float] | None:
        """Return a BGR frame and host decode-completion time, not camera capture time."""
        if not math.isfinite(timeout) or not 0 <= timeout <= 1:
            raise ValueError("read timeout must be between zero and one second")
        if not self._started or self._closed:
            raise RuntimeError("webcam stream is not running")
        return self._mailbox.get(timeout=timeout)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        if self._started:
            self._process.join(0.2)
            if self._process.is_alive():
                self._process.terminate()
                self._process.join(0.5)
            if self._process.is_alive():
                self._process.kill()
                self._process.join(0.5)
            if not self._process.is_alive():
                self._process.close()

    def __enter__(self) -> WebcamStream:
        return self.start()

    def __exit__(self, *_: object) -> None:
        self.close()
