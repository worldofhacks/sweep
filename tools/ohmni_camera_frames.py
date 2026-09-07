"""Validate bounded Ohmni camera recordings and extract selected Gray8 frames as PNG files."""

import argparse
import hashlib
import json
import os
import stat
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path

SCHEMA_VERSION = "ohmni-camera-capture/v1"
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_RAW_BYTES = 512 * 1024 * 1024
MAX_INDEX_BYTES = 64 * 1024 * 1024
MAX_INDEX_ENTRIES = 100000
MAX_FRAME_BYTES = 16 * 1024 * 1024
MAX_DIMENSION = 4096
MAX_EXTRACTED_FRAMES = 1000
MAX_INDEX_LINE_BYTES = 512


class CaptureError(ValueError):
    pass


@dataclass(frozen=True)
class CameraFrame:
    index: int
    received_monotonic_ns: int
    gray8: bytes


@dataclass(frozen=True)
class _FrameIndex:
    index: int
    offset_bytes: int
    length_bytes: int
    received_monotonic_ns: int


class CameraCapture:
    """A verified capture whose raw file remains open for bounded iteration."""

    def __init__(self, manifest, frames_handle, frame_index):
        self.metadata = manifest
        self._frames_handle = frames_handle
        self._frame_index = tuple(frame_index)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def close(self):
        if self._frames_handle is not None:
            self._frames_handle.close()
            self._frames_handle = None

    def frames(self):
        if self._frames_handle is None:
            raise CaptureError("camera capture is closed")
        self._frames_handle.seek(0)
        for row in self._frame_index:
            self._frames_handle.seek(row.offset_bytes)
            gray8 = self._frames_handle.read(row.length_bytes)
            if len(gray8) != row.length_bytes:
                raise CaptureError("raw frame file changed after validation")
            yield CameraFrame(row.index, row.received_monotonic_ns, gray8)


def _require(condition, message):
    if not condition:
        raise CaptureError(message)


def _integer(value, name, minimum=0, maximum=None):
    _require(type(value) is int, f"{name} must be an integer")
    _require(value >= minimum, f"{name} is below its allowed range")
    if maximum is not None:
        _require(value <= maximum, f"{name} exceeds its allowed range")
    return value


def _text(value, name):
    _require(isinstance(value, str) and value.strip(), f"{name} must be nonempty text")
    return value


def _safe_sidecar_name(value, name):
    _text(value, name)
    candidate = Path(value)
    _require(not candidate.is_absolute() and candidate.name == value, f"{name} must be a file name")
    return value


def _require_real_directory(path):
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        entry = os.lstat(current)
        _require(not stat.S_ISLNK(entry.st_mode), "capture path contains a symlink")
        _require(stat.S_ISDIR(entry.st_mode), "capture path contains a non-directory")


def _capture_directory(path):
    directory = Path(path).absolute()
    _require_real_directory(directory)
    return directory


def _open_regular(directory, name, maximum):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(directory / name, flags)
    try:
        entry = os.fstat(descriptor)
        _require(stat.S_ISREG(entry.st_mode), f"{name} must be a regular file")
        _require(entry.st_size <= maximum, f"{name} exceeds its allowed size")
        return os.fdopen(descriptor, "rb")
    except Exception:
        os.close(descriptor)
        raise


def _sha256(handle):
    digest = hashlib.sha256()
    handle.seek(0)
    while True:
        block = handle.read(1024 * 1024)
        if not block:
            handle.seek(0)
            return digest.hexdigest()
        digest.update(block)


def _read_manifest(directory):
    with _open_regular(directory, "manifest.json", MAX_MANIFEST_BYTES) as handle:
        payload = handle.read(MAX_MANIFEST_BYTES + 1)
    _require(len(payload) <= MAX_MANIFEST_BYTES, "manifest.json exceeds its allowed size")
    try:
        manifest = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CaptureError(f"manifest.json is not valid JSON: {exc}") from exc
    _require(isinstance(manifest, dict), "manifest.json must contain an object")
    return manifest


def _validate_manifest(manifest):
    _require(manifest.get("schema_version") == SCHEMA_VERSION, "unsupported camera capture schema")
    _require(manifest.get("status") == "complete", "camera capture is not complete")
    run_id = _text(manifest.get("run_id"), "run_id")
    provenance = manifest.get("provenance")
    _require(isinstance(provenance, dict), "provenance must be an object")
    _require(provenance.get("run_id") == run_id, "run_id and provenance.run_id disagree")
    for name in ("device_id", "camera_id", "camera_holder"):
        _text(provenance.get(name), f"provenance.{name}")

    image = manifest.get("image")
    _require(isinstance(image, dict), "image must be an object")
    _require(image.get("pixel_format") == "gray8", "image.pixel_format must be gray8")
    width = _integer(image.get("width"), "image.width", 1, MAX_DIMENSION)
    height = _integer(image.get("height"), "image.height", 1, MAX_DIMENSION)
    frame_bytes = _integer(image.get("frame_bytes"), "image.frame_bytes", 1, MAX_FRAME_BYTES)
    _require(frame_bytes == width * height, "image.frame_bytes must equal width times height")
    _require(
        image.get("hal_format_code") is None or type(image.get("hal_format_code")) is int,
        "image.hal_format_code must be an integer or null",
    )

    capture = manifest.get("capture")
    _require(isinstance(capture, dict), "capture must be an object")
    _text(capture.get("receipt_clock_domain"), "capture.receipt_clock_domain")
    _text(capture.get("receipt_timestamp_meaning"), "capture.receipt_timestamp_meaning")
    correlation = capture.get("utc_monotonic_correlation")
    _require(isinstance(correlation, dict), "capture.utc_monotonic_correlation must be an object")
    _integer(correlation.get("monotonic_ns"), "capture.utc_monotonic_correlation.monotonic_ns")
    _integer(correlation.get("utc_ns"), "capture.utc_monotonic_correlation.utc_ns")

    recording = manifest.get("recording")
    _require(isinstance(recording, dict), "recording must be an object")
    max_bytes = _integer(recording.get("max_bytes"), "recording.max_bytes", 1, MAX_RAW_BYTES)
    total_bytes = _integer(recording.get("bytes"), "recording.bytes", 1, max_bytes)
    max_entries = _integer(
        recording.get("max_frame_index_entries"),
        "recording.max_frame_index_entries",
        1,
        MAX_INDEX_ENTRIES,
    )
    frame_count = _integer(recording.get("frame_count"), "recording.frame_count", 1, max_entries)
    _safe_sidecar_name(recording.get("frames_file"), "recording.frames_file")
    _safe_sidecar_name(recording.get("frame_index_file"), "recording.frame_index_file")
    for name in ("frames_sha256", "frame_index_sha256"):
        value = recording.get(name)
        _require(
            isinstance(value, str)
            and len(value) == 64
            and all(character in "0123456789abcdef" for character in value),
            f"recording.{name} must be a lowercase SHA-256",
        )
    _require(
        total_bytes == frame_count * frame_bytes,
        "recording bytes do not fit the image layout",
    )
    return recording, frame_bytes, frame_count


def _read_frame_index(handle, count, frame_bytes, total_bytes):
    rows = []
    previous_timestamp = None
    for expected in range(count):
        line = handle.readline(MAX_INDEX_LINE_BYTES + 1)
        _require(line and len(line) <= MAX_INDEX_LINE_BYTES, "frame index line is invalid")
        try:
            row = json.loads(line.decode("ascii"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CaptureError(f"frame index contains invalid JSON: {exc}") from exc
        _require(isinstance(row, dict), "frame index record must be an object")
        _require(
            set(row) == {"index", "offset_bytes", "length_bytes", "received_monotonic_ns"},
            "frame index record fields are invalid",
        )
        index = _integer(row["index"], "frame index", 0)
        offset = _integer(row["offset_bytes"], "frame offset", 0)
        length = _integer(row["length_bytes"], "frame length", 1, MAX_FRAME_BYTES)
        timestamp = _integer(row["received_monotonic_ns"], "frame receipt timestamp", 0)
        _require(index == expected, "frame index order is not sequential")
        _require(offset == expected * frame_bytes, "frame offsets are not sequential")
        _require(length == frame_bytes, "frame length does not match image layout")
        _require(offset + length <= total_bytes, "frame range exceeds raw file")
        if previous_timestamp is not None:
            _require(timestamp >= previous_timestamp, "frame receipt timestamps are not ordered")
        previous_timestamp = timestamp
        rows.append(_FrameIndex(index, offset, length, timestamp))
    _require(not handle.read(1), "frame index has records beyond frame_count")
    _require(
        rows[-1].offset_bytes + rows[-1].length_bytes == total_bytes,
        "frame index misses bytes",
    )
    return rows


def open_capture(path):
    directory = _capture_directory(path)
    _require(not os.path.lexists(directory / "INCOMPLETE"), "camera capture is marked incomplete")
    manifest = _read_manifest(directory)
    recording, frame_bytes, frame_count = _validate_manifest(manifest)
    frames_handle = _open_regular(directory, recording["frames_file"], MAX_RAW_BYTES)
    try:
        _require(
            os.fstat(frames_handle.fileno()).st_size == recording["bytes"],
            "raw frame file size does not match manifest",
        )
        _require(_sha256(frames_handle) == recording["frames_sha256"], "raw frame hash mismatch")
        with _open_regular(
            directory, recording["frame_index_file"], MAX_INDEX_BYTES
        ) as index_handle:
            _require(
                _sha256(index_handle) == recording["frame_index_sha256"],
                "frame index hash mismatch",
            )
            frame_index = _read_frame_index(
                index_handle,
                frame_count,
                frame_bytes,
                recording["bytes"],
            )
        return CameraCapture(manifest, frames_handle, frame_index)
    except Exception:
        frames_handle.close()
        raise


def _png_chunk(name, payload):
    checksum = struct.pack(">I", zlib.crc32(name + payload))
    return struct.pack(">I", len(payload)) + name + payload + checksum


def _write_png(path, frame, width, height):
    scanlines = b"".join(
        b"\x00" + frame.gray8[row * width : (row + 1) * width] for row in range(height)
    )
    signature = b"\x89PNG\r\n\x1a\n"
    header = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    with open(path, "xb") as handle:
        handle.write(signature)
        handle.write(_png_chunk(b"IHDR", header))
        handle.write(_png_chunk(b"IDAT", zlib.compress(scanlines)))
        handle.write(_png_chunk(b"IEND", b""))


def _create_output_directory(path):
    output = Path(path).absolute()
    _require_real_directory(output.parent)
    _require(not os.path.lexists(output), "output directory already exists")
    output.mkdir(mode=0o700)
    return output


def extract_pngs(capture, output_dir, every_nth=10, max_frames=MAX_EXTRACTED_FRAMES):
    _integer(every_nth, "every_nth", 1)
    _integer(max_frames, "max_frames", 1, MAX_EXTRACTED_FRAMES)
    output = _create_output_directory(output_dir)
    image = capture.metadata["image"]
    written = []
    for frame in capture.frames():
        if frame.index % every_nth:
            continue
        path = output / f"frame-{frame.index:06d}.png"
        _write_png(path, frame, image["width"], image["height"])
        written.append(path)
        if len(written) == max_frames:
            break
    return written


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--every-nth", type=int, default=10)
    parser.add_argument("--max-frames", type=int, default=MAX_EXTRACTED_FRAMES)
    args = parser.parse_args(argv)
    try:
        with open_capture(args.capture_dir) as capture:
            frames = extract_pngs(capture, args.output_dir, args.every_nth, args.max_frames)
            result = {
                "camera_id": capture.metadata["provenance"]["camera_id"],
                "frames": len(frames),
                "output_dir": str(args.output_dir),
                "run_id": capture.metadata["run_id"],
            }
    except (CaptureError, OSError) as exc:
        print(json.dumps({"valid": False, "error": str(exc)}))
        return 1
    print(json.dumps({"valid": True, **result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
