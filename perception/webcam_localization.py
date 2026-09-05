"""Observation-only webcam localization using capture-time estimates in a monotonic clock."""

import argparse
import hashlib
import json
import math
import os
import time
from pathlib import Path
from urllib.parse import urlsplit

import cv2
import numpy as np

from perception.tag_localization import TagLocalizer
from perception.webcam_filter import WebcamFilter
from perception.webcam_stream import WebcamStream


def pinned_json(path, expected):
    payload = Path(path).read_bytes()
    if hashlib.sha256(payload).hexdigest() != expected:
        raise ValueError("artifact hash mismatch")
    return json.loads(payload)


class WebcamLocalization:
    def __init__(self, config, *, allow_synthetic=False):
        if config.get("stream_path") not in {f"drone{i}" for i in range(1, 7)}:
            raise ValueError("stream_path must name a MediaMTX drone1 through drone6 path")
        self.localizer = TagLocalizer(**config["localizer"])
        self.latency = pinned_json(config["latency_path"], config["latency_sha256"])
        pipeline = config["localizer"]["pipeline"]
        if (
            self.latency.get("schema_version") != 1
            or self.latency.get("status") != "offline"
            or self.latency.get("camera_serial") != config["localizer"]["camera_serial"]
            or self.latency.get("pipeline") != pipeline
            or pipeline.get("decoder_path") != "opencv-ffmpeg-rtsp"
            or pipeline.get("latency_endpoint") != "localization_decode"
        ):
            raise ValueError("latency must match the camera and localization decoder pipeline")
        kinds = (self.localizer.evidence_kind, self.latency.get("evidence_kind"))
        if not allow_synthetic and any(kind != "recorded_live" for kind in kinds):
            raise ValueError("live mode requires recorded_live calibration and latency evidence")
        if any(
            kind not in ("recorded_live", "synthetic", "synthetic_known_intrinsics")
            for kind in kinds
        ):
            raise ValueError("invalid evidence kind")
        samples = self.latency.get("samples_ms")
        times = self.latency.get("sample_times_ms")
        duration = self.latency.get("duration_ms")
        if (
            not isinstance(samples, list)
            or len(samples) < 20
            or not isinstance(times, list)
            or len(times) != len(samples)
            or type(duration) not in (int, float)
            or not math.isfinite(duration)
            or any(
                type(v) not in (int, float) or not math.isfinite(v) or v < 0
                for v in samples + times
            )
            or any(b <= a for a, b in zip(times, times[1:], strict=False))
            or times[-1] > duration
            or times[-1] - times[0] < 60000
        ):
            raise ValueError("latency requires 20 measured samples spanning 60 seconds")
        p50, p95 = np.percentile(samples, [50, 95]) / 1000
        if not 0 <= p50 <= p95 < 0.5:
            raise ValueError("latency p95 must be below the 500 ms localization budget")
        self.delay = float(p50)
        self.tail = float(p95 - p50)
        self.filter = WebcamFilter()
        self.sequence = 0
        self.last_pose = None
        self.provenance = {
            "stream_path": config["stream_path"],
            "map_sha256": config["localizer"]["map_sha256"],
            "accepted_versions": config["localizer"]["accepted_versions"],
            "calibration_sha256": config["localizer"]["calibration_sha256"],
            "latency_sha256": config["latency_sha256"],
            "camera_serial": config["localizer"]["camera_serial"],
            "timing_provenance": "decode_monotonic_minus_measured_p50",
            "latency_p50_s": self.delay,
            "latency_p95_s": float(p95),
            "capture_time_verified": False,
            "synthetic": any(kind != "recorded_live" for kind in kinds),
        }

    def update(self, image, decode_time, now):
        capture_time = decode_time - self.delay
        if capture_time < 0:
            raise ValueError("estimated capture time precedes monotonic clock origin")
        pose = self.localizer.estimate(image, capture_time, decode_time, now)
        pose["timing_provenance"] = self.provenance["timing_provenance"]
        pose["capture_time_verified"] = False
        self.sequence += 1
        if pose["accepted"]:
            observation = self.filter.observe(
                str(self.sequence), capture_time, np.array(pose["T_map_body"])[:3, 3], now
            )
            pose["filter_status"] = observation["observation_status"]
        self.last_pose = pose
        return self.at(now)

    def at(self, now):
        state = self.filter.at(now)
        age = state["fix_age_s"]
        conservative_age = None if age is None else age + self.tail
        confidence = (
            "red"
            if conservative_age is None or conservative_age >= 2
            else "amber"
            if conservative_age >= 0.5
            else "green"
        )
        return (
            state
            | self.provenance
            | {
                "type": "webcam_localization",
                "flight_approved": False,
                "control_eligible": False,
                "spacing_certified": False,
                "confidence": confidence,
                "accepted": confidence == "green",
                "fix_age_with_p95_tail_s": conservative_age,
                "pose_observation": self.last_pose,
                "prediction_model": "constant_velocity_linear_EKF_specialization",
                "measured_inputs": ["AprilTag_PnP_position"],
                "missing_inputs": ["MSDK_velocity", "ToF", "IMU"],
            }
        )


def load_config(path):
    path = Path(path).resolve()
    config = json.loads(path.read_text())
    for key in ("bundle", "calibration_path"):
        config["localizer"][key] = str(path.parent / config["localizer"][key])
    config["latency_path"] = str(path.parent / config["latency_path"])
    return config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--url-env", default="SWEEP_LOCALIZATION_RTSP_URL")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--duration", type=float, default=60)
    parser.add_argument("--allow-synthetic", action="store_true")
    args = parser.parse_args()
    try:
        if not math.isfinite(args.duration) or args.duration <= 0:
            raise ValueError("duration must be positive seconds")
        url = os.environ.get(args.url_env, "")
        if not url.startswith("rtsp://"):
            raise ValueError("URL environment variable must contain the MediaMTX RTSP read URL")
        loop = WebcamLocalization(load_config(args.config), allow_synthetic=args.allow_synthetic)
        if urlsplit(url).path != "/" + loop.provenance["stream_path"]:
            raise ValueError("RTSP path does not match the pinned source configuration")
        started = time.monotonic()
        with args.output.open("x", encoding="utf-8") as output, WebcamStream(url) as source:
            while time.monotonic() - started < args.duration:
                frame = source.read(timeout=0.1)
                now = time.monotonic()
                try:
                    if frame is not None:
                        loop.update(*frame, now)
                    now = time.monotonic()
                    state = loop.at(now)
                except (ValueError, cv2.error):
                    state = loop.at(now) | {"frame_error": "invalid_frame_or_pose"}
                output.write(
                    json.dumps(
                        state | {"run_elapsed_s": now - started, "stream_status": source.status},
                        allow_nan=False,
                    )
                    + "\n"
                )
                output.flush()
    except (
        ValueError,
        OSError,
        KeyError,
        TypeError,
        RuntimeError,
        OverflowError,
        cv2.error,
    ) as error:
        raise SystemExit(
            f"webcam localization failed ({type(error).__name__}); "
            "check configuration and artifacts"
        ) from None


if __name__ == "__main__":
    main()
