#!/usr/bin/env python3
"""RPLIDAR A2M8 probe over the robot's USB serial port, run inside the Ohmni container.

    python3 lidar_probe.py --seconds 10
    python3 lidar_probe.py --seconds 30 --record scans.jsonl
    python3 lidar_probe.py --port /dev/usb/tty1-2.1 --no-release

Order of operations: ask the telebot app to release the port (``lidar_release`` over the bot
shell), open the port with the standard-library ``termios`` (raw, 115200 8N1), then STOP,
RESET (wait 2 s and print the boot banner), GET_INFO, GET_HEALTH, SET_MOTOR_PWM, SCAN. Each
full revolution prints its point count, valid count, nearest and farthest range, and the
revolution rate; ``--record`` appends one JSON object per revolution. On any exit the probe
sends STOP and motor PWM 0. The port comes from ``--port``, else ``serialport`` in
``/app/telebot_config.json``, else the USB expansion hub default. No pyserial needed.
"""

import argparse
import fcntl
import json
import os
import select
import struct
import sys
import termios
import time

try:
    from . import botshell
    from . import rplidar_protocol as rp
except ImportError:  # run as a script from the spike directory
    import botshell
    import rplidar_protocol as rp

DEFAULT_PORT = "/dev/usb/tty1-2.1"
TELEBOT_CONFIG = "/app/telebot_config.json"
RESET_WAIT_S = 2.0
SPINUP_WAIT_S = 0.5
IDLE_WARN_S = 3.0


class ProbeError(Exception):
    """The lidar did not answer the way the protocol says it should."""


def configured_port(config_path):
    """The ``serialport`` value from the telebot config, or ``None`` when unset or unreadable."""
    try:
        with open(config_path) as handle:
            config = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(config, dict):
        return None
    port = config.get("serialport")
    return port if isinstance(port, str) and port else None


class SerialPort:
    """A raw 8N1 serial port configured with ``termios``; reads are select-driven."""

    def __init__(self, path, baud=termios.B115200):
        self.path = path
        self.baud = baud
        self.fd = None

    def open(self):
        fd = os.open(self.path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            iflag, oflag, cflag, lflag, _, _, cc = termios.tcgetattr(fd)
            iflag &= ~(
                termios.IGNBRK
                | termios.BRKINT
                | termios.PARMRK
                | termios.ISTRIP
                | termios.INLCR
                | termios.IGNCR
                | termios.ICRNL
                | termios.IXON
                | termios.IXOFF
                | termios.IXANY
            )
            oflag &= ~termios.OPOST
            lflag &= ~(
                termios.ECHO | termios.ECHONL | termios.ICANON | termios.ISIG | termios.IEXTEN
            )
            cflag &= ~(termios.CSIZE | termios.PARENB | termios.CSTOPB)
            cflag |= termios.CS8 | termios.CREAD | termios.CLOCAL
            cc[termios.VMIN] = 0
            cc[termios.VTIME] = 0
            termios.tcsetattr(
                fd, termios.TCSANOW, [iflag, oflag, cflag, lflag, self.baud, self.baud, cc]
            )
            termios.tcflush(fd, termios.TCIOFLUSH)
        except Exception:
            os.close(fd)
            raise
        self.fd = fd
        return self

    def close(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            finally:
                self.fd = None

    def set_dtr(self, asserted):
        """Best effort; some RPLIDAR USB adapters power the motor only while DTR is clear."""
        names = ("TIOCM_DTR", "TIOCMBIS", "TIOCMBIC")
        if any(not hasattr(termios, name) for name in names):
            return False
        flag = struct.pack("I", termios.TIOCM_DTR)
        request = termios.TIOCMBIS if asserted else termios.TIOCMBIC
        try:
            fcntl.ioctl(self.fd, request, flag)
        except OSError:
            return False
        return True

    def write(self, data, timeout=1.0):
        view = memoryview(bytes(data))
        deadline = time.monotonic() + timeout
        while view:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProbeError(f"write timed out with {len(view)} bytes unsent")
            _, ready, _ = select.select([], [self.fd], [], remaining)
            if not ready:
                continue
            try:
                sent = os.write(self.fd, view)
            except BlockingIOError:
                continue
            view = view[sent:]

    def read(self, max_bytes=4096, timeout=0.1):
        ready, _, _ = select.select([self.fd], [], [], timeout)
        if not ready:
            return b""
        try:
            return os.read(self.fd, max_bytes)
        except BlockingIOError:
            return b""

    def read_exact(self, count, timeout):
        """Up to ``count`` bytes within ``timeout`` seconds; shorter when the device stalls."""
        buf = bytearray()
        deadline = time.monotonic() + timeout
        while len(buf) < count:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            buf.extend(self.read(count - len(buf), remaining))
        return bytes(buf)

    def drain(self, seconds):
        """Read and return whatever arrives during ``seconds``."""
        buf = bytearray()
        deadline = time.monotonic() + seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            buf.extend(self.read(4096, remaining))
        return bytes(buf)


def read_descriptor(port, timeout, expected_type=None):
    """Find the next ``A5 5A`` descriptor within ``timeout`` seconds, skipping stray bytes."""
    buf = bytearray()
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProbeError(
                f"no response descriptor within {timeout:.1f} s (received {bytes(buf).hex()!r})"
            )
        buf.extend(port.read(64, remaining))
        start = buf.find(b"\xa5\x5a")
        if start < 0:
            if len(buf) > 1:
                del buf[:-1]
            continue
        if start > 0:
            del buf[:start]
        if len(buf) < rp.DESCRIPTOR_LEN:
            continue
        descriptor = rp.parse_descriptor(buf[: rp.DESCRIPTOR_LEN])
        if expected_type is not None and descriptor.data_type != expected_type:
            raise ProbeError(f"expected data type 0x{expected_type:02x}, got {descriptor!r}")
        return descriptor


def single_response(port, request, expected_type, timeout=1.0):
    """Send a single-response request and return its payload bytes."""
    port.write(request)
    descriptor = read_descriptor(port, timeout, expected_type)
    if descriptor.send_mode != rp.SEND_MODE_SINGLE:
        raise ProbeError(f"expected a single response, got {descriptor!r}")
    payload = port.read_exact(descriptor.data_length, timeout)
    if len(payload) < descriptor.data_length:
        raise ProbeError(
            f"short payload: {len(payload)} of {descriptor.data_length} bytes within {timeout} s"
        )
    return payload


def release_port(sock_path, timeout):
    """Ask the telebot app to let go of the lidar serial port; a missing bot shell is a warning."""
    try:
        with botshell.BotShell(sock_path, timeout) as shell:
            reply = shell.command("lidar_release")
    except botshell.BotShellError as exc:
        print(f"warning: {exc}; opening the port without lidar_release", file=sys.stderr)
        return
    botshell.print_reply(reply)


def scan_loop(port, seconds, recorder):
    parser = rp.ScanParser()
    collector = rp.RevolutionCollector()
    deadline = None if seconds <= 0 else time.monotonic() + seconds
    last_rev = None
    last_data = time.monotonic()
    warned_idle = False
    revolutions = 0
    points = 0
    rates = []
    while deadline is None or time.monotonic() < deadline:
        data = port.read(4096, 0.2)
        if not data:
            if not warned_idle and time.monotonic() - last_data > IDLE_WARN_S:
                print("no scan bytes for 3 s: is the motor spinning and the port released?")
                warned_idle = True
            continue
        last_data = time.monotonic()
        warned_idle = False
        for revolution in collector.feed(parser.feed(data)):
            now_wall = time.time()
            now = time.monotonic()
            summary = rp.summarize_revolution(revolution)
            rate = None if last_rev is None else 1.0 / max(now - last_rev, 1e-6)
            last_rev = now
            revolutions += 1
            points += summary["points"]
            if rate is not None:
                rates.append(rate)
            rate_text = "n/a" if rate is None else f"{rate:.2f} rev/s"
            print(
                f"rev {revolutions}: {summary['points']} points, {summary['valid']} valid, "
                f"min {summary['min_m']} m, max {summary['max_m']} m, {rate_text}"
            )
            if recorder is not None:
                recorder.write(json.dumps(rp.revolution_record(now_wall, revolution)) + "\n")
                recorder.flush()
    mean_text = "n/a" if not rates else "{:.2f} rev/s".format(sum(rates) / len(rates))  # noqa: UP032
    print(
        f"done: {revolutions} revolutions, {points} points, mean {mean_text}, "
        f"{parser.resyncs} resyncs"
    )
    return revolutions


def run(args):
    if args.port:
        port_path, source = args.port, "--port"
    else:
        configured = configured_port(args.config)
        if configured:
            port_path, source = configured, args.config
        else:
            port_path, source = DEFAULT_PORT, "default"
    print(f"serial port: {port_path} (from {source})")
    if not args.no_release:
        release_port(args.sock, args.timeout)
    port = SerialPort(port_path).open()
    recorder = open(args.record, "a") if args.record else None
    try:
        port.set_dtr(False)
        port.write(rp.stop_request())
        time.sleep(0.05)
        port.drain(0.1)
        print("reset; waiting 2 s")
        port.write(rp.reset_request())
        time.sleep(RESET_WAIT_S)
        banner = port.drain(0.3).decode("ascii", "replace").strip()
        if banner:
            print("boot banner: " + " | ".join(line.strip() for line in banner.splitlines()))
        info = rp.parse_info(single_response(port, rp.get_info_request(), rp.TYPE_INFO))
        print(f"info: {json.dumps(info)}")
        health = rp.parse_health(single_response(port, rp.get_health_request(), rp.TYPE_HEALTH))
        print(f"health: {json.dumps(health)}")
        if health["status"] == 2:
            print("health reports an error; a RESET is the documented recovery", file=sys.stderr)
        print(f"motor pwm {args.pwm}; scanning for {args.seconds or 'unbounded'} s")
        port.write(rp.set_motor_pwm_request(args.pwm))
        time.sleep(SPINUP_WAIT_S)
        port.write(rp.scan_request())
        descriptor = read_descriptor(port, 2.0, rp.TYPE_SCAN)
        print(f"scan descriptor: {descriptor!r}")
        revolutions = scan_loop(port, args.seconds, recorder)
        if args.record:
            print(f"recorded {revolutions} revolutions to {args.record}")
        return 0 if revolutions else 1
    finally:
        try:
            port.write(rp.stop_request())
            time.sleep(0.05)
            port.write(rp.set_motor_pwm_request(0))
            port.set_dtr(True)
            print("lidar stopped, motor pwm 0")
        except (OSError, ProbeError) as exc:
            print(f"warning: could not stop the lidar cleanly: {exc}", file=sys.stderr)
        port.close()
        if recorder is not None:
            recorder.close()


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--port", help=f"serial device (default: config, else {DEFAULT_PORT})")
    parser.add_argument("--config", default=TELEBOT_CONFIG, help="telebot config with serialport")
    parser.add_argument("--seconds", type=float, default=10.0, help="scan time; 0 runs until ^C")
    parser.add_argument("--record", help="append one JSON object per revolution to this file")
    parser.add_argument(
        "--pwm", type=int, default=rp.DEFAULT_MOTOR_PWM, help="motor pwm 0..1023 (default 660)"
    )
    parser.add_argument("--no-release", action="store_true", help="skip lidar_release")
    parser.add_argument("--sock", default=botshell.DEFAULT_SOCK, help="bot shell socket")
    parser.add_argument("--timeout", type=float, default=botshell.DEFAULT_TIMEOUT)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except KeyboardInterrupt:
        print("interrupted")
        return 130
    except (OSError, ProbeError, rp.ProtocolError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
