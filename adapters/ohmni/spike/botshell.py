#!/usr/bin/env python3
"""Bot shell client for the Ohmni telebot socket ``/app/bot_shell.sock``.

The bot shell speaks a shell-like, newline-terminated text protocol: send ``say hello\\n``.
Reply formats are not documented, so ``BotShell.command`` returns whatever lines arrive
until the socket has been idle for the timeout, together with the time to the first reply
byte. Runs on the container's Python 3.6 with the standard library only.

    python3 botshell.py battery
    python3 botshell.py say "hello"
    python3 botshell.py raw "scan_lidar_device"
    python3 botshell.py stop
    python3 botshell.py pulse 300 300 500

``pulse`` sends ``manual_move l r``, waits, and always sends ``manual_move 0 0``, because
``manual_move`` does not stop by itself. Speeds above 800 and pulses above 2000 ms are
refused unless ``--i-know`` is given; the unit of ``manual_move`` speeds is not documented.
"""

import argparse
import socket
import sys
import time

DEFAULT_SOCK = "/app/bot_shell.sock"
DEFAULT_TIMEOUT = 0.5
PULSE_MAX_MS = 2000
PULSE_MAX_SPEED = 800
STOP_COMMAND = "manual_move 0 0"


class BotShellError(Exception):
    """The bot shell socket is unavailable or a command could not be delivered."""


class Reply:
    """Lines received after one command and the latency to the first reply byte (seconds)."""

    __slots__ = ("command", "lines", "latency_s", "sent_at")

    def __init__(self, command, lines, latency_s, sent_at):
        self.command = command
        self.lines = lines
        self.latency_s = latency_s
        self.sent_at = sent_at

    def as_record(self):
        return {
            "command": self.command,
            "lines": self.lines,
            "latency_ms": None if self.latency_s is None else round(self.latency_s * 1000, 2),
            "sent_at": self.sent_at,
        }


class BotShell:
    """A connection to the bot shell; use as a context manager so the socket closes."""

    def __init__(self, path=DEFAULT_SOCK, timeout=DEFAULT_TIMEOUT):
        self.path = path
        self.timeout = timeout
        self._sock = None
        self._pending = b""

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect(self.path)
        except OSError as exc:
            sock.close()
            raise BotShellError(f"cannot connect to bot shell at {self.path}: {exc}") from exc
        self._sock = sock
        return self

    def close(self):
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def __enter__(self):
        if self._sock is None:
            self.connect()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    @property
    def connected(self):
        return self._sock is not None

    def send(self, command):
        """Send one command line; returns the wall-clock send time (``time.time``)."""
        line = command.strip()
        if not line or "\n" in line:
            raise ValueError("a bot shell command is one non-empty line")
        if self._sock is None:
            raise BotShellError("not connected")
        sent_at = time.time()
        try:
            self._sock.sendall((line + "\n").encode("utf-8"))
        except OSError as exc:
            raise BotShellError(f"send failed: {exc}") from exc
        return sent_at

    def read_lines(self, timeout=None):
        """Read lines until the socket is idle for ``timeout`` seconds (default: the client's)."""
        lines, _ = self._read(self.timeout if timeout is None else timeout)
        return lines

    def command(self, command, timeout=None):
        """Send ``command`` and collect the reply lines; latency is to the first byte back."""
        sent_at = self.send(command)
        started = time.monotonic()
        lines, first_byte = self._read(self.timeout if timeout is None else timeout)
        latency = None if first_byte is None else first_byte - started
        return Reply(command.strip(), lines, latency, sent_at)

    def stop(self, timeout=None):
        """``manual_move 0 0``: the only way to stop a ``manual_move``."""
        return self.command(STOP_COMMAND, timeout=timeout)

    def pulse(self, left, right, duration_ms, i_know=False):
        """Drive ``manual_move left right`` for ``duration_ms``, then always stop."""
        check_pulse(left, right, duration_ms, i_know)
        replies = []
        try:
            replies.append(self.command(f"manual_move {left} {right}", timeout=0.05))
            time.sleep(duration_ms / 1000.0)
        finally:
            replies.append(self.stop())
        return replies

    def _read(self, idle_timeout):
        if self._sock is None:
            raise BotShellError("not connected")
        lines = []
        first_byte = None
        self._sock.settimeout(idle_timeout)
        while True:
            try:
                chunk = self._sock.recv(4096)
            except socket.timeout:  # noqa: UP041 (not an alias of TimeoutError before 3.10)
                break
            except OSError as exc:
                raise BotShellError(f"recv failed: {exc}") from exc
            if not chunk:
                self.close()
                break
            if first_byte is None:
                first_byte = time.monotonic()
            self._pending += chunk
            while b"\n" in self._pending:
                line, _, self._pending = self._pending.partition(b"\n")
                lines.append(line.decode("utf-8", "replace").rstrip("\r"))
        return lines, first_byte


def check_pulse(left, right, duration_ms, i_know=False):
    """Refuse pulses outside the spike caps unless the operator opts out."""
    if duration_ms <= 0:
        raise ValueError("pulse duration must be positive")
    if i_know:
        return
    if duration_ms > PULSE_MAX_MS:
        raise ValueError(f"pulse of {duration_ms} ms exceeds {PULSE_MAX_MS} ms; pass --i-know")
    if max(abs(left), abs(right)) > PULSE_MAX_SPEED:
        raise ValueError(f"speed above {PULSE_MAX_SPEED} refused; pass --i-know")


def print_reply(reply, stream=None):
    """Print reply lines and the latency; ``stream`` defaults to the current ``sys.stdout``."""
    if stream is None:
        stream = sys.stdout
    for line in reply.lines:
        print(f"< {line}", file=stream)
    if reply.latency_s is None:
        print(f"[{reply.command}] no reply", file=stream)
    else:
        print(
            f"[{reply.command}] first byte after {reply.latency_s * 1000:.1f} ms, "
            f"{len(reply.lines)} line(s)",
            file=stream,
        )


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--sock", default=DEFAULT_SOCK, help="bot shell socket path")
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help="seconds of silence that end a reply (default %(default)s)",
    )
    sub = parser.add_subparsers(dest="cmd")
    sub.required = True
    sub.add_parser("battery", help="query charge and docked state")
    say = sub.add_parser("say", help="text to speech")
    say.add_argument("text")
    raw = sub.add_parser("raw", help="send any bot shell command line")
    raw.add_argument("command")
    sub.add_parser("stop", help=f"send '{STOP_COMMAND}'")
    pulse = sub.add_parser("pulse", help="manual_move for a bounded time, then stop")
    pulse.add_argument("left", type=int)
    pulse.add_argument("right", type=int)
    pulse.add_argument("ms", type=int)
    pulse.add_argument("--i-know", action="store_true", help="lift the speed and time caps")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        with BotShell(args.sock, args.timeout) as shell:
            if args.cmd == "battery":
                print_reply(shell.command("battery"))
            elif args.cmd == "say":
                print_reply(shell.command(f"say {args.text}"))
            elif args.cmd == "raw":
                print_reply(shell.command(args.command))
            elif args.cmd == "stop":
                print_reply(shell.stop())
            elif args.cmd == "pulse":
                for reply in shell.pulse(args.left, args.right, args.ms, args.i_know):
                    print_reply(reply)
    except ValueError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    except BotShellError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
