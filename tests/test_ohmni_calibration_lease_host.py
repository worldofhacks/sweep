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
