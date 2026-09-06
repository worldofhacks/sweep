"""Bot shell reply framing against a temporary UNIX socket server."""

import os
import shutil
import socket
import tempfile
import threading
import time

import pytest

from adapters.ohmni.spike import botshell


def wait_until(predicate, timeout=2.0):
    """Poll ``predicate`` without ``time.sleep`` so tests that patch it still work."""
    pause = threading.Event()
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            return False
        pause.wait(0.01)
    return True


class FakeBotShell:
    """A UNIX stream server that records command lines and answers from a script.

    ``replies`` maps a command line to a list of ``(bytes, delay_seconds)`` chunks so a test
    can split lines across writes and delay them. Serves connections one after another.
    """

    def __init__(self, replies):
        self.replies = replies
        self.received = []
        self.closing = False
        self.dir = tempfile.mkdtemp(prefix="sweep-bot-")
        self.path = os.path.join(self.dir, "bot_shell.sock")
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(self.path)
        self.server.listen(2)
        self.server.settimeout(0.2)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while not self.closing:
            try:
                conn, _ = self.server.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with conn:
                self._handle(conn)

    def _handle(self, conn):
        conn.settimeout(2)
        pending = b""
        while True:
            try:
                chunk = conn.recv(1024)
            except OSError:
                return
            if not chunk:
                return
            pending += chunk
            while b"\n" in pending:
                line, _, pending = pending.partition(b"\n")
                command = line.decode()
                self.received.append(command)
                for piece, delay in self.replies.get(command, []):
                    if delay:
                        threading.Event().wait(delay)
                    conn.sendall(piece)

    def close(self):
        self.closing = True
        try:
            self.server.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.server.close()
        self.thread.join(timeout=2)
        shutil.rmtree(self.dir, ignore_errors=True)


@pytest.fixture
def fake_shell():
    servers = []

    def make(replies):
        server = FakeBotShell(replies)
        servers.append(server)
        return server

    yield make
    for server in servers:
        server.close()


def test_command_reassembles_lines_split_across_writes(fake_shell):
    server = fake_shell(
        {"battery": [(b"batt", 0), (b"ery 87\r\ndocked", 0.05), (b" 1\n", 0)]},
    )
    with botshell.BotShell(server.path, timeout=0.3) as shell:
        reply = shell.command("battery")
    assert reply.lines == ["battery 87", "docked 1"]
    assert reply.latency_s is not None
    assert reply.latency_s < 0.3
    assert server.received == ["battery"]
    record = reply.as_record()
    assert record["command"] == "battery"
    assert record["latency_ms"] > 0
    assert record["sent_at"] == reply.sent_at


def test_silent_command_reports_no_latency(fake_shell):
    server = fake_shell({})
    with botshell.BotShell(server.path, timeout=0.1) as shell:
        reply = shell.command("init")
    assert reply.lines == []
    assert reply.latency_s is None
    assert reply.as_record()["latency_ms"] is None
    assert wait_until(lambda: server.received == ["init"])


def test_partial_line_waits_for_its_newline(fake_shell):
    server = fake_shell({"a": [(b"first\nsecond", 0)], "b": [(b" half\n", 0)]})
    with botshell.BotShell(server.path, timeout=0.1) as shell:
        assert shell.command("a").lines == ["first"]
        assert shell.command("b").lines == ["second half"]


def test_pulse_always_sends_stop(fake_shell, monkeypatch):
    server = fake_shell({})

    def broken_sleep(seconds):
        raise RuntimeError("interrupted mid-pulse")

    monkeypatch.setattr(botshell.time, "sleep", broken_sleep)
    with botshell.BotShell(server.path, timeout=0.1) as shell:
        with pytest.raises(RuntimeError):
            shell.pulse(300, -300, 500)
    assert wait_until(lambda: len(server.received) == 2)
    assert server.received == ["manual_move 300 -300", "manual_move 0 0"]


def test_pulse_caps_and_override():
    with pytest.raises(ValueError):
        botshell.check_pulse(900, 0, 100)
    with pytest.raises(ValueError):
        botshell.check_pulse(0, 0, 3000)
    with pytest.raises(ValueError):
        botshell.check_pulse(0, 0, 0, i_know=True)
    botshell.check_pulse(900, -900, 3000, i_know=True)
    botshell.check_pulse(800, 800, 2000)


def test_send_rejects_multiline_and_empty(fake_shell):
    server = fake_shell({})
    with botshell.BotShell(server.path, timeout=0.1) as shell:
        with pytest.raises(ValueError):
            shell.send("a\nb")
        with pytest.raises(ValueError):
            shell.send("   ")
    with pytest.raises(botshell.BotShellError):
        shell.send("battery")


def test_cli_battery_prints_reply_lines(fake_shell, capsys):
    server = fake_shell({"battery": [(b"battery 90 docked 0\n", 0)]})
    code = botshell.main(["--sock", server.path, "--timeout", "0.1", "battery"])
    out = capsys.readouterr().out
    assert code == 0
    assert "< battery 90 docked 0" in out
    assert "[battery] first byte after" in out


def test_cli_stop_and_raw_use_separate_connections(fake_shell):
    server = fake_shell({})
    assert botshell.main(["--sock", server.path, "--timeout", "0.05", "stop"]) == 0
    assert botshell.main(["--sock", server.path, "--timeout", "0.05", "raw", "say hi"]) == 0
    assert wait_until(lambda: len(server.received) == 2)
    assert server.received == ["manual_move 0 0", "say hi"]


def test_cli_missing_socket_returns_1(tmp_path):
    missing = str(tmp_path / "none.sock")
    assert botshell.main(["--sock", missing, "battery"]) == 1


def test_cli_refused_pulse_returns_2(fake_shell):
    server = fake_shell({})
    assert botshell.main(["--sock", server.path, "pulse", "2000", "0", "100"]) == 2
    assert server.received == []
