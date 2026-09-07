"""Record bounded head-camera Gray8 evidence from the existing RTSP stream."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from pathlib import Path

import cv2

from perception.webcam_stream import WebcamStream
from tools.map_common import finite_number

MAX_BYTES = 512 * 1024 * 1024
MAX_FRAMES = 10000


def record_head(
    url: str,
    output: Path,
    *,
    run_id: str,
    device_id: str,
    camera_id: str,
    duration_s: float = 60,
    max_bytes: int = MAX_BYTES,
) -> dict[str, object]:
    """Store decode receipt times; source exposure time and latency are unavailable."""
    duration_s = finite_number(duration_s, "duration")
    if (
        not 0 < duration_s <= 300
        or type(max_bytes) is not int
        or not 307200 <= max_bytes <= MAX_BYTES
    ):
        raise ValueError(
            "duration must be at most 300 seconds and bytes between one frame and 512 MiB"
        )
    if any(
        not isinstance(value, str) or not 1 <= len(value) <= 128
        for value in (run_id, device_id, camera_id)
    ):
        raise ValueError("run, device and camera identities must be 1 to 128 characters")
    stream = WebcamStream(url, resolution=(640, 480))
    output.mkdir(parents=False, exist_ok=False)
    (output / "INCOMPLETE").touch(exist_ok=False)
    started_mono, started_utc = time.monotonic_ns(), time.time_ns()
    hashes = [hashlib.sha256(), hashlib.sha256()]
    count, size, first, last = 0, 0, None, None
    with (output / "frames.gray").open("xb") as raw, (output / "frames.jsonl").open("xb") as index:
        with stream:
            while time.monotonic_ns() - started_mono < duration_s * 1e9:
                result = stream.read(0.1)
                if result is None:
                    continue
                frame, received_s = result
                received_ns = int(received_s * 1e9)
                if last is not None and received_ns <= last:
                    raise ValueError("decoder receipt timestamps did not increase")
                payload = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).tobytes()
                if size + len(payload) > max_bytes or count == MAX_FRAMES:
                    break
                row = dict(
                    index=count,
                    offset_bytes=size,
                    length_bytes=len(payload),
                    received_monotonic_ns=received_ns,
                )
                line = (json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n").encode()
                raw.write(payload)
                index.write(line)
                hashes[0].update(payload)
                hashes[1].update(line)
                first = received_ns if first is None else first
                last = received_ns
                count += 1
                size += len(payload)
        for file in (raw, index):
            file.flush()
            os.fsync(file.fileno())
    if not count:
        raise ValueError("no matching 640x480 frames received")
    ended_mono, ended_utc = time.monotonic_ns(), time.time_ns()
    manifest = {
        "schema_version": "ohmni-camera-capture/v1",
        "status": "complete",
        "run_id": run_id,
        "capture": {
            "started_monotonic_ns": started_mono,
            "ended_monotonic_ns": ended_mono,
            "started_utc_ns": started_utc,
            "ended_utc_ns": ended_utc,
            "utc_monotonic_correlation": {"monotonic_ns": started_mono, "utc_ns": started_utc},
            "receipt_clock_domain": "capture_host_monotonic",
            "receipt_timestamp_meaning": "host decode completion; source exposure time unavailable",
            "first_received_monotonic_ns": first,
            "last_received_monotonic_ns": last,
        },
        "image": {
            "pixel_format": "gray8",
            "width": 640,
            "height": 480,
            "frame_bytes": 307200,
            "hal_format_code": None,
            "ordering": "decoder_delivery_order",
        },
        "recording": {
            "frames_file": "frames.gray",
            "frames_sha256": hashes[0].hexdigest(),
            "frame_index_file": "frames.jsonl",
            "frame_index_sha256": hashes[1].hexdigest(),
            "bytes": size,
            "frame_count": count,
            "max_bytes": max_bytes,
            "max_frame_index_entries": MAX_FRAMES,
        },
        "provenance": {
            "run_id": run_id,
            "device_id": device_id,
            "camera_id": camera_id,
            "camera_holder": "rtsp_decoder",
            "decoder": "opencv_ffmpeg",
            "resolution_px": [640, 480],
        },
        "measurements": {
            "received_fps_mean": count / ((ended_mono - started_mono) / 1e9),
            "latency": {
                "status": "unavailable",
                "reason": "RTSP decoder supplies no exposure timestamp",
            },
        },
    }
    temporary = output / "manifest.pending.json"
    with temporary.open("x") as handle:
        json.dump(manifest, handle, indent=2, allow_nan=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.rename(temporary, output / "manifest.json")
    (output / "INCOMPLETE").unlink()
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url-env", default="OHMNI_HEAD_RTSP_URL")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--device-id", required=True)
    parser.add_argument("--camera-id", required=True)
    parser.add_argument("--duration-s", type=float, default=60)
    parser.add_argument("--max-bytes", type=int, default=MAX_BYTES)
    args = parser.parse_args()
    try:
        record_head(
            os.environ.get(args.url_env, ""),
            args.output,
            run_id=args.run_id,
            device_id=args.device_id,
            camera_id=args.camera_id,
            duration_s=args.duration_s,
            max_bytes=args.max_bytes,
        )
    except (ValueError, OSError, RuntimeError, cv2.error):
        raise SystemExit("head capture failed; check source access, output and bounds") from None


if __name__ == "__main__":
    main()
