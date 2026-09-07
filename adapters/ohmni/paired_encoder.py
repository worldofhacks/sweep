"""Read paired absolute encoder samples emitted by the Telebot owner."""

from __future__ import annotations

import json
import socket
import time
from dataclasses import dataclass

MAX_PAIR_SKEW_NS = 350_000_000
MAX_PAIR_AGE_NS = 350_000_000
MAX_LINE_BYTES = 4096


class EncoderStreamUnavailable(OSError):
    pass


@dataclass(frozen=True)
class EncoderPair:
    poll_id: int
    left: int
    right: int
    left_receipt_ns: int
    right_receipt_ns: int


class PairedEncoderStream:
    def __init__(self, path: str) -> None:
        self.path = path
        self._socket: socket.socket | None = None
        self._buffer = b""
        self._last_poll_id = 0
        self._last_right_receipt_ns = 0

    def read_pair(self, timeout: float) -> EncoderPair | None:
        deadline = time.monotonic() + timeout
        while True:
            line = self._next_line(deadline)
            if line is None:
                return None
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict) and event.get("type") == "sweep_encoder_unavailable":
                self.close()
                reason = str(event.get("reason", "encoder stream unavailable"))
                raise EncoderStreamUnavailable(reason)
            pair = _parse_pair(event, self._last_poll_id)
            if pair is None or not _is_current_pair(
                pair, time.monotonic_ns(), self._last_right_receipt_ns
            ):
                continue
            self._last_poll_id = pair.poll_id
            self._last_right_receipt_ns = pair.right_receipt_ns
            return pair

    def _next_line(self, deadline: float) -> str | None:
        while True:
            newline = self._buffer.find(b"\n")
            if newline >= 0:
                line, self._buffer = self._buffer[:newline], self._buffer[newline + 1 :]
                return line.decode("utf-8", "strict")
            if len(self._buffer) > MAX_LINE_BYTES:
                self.close()
                raise EncoderStreamUnavailable("encoder stream line exceeds bound")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                if self._socket is None:
                    self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    self._socket.settimeout(remaining)
                    self._socket.connect(self.path)
                self._socket.settimeout(remaining)
                chunk = self._socket.recv(MAX_LINE_BYTES + 1 - len(self._buffer))
            except OSError:
                self.close()
                raise
            if not chunk:
                self.close()
                raise EncoderStreamUnavailable("encoder stream disconnected")
            self._buffer += chunk

    def close(self) -> None:
        if self._socket is not None:
            self._socket.close()
            self._socket = None
        self._buffer = b""


def _parse_pair(event: object, previous_poll_id: int) -> EncoderPair | None:
    if (
        not isinstance(event, dict)
        or event.get("v") != 1
        or event.get("type") != "sweep_encoder_pair"
    ):
        return None
    poll_id = event.get("poll_id")
    values = (event.get("left"), event.get("right"))
    receipts = (event.get("left_receipt_ns"), event.get("right_receipt_ns"))
    if (
        type(poll_id) is not int
        or poll_id <= previous_poll_id
        or any(type(value) is not int or not 0 <= value < 16384 for value in values)
        or any(not isinstance(receipt, str) or not receipt.isdecimal() for receipt in receipts)
    ):
        return None
    left_receipt, right_receipt = (int(receipt) for receipt in receipts)
    if abs(right_receipt - left_receipt) > MAX_PAIR_SKEW_NS:
        return None
    return EncoderPair(poll_id, values[0], values[1], left_receipt, right_receipt)


def _is_current_pair(pair: EncoderPair, now_ns: int, previous_right_receipt_ns: int) -> bool:
    return (
        pair.left_receipt_ns <= pair.right_receipt_ns <= now_ns
        and now_ns - pair.left_receipt_ns <= MAX_PAIR_AGE_NS
        and pair.right_receipt_ns > previous_right_receipt_ns
    )


def default_socket_path() -> str:
    return "/data/data/com.ohmnilabs.telebot_rtc/files/sweep_encoder.sock"
