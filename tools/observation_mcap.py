"""Export and replay canonical observation JSONL through a bounded MCAP file."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import stat
import struct
import tempfile
import zlib
from collections.abc import Sequence
from contextlib import contextmanager
from pathlib import Path

from mcap.opcode import Opcode
from mcap.reader import FOOTER_SIZE, MAGIC_SIZE, NonSeekingReader, SeekingReader
from mcap.records import (
    Attachment,
    AttachmentIndex,
    Chunk,
    ChunkIndex,
    DataEnd,
    Footer,
    MessageIndex,
    Metadata,
    MetadataIndex,
)
from mcap.stream_reader import StreamReader
from mcap.writer import CompressionType, Writer

from relay.observations import MAX_EVENT_BYTES, Observation, decode_observation

MAX_RECORDS = 10_000
MAX_TOTAL_MESSAGE_BYTES = 64 * 1024 * 1024
MAX_MCAP_BYTES = MAX_TOTAL_MESSAGE_BYTES + 8 * 1024 * 1024
MAX_MCAP_RECORD_BYTES = 128 * 1024
MAX_MCAP_TIME_NS = 2**64 - 1
PROFILE = "sweep-observation/v1"
LIBRARY = "sweep-observation-mcap/1"
TOPIC = "/sweep/observations"
SCHEMA_NAME = "sweep.observation.v1"
SCHEMA_ENCODING = "jsonschema"
MESSAGE_ENCODING = "json"
CHANNEL_METADATA = {"sweep_contract": "observation/v1"}
SCHEMA = (Path(__file__).resolve().parents[1] / "schemas/observation-v1.schema.json").read_bytes()


class McapError(ValueError):
    pass


def export_jsonl(input_jsonl: Path, output: Path) -> int:
    records = _read_jsonl(input_jsonl)
    for observation in records:
        _ingest_time_ns(observation)
    _write_mcap(records, output)
    return len(records)


def import_mcap(input_mcap: Path, output_jsonl: Path | None = None) -> tuple[Observation, ...]:
    records = _read_mcap(input_mcap)
    if output_jsonl is not None:
        _write_jsonl(records, output_jsonl)
    return records


def _read_jsonl(path: Path) -> tuple[Observation, ...]:
    total = 0
    records: list[Observation] = []
    with _open_regular(path, MAX_TOTAL_MESSAGE_BYTES, "rb") as source:
        while line := source.readline(MAX_EVENT_BYTES + 2):
            if len(line) > MAX_EVENT_BYTES + 1:
                raise McapError("JSONL observation exceeds the per-message byte limit")
            if not line.endswith(b"\n"):
                raise McapError("JSONL observations must end with a newline")
            encoded = line[:-1]
            if not encoded:
                raise McapError("JSONL observations must not contain blank lines")
            observation = _decode(encoded)
            canonical = observation.encode()
            if encoded != canonical:
                raise McapError("JSONL observation is not canonical")
            total += len(canonical)
            _check_record_bounds(len(records), total)
            records.append(observation)
    if not records:
        raise McapError("JSONL input contains no observations")
    return tuple(records)


def _read_mcap(path: Path) -> tuple[Observation, ...]:
    try:
        records: list[Observation] = []
        total = 0
        with _mcap_snapshot(path) as source:
            _preflight_mcap(source)
            _validate_mcap_header(source)
            source.seek(0)
            reader = NonSeekingReader(
                source, validate_crcs=True, record_size_limit=MAX_MCAP_RECORD_BYTES
            )
            for schema, channel, message in reader.iter_messages(log_time_order=False):
                _validate_channel(schema, channel)
                if not isinstance(message.data, bytes) or len(message.data) > MAX_EVENT_BYTES:
                    raise McapError("MCAP message exceeds the observation byte limit")
                observation = _decode(message.data)
                canonical = observation.encode()
                if message.data != canonical:
                    raise McapError("MCAP observation body is not canonical")
                if message.log_time != _ingest_time_ns(observation):
                    raise McapError("MCAP log time does not match relay ingest time")
                if message.publish_time != _ingest_time_ns(observation):
                    raise McapError("MCAP publish time does not match relay ingest time")
                if message.sequence != len(records):
                    raise McapError("MCAP observation sequence is not canonical")
                total += len(canonical)
                _check_record_bounds(len(records), total)
                records.append(observation)
    except McapError:
        raise
    except Exception as error:
        raise McapError(f"invalid MCAP input: {error}") from None
    if not records:
        raise McapError("MCAP input contains no observations")
    return tuple(records)


def _preflight_mcap(source) -> None:
    source.seek(0)
    reader = StreamReader(
        source,
        emit_chunks=True,
        validate_crcs=True,
        record_size_limit=MAX_MCAP_RECORD_BYTES,
    )
    unsupported = (
        Attachment,
        AttachmentIndex,
        Chunk,
        ChunkIndex,
        MessageIndex,
        Metadata,
        MetadataIndex,
    )
    data_ends: list[DataEnd] = []
    footers: list[Footer] = []
    for record in reader.records:
        if isinstance(record, unsupported):
            raise McapError("MCAP contains a record outside the observation contract")
        if isinstance(record, DataEnd):
            data_ends.append(record)
        elif isinstance(record, Footer):
            footers.append(record)
    if len(data_ends) != 1 or data_ends[0].data_section_crc == 0:
        raise McapError("MCAP must contain one nonzero data section checksum")
    if len(footers) != 1:
        raise McapError("MCAP must contain one footer")
    _validate_summary_crc(source, footers[0])


def _validate_summary_crc(source, footer: Footer) -> None:
    source.seek(0, os.SEEK_END)
    footer_start = source.tell() - FOOTER_SIZE - MAGIC_SIZE
    if (
        footer.summary_crc == 0
        or footer.summary_start == 0
        or footer.summary_start > footer_start
        or not footer.summary_start <= footer.summary_offset_start <= footer_start
    ):
        raise McapError("MCAP summary checksum is required")

    source.seek(footer.summary_start)
    checksum = 0
    remaining = footer_start - footer.summary_start
    while remaining:
        block = source.read(min(64 * 1024, remaining))
        if not block:
            raise McapError("MCAP summary ends before its footer")
        checksum = zlib.crc32(block, checksum)
        remaining -= len(block)
    checksum = zlib.crc32(
        struct.pack(
            "<BQQQ",
            Opcode.FOOTER,
            20,
            footer.summary_start,
            footer.summary_offset_start,
        ),
        checksum,
    )
    if checksum != footer.summary_crc:
        raise McapError("MCAP summary checksum does not match")


def _validate_mcap_header(source) -> None:
    source.seek(0)
    reader = SeekingReader(source, validate_crcs=True, record_size_limit=MAX_MCAP_RECORD_BYTES)
    header = reader.get_header()
    if header.profile != PROFILE or header.library != LIBRARY:
        raise McapError("MCAP header does not declare the observation contract")
    summary = reader.get_summary()
    if (
        summary is None
        or summary.statistics is None
        or summary.statistics.message_count > MAX_RECORDS
    ):
        raise McapError("MCAP record count exceeds the observation limit")
    if len(summary.schemas) != 1 or len(summary.channels) != 1:
        raise McapError("MCAP must contain exactly one observation schema and channel")
    schema = next(iter(summary.schemas.values()))
    channel = next(iter(summary.channels.values()))
    _validate_channel(schema, channel)


def _validate_channel(schema: object, channel: object) -> None:
    if (
        getattr(schema, "name", None) != SCHEMA_NAME
        or getattr(schema, "encoding", None) != SCHEMA_ENCODING
        or getattr(schema, "data", None) != SCHEMA
        or getattr(channel, "topic", None) != TOPIC
        or getattr(channel, "message_encoding", None) != MESSAGE_ENCODING
        or getattr(channel, "metadata", None) != CHANNEL_METADATA
        or getattr(channel, "schema_id", None) != getattr(schema, "id", None)
    ):
        raise McapError("MCAP channel or schema does not match the observation contract")


def _decode(encoded: bytes) -> Observation:
    try:
        return decode_observation(encoded)
    except ValueError as error:
        raise McapError(f"invalid canonical observation: {error}") from None


def _check_record_bounds(index: int, total: int) -> None:
    if index >= MAX_RECORDS:
        raise McapError("observation record count exceeds the limit")
    if total > MAX_TOTAL_MESSAGE_BYTES:
        raise McapError("observation messages exceed the total byte limit")


def _ingest_time_ns(observation: Observation) -> int:
    return _mcap_time_ns(observation.t_ingest, "ms")


def _mcap_time_ns(value: int, unit: str) -> int:
    timestamp = value * (1_000_000 if unit == "ms" else 1)
    if timestamp > MAX_MCAP_TIME_NS:
        raise McapError("observation timestamp cannot be represented by MCAP")
    return timestamp


def _write_mcap(records: tuple[Observation, ...], output: Path) -> None:
    temporary, stream = _exclusive_temporary(output)
    try:
        writer = Writer(
            stream,
            compression=CompressionType.NONE,
            use_chunking=False,
            enable_data_crcs=True,
        )
        writer.start(profile=PROFILE, library=LIBRARY)
        schema_id = writer.register_schema(SCHEMA_NAME, SCHEMA_ENCODING, SCHEMA)
        channel_id = writer.register_channel(
            TOPIC, MESSAGE_ENCODING, schema_id, metadata=CHANNEL_METADATA
        )
        for sequence, observation in enumerate(records):
            writer.add_message(
                channel_id,
                _ingest_time_ns(observation),
                observation.encode(),
                _ingest_time_ns(observation),
                sequence=sequence,
            )
        writer.finish()
        stream.flush()
        os.fsync(stream.fileno())
        if os.fstat(stream.fileno()).st_size > MAX_MCAP_BYTES:
            raise McapError("MCAP output exceeds the total byte limit")
    except Exception:
        stream.close()
        _discard(temporary)
        raise
    stream.close()
    _publish_exclusive(temporary, output)


def _write_jsonl(records: tuple[Observation, ...], output: Path) -> None:
    temporary, stream = _exclusive_temporary(output)
    try:
        for observation in records:
            stream.write(observation.encode() + b"\n")
        stream.flush()
        os.fsync(stream.fileno())
    except Exception:
        stream.close()
        _discard(temporary)
        raise
    stream.close()
    _publish_exclusive(temporary, output)


def _open_regular(path: Path, maximum: int, mode: str):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
            raise McapError("input must be a bounded regular file")
        return os.fdopen(descriptor, mode)
    except Exception:
        os.close(descriptor)
        raise


@contextmanager
def _mcap_snapshot(path: Path):
    with (
        _open_regular(path, MAX_MCAP_BYTES, "rb") as source,
        tempfile.TemporaryFile("w+b") as snapshot,
    ):
        total = 0
        while block := source.read(64 * 1024):
            total += len(block)
            if total > MAX_MCAP_BYTES:
                raise McapError("MCAP input exceeds the total byte limit")
            snapshot.write(block)
        snapshot.seek(0)
        yield snapshot


def _exclusive_temporary(output: Path):
    if output.name in {"", ".", ".."} or not output.parent.is_dir():
        raise McapError("output must name a file in an existing directory")
    temporary = output.parent / f".{output.name}.{secrets.token_hex(16)}.pending"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    return temporary, os.fdopen(descriptor, "wb")


def _publish_exclusive(temporary: Path, output: Path) -> None:
    try:
        os.link(temporary, output, follow_symlinks=False)
        directory = os.open(output.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except FileExistsError as error:
        raise McapError("output already exists") from error
    finally:
        _discard(temporary)


def _discard(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export")
    export.add_argument("--input-jsonl", type=Path, required=True)
    export.add_argument("--output", type=Path, required=True)
    import_ = commands.add_parser("import")
    import_.add_argument("--input-mcap", type=Path, required=True)
    import_.add_argument("--output-jsonl", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "export":
            count = export_jsonl(args.input_jsonl, args.output)
        else:
            count = len(import_mcap(args.input_mcap, args.output_jsonl))
    except (McapError, OSError) as error:
        parser.error(str(error))
    print(json.dumps({"records": count, "valid": True}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
