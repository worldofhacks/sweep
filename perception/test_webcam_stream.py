from __future__ import annotations

import multiprocessing as mp
import time
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from perception.webcam_stream import WebcamStream, _decode, _Mailbox


def test_mailbox_returns_latest_frame_and_copies_storage() -> None:
    mailbox = _Mailbox(mp.get_context("spawn"))
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    mailbox.put(frame, 1.0)
    frame[:] = 2
    mailbox.put(frame, 2.0)
    result = mailbox.get(0)
    assert result is not None
    image, timestamp = result
    assert timestamp == 2.0
    assert np.all(image == 2)
    assert mailbox.get(0) is None
    frame[:] = 3
    mailbox.put(frame, 3.0)
    assert np.all(image == 2)


def test_mailbox_preserves_decoder_receipt_timing() -> None:
    mailbox = _Mailbox(mp.get_context("spawn"))
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    mailbox.put(frame, 1.0, 9.0, False)

    result = mailbox.get_timed(0)

    assert result is not None
    assert result.captured_at_s == 1.0
    assert result.received_at_s == 9.0
    assert result.capture_time_verified is False


def test_abandoned_frame_lock_cannot_block_read() -> None:
    mailbox = _Mailbox(mp.get_context("spawn"))
    mailbox._available.set()
    mailbox._lock.acquire()
    try:
        started = time.monotonic()
        assert mailbox.get(0.02) is None
        assert time.monotonic() - started < 0.3
    finally:
        mailbox._lock.release()


class _Process:
    def __init__(self, *, start_error: bool = False) -> None:
        self.start_error = start_error
        self.alive = False
        self.joins: list[float] = []
        self.terminated = False
        self.killed = False
        self.closed = False

    def start(self) -> None:
        if self.start_error:
            raise RuntimeError("rtsp://user:secret@example/path")
        self.alive = True

    def is_alive(self) -> bool:
        return self.alive

    def join(self, timeout: float) -> None:
        self.joins.append(timeout)

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True
        self.alive = False

    def close(self) -> None:
        self.closed = True


def test_hung_decoder_does_not_block_poll_or_shutdown() -> None:
    stream = WebcamStream("rtsp://localhost/drone1")
    process = _Process()
    stream._process = process
    with stream:
        started = time.monotonic()
        assert stream.read(0.01) is None
        assert time.monotonic() - started < 0.3
    assert process.joins == [0.2, 0.5, 0.5]
    assert process.terminated and process.killed and process.closed
    assert stream.status == "stopped"
    stream.close()


def test_failed_start_is_redacted_and_closed() -> None:
    stream = WebcamStream("rtsp://user:secret@example/path")
    stream._process = _Process(start_error=True)
    with pytest.raises(RuntimeError, match="^cannot start webcam decoder$"):
        stream.start()
    assert stream.status == "stopped"
    with pytest.raises(RuntimeError, match="closed"):
        stream.start()


@pytest.mark.parametrize("timeout", [-1, 1.1, float("inf"), float("nan")])
def test_poll_timeout_cannot_exceed_heartbeat_bound(timeout: float) -> None:
    with pytest.raises(ValueError, match="timeout"):
        WebcamStream("rtsp://localhost/drone1").read(timeout)


@pytest.mark.parametrize("url", ["", "https://user:secret@example", None])
def test_non_rtsp_sources_are_rejected_without_echoing_credentials(url: object) -> None:
    with pytest.raises(ValueError, match="^webcam source must be an RTSP URL$"):
        WebcamStream(url)


def test_worker_checks_decoded_frames_and_reconnects_after_eof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("perception.webcam_stream.os.dup2", lambda *_: None)
    stop = mp.get_context("spawn").Event()
    state = SimpleNamespace(value=0)
    images = [
        np.zeros((360, 640, 3), dtype=np.uint8),
        np.zeros((720, 1280, 3), dtype=np.float32),
        np.zeros((720, 1280, 3), dtype=np.uint8),
    ]
    captures = []
    received = []

    class Capture:
        def __init__(self, *args: object) -> None:
            assert args[1] == cv2.CAP_FFMPEG
            assert args[2] == [
                cv2.CAP_PROP_OPEN_TIMEOUT_MSEC,
                1000,
                cv2.CAP_PROP_READ_TIMEOUT_MSEC,
                1000,
            ]
            self.frames = iter(images if not captures else [])
            self.released = False
            captures.append(self)
            if len(captures) == 2:
                stop.set()

        def isOpened(self) -> bool:
            return True

        def read(self) -> tuple[bool, np.ndarray | None]:
            frame = next(self.frames, None)
            return frame is not None, frame

        def release(self) -> None:
            self.released = True

    mailbox = SimpleNamespace(
        put=lambda image, captured_at_s, received_at_s, capture_time_verified: received.append(
            (image, captured_at_s, received_at_s, capture_time_verified)
        )
    )
    monkeypatch.setattr("perception.webcam_stream.cv2.VideoCapture", Capture)
    started = time.monotonic()
    _decode("rtsp://localhost/drone1", mailbox, stop, state)
    assert len(received) == 1
    assert received[0][0] is images[-1]
    assert started <= received[0][1] <= time.monotonic()
    assert received[0][1] == received[0][2]
    assert received[0][3] is False
    assert len(captures) == 2
    assert all(capture.released for capture in captures)
    assert state.value == 5


def test_decoder_open_errors_stay_generic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("perception.webcam_stream.os.dup2", lambda *_: None)
    stop = mp.get_context("spawn").Event()
    state = SimpleNamespace(value=0)

    def fail_open(*_: object) -> None:
        stop.set()
        raise cv2.error("rtsp://user:secret@example/path")

    monkeypatch.setattr("perception.webcam_stream.cv2.VideoCapture", fail_open)
    _decode("rtsp://user:secret@example/path", None, stop, state)
    assert state.value == 5


def test_close_before_start_prevents_reuse() -> None:
    stream = WebcamStream("rtsp://localhost/drone1")
    with pytest.raises(RuntimeError, match="not running"):
        stream.read()
    stream.close()
    stream.close()
    with pytest.raises(RuntimeError, match="closed"):
        stream.start()
