"""Collect decoded checkerboard or visible-clock frames through the localization reader."""

import argparse
import json
import math
import os
import time
from pathlib import Path

import cv2

from perception.webcam_stream import WebcamStream


def collect(url, output, count=30, interval=1.0, duration=90.0):
    if type(count) is not int or not 1 <= count <= 1000:
        raise ValueError("count must be between 1 and 1000")
    if not math.isfinite(interval) or not math.isfinite(duration) or min(interval, duration) <= 0:
        raise ValueError("interval and duration must be positive seconds")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    start = time.monotonic()
    next_frame = start
    captured = 0
    with (output / "decode-times.jsonl").open("x") as log, WebcamStream(url) as stream:
        while captured < count and time.monotonic() - start < duration:
            frame = stream.read(0.1)
            if frame is None or frame[1] < next_frame:
                continue
            image, decoded = frame
            name = f"frame-{captured:04}.png"
            if not cv2.imwrite(str(output / name), image):
                raise OSError("cannot save decoded frame")
            log.write(
                json.dumps(
                    {"file": name, "decode_monotonic_s": decoded, "capture_time_verified": False}
                )
                + "\n"
            )
            log.flush()
            next_frame = decoded + interval
            captured += 1
    return {"captured_frames": captured, "requested_frames": count, "complete": captured == count}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--count", type=int, default=30)
    parser.add_argument("--interval", type=float, default=1)
    parser.add_argument("--duration", type=float, default=90)
    parser.add_argument("--url-env", default="SWEEP_LOCALIZATION_RTSP_URL")
    args = parser.parse_args()
    try:
        result = collect(
            os.environ[args.url_env], args.output, args.count, args.interval, args.duration
        )
        print(json.dumps(result))
        if not result["complete"]:
            raise SystemExit(1)
    except (ValueError, OSError, KeyError, RuntimeError, cv2.error):
        raise SystemExit(
            "webcam capture failed; check source and use a new output directory"
        ) from None


if __name__ == "__main__":
    main()
