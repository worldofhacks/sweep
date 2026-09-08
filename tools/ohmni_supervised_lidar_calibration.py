"""Serve a short-lived host lease for a separately staged calibration runner."""

from __future__ import annotations

import argparse
import hmac
import socket
import time
from pathlib import Path

LEASE_PORT = 18912
TICK_SECONDS = 0.1
MAX_RUNTIME_SECONDS = 90.0


def serve_lease(listener: socket.socket, token: bytes, *, lifetime_s: float = 60.0) -> None:
    """Serve one authenticated client; disconnect after the bounded lifetime."""
    if len(token) != 32 or not 0 < lifetime_s <= MAX_RUNTIME_SECONDS:
        raise ValueError("invalid bounded lease settings")
    encoded_token = token.hex().encode("ascii")
    deadline = time.monotonic() + lifetime_s
    listener.settimeout(min(10.0, lifetime_s))
    connection, _ = listener.accept()
    with connection:
        connection.settimeout(0.35)
        with connection.makefile("rb") as stream:
            if not hmac.compare_digest(stream.readline(66), encoded_token + b"\n"):
                raise ValueError("lease authentication failed")
            sequence = 0
            while time.monotonic() < deadline:
                sequence += 1
                connection.sendall(str(sequence).encode() + b" " + encoded_token + b"\n")
                time.sleep(min(TICK_SECONDS, max(0.0, deadline - time.monotonic())))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--token-file", required=True, type=Path)
    parser.add_argument("--port", type=int, default=LEASE_PORT)
    parser.add_argument("--lifetime-s", type=float, choices=(60.0, 90.0), default=60.0)
    args = parser.parse_args(argv)
    token = bytes.fromhex(args.token_file.read_text(encoding="ascii").strip())
    if len(token) != 32 or not 1024 <= args.port <= 65535:
        parser.error("a 256-bit token and an unprivileged TCP port are required")
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", args.port))
        listener.listen(1)
        print("Calibration lease listener ready on loopback.", flush=True)
        try:
            serve_lease(listener, token, lifetime_s=args.lifetime_s)
        except (OSError, ValueError):
            print("Calibration lease ended or was refused.", flush=True)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
