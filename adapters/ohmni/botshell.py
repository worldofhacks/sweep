"""Bounded, content-matched UNIX bot-shell client. Never touch the FT230X wheel bus."""

from __future__ import annotations

import re
import socket
import threading
import time

DEFAULT_PATH = "/data/data/com.ohmnilabs.telebot_rtc/files/bot_shell.sock"


class BotShell:
    def __init__(self, path: str = DEFAULT_PATH) -> None:
        self.path = path
        self._socket: socket.socket | None = None
        self._lock = threading.Lock()

    def command(self, text: str, *, expected: str | None = None, timeout: float = 0.04) -> str:
        if "\n" in text or "\r" in text:
            raise ValueError("one bot-shell command per call")
        with self._lock:
            try:
                if self._socket is None:
                    self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    self._socket.settimeout(0.15)
                    self._socket.connect(self.path)
                sock = self._socket
                # Drain unsolicited old reports before issuing a fresh encoder query.
                sock.setblocking(False)
                try:
                    while sock.recv(8192):
                        pass
                except BlockingIOError:
                    pass
                sock.settimeout(timeout)
                sock.sendall((text + "\n").encode())
                if expected is None:
                    return ""
                deadline = time.monotonic() + timeout
                result = ""
                while time.monotonic() < deadline:
                    sock.settimeout(max(0.001, deadline - time.monotonic()))
                    try:
                        chunk = sock.recv(8192)
                    except TimeoutError:
                        break
                    if not chunk:
                        raise ConnectionError("bot-shell socket closed")
                    result += chunk.decode("utf-8", "replace")
                    if re.search(expected, result):
                        return result
                    if len(result) > 32768:
                        raise OSError("bot-shell reply exceeded the bounded buffer")
                return result
            except OSError:
                if self._socket is not None:
                    self._socket.close()
                    self._socket = None
                raise

    def close(self) -> None:
        with self._lock:
            if self._socket is not None:
                self._socket.close()
                self._socket = None
