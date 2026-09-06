#!/usr/bin/env python3
"""Camera stream probe: bind ``/dev/libcamera_stream`` and measure what the HAL sends.

    python3 camera_probe.py --seconds 10
    python3 camera_probe.py --seconds 30 --out frames.raw
    python3 camera_probe.py --seconds 60 --publish rtsp://<laptop_ip>:8554/ground1

The HAL only copies frames while some app holds the camera (a call, or the standalone WebAPI
page calling ``getUserMedia``); with nothing holding it the probe just waits. The reader
binds the datagram socket itself, as the vendor sample does, so run this as root inside the
container and stop anything else that binds the same path. It reports resolution, format
code, frame size, frame rate, and dropped frames; ``--out`` creates a raw grayscale file and
the final report prints the ``ffmpeg`` line that turns them into a video. ``--publish`` pipes
frames into an ``ffmpeg`` subprocess (libx264 ultrafast, zerolatency) towards an RTSP path
and samples that process's CPU from ``/proc``. Python 3.6, standard library only.
"""

import argparse
import fcntl
import os
import resource
import select
import shutil
import socket
import stat
import subprocess
import sys
import time
from urllib.parse import urlsplit, urlunsplit

try:
    from . import ohmnicam
except ImportError:  # run as a script from the spike directory
    import ohmnicam

SOCKET_PATH = "/dev/libcamera_stream"
HAL_UID = 1047
RECV_BUFFER = 65536
MAX_CAPTURE_SECONDS = 300.0
MAX_WARMUP_SECONDS = 30.0
MAX_RAW_OUTPUT_BYTES = 512 * 1024 * 1024
MAX_PUBLISH_URL_BYTES = 2048
MAX_PUBLISH_WRITE_S = 0.5


class ProbeError(Exception):
    """The requested camera probe run is unsafe or its local protocol is invalid."""


def _finite(value):
    return not (value != value or value in (float("inf"), float("-inf")))


def validate_args(args):
    """Refuse unbounded capture runs and unusable publish settings."""
    if not _finite(args.seconds) or not 0 < args.seconds <= MAX_CAPTURE_SECONDS:
        raise ValueError(f"seconds must be within 0..{MAX_CAPTURE_SECONDS:g}")
    if not _finite(args.warmup) or not 0 < args.warmup <= MAX_WARMUP_SECONDS:
        raise ValueError(f"warmup must be within 0..{MAX_WARMUP_SECONDS:g}")
    if args.publish:
        if args.warmup >= args.seconds:
            raise ValueError("warmup must be shorter than the capture")
        if len(args.publish.encode("utf-8")) > MAX_PUBLISH_URL_BYTES:
            raise ValueError(f"publish URL exceeds {MAX_PUBLISH_URL_BYTES} UTF-8 bytes")
        parsed = urlsplit(args.publish)
        if parsed.scheme != "rtsp" or not parsed.hostname:
            raise ValueError("publish URL must be an rtsp:// URL with a host")
        try:
            _ = parsed.port
        except ValueError as exc:
            raise ValueError(f"publish URL has an invalid port: {exc}") from exc


def redact_publish_url(url):
    """Hide RTSP userinfo before a command is printed or logged."""
    parsed = urlsplit(url)
    if (
        parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
    ):
        return url
    netloc = parsed.netloc
    if parsed.username is not None or parsed.password is not None:
        host = parsed.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        if parsed.port is not None:
            host = f"{host}:{parsed.port}"
        netloc = f"<credentials>@{host}"
    query = "<redacted>" if parsed.query else ""
    return urlunsplit((parsed.scheme, netloc, parsed.path, query, ""))


def bind_stream(path):
    """Bind the HAL socket, refusing to unlink anything except a stale socket."""
    try:
        existing = os.lstat(path)
    except FileNotFoundError:
        existing = None
    if existing is not None:
        if not stat.S_ISSOCK(existing.st_mode):
            raise ProbeError(f"refusing to remove non-socket path: {path}")
        os.remove(path)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        server.bind(path)
    except Exception:
        server.close()
        raise
    try:
        os.chown(path, HAL_UID, HAL_UID)
    except OSError as exc:
        print(f"warning: chown {path} to {HAL_UID}: {exc} (run as root)", file=sys.stderr)
    server.settimeout(1.0)
    bound = os.lstat(path)
    return server, (bound.st_dev, bound.st_ino)


def remove_bound_stream(path, identity):
    """Remove only the exact socket created by ``bind_stream``."""
    try:
        current = os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError as exc:
        print(f"warning: cannot inspect camera socket for cleanup: {exc}", file=sys.stderr)
        return False
    if not stat.S_ISSOCK(current.st_mode) or (current.st_dev, current.st_ino) != identity:
        print(f"warning: refusing to remove replaced socket path: {path}", file=sys.stderr)
        return False
    try:
        os.remove(path)
    except OSError as exc:
        print(f"warning: cannot remove camera socket: {exc}", file=sys.stderr)
        return False
    return True


class ProcCpu:
    """CPU percent of one process from ``/proc/<pid>/stat`` between samples."""

    def __init__(self, pid):
        self.pid = pid
        self.ticks = os.sysconf("SC_CLK_TCK")
        self.last = None
        self.sample_total = 0.0
        self.sample_count = 0

    def _cpu_seconds(self):
        try:
            with open(f"/proc/{self.pid}/stat") as handle:
                line = handle.read()
        except OSError:
            return None
        fields = line[line.rindex(")") + 2 :].split()
        return (int(fields[11]) + int(fields[12])) / float(self.ticks)

    def sample(self):
        """Percent of one core used since the previous sample, or ``None`` off Linux."""
        now = time.monotonic()
        cpu = self._cpu_seconds()
        if cpu is None:
            return None
        percent = None
        if self.last is not None:
            wall = now - self.last[0]
            if wall > 0:
                percent = (cpu - self.last[1]) / wall * 100.0
                self.sample_total += percent
                self.sample_count += 1
        self.last = (now, cpu)
        return percent

    def mean(self):
        return self.sample_total / self.sample_count if self.sample_count else None


def publish_command(width, height, fps, url):
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "gray",
        "-s",
        f"{width}x{height}",
        "-framerate",
        f"{fps:.2f}",
        "-i",
        "-",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-tune",
        "zerolatency",
        "-pix_fmt",
        "yuv420p",
    ]
    if width % 2 or height % 2:
        cmd += ["-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2"]
    cmd += ["-rtsp_transport", "tcp", "-f", "rtsp", url]
    return cmd


def file_command(width, height, fps, path):
    return (
        f"ffmpeg -f rawvideo -pix_fmt gray -s {width}x{height} -r {fps:.2f} -i {path} "
        "-c:v libx264 -pix_fmt yuv420p frames.mp4"
    )


class Publisher:
    """An ``ffmpeg`` child that reads raw frames on stdin and publishes them over RTSP."""

    def __init__(self, width, height, fps, url):
        self.command = publish_command(width, height, fps, url)
        self.display_command = self.command[:-1] + [redact_publish_url(url)]
        self.process = subprocess.Popen(self.command, stdin=subprocess.PIPE, bufsize=0)
        fd = self.process.stdin.fileno()
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        self.cpu = ProcCpu(self.process.pid)
        self.alive = True
        self.written = 0

    def write(self, data):
        if not self.alive:
            return
        view = memoryview(data)
        deadline = time.monotonic() + MAX_PUBLISH_WRITE_S
        try:
            while view:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ProbeError("ffmpeg did not accept a frame within the write deadline")
                _, ready, _ = select.select([], [self.process.stdin.fileno()], [], remaining)
                if not ready:
                    raise ProbeError("ffmpeg did not accept a frame within the write deadline")
                try:
                    written = os.write(self.process.stdin.fileno(), view)
                except BlockingIOError:
                    continue
                view = view[written:]
            self.written += 1
        except (BrokenPipeError, OSError, ProbeError) as exc:
            self.alive = False
            print(f"ffmpeg stopped accepting frames: {exc}", file=sys.stderr)

    def close(self):
        try:
            self.process.stdin.close()
        except OSError:
            pass
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        return self.process.returncode


class RawOutput:
    """Exclusive, byte-bounded raw-frame evidence file."""

    def __init__(self, path):
        self.handle = open(path, "xb")
        self.bytes = 0

    def write(self, data):
        if self.bytes + len(data) > MAX_RAW_OUTPUT_BYTES:
            raise ProbeError(f"raw output exceeds {MAX_RAW_OUTPUT_BYTES} bytes")
        self.handle.write(data)
        self.bytes += len(data)

    def close(self):
        self.handle.close()


class Stats:
    def __init__(self):
        self.frames = 0
        self.first = None
        self.last = None
        self.header = None
        self.header_changes = 0
        self.window_start = None
        self.window_frames = 0

    def add(self, frame, now):
        self.frames += 1
        self.last = now
        if self.first is None:
            self.first = now
            self.window_start = now
        if frame.header != self.header:
            self.header = frame.header
            self.header_changes += 1
            print(f"frame header: {frame.header!r}")
        self.window_frames += 1

    def window_fps(self, now):
        """Frames per second over the window since the last call, then start a new window."""
        elapsed = now - self.window_start if self.window_start else 0.0
        fps = self.window_frames / elapsed if elapsed > 0 else 0.0
        self.window_start = now
        self.window_frames = 0
        return fps

    def mean_fps(self):
        if self.frames < 2 or self.first is None:
            return 0.0
        return (self.frames - 1) / max(self.last - self.first, 1e-6)


def run(args):
    validate_args(args)
    if args.publish and shutil.which("ffmpeg") is None:
        print("error: --publish needs ffmpeg on PATH inside the container", file=sys.stderr)
        return 2
    assembler = ohmnicam.FrameAssembler()
    stats = Stats()
    out = RawOutput(args.out) if args.out else None
    publisher = None
    server = None
    socket_identity = None
    try:
        server, socket_identity = bind_stream(args.socket)
        deadline = time.monotonic() + args.seconds
        next_report = None
        waited_since = time.monotonic()
        print(f"listening on {args.socket} for {args.seconds:.0f} s")
        while time.monotonic() < deadline:
            try:
                datagram = server.recv(RECV_BUFFER)
            except socket.timeout:  # noqa: UP041 (not an alias of TimeoutError before 3.10)
                if stats.frames == 0 and time.monotonic() - waited_since >= 5.0:
                    print("no frames yet: is an app holding the camera?")
                    waited_since = time.monotonic()
                continue
            frame = assembler.feed(datagram)
            if frame is None:
                continue
            now = time.monotonic()
            stats.add(frame, now)
            if out is not None:
                out.write(frame.data)
            if publisher is not None:
                publisher.write(frame.data)
            elif args.publish and now - stats.first >= args.warmup and stats.frames > 1:
                fps = stats.mean_fps()
                publisher = Publisher(frame.header.width, frame.header.height, fps, args.publish)
                command_text = " ".join(publisher.display_command)
                print(f"ffmpeg started (pid {publisher.process.pid}): {command_text}")
            if next_report is None:
                next_report = now + 1.0
            elif now >= next_report:
                next_report = now + 1.0
                cpu = publisher.cpu.sample() if publisher is not None else None
                cpu_text = "" if cpu is None else f" ffmpeg_cpu={cpu:.0f}%"
                print(
                    f"t={now - stats.first:5.1f}s frames={stats.frames} "
                    f"fps={stats.window_fps(now):.1f} dropped={assembler.dropped}{cpu_text}"
                )
    except KeyboardInterrupt:
        print("interrupted")
    finally:
        if server is not None:
            server.close()
        if socket_identity is not None:
            remove_bound_stream(args.socket, socket_identity)
        if out is not None:
            out.close()
        if publisher is not None:
            code = publisher.close()
            print(f"ffmpeg exited with {code} after {publisher.written} frames")
    report(args, stats, assembler, publisher)
    return 0 if stats.frames else 1


def report(args, stats, assembler, publisher):
    own = resource.getrusage(resource.RUSAGE_SELF)
    print("---")
    if stats.frames == 0:
        print("no frames received")
        return
    header = stats.header
    fps = stats.mean_fps()
    print(f"resolution: {header.width}x{header.height}")
    print(f"format code: {header.format} (grayscale, one byte per pixel)")
    print(f"frame size: {header.size} bytes")
    print(f"frames: {stats.frames} in {stats.last - stats.first:.1f} s, mean {fps:.2f} fps")
    print(
        f"dropped: {assembler.dropped}, malformed: {assembler.malformed}, "
        f"ignored datagrams: {assembler.ignored}, headers seen: {stats.header_changes}"
    )
    print(f"probe cpu: {own.ru_utime + own.ru_stime:.1f} s user+sys")
    if publisher is not None:
        mean = publisher.cpu.mean()
        mean_text = "n/a (no /proc)" if mean is None else f"{mean:.0f}% of one core (mean)"
        print(f"ffmpeg cpu: {mean_text}")
    if args.out:
        print("to encode the raw file:")
        print("  " + file_command(header.width, header.height, fps, args.out))
    print("to publish live:")
    live = publish_command(header.width, header.height, fps, "rtsp://<host>:8554/ground1")
    print("  " + " ".join(live))


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--socket", default=SOCKET_PATH, help="datagram socket path to bind")
    parser.add_argument(
        "--seconds", type=float, default=10.0, help="capture time in seconds (max 300)"
    )
    parser.add_argument("--out", help="create a new bounded raw grayscale frame file")
    parser.add_argument("--publish", metavar="RTSP_URL", help="pipe frames to ffmpeg -> RTSP")
    parser.add_argument(
        "--warmup",
        type=float,
        default=2.0,
        help="seconds of frames to measure the rate before ffmpeg starts (default 2)",
    )
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except ValueError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    except FileExistsError as exc:
        print(f"refused: output file already exists: {exc.filename}", file=sys.stderr)
        return 2
    except (OSError, ProbeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
