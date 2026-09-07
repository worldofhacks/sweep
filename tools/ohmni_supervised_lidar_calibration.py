"""Run the fixed, host-leased Ohmni11 LiDAR calibration capture over ADB."""

from __future__ import annotations

import argparse
import hmac
import os
import secrets
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path

LEASE_PORT = 18912
TICK_SECONDS = 0.1
MAX_RUNTIME_SECONDS = 60.0


class LeaseServer:
    def __init__(self, token: bytes, port: int) -> None:
        self.token, self.port = token, port
        self._closed = threading.Event()
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", port))
        self._listener.listen(1)
        self._listener.settimeout(TICK_SECONDS)
        self._thread = threading.Thread(target=self._serve, name="ohmni-calibration-host-lease")
        self._accepted = threading.Event()
        self.failure: str | None = None

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        self._closed.set()
        self._listener.close()
        if self._thread.is_alive():
            self._thread.join(timeout=1)

    def wait_for_client(self, timeout: float) -> bool:
        return self._accepted.wait(timeout)

    def _serve(self) -> None:
        deadline = time.monotonic() + MAX_RUNTIME_SECONDS
        try:
            connection = self._accept(deadline)
            if connection is None:
                self.failure = "calibration lease client did not connect"
                return
            with connection:
                connection.settimeout(1)
                token = connection.makefile("rb").readline(80).strip()
                if not hmac.compare_digest(token, self.token.hex().encode()):
                    self.failure = "calibration lease token refused"
                    return
                self._accepted.set()
                sequence = 0
                while not self._closed.is_set() and time.monotonic() < deadline:
                    sequence += 1
                    connection.sendall(f"{sequence} {self.token.hex()}\n".encode())
                    self._closed.wait(TICK_SECONDS)
        except OSError as error:
            if not self._closed.is_set():
                self.failure = str(error)

    def _accept(self, deadline: float) -> socket.socket | None:
        while not self._closed.is_set() and time.monotonic() < deadline:
            try:
                return self._listener.accept()[0]
            except TimeoutError:
                pass
        return None


def _write_token(path: Path) -> bytes:
    token = secrets.token_bytes(32)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(token.hex().encode() + b"\n")
    return token


def _adb(adb: str, serial: str, *args: str) -> None:
    subprocess.run([adb, "-s", serial, *args], check=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--remote-output", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--adb", default="adb")
    parser.add_argument("--lease-port", type=int, default=LEASE_PORT)
    args = parser.parse_args(argv)
    if args.output.exists():
        raise SystemExit("calibration output already exists")
    args.output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    token_file = args.output.parent / f".{args.output.name}.lease-token"
    token = _write_token(token_file)
    server = LeaseServer(token, args.lease_port)
    remote_token = "/data/local/sweep/calibration.lease-token"
    remote_process: subprocess.Popen[bytes] | None = None

    def stop(_signal: int, _frame: object) -> None:
        server.close()
        if remote_process is not None:
            _adb(
                args.adb,
                args.serial,
                "shell",
                "su",
                "0",
                "pkill",
                "-TERM",
                "-f",
                "adapters.ohmni.calibration",
            )

    previous = signal.signal(signal.SIGTERM, stop)
    try:
        server.start()
        _adb(args.adb, args.serial, "reverse", f"tcp:{args.lease_port}", f"tcp:{args.lease_port}")
        _adb(args.adb, args.serial, "push", str(token_file), remote_token)
        _adb(args.adb, args.serial, "shell", "su", "0", "chmod", "600", remote_token)
        remote_process = subprocess.Popen(
            [
                args.adb,
                "-s",
                args.serial,
                "shell",
                "su",
                "0",
                "sh",
                "-c",
                (
                    "cd /data/local/sweep && "
                    "./lib/ld-musl-x86_64.so.1 ./python/bin/python3.12 "
                    "-m adapters.ohmni.calibration "
                    f"--lease-port {args.lease_port} --lease-token-file {remote_token} "
                    f"--output {args.remote_output}"
                ),
            ]
        )
        if not server.wait_for_client(5):
            raise RuntimeError(server.failure or "calibration lease was not accepted")
        if remote_process.wait(MAX_RUNTIME_SECONDS + 10) != 0:
            raise RuntimeError("remote calibration runner failed")
        _adb(args.adb, args.serial, "pull", args.remote_output, str(args.output))
        return 0
    finally:
        server.close()
        if remote_process is not None and remote_process.poll() is None:
            _adb(
                args.adb,
                args.serial,
                "shell",
                "su",
                "0",
                "pkill",
                "-TERM",
                "-f",
                "adapters.ohmni.calibration",
            )
            remote_process.wait(timeout=5)
        token_file.unlink(missing_ok=True)
        signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    raise SystemExit(main())
