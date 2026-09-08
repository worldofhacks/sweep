"""Read committed relay audit records into bounded MCAP recordings."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import stat
import struct
import tempfile
import zlib
from contextlib import closing, contextmanager
from pathlib import Path

from mcap.exceptions import McapError
from mcap.records import (
    Channel,
    DataEnd,
    Footer,
    Header,
    Message,
    Schema,
    Statistics,
    SummaryOffset,
)
from mcap.stream_reader import StreamReader
from mcap.writer import CompressionType, Writer

from relay.audit import MAX_AUDIT_RECORD_BYTES
from relay.observations import Observation
from tools.world_replay_scene import SCENE_SCHEMA, scene_update

MAX_RECORDS = 100_000
MAX_FILE_BYTES = 256 << 20
BATCH_RECORDS = 16
PROFILE = "sweep.audit.v1"
TOPICS = (
    "roster",
    "plans",
    "acknowledgements",
    "aircraft",
    "ground",
    "tags",
    "registration",
    "map",
    "occupancy",
    "safety",
    "observations",
    "events",
)


class ReplayError(ValueError):
    pass


def channel_name(record: dict) -> str:
    event = record["event"]
    kind = event.get("type")
    if kind == "observation":
        payload_kind = event["payload"]["kind"]
        if payload_kind == "tag_observation":
            return "tags"
        if payload_kind in {"pose", "telemetry"}:
            return event["node_type"]
        return "observations"
    if kind in {"membership", "state", "node_status"}:
        return "roster"
    if kind in {"refusal", "safety_hold", "safety_stop"}:
        return "safety"
    if kind in {"acknowledgement", "ack"}:
        return "acknowledgements"
    if kind in {"intent_record", "plan", "autonomy_result", "navigation_route_authorization"}:
        return "plans"
    if kind in {"telemetry", "control_pose", "navigation_pose"}:
        return "aircraft"
    if kind in {"registration", "registration_residual"}:
        return "registration"
    if kind in {"static_grid", "map_identity"}:
        return "map"
    if kind in {"live_occupancy", "occupancy_expiry"}:
        return "occupancy"
    if kind == "command":
        return "safety" if event.get("operation") in {"hold", "hover", "estop"} else "plans"
    return "events"


def channel_schema(name: str) -> bytes:
    if name not in TOPICS:
        raise ReplayError("unknown replay channel")
    event = {
        "type": "object",
        "required": ["type", "session", "event_id"],
        "properties": {
            "type": {"type": "string"},
            "session": {"type": "string"},
            "event_id": {"type": "string"},
            "t": {"type": "integer", "minimum": 0},
            "device_id": {"type": ["integer", "null"]},
            "drone_id": {"type": ["integer", "null"]},
            "node_type": {"enum": ["ground", "aircraft"]},
            "connection_epoch": {"type": ["integer", "null"], "minimum": 1},
            "source_id": {"type": "string"},
            "frame": {"type": "string"},
            "reason": {"type": ["string", "null"]},
            "map_id": {"type": "string"},
            "map_version": {"type": "string"},
            "registration_id": {"type": "string"},
            "expires_at": {"type": "integer"},
        },
    }
    observation = json.loads(
        (Path(__file__).resolve().parents[1] / "schemas/observation-v1.schema.json").read_bytes()
    )
    observation.pop("$id", None)
    pose_payload = next(
        payload
        for payload in observation["properties"]["payload"]["oneOf"]
        if payload["properties"]["kind"].get("const") == "pose"
    )
    pose_payload["properties"].update(
        {
            "capture_alignment": {"type": "object"},
            "encoder_timing": {"type": "object"},
        }
    )
    event["allOf"] = [
        {
            "if": {"properties": {"type": {"const": "observation"}}},
            "then": observation,
        }
    ]
    return json.dumps(
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "title": f"Sweep {name} audit v1",
            "type": "object",
            "additionalProperties": False,
            "required": ["seq", "event"],
            "properties": {"seq": {"type": "integer", "minimum": 1}, "event": event},
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _decode(data: bytes, session: str, expected: int | None = None) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate audit key")
            result[key] = value
        return result

    try:
        record = json.loads(data, object_pairs_hook=unique)
        if set(record) != {"seq", "event"} or type(record["seq"]) is not int:
            raise ValueError("invalid audit envelope")
        if not 1 <= record["seq"] < 1 << 63 or (expected is not None and record["seq"] != expected):
            raise ValueError("non-contiguous audit sequence")
        event = record["event"]
        if event["session"] != session or any(
            not isinstance(event[name], str) or not 1 <= len(event[name]) <= 512
            for name in ("event_id", "type")
        ):
            raise ValueError("audit session or event identity mismatch")
        if event["type"] == "observation":
            Observation.parse(event)
        timestamp_ns(record)
        json.dumps(record, allow_nan=False)
        return record
    except (KeyError, TypeError, ValueError, RecursionError) as error:
        raise ReplayError(f"invalid audit record: {error}") from None


def timestamp_ns(record: dict) -> int:
    event = record["event"]
    value = event.get("t_ingest", event.get("t"))
    if type(value) is not int or not 0 <= value <= ((1 << 64) - 1) // 1_000_000:
        raise ReplayError("audit ingest timestamp cannot be represented by MCAP")
    return value * 1_000_000


def _regular(path: Path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise ReplayError("audit input must be a regular file")
    return os.fdopen(descriptor, "rb")


class CommittedTail:
    """Read complete JSONL records whose bytes match committed SQLite fencing metadata."""

    def __init__(self, path: Path, session: str):
        self.path = Path(path)
        self.session = session
        if self.path.name != hashlib.sha256(session.encode()).hexdigest() + ".jsonl":
            raise ReplayError("audit filename does not match session")
        self.database = self.path.with_suffix(".sqlite3")
        self.sequence = 0
        self.offset = 0
        self.identities: dict[Path, tuple[int, int]] = {}
        self.caught_up = False

    def read_batch(self) -> list[dict]:
        for path in (self.path, self.database):
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode):
                raise ReplayError("audit input must be a regular file")
            identity = (info.st_dev, info.st_ino)
            if self.identities.setdefault(path, identity) != identity:
                raise ReplayError("audit file replaced while mirroring")
        try:
            with closing(
                sqlite3.connect(
                    self.database.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.05
                )
            ) as database:
                rows = database.execute(
                    "SELECT seq,digest,length,status FROM records "
                    "JOIN operations ON operations.id=records.operation_id "
                    "WHERE seq>? ORDER BY seq LIMIT ?",
                    (self.sequence, BATCH_RECORDS),
                ).fetchall()
        except sqlite3.Error as error:
            raise ReplayError(f"cannot read committed audit: {error}") from None
        result = []
        self.caught_up = not rows
        with _regular(self.path) as stream:
            if os.fstat(stream.fileno()).st_size < self.offset:
                raise ReplayError("audit file truncated while mirroring")
            stream.seek(self.offset)
            for sequence, digest, length, status in rows:
                if status != "complete":
                    break
                if type(length) is not int or not 1 <= length <= MAX_AUDIT_RECORD_BYTES:
                    raise ReplayError("audit record length exceeds limit")
                encoded = stream.read(length)
                if len(encoded) < length:
                    break
                if not encoded.endswith(b"\n") or hashlib.sha256(encoded).digest() != digest:
                    raise ReplayError("audit checksum mismatch")
                if sequence != self.sequence + 1:
                    raise ReplayError("non-contiguous committed audit")
                result.append(_decode(encoded, self.session, sequence))
                self.sequence = sequence
                self.offset += length
        return result


class Recording:
    """Create a new bounded MCAP; publish the completed file atomically on close."""

    def __init__(self, output: Path, session: str):
        self.output = Path(output)
        if self.output.exists():
            raise ReplayError("output already exists")
        descriptor, pending = tempfile.mkstemp(prefix=".sweep-replay-", dir=self.output.parent)
        self.pending = Path(pending)
        self.stream = os.fdopen(descriptor, "w+b")
        self.session = session
        self.count = 0
        self.sequence: int | None = None
        self.writer = Writer(
            self.stream,
            compression=CompressionType.NONE,
            use_chunking=False,
            enable_crcs=True,
            enable_data_crcs=True,
        )
        self.writer.start(profile=PROFILE, library=PROFILE)
        self.channels = {}
        for name in TOPICS:
            schema = self.writer.register_schema(
                f"sweep.{name}.v1", "jsonschema", channel_schema(name)
            )
            self.channels[name] = self.writer.register_channel(
                f"/sweep/{name}",
                "json",
                schema,
                metadata={"session": session, "clock": "relay_ingest_unix_ns"},
            )
        schema = self.writer.register_schema("foxglove.SceneUpdate", "jsonschema", SCENE_SCHEMA)
        self.channels["scene"] = self.writer.register_channel(
            "/sweep/scene",
            "json",
            schema,
            metadata={"session": session, "clock": "relay_ingest_unix_ns"},
        )

    def append(self, record: dict) -> None:
        encoded = json.dumps(
            record, allow_nan=False, sort_keys=True, separators=(",", ":")
        ).encode()
        _decode(encoded, self.session, None if self.sequence is None else self.sequence + 1)
        if (
            len(encoded) > MAX_AUDIT_RECORD_BYTES
            or self.count >= MAX_RECORDS
            or self.stream.tell() + len(encoded) + (1 << 20) > MAX_FILE_BYTES
        ):
            raise ReplayError("MCAP recording limit reached")
        stamp = timestamp_ns(record)
        scene = scene_update(record)
        scene_encoded = (
            None
            if scene is None
            else json.dumps(scene, allow_nan=False, separators=(",", ":")).encode()
        )
        # Native capture clocks stay in the envelope; only relay ingest belongs on this timeline.
        self.writer.add_message(
            self.channels[channel_name(record)], stamp, encoded, stamp, self.count
        )
        if scene_encoded is not None:
            self.writer.add_message(
                self.channels["scene"],
                stamp,
                scene_encoded,
                stamp,
                self.count,
            )
        self.sequence = record["seq"]
        self.count += 1

    def close(self, *, publish: bool = True) -> None:
        if self.stream.closed:
            return
        try:
            if publish:
                self.writer.finish()
                self.stream.flush()
                os.fsync(self.stream.fileno())
                os.link(self.pending, self.output)
        finally:
            self.stream.close()
            self.pending.unlink(missing_ok=True)


def export_audit(path: Path, session: str, output: Path) -> int:
    tail = CommittedTail(path, session)
    recording = Recording(output, session)
    try:
        while records := tail.read_batch():
            for record in records:
                recording.append(record)
        if not tail.caught_up:
            raise ReplayError("committed audit mirror is still catching up")
        recording.close()
        return recording.count
    except BaseException:
        recording.close(publish=False)
        raise


@contextmanager
def _snapshot(path: Path):
    with _regular(path) as source, tempfile.TemporaryFile("w+b") as snapshot:
        total = 0
        while block := source.read(1 << 20):
            total += len(block)
            if total > MAX_FILE_BYTES:
                raise ReplayError("MCAP input exceeds limit")
            snapshot.write(block)
        snapshot.seek(0)
        yield snapshot


def _records(stream):
    return StreamReader(
        stream,
        emit_chunks=True,
        validate_crcs=True,
        record_size_limit=MAX_AUDIT_RECORD_BYTES + (1 << 16),
    ).records


def read_replay(path: Path, session: str):
    """Verify the entire recording before yielding any original audit records."""
    try:
        with _snapshot(path) as stream:
            schemas, channels = {}, {}
            footer = None
            data_end = None
            count = 0
            previous = None
            expected_scene = None
            previous_stamp = None
            for item in _records(stream):
                if not isinstance(
                    item,
                    (Header, Schema, Channel, Message, DataEnd, Footer, Statistics, SummaryOffset),
                ):
                    raise ReplayError("unsupported MCAP record type")
                if isinstance(item, Header) and (
                    item.profile != PROFILE or item.library != PROFILE
                ):
                    raise ReplayError("unsupported MCAP profile")
                if isinstance(item, Schema):
                    name = (
                        "scene"
                        if item.name == "foxglove.SceneUpdate"
                        else item.name.removeprefix("sweep.").removesuffix(".v1")
                    )
                    schema = SCENE_SCHEMA if name == "scene" else channel_schema(name)
                    if (
                        item.encoding != "jsonschema"
                        or item.data != schema
                        or not 1 <= item.id <= len(TOPICS) + 1
                        or item.name
                        != ("foxglove.SceneUpdate" if name == "scene" else f"sweep.{name}.v1")
                        or schemas.get(item.id, name) != name
                    ):
                        raise ReplayError("MCAP schema mismatch")
                    schemas[item.id] = name
                elif isinstance(item, Channel):
                    name = schemas.get(item.schema_id)
                    if (
                        item.topic != f"/sweep/{name}"
                        or item.message_encoding != "json"
                        or not 1 <= item.id <= len(TOPICS) + 1
                        or channels.get(item.id, name) != name
                        or item.metadata != {"session": session, "clock": "relay_ingest_unix_ns"}
                    ):
                        raise ReplayError("MCAP channel identity mismatch")
                    channels[item.id] = name
                elif isinstance(item, Message):
                    if channels.get(item.channel_id) == "scene":
                        if (
                            expected_scene is None
                            or json.loads(item.data) != expected_scene
                            or item.log_time != previous_stamp
                            or item.publish_time != previous_stamp
                            or item.sequence != count - 1
                        ):
                            raise ReplayError("MCAP scene differs from audited evidence")
                        expected_scene = None
                        continue
                    if expected_scene is not None:
                        raise ReplayError("MCAP missing derived scene")
                    record = _decode(item.data, session, None if previous is None else previous + 1)
                    if (
                        channels.get(item.channel_id) != channel_name(record)
                        or item.log_time != timestamp_ns(record)
                        or item.publish_time != item.log_time
                        or item.sequence != count
                    ):
                        raise ReplayError("MCAP message identity or timestamp mismatch")
                    count += 1
                    if count > MAX_RECORDS:
                        raise ReplayError("MCAP record count exceeds limit")
                    previous = record["seq"]
                    expected_scene = scene_update(record)
                    previous_stamp = item.log_time
                elif isinstance(item, DataEnd):
                    if data_end is not None or not item.data_section_crc:
                        raise ReplayError("MCAP requires one data checksum")
                    data_end = item
                elif isinstance(item, Footer):
                    footer = item
            if (
                data_end is None
                or footer is None
                or not footer.summary_crc
                or expected_scene is not None
            ):
                raise ReplayError("incomplete MCAP recording")
            end = stream.tell()
            if stream.read(1):
                raise ReplayError("trailing MCAP bytes")
            footer_start = end - 8 - 29
            if not 0 < footer.summary_start <= footer.summary_offset_start <= footer_start:
                raise ReplayError("invalid MCAP summary bounds")
            stream.seek(footer.summary_start)
            checksum = 0
            remaining = footer_start - footer.summary_start
            while remaining:
                block = stream.read(min(1 << 20, remaining))
                checksum = zlib.crc32(block, checksum)
                remaining -= len(block)
            checksum = zlib.crc32(
                struct.pack("<BQQQ", 2, 20, footer.summary_start, footer.summary_offset_start),
                checksum,
            )
            if checksum != footer.summary_crc:
                raise ReplayError("MCAP summary checksum mismatch")
            stream.seek(0)
            for item in _records(stream):
                if isinstance(item, Message) and channels[item.channel_id] != "scene":
                    yield json.loads(item.data)
    except (McapError, OSError, ValueError, TypeError, KeyError, EOFError, struct.error) as error:
        raise ReplayError(str(error)) from None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export")
    export.add_argument("--audit", type=Path, required=True)
    export.add_argument("--session", required=True)
    export.add_argument("--output", type=Path, required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("--input", type=Path, required=True)
    verify.add_argument("--session", required=True)
    args = parser.parse_args(argv)
    try:
        count = (
            export_audit(args.audit, args.session, args.output)
            if args.command == "export"
            else sum(1 for _ in read_replay(args.input, args.session))
        )
        print(json.dumps({"records": count, "session": args.session}))
    except (ReplayError, OSError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
