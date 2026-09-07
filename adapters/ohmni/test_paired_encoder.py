from __future__ import annotations

import json
import socket
import threading

import pytest

from .paired_encoder import EncoderStreamUnavailable, PairedEncoderStream


def _stream(path: str, events: list[dict[str, object]]) -> threading.Thread:
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


def test_paired_encoder_stream_requires_monotonic_bounded_owner_pairs(tmp_path) -> None:
    path = str(tmp_path / "encoder.sock")
    thread = _stream(
        path,
        [
            {
                "v": 1,
                "type": "sweep_encoder_pair",
                "poll_id": 3,
                "left": 5,
                "right": 6,
                "left_receipt_ns": "100",
                "right_receipt_ns": "350000101",
            },
            {
                "v": 1,
                "type": "sweep_encoder_pair",
                "poll_id": 4,
                "left": 7,
                "right": 8,
                "left_receipt_ns": "400",
                "right_receipt_ns": "350000400",
            },
        ],
    )
    stream = PairedEncoderStream(path)
    assert stream.read_pair(1).poll_id == 4
    stream.close()
    thread.join(timeout=1)


def test_paired_encoder_stream_withdraws_after_owner_reports_a_missing_reply(tmp_path) -> None:
    path = str(tmp_path / "encoder.sock")
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
