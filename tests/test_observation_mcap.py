import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from jsonschema import validate
from mcap.reader import make_reader
from mcap.writer import CompressionType, Writer

from relay.observations import decode_observation
from tools.observation_mcap import (
    LIBRARY,
    MESSAGE_ENCODING,
    PROFILE,
    SCHEMA,
    SCHEMA_ENCODING,
    SCHEMA_NAME,
    TOPIC,
    McapError,
    export_jsonl,
    import_mcap,
)

FIXTURES = Path("relay/tests/fixtures/observation_v1")


def _jsonl(path: Path, names: tuple[str, ...]) -> list[bytes]:
    records = [(FIXTURES / name).read_bytes().rstrip(b"\n") for name in names]
    path.write_bytes(b"".join(record + b"\n" for record in records))
    return records


def test_exported_mcap_is_readable_by_the_official_reader_and_replays_exactly(tmp_path):
    input_jsonl = tmp_path / "input.jsonl"
    expected = _jsonl(
        input_jsonl,
        ("aircraft-world.json", "ground-odom-range-scan.json", "camera-tag-observation.json"),
    )
    output = tmp_path / "observations.mcap"

    assert export_jsonl(input_jsonl, output) == 3

    with output.open("rb") as source:
        messages = list(make_reader(source).iter_messages())
    messages.sort(key=lambda item: item[2].sequence)
    assert [message.data for _, _, message in messages] == expected
    for _, _, message in messages:
        validate(instance=json.loads(message.data), schema=json.loads(SCHEMA))
    assert [channel.topic for _, channel, _ in messages] == [TOPIC] * 3
    assert [message.log_time for _, _, message in messages] == [
        decode_observation(record).t_ingest * 1_000_000 for record in expected
    ]
    assert [message.publish_time for _, _, message in messages] == [
        message.log_time for _, _, message in messages
    ]

    replay_jsonl = tmp_path / "replay.jsonl"
    replayed = import_mcap(output, replay_jsonl)
    assert [item.encode() for item in replayed] == expected
    assert replay_jsonl.read_bytes() == b"".join(record + b"\n" for record in expected)


def test_import_rejects_falsified_contract_and_corrupt_mcap(tmp_path):
    encoded = _jsonl(tmp_path / "input.jsonl", ("aircraft-world.json",))[0]
    false_contract = tmp_path / "false.mcap"
    with false_contract.open("xb") as stream:
        writer = Writer(
            stream,
            compression=CompressionType.NONE,
            use_chunking=False,
            enable_data_crcs=True,
        )
        writer.start(profile=PROFILE, library=LIBRARY)
        schema_id = writer.register_schema(SCHEMA_NAME, SCHEMA_ENCODING, SCHEMA)
        channel_id = writer.register_channel("/other", MESSAGE_ENCODING, schema_id)
        writer.add_message(channel_id, 1_005_000_000, encoded, 100_000_000)
        writer.finish()
    with pytest.raises(McapError, match="channel or schema"):
        import_mcap(false_contract)

    corrupt = tmp_path / "corrupt.mcap"
    corrupt.write_bytes(false_contract.read_bytes()[:-8])
    with pytest.raises(McapError, match="invalid MCAP"):
        import_mcap(corrupt)


def test_import_rejects_an_unchecked_tampered_data_section(tmp_path):
    encoded = _jsonl(tmp_path / "input.jsonl", ("aircraft-world.json",))[0]
    unchecked = tmp_path / "unchecked.mcap"
    with unchecked.open("xb") as stream:
        writer = Writer(
            stream,
            compression=CompressionType.NONE,
            use_chunking=False,
            enable_data_crcs=False,
        )
        writer.start(profile=PROFILE, library=LIBRARY)
        schema_id = writer.register_schema(SCHEMA_NAME, SCHEMA_ENCODING, SCHEMA)
        channel_id = writer.register_channel(
            TOPIC,
            MESSAGE_ENCODING,
            schema_id,
            metadata={"sweep_contract": "observation/v1"},
        )
        writer.add_message(channel_id, 1_005_000_000, encoded, 1_005_000_000)
        writer.finish()
    unchecked.write_bytes(
        unchecked.read_bytes().replace(b'"confidence":0.9', b'"confidence":0.8', 1)
    )

    with pytest.raises(McapError, match="data section checksum"):
        import_mcap(unchecked)


def test_import_rejects_a_tampered_summary_before_using_its_metadata(tmp_path):
    input_jsonl = tmp_path / "input.jsonl"
    _jsonl(input_jsonl, ("aircraft-world.json",))
    mcap = tmp_path / "observations.mcap"
    export_jsonl(input_jsonl, mcap)
    data = mcap.read_bytes()
    offset = data.rfind(b"/sweep/observations")
    mcap.write_bytes(data[:offset] + b"/sweep/observationx" + data[offset + 19 :])

    with pytest.raises(McapError, match="summary checksum"):
        import_mcap(mcap)


def test_import_rejects_compressed_chunks_before_decompression(tmp_path, monkeypatch):
    encoded = _jsonl(tmp_path / "input.jsonl", ("aircraft-world.json",))[0]
    compressed = tmp_path / "compressed.mcap"
    with compressed.open("xb") as stream:
        writer = Writer(stream, compression=CompressionType.ZSTD)
        writer.start(profile=PROFILE, library=LIBRARY)
        schema_id = writer.register_schema(SCHEMA_NAME, SCHEMA_ENCODING, SCHEMA)
        channel_id = writer.register_channel(
            TOPIC,
            MESSAGE_ENCODING,
            schema_id,
            metadata={"sweep_contract": "observation/v1"},
        )
        writer.add_message(channel_id, 1_005_000_000, encoded, 1_005_000_000)
        writer.finish()

    def decompression_attempt(*_args, **_kwargs):
        raise AssertionError("compressed MCAP must be rejected before decompression")

    monkeypatch.setattr("mcap.stream_reader.breakup_chunk", decompression_attempt)
    with pytest.raises(McapError, match="outside the observation contract"):
        import_mcap(compressed)


def test_export_rejects_noncanonical_jsonl_and_never_replaces_output(tmp_path):
    malformed = tmp_path / "malformed.jsonl"
    record = json.loads((FIXTURES / "aircraft-world.json").read_text())
    malformed.write_text(json.dumps(record, indent=2) + "\n")
    with pytest.raises(McapError, match="canonical"):
        export_jsonl(malformed, tmp_path / "unused.mcap")

    input_jsonl = tmp_path / "input.jsonl"
    _jsonl(input_jsonl, ("aircraft-world.json",))
    output = tmp_path / "existing.mcap"
    output.write_bytes(b"existing")
    with pytest.raises(McapError, match="already exists"):
        export_jsonl(input_jsonl, output)
    assert output.read_bytes() == b"existing"


def test_export_rejects_a_core_valid_timestamp_that_mcap_cannot_represent(tmp_path):
    raw = json.loads((FIXTURES / "aircraft-world.json").read_text())
    raw["t_ingest"] = 2**63 - 1
    input_jsonl = tmp_path / "input.jsonl"
    input_jsonl.write_bytes(decode_observation(json.dumps(raw).encode()).encode() + b"\n")
    output = tmp_path / "unused.mcap"

    with pytest.raises(McapError, match="cannot be represented"):
        export_jsonl(input_jsonl, output)
    assert not output.exists()


def test_module_cli_exports_and_imports_an_actual_mcap_file(tmp_path):
    input_jsonl = tmp_path / "input.jsonl"
    expected = _jsonl(input_jsonl, ("aircraft-world.json",))
    output = tmp_path / "observations.mcap"
    replay = tmp_path / "replay.jsonl"
    root = Path(__file__).parents[1]

    exported = subprocess.run(
        [
            sys.executable,
            "-m",
            "tools.observation_mcap",
            "export",
            "--input-jsonl",
            str(input_jsonl),
            "--output",
            str(output),
        ],
        cwd=root,
        env={"PATH": os.defpath},
        capture_output=True,
        check=False,
        text=True,
    )
    assert exported.returncode == 0, exported.stderr
    assert json.loads(exported.stdout) == {"records": 1, "valid": True}

    imported = subprocess.run(
        [
            sys.executable,
            "-m",
            "tools.observation_mcap",
            "import",
            "--input-mcap",
            str(output),
            "--output-jsonl",
            str(replay),
        ],
        cwd=root,
        env={"PATH": os.defpath},
        capture_output=True,
        check=False,
        text=True,
    )
    assert imported.returncode == 0, imported.stderr
    assert replay.read_bytes() == expected[0] + b"\n"
