"""Measure real per-frame decode latency through the live localization decode path.

Times each cv2.VideoCapture(..., cv2.CAP_FFMPEG).read() call against an RTSP
source -- the same call perception.webcam_stream._decode makes in the live
loop -- and writes the raw sample stream to samples.json.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import cv2


def capture(url: str, *, min_duration_s: float, min_samples: int) -> dict[str, object]:
    capture = cv2.VideoCapture(
        url,
        cv2.CAP_FFMPEG,
        [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 5000, cv2.CAP_PROP_READ_TIMEOUT_MSEC, 5000],
    )
    if not capture.isOpened():
        raise RuntimeError("could not open RTSP source")
    samples_ms: list[float] = []
    sample_times_ms: list[float] = []
    start = time.monotonic()
    try:
        while True:
            read_start = time.monotonic()
            ok, frame = capture.read()
            read_end = time.monotonic()
            if not ok:
                continue
            elapsed_since_start = (read_end - start) * 1000
            if sample_times_ms and elapsed_since_start <= sample_times_ms[-1]:
                continue
            samples_ms.append((read_end - read_start) * 1000)
            sample_times_ms.append(elapsed_since_start)
            span_ms = sample_times_ms[-1] - sample_times_ms[0]
            if span_ms >= min_duration_s * 1000 and len(samples_ms) >= min_samples:
                break
    finally:
        capture.release()
    return {
        "duration_ms": int(sample_times_ms[-1]) + 1,
        "samples_ms": samples_ms,
        "sample_times_ms": sample_times_ms,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url-env", default="SWEEP_LOCALIZATION_RTSP_URL")
    parser.add_argument("--min-duration-s", type=float, default=65)
    parser.add_argument("--min-samples", type=int, default=25)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    import os

    result = capture(
        os.environ[args.url_env],
        min_duration_s=args.min_duration_s,
        min_samples=args.min_samples,
    )
    args.output.write_text(json.dumps(result, indent=2))
    print(
        json.dumps(
            {
                "sample_count": len(result["samples_ms"]),
                "span_ms": result["sample_times_ms"][-1] - result["sample_times_ms"][0],
            }
        )
    )


if __name__ == "__main__":
    main()
