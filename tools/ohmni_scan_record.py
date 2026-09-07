"""Record canonical Ohmni range-scan observations from relay fan-out."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import shutil
import stat
import tempfile
import time
import uuid
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

from relay.observations import MAX_EVENT_BYTES, Observation, decode_observation

FORMAT = "ohmni.canonical-scan-recording.v1"
DEFAULT_MAX_RECORDS = 10_000
DEFAULT_MAX_BYTES = 64 * 1024 * 1024
DEFAULT_DURATION_S = 300.0
MAX_RECORDS = 100_000
MAX_BYTES = 256 * 1024 * 1024
MAX_DURATION_S = 3_600.0
MANIFEST_RESERVE_BYTES = 4 * 1024
MAX_RELAY_FRAME_BYTES = 1_048_576
MAX_IDENTIFIER_CHARS = 128
MAX_SESSION_CHARS = 512


@dataclass(frozen=True, slots=True)
class RecordingConfig:
    session: str
    device_id: int
    connection_epoch: int
    source_id: str
    odom_frame: str
    lidar_frame: str
    mount_id: str
    run_id: str
    max_records: int = DEFAULT_MAX_RECORDS
    max_bytes: int = DEFAULT_MAX_BYTES
    duration_s: float = DEFAULT_DURATION_S

    def __post_init__(self) -> None:
        _identifier(self.session, "session", MAX_SESSION_CHARS)
        for name in ("source_id", "odom_frame", "lidar_frame", "mount_id", "run_id"):
            _identifier(getattr(self, name), name)
        if self.odom_frame == "world" or self.lidar_frame == "world":
            raise ValueError("scan frames must remain source-scoped local frames")
        if self.odom_frame == self.lidar_frame:
            raise ValueError("odometry and lidar frames must differ")
        if type(self.device_id) is not int or not 1 <= self.device_id <= 2_147_483_647:
            raise ValueError("device ID must be a positive signed 32-bit integer")
        if type(self.connection_epoch) is not int or self.connection_epoch <= 0:
            raise ValueError("connection epoch must be positive")
        if type(self.max_records) is not int or not 1 <= self.max_records <= MAX_RECORDS:
            raise ValueError(f"record bound must be an integer from 1 through {MAX_RECORDS}")
        if (
            type(self.max_bytes) is not int
            or not MANIFEST_RESERVE_BYTES <= self.max_bytes <= MAX_BYTES
        ):
            raise ValueError(
                f"byte bound must be an integer from {MANIFEST_RESERVE_BYTES} through {MAX_BYTES}"
            )
        if (
            isinstance(self.duration_s, bool)
            or not isinstance(self.duration_s, int | float)
            or not math.isfinite(self.duration_s)
            or not 0 < self.duration_s <= MAX_DURATION_S
        ):
            raise ValueError(f"duration must be between 0 and {MAX_DURATION_S:g} seconds")


class _Writer:
    def __init__(self, output: Path, config: RecordingConfig, transport: str):
        self.output = output.absolute()
        self.config = config
        self.transport = transport
        self.output.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.mkdir(self.output)
        except FileExistsError as error:
            raise ValueError(f"recording output already exists: {self.output}") from error
        self.reserved_output = True
        try:
            self.temporary = Path(
                tempfile.mkdtemp(prefix=f".{self.output.name}.", dir=self.output.parent)
            )
        except BaseException:
            shutil.rmtree(self.output, ignore_errors=True)
            raise
        self.stream = (self.temporary / "observations.jsonl").open("xb")
        self.digest = hashlib.sha256()
        self.count = 0
        self.size = 0
        self.input_bytes = 0
        self.confidence = _Confidence()
        self.stop_reason = "input_exhausted"

    def admit(self, raw: bytes | str) -> bool:
        encoded = _encoded(raw)
        self.input_bytes += len(encoded)
        if self.input_bytes > MAX_BYTES:
            raise ValueError("recorder input exceeds the 256 MiB byte ceiling")
        encoded = encoded.rstrip(b"\r\n")
        if not encoded:
            raise ValueError("relay frame must not be empty")
        if _outer_type(encoded) != "observation":
            return False
        if len(encoded) > MAX_EVENT_BYTES:
            raise ValueError("canonical observation exceeds 65536 bytes")
        observation = decode_observation(encoded)
        encoded = observation.encode()
        if not _selected(observation, self.config):
            return False
        if self.count >= self.config.max_records:
            self.stop_reason = "max_records"
            return True
        line = encoded + b"\n"
        if self.size + len(line) > self.config.max_bytes - MANIFEST_RESERVE_BYTES:
            self.stop_reason = "max_bytes"
            return True
        self.stream.write(line)
        self.digest.update(line)
        self.count += 1
        self.size += len(line)
        self.confidence.add(observation.submission.confidence)
        return self.count >= self.config.max_records

    def finish(self) -> dict[str, object]:
        if self.count >= self.config.max_records and self.stop_reason == "input_exhausted":
            self.stop_reason = "max_records"
        self.stream.flush()
        os.fsync(self.stream.fileno())
        self.stream.close()
        manifest = {
            "format": FORMAT,
            "run_id": self.config.run_id,
            "created_at_ms": int(time.time() * 1000),
            "source": {
                "transport": self.transport,
                "session": self.config.session,
                "device_id": self.config.device_id,
                "connection_epoch": self.config.connection_epoch,
                "source_id": self.config.source_id,
                "node_type": "ground",
            },
            "frames": {
                "odometry": self.config.odom_frame,
                "lidar": self.config.lidar_frame,
                "sensor_pose": {
                    "parent_frame": self.config.odom_frame,
                    "child_frame": self.config.lidar_frame,
                },
            },
            "mount_id": self.config.mount_id,
            "observations": {
                "path": "observations.jsonl",
                "count": self.count,
                "bytes": self.size,
                "sha256": self.digest.hexdigest(),
                "stop_reason": self.stop_reason,
                "confidence": self.confidence.to_mapping(),
                "timestamps": {
                    "t_capture": "preserved in each canonical observation",
                    "t_source_receipt": "preserved in each canonical observation",
                    "t_ingest": "preserved in each canonical observation",
                },
            },
            "limits": {
                "max_records": self.config.max_records,
                "max_bytes": self.config.max_bytes,
                "max_runtime_s": self.config.duration_s,
            },
            "registration": {"frame": self.config.odom_frame, "registered_to_world": False},
        }
        encoded_manifest = (
            json.dumps(manifest, allow_nan=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"
        )
        if len(encoded_manifest) > MANIFEST_RESERVE_BYTES:
            raise ValueError("recording manifest exceeds its reserved byte budget")
        with (self.temporary / "recording.json").open("xb") as manifest_stream:
            manifest_stream.write(encoded_manifest)
            manifest_stream.flush()
            os.fsync(manifest_stream.fileno())
        for name in ("observations.jsonl", "recording.json"):
            os.replace(self.temporary / name, self.output / name)
        os.rmdir(self.temporary)
        self.reserved_output = False
        return manifest

    def abort(self) -> None:
        if not self.stream.closed:
            self.stream.close()
        shutil.rmtree(self.temporary, ignore_errors=True)
        if self.reserved_output:
            shutil.rmtree(self.output, ignore_errors=True)


def _identifier(value: object, name: str, maximum: int = MAX_IDENTIFIER_CHARS) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or not value.isprintable()
        or len(value) > maximum
    ):
        raise ValueError(f"{name} must be canonical printable text")
    return value


def _encoded(raw: bytes | str) -> bytes:
    try:
        encoded = raw.encode("utf-8") if type(raw) is str else raw
    except UnicodeEncodeError as error:
        raise ValueError("observation is not valid UTF-8") from error
    if type(encoded) is not bytes or not encoded or len(encoded) > MAX_RELAY_FRAME_BYTES:
        raise ValueError("relay frame must be 1 through 1048576 bytes")
    return encoded


def _outer_type(encoded: bytes) -> str | None:
    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("relay frame contains a duplicate JSON key")
            result[key] = value
        return result

    try:
        value = json.loads(encoded, object_pairs_hook=unique)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError("relay frame is not valid JSON") from error
    return value.get("type") if isinstance(value, dict) and type(value.get("type")) is str else None


def _selected(observation: Observation, config: RecordingConfig) -> bool:
    submission = observation.submission
    same_source = (
        submission.session == config.session
        and submission.device_id == config.device_id
        and submission.source_id == config.source_id
    )
    if same_source and submission.connection_epoch != config.connection_epoch:
        raise ValueError("selected source changed connection epoch during recording")
    if not same_source or submission.connection_epoch != config.connection_epoch:
        return False
    payload = submission.payload
    if submission.node_type != "ground" or payload["kind"] != "range_scan":
        return False
    pose = payload["sensor_pose"]
    if (
        submission.frame != config.lidar_frame
        or payload["mount_id"] != config.mount_id
        or pose["parent_frame"] != config.odom_frame
        or pose["child_frame"] != config.lidar_frame
    ):
        raise ValueError("selected range scan does not match the configured source frames or mount")
    return True


class _Confidence:
    def __init__(self) -> None:
        self.count = 0
        self.total = 0.0
        self.minimum: float | None = None
        self.maximum: float | None = None

    def add(self, value: float) -> None:
        self.count += 1
        self.total += value
        self.minimum = value if self.minimum is None else min(self.minimum, value)
        self.maximum = value if self.maximum is None else max(self.maximum, value)

    def to_mapping(self) -> dict[str, float | int | None]:
        return {
            "count": self.count,
            "minimum": self.minimum,
            "maximum": self.maximum,
            "mean": None if self.count == 0 else self.total / self.count,
        }


def record_events(
    events: Iterable[bytes | str], output: Path, config: RecordingConfig
) -> dict[str, object]:
    writer = _Writer(output, config, "file_jsonl")
    deadline = time.monotonic() + config.duration_s
    try:
        for raw in events:
            if time.monotonic() >= deadline:
                writer.stop_reason = "duration"
                break
            if writer.admit(raw):
                break
        if writer.stop_reason == "input_exhausted" and time.monotonic() >= deadline:
            writer.stop_reason = "duration"
        return writer.finish()
    except BaseException:
        writer.abort()
        raise


async def record_relay(
    relay_url: str, token: str, output: Path, config: RecordingConfig
) -> dict[str, object]:
    import websockets

    writer = _Writer(output, config, "relay_websocket_console_read_only")
    deadline = time.monotonic() + config.duration_s
    try:
        url = f"{relay_url.rstrip('/')}/ws/{config.session}"
        async with websockets.connect(url, max_size=MAX_RELAY_FRAME_BYTES) as socket:
            await socket.send(
                json.dumps({"v": 1, "type": "auth", "source": "console", "token": token})
            )
            while time.monotonic() < deadline:
                try:
                    raw = await asyncio.wait_for(socket.recv(), timeout=deadline - time.monotonic())
                except TimeoutError:
                    writer.stop_reason = "duration"
                    break
                if type(raw) is not str:
                    raise ValueError("relay sent a binary frame")
                if writer.admit(raw):
                    break
        return writer.finish()
    except BaseException:
        writer.abort()
        raise


def _open_regular(path: Path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError(f"{path} must be a regular file")
        return os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise


def _events_from_jsonl(path: Path) -> Iterator[bytes]:
    with _open_regular(path) as stream:
        if os.fstat(stream.fileno()).st_size > MAX_BYTES:
            raise ValueError(f"{path} exceeds the 256 MiB input byte ceiling")
        line_number = 0
        total = 0
        while line := stream.readline(MAX_EVENT_BYTES + 2):
            line_number += 1
            if len(line) > MAX_EVENT_BYTES + 1:
                raise ValueError(f"{path}:{line_number}: line exceeds 65536 bytes")
            total += len(line)
            if total > MAX_BYTES:
                raise ValueError(f"{path} exceeds the 256 MiB input byte ceiling")
            if line.rstrip(b"\r\n"):
                yield line


def _token(path: Path) -> str:
    with _open_regular(path) as stream:
        encoded = stream.read(4_097)
    if len(encoded) > 4_096:
        raise ValueError("relay token file exceeds 4 KiB")
    try:
        token = encoded.decode("utf-8").strip()
    except UnicodeDecodeError as error:
        raise ValueError("relay token file is not valid UTF-8") from error
    if not token:
        raise ValueError("relay token file is empty")
    return token


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input-jsonl", type=Path)
    source.add_argument("--relay-url")
    parser.add_argument("--token-file", type=Path)
    parser.add_argument("--session", required=True)
    parser.add_argument("--device-id", required=True, type=int)
    parser.add_argument("--connection-epoch", required=True, type=int)
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--odom-frame", required=True)
    parser.add_argument("--lidar-frame", required=True)
    parser.add_argument("--mount-id", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--duration-s", type=float, default=DEFAULT_DURATION_S)
    parser.add_argument("--max-records", type=int, default=DEFAULT_MAX_RECORDS)
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.relay_url is not None and args.token_file is None:
            raise ValueError("--relay-url requires --token-file")
        if args.input_jsonl is not None and args.token_file is not None:
            raise ValueError("--token-file only applies to --relay-url")
        config = RecordingConfig(
            args.session,
            args.device_id,
            args.connection_epoch,
            args.source_id,
            args.odom_frame,
            args.lidar_frame,
            args.mount_id,
            args.run_id or str(uuid.uuid4()),
            args.max_records,
            args.max_bytes,
            args.duration_s,
        )
        manifest = (
            record_events(_events_from_jsonl(args.input_jsonl), args.output, config)
            if args.input_jsonl
            else asyncio.run(
                record_relay(args.relay_url, _token(args.token_file), args.output, config)
            )
        )
    except (OSError, ValueError) as error:
        raise SystemExit(f"ohmni scan recording failed: {error}") from error
    print(json.dumps({"output": str(args.output), "records": manifest["observations"]["count"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
