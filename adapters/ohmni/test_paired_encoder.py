from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Iterator
from tempfile import TemporaryDirectory

import pytest

from .odometry import MAX_SAMPLE_GAP_S, Odometry
from .paired_encoder import (
    MAX_LINE_BYTES,
    EncoderPair,
    EncoderStreamUnavailable,
    PairedEncoderStream,
)


@pytest.fixture
def encoder_socket_path() -> Iterator[str]:
    # macOS pytest roots can exceed AF_UNIX's sun_path limit before the filename.
    # Each test owns a short private directory; cleanup also removes the socket.
    with TemporaryDirectory(prefix="sweep-enc-", dir="/tmp") as directory:
        yield f"{directory}/encoder.sock"


def _stream(path: str, events: list[object]) -> threading.Thread:
    ready = threading.Event()

    def serve() -> None:
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(path)
        listener.listen(1)
        ready.set()
        connection, _ = listener.accept()
        with listener, connection:
            for event in events:
                connection.sendall((json.dumps(event) + "\n").encode())

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    assert ready.wait(1)
    return thread


def test_paired_encoder_stream_requires_monotonic_bounded_owner_pairs(
    encoder_socket_path: str,
) -> None:
    path = encoder_socket_path
    now = time.monotonic_ns()
    thread = _stream(
        path,
        [
            {
                "v": 1,
                "type": "sweep_encoder_pair",
                "poll_id": 3,
                "left": 5,
                "right": 6,
                "left_receipt_ns": str(now - 360_000_001),
                "right_receipt_ns": str(now - 10_000_000),
            },
            {
                "v": 1,
                "type": "sweep_encoder_pair",
                "poll_id": 4,
                "left": 7,
                "right": 8,
                "left_receipt_ns": str(now - 20_000_000),
                "right_receipt_ns": str(now - 5_000_000),
            },
        ],
    )
    stream = PairedEncoderStream(path)
    assert stream.read_pair(1).poll_id == 4
    stream.close()
    thread.join(timeout=1)


def test_paired_encoder_stream_ignores_non_object_json_without_killing_odometry(
    encoder_socket_path: str,
) -> None:
    path = encoder_socket_path
    now = time.monotonic_ns()
    thread = _stream(
        path,
        [
            ["not", "an", "event"],
            {
                "v": 1,
                "type": "sweep_encoder_pair",
                "poll_id": 1,
                "left": 5,
                "right": 6,
                "left_receipt_ns": str(now - 20_000_000),
                "right_receipt_ns": str(now - 10_000_000),
            },
        ],
    )
    stream = PairedEncoderStream(path)
    assert stream.read_pair(1).poll_id == 1
    stream.close()
    thread.join(timeout=1)


def test_paired_encoder_stream_rejects_stale_future_and_reversed_receipts(
    encoder_socket_path: str,
) -> None:
    path = encoder_socket_path
    now = time.monotonic_ns()
    thread = _stream(
        path,
        [
            {
                "v": 1,
                "type": "sweep_encoder_pair",
                "poll_id": 1,
                "left": 1,
                "right": 2,
                "left_receipt_ns": str(now - 360_000_000),
                "right_receipt_ns": str(now - 360_000_000),
            },
            {
                "v": 1,
                "type": "sweep_encoder_pair",
                "poll_id": 2,
                "left": 3,
                "right": 4,
                "left_receipt_ns": str(now + 10_000_000_000),
                "right_receipt_ns": str(now + 10_000_000_001),
            },
            {
                "v": 1,
                "type": "sweep_encoder_pair",
                "poll_id": 3,
                "left": 5,
                "right": 6,
                "left_receipt_ns": str(now - 1_000_000),
                "right_receipt_ns": str(now - 2_000_000),
            },
            {
                "v": 1,
                "type": "sweep_encoder_pair",
                "poll_id": 4,
                "left": 7,
                "right": 8,
                "left_receipt_ns": str(now - 20_000_000),
                "right_receipt_ns": str(now - 10_000_000),
            },
        ],
    )
    stream = PairedEncoderStream(path)
    assert stream.read_pair(1).poll_id == 4
    stream.close()
    thread.join(timeout=1)


def test_paired_encoder_stream_rejects_receipts_that_go_backwards_between_polls(
    encoder_socket_path: str,
) -> None:
    path = encoder_socket_path
    now = time.monotonic_ns()
    thread = _stream(
        path,
        [
            {
                "v": 1,
                "type": "sweep_encoder_pair",
                "poll_id": 1,
                "left": 1,
                "right": 2,
                "left_receipt_ns": str(now - 50_000_000),
                "right_receipt_ns": str(now - 40_000_000),
            },
            {
                "v": 1,
                "type": "sweep_encoder_pair",
                "poll_id": 2,
                "left": 3,
                "right": 4,
                "left_receipt_ns": str(now - 47_000_000),
                "right_receipt_ns": str(now - 45_000_000),
            },
            {
                "v": 1,
                "type": "sweep_encoder_pair",
                "poll_id": 3,
                "left": 5,
                "right": 6,
                "left_receipt_ns": str(now - 20_000_000),
                "right_receipt_ns": str(now - 10_000_000),
            },
        ],
    )
    stream = PairedEncoderStream(path)
    assert stream.read_pair(1).poll_id == 1
    assert stream.read_pair(1).poll_id == 3
    stream.close()
    thread.join(timeout=1)


def test_paired_encoder_stream_never_calls_recv_with_zero_bytes() -> None:
    class ExactBoundSocket:
        def __init__(self) -> None:
            self.requests: list[int] = []

        def settimeout(self, _timeout: float) -> None:
            pass

        def recv(self, size: int) -> bytes:
            self.requests.append(size)
            return b"x"

        def close(self) -> None:
            pass

    stream = PairedEncoderStream("unused")
    socket_at_bound = ExactBoundSocket()
    stream._socket = socket_at_bound  # type: ignore[assignment]
    stream._buffer = b"x" * MAX_LINE_BYTES
    with pytest.raises(EncoderStreamUnavailable, match="exceeds bound"):
        stream._next_line(time.monotonic() + 1)
    assert socket_at_bound.requests == [1]


def test_odometry_uses_owner_receipt_time_for_freshness() -> None:
    odometry = Odometry(object(), (0.0, 0.0, 0.0))
    odometry._update_paired_sample(EncoderPair(1, 10, 10, 900_000_000, 1_000_000_000))
    odometry._update_paired_sample(EncoderPair(2, 11, 11, 1_000_000_000, 1_100_000_000))
    assert odometry.updated == pytest.approx(1.1)
    assert odometry.snapshot(1.1 + MAX_SAMPLE_GAP_S + 0.001).quality == 0.0


def test_paired_encoder_stream_withdraws_after_owner_reports_a_missing_reply(
    encoder_socket_path: str,
) -> None:
    path = encoder_socket_path
    thread = _stream(
        path,
        [
            {
                "v": 1,
                "type": "sweep_encoder_unavailable",
                "poll_id": 5,
                "reason": "missing_encoder_reply",
            }
        ],
    )
    stream = PairedEncoderStream(path)
    with pytest.raises(EncoderStreamUnavailable, match="missing_encoder_reply"):
        stream.read_pair(1)
    thread.join(timeout=1)
