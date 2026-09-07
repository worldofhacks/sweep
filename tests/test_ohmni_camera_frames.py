import hashlib
import json
import struct
import zlib

import pytest

from tools import ohmni_camera_frames


def _canonical_json(data):
    return (json.dumps(data, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")


def _capture(tmp_path, frames=(b"\x00\x40\x80\xff", b"\xff\x80\x40\x00"), index_rows=None):
    directory = tmp_path / "capture"
    directory.mkdir()
    raw = b"".join(frames)
    if index_rows is None:
        index_rows = [
            {
                "index": index,
                "offset_bytes": index * 4,
                "length_bytes": 4,
                "received_monotonic_ns": 1000 + index * 10,
            }
            for index in range(len(frames))
        ]
    index = b"".join(_canonical_json(row) for row in index_rows)
    (directory / "frames.gray").write_bytes(raw)
    (directory / "frames.jsonl").write_bytes(index)
    manifest = {
        "schema_version": "ohmni-camera-capture/v1",
        "status": "complete",
        "run_id": "camera-smoke-001",
        "capture": {
            "started_monotonic_ns": 900,
            "ended_monotonic_ns": 1100,
            "started_utc_ns": 2000,
            "ended_utc_ns": 2200,
            "receipt_clock_domain": "capture_host_monotonic",
            "receipt_timestamp_meaning": "receiver receipt, not device capture or exposure",
            "utc_monotonic_correlation": {"monotonic_ns": 900, "utc_ns": 2000},
        },
        "image": {
            "pixel_format": "gray8",
            "width": 2,
            "height": 2,
            "frame_bytes": 4,
            "hal_format_code": 7,
            "ordering": "hal_delivery_order",
        },
        "measurements": {},
        "provenance": {
            "run_id": "camera-smoke-001",
            "device_id": "ohmni-5-2",
            "camera_id": "downward-fisheye",
            "camera_holder": "call",
        },
        "recording": {
            "frames_file": "frames.gray",
            "frames_sha256": hashlib.sha256(raw).hexdigest(),
            "frame_index_file": "frames.jsonl",
            "frame_index_sha256": hashlib.sha256(index).hexdigest(),
            "bytes": len(raw),
            "frame_count": len(frames),
            "max_bytes": 512 * 1024 * 1024,
            "max_frame_index_entries": 100000,
        },
    }
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return directory


def _png_gray8(path):
    payload = path.read_bytes()
    assert payload.startswith(b"\x89PNG\r\n\x1a\n")
    cursor = 8
    chunks = []
    while cursor < len(payload):
        length = struct.unpack(">I", payload[cursor : cursor + 4])[0]
        name = payload[cursor + 4 : cursor + 8]
        data = payload[cursor + 8 : cursor + 8 + length]
        chunks.append((name, data))
        cursor += 12 + length
    header = next(data for name, data in chunks if name == b"IHDR")
    width, height, depth, color, compression, filtering, interlace = struct.unpack(
        ">IIBBBBB", header
    )
    assert (width, height, depth, color, compression, filtering, interlace) == (
        2,
        2,
        8,
        0,
        0,
        0,
        0,
    )
    return zlib.decompress(b"".join(data for name, data in chunks if name == b"IDAT"))


def test_open_capture_yields_verified_gray8_frames_and_metadata(tmp_path):
    directory = _capture(tmp_path)

    with ohmni_camera_frames.open_capture(directory) as capture:
        frames = list(capture.frames())
        assert capture.metadata["run_id"] == "camera-smoke-001"
        assert capture.metadata["provenance"]["camera_id"] == "downward-fisheye"

    assert [(frame.index, frame.received_monotonic_ns, frame.gray8) for frame in frames] == [
        (0, 1000, b"\x00\x40\x80\xff"),
        (1, 1010, b"\xff\x80\x40\x00"),
    ]


def test_reader_accepts_the_shared_rtsp_gray8_variant(tmp_path):
    directory = _capture(tmp_path)
    manifest = json.loads((directory / "manifest.json").read_text())
    manifest["image"]["hal_format_code"] = None
    manifest["provenance"]["camera_holder"] = "rtsp_decoder"
    manifest["capture"]["receipt_clock_domain"] = "laptop_decode_monotonic"
    manifest["capture"]["receipt_timestamp_meaning"] = "host_decode_complete"
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    with ohmni_camera_frames.open_capture(directory) as capture:
        assert [frame.index for frame in capture.frames()] == [0, 1]


def test_reader_rejects_marked_incomplete_or_nonsequential_index(tmp_path):
    directory = _capture(tmp_path)
    (directory / "INCOMPLETE").write_text("in progress", encoding="utf-8")
    with pytest.raises(ohmni_camera_frames.CaptureError, match="marked incomplete"):
        ohmni_camera_frames.open_capture(directory)

    (directory / "INCOMPLETE").unlink()
    bad_index = [
        {"index": 0, "offset_bytes": 0, "length_bytes": 4, "received_monotonic_ns": 1000},
        {"index": 1, "offset_bytes": 5, "length_bytes": 4, "received_monotonic_ns": 1010},
    ]
    index = b"".join(_canonical_json(row) for row in bad_index)
    (directory / "frames.jsonl").write_bytes(index)
    manifest = json.loads((directory / "manifest.json").read_text())
    manifest["recording"]["frame_index_sha256"] = hashlib.sha256(index).hexdigest()
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ohmni_camera_frames.CaptureError, match="offsets are not sequential"):
        ohmni_camera_frames.open_capture(directory)


def test_cli_extracts_bounded_pngs_with_their_source_frame_indices(tmp_path, capsys):
    directory = _capture(
        tmp_path,
        frames=(
            b"\x00\x40\x80\xff",
            b"\xff\x80\x40\x00",
            b"\x01\x02\x03\x04",
            b"\x04\x03\x02\x01",
        ),
    )
    output = tmp_path / "pngs"

    assert ohmni_camera_frames.main(
        [
            "--capture-dir",
            str(directory),
            "--output-dir",
            str(output),
            "--every-nth",
            "2",
            "--max-frames",
            "2",
        ]
    ) == 0
    result = json.loads(capsys.readouterr().out)
    assert result == {
        "camera_id": "downward-fisheye",
        "frames": 2,
        "output_dir": str(output),
        "run_id": "camera-smoke-001",
        "valid": True,
    }
    assert _png_gray8(output / "frame-000000.png") == b"\x00\x00\x40\x00\x80\xff"
    assert _png_gray8(output / "frame-000002.png") == b"\x00\x01\x02\x00\x03\x04"


def test_extractor_refuses_an_existing_output_directory(tmp_path):
    directory = _capture(tmp_path)
    output = tmp_path / "pngs"
    output.mkdir()
    with ohmni_camera_frames.open_capture(directory) as capture:
        with pytest.raises(ohmni_camera_frames.CaptureError, match="already exists"):
            ohmni_camera_frames.extract_pngs(capture, output)
