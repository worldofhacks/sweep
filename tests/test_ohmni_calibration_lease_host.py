from __future__ import annotations

import socket
import threading

from tools.ohmni_supervised_lidar_calibration import serve_lease


def test_host_lease_authenticates_then_expires_and_closes_the_actual_socket() -> None:
    token = b"k" * 32
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        errors = []

        def serve() -> None:
            try:
                serve_lease(listener, token, lifetime_s=0.4)
            except BaseException as error:
                errors.append(error)

        thread = threading.Thread(target=serve)
        thread.start()
        with socket.create_connection(listener.getsockname(), timeout=2) as client:
            client.sendall(token.hex().encode() + b"\n")
            with client.makefile("rb") as stream:
                lines = list(stream)
        thread.join(2)
    assert not thread.is_alive()
    assert errors == []
    assert len(lines) >= 1
    assert lines == [f"{index} {token.hex()}\n".encode() for index in range(1, len(lines) + 1)]


def test_wrong_token_receives_no_lease_ticks() -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        errors = []

        def serve() -> None:
            try:
                serve_lease(listener, b"k" * 32, lifetime_s=0.4)
            except ValueError as error:
                errors.append(str(error))

        thread = threading.Thread(target=serve)
        thread.start()
        with socket.create_connection(listener.getsockname(), timeout=2) as client:
            client.sendall((b"z" * 32).hex().encode() + b"\n")
            assert client.recv(1) == b""
        thread.join(2)
    assert not thread.is_alive()
    assert errors == ["lease authentication failed"]


def test_slow_send_does_not_accumulate_heartbeat_schedule_drift(monkeypatch) -> None:
    from tools import ohmni_supervised_lidar_calibration as host

    token = b"k" * 32
    clock = [0.0]
    sends = []
    options = []

    class Stream:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def readline(self, limit):
            return token.hex().encode() + b"\n"

    class Connection(Stream):
        def settimeout(self, timeout):
            pass

        def setsockopt(self, *option):
            options.append(option)

        def makefile(self, mode):
            return Stream()

        def sendall(self, data):
            sends.append(clock[0])
            clock[0] += 0.02

    class Listener:
        def settimeout(self, timeout):
            pass

        def accept(self):
            return Connection(), None

    monkeypatch.setattr(host.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        host.time, "sleep", lambda duration: clock.__setitem__(0, clock[0] + duration)
    )
    host.serve_lease(Listener(), token, lifetime_s=0.19)
    assert len(sends) == 4
    assert all(
        abs(actual - expected) < 1e-9 for actual, expected in zip(sends, [0, 0.05, 0.10, 0.15], strict=True)
    )
    assert (socket.IPPROTO_TCP, socket.TCP_NODELAY, 1) in options
