"""Observation-only webcam localization using capture-time estimates in a monotonic clock."""

import argparse
import hashlib
import json
import math
import os
import signal
import time
from pathlib import Path
from urllib.parse import urlsplit

import cv2
import numpy as np

from perception.localization_lease import LocalizationLeaseStatus
from perception.tag_localization import TagLocalizer
from perception.webcam_filter import WebcamFilter
from perception.webcam_stream import WebcamStream
from tools.map_common import parse_document


def pinned_json(path, expected):
    payload = Path(path).read_bytes()
    if hashlib.sha256(payload).hexdigest() != expected:
        raise ValueError("artifact hash mismatch")
    return parse_document(payload, str(path))


class WebcamLocalization:
    def __init__(self, config, *, allow_synthetic=False):
        if not isinstance(config, dict) or not isinstance(config.get("localizer"), dict):
            raise ValueError("webcam configuration must contain a localizer object")
        if config.get("stream_path") not in {f"drone{i}" for i in range(1, 7)}:
            raise ValueError("stream_path must name a MediaMTX drone1 through drone6 path")
        localizer_config = config["localizer"]
        pipeline = localizer_config.get("pipeline")
        if not isinstance(pipeline, dict):
            raise ValueError("localizer pipeline must be an object")
        if (
            not isinstance(localizer_config.get("camera_serial"), str)
            or not localizer_config["camera_serial"]
        ):
            raise ValueError("camera_serial must be nonempty text")
        self.localizer = TagLocalizer(**localizer_config)
        self.latency = pinned_json(config["latency_path"], config["latency_sha256"])
        if (
            self.latency.get("schema_version") != 1
            or self.latency.get("status") != "offline"
            or self.latency.get("camera_serial") != localizer_config["camera_serial"]
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
            "pose_frame": dict(self.localizer.pose_frame),
            "stream_path": config["stream_path"],
            "bundle_version": self.localizer.manifest["bundle_version"],
            "map_sha256": self.localizer.manifest["content_sha256"],
            "calibration_sha256": localizer_config["calibration_sha256"],
            "latency_sha256": config["latency_sha256"],
            "camera_serial": localizer_config["camera_serial"],
            "timing_provenance": "decode_monotonic_minus_measured_p50",
            "latency_p50_s": self.delay,
            "latency_p95_s": float(p95),
            "capture_time_verified": False,
            "publisher_identity_verified": False,
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
            transform_key = "T_world_body" if self.localizer.world else "T_map_body"
            observation = self.filter.observe(
                str(self.sequence), capture_time, np.array(pose[transform_key])[:3, 3], now
            )
            pose["filter_status"] = observation["observation_status"]
        self.last_pose = pose
        return self.at(now)

    def at(self, now):
        state = self.filter.at(now)
        if self.localizer.world:
            state["position_world_m"] = state.pop("position_map_m")
            state["velocity_world_mps"] = state.pop("velocity_map_mps")
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


class WebcamLocalizationService:
    """Production owner for one shared localization, detection, and map decoder."""

    def __init__(
        self,
        loop,
        url,
        *,
        stream_factory=WebcamStream,
        source_id=None,
        detector=None,
        mission_id=None,
        on_detection=None,
        map_builder=None,
        camera_pipeline_config=None,
    ):
        if not isinstance(url, str) or not url:
            raise ValueError("localization stream URL is invalid")
        if not callable(stream_factory):
            raise ValueError("stream_factory must be callable")
        if source_id is None:
            source_id = urlsplit(url).path.lstrip("/")
        if not isinstance(source_id, str) or not source_id:
            raise ValueError("localization source ID is invalid")
        from perception.shared_camera_pipeline import SharedCameraPipeline

        provenance = getattr(loop, "provenance", None)
        self._pipeline = SharedCameraPipeline(
            url,
            loop,
            source_id=source_id,
            stream_factory=stream_factory,
            detector=detector,
            mission_id=mission_id,
            on_detection=on_detection,
            map_builder=map_builder,
            alignment_evidence=provenance if isinstance(provenance, dict) else None,
            config=camera_pipeline_config,
        )
        self._started = False

    def _start(self):
        if not self._started:
            self._pipeline.start()
            self._started = True

    @property
    def status(self) -> LocalizationLeaseStatus:
        return self._pipeline.localization_status

    def resume(self, now):
        self._start()
        return self._pipeline.resume_localization(now)

    def pause(self):
        return self._pipeline.pause_localization()

    def close(self):
        self._pipeline.close()

    def poll(self, now):
        status = self._pipeline.poll_localization(now)
        decoder_status = self._pipeline.decoder_status
        lease_state = {
            "stream_status": status.state.value if decoder_status == "running" else decoder_status,
            "localization_consumer_state": status.state.value,
            "localization_failure_reason": status.failure_reason,
        }
        if status.current_fix is not None:
            return dict(status.current_fix) | lease_state
        return {
            "type": "webcam_localization",
            "accepted": False,
            "control_eligible": False,
            "flight_approved": False,
        } | lease_state


def load_config(path):
    path = Path(path).resolve()
    config = parse_document(path.read_bytes(), str(path))
    if not isinstance(config.get("localizer"), dict):
        raise ValueError("webcam configuration must contain a localizer object")
    for key in ("bundle", "calibration_path"):
        value = config["localizer"].get(key)
        if not isinstance(value, str) or not value:
            raise ValueError(f"localizer {key} must be a nonempty path")
        config["localizer"][key] = str(path.parent / value)
    latency_path = config.get("latency_path")
    if not isinstance(latency_path, str) or not latency_path:
        raise ValueError("latency_path must be a nonempty path")
    config["latency_path"] = str(path.parent / latency_path)
    return config


def _install_signal(signum, callback):
    previous = signal.getsignal(signum)

    def handler(_signum, _frame):
        callback()

    signal.signal(signum, handler)
    return previous


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--url-env", default="SWEEP_LOCALIZATION_RTSP_URL")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--duration", type=float, default=60)
    parser.add_argument("--allow-synthetic", action="store_true")
    parser.add_argument("--detector-model", type=Path)
    parser.add_argument("--detector-model-sha256")
    parser.add_argument("--mission-id")
    args = parser.parse_args()
    service = None
    previous_pause = None
    previous_resume = None
    requested_lease_action = [None]
    try:
        if not math.isfinite(args.duration) or args.duration <= 0:
            raise ValueError("duration must be positive seconds")
        url = os.environ.get(args.url_env, "")
        parsed_url = urlsplit(url)
        if parsed_url.scheme not in ("rtsp", "rtsps") or not parsed_url.netloc:
            raise ValueError("URL environment variable must contain the MediaMTX RTSP read URL")
        loop = WebcamLocalization(load_config(args.config), allow_synthetic=args.allow_synthetic)
        if parsed_url.path != "/" + loop.provenance["stream_path"]:
            raise ValueError("RTSP path does not match the pinned source configuration")
        if bool(args.detector_model) != bool(args.detector_model_sha256):
            raise ValueError("detector model and SHA-256 must be provided together")
        if args.detector_model is not None and not args.mission_id:
            raise ValueError("detector requires a mission ID")
        detector = None
        if args.detector_model is not None:
            from perception.yolox_onnx import YoloXOnnxDetector

            detector = YoloXOnnxDetector(
                args.detector_model, expected_model_sha256=args.detector_model_sha256
            )
        service = WebcamLocalizationService(
            loop,
            url,
            source_id=loop.provenance["stream_path"],
            detector=detector,
            mission_id=args.mission_id,
        )
        started = time.monotonic()
        service.resume(started)
        previous_pause = _install_signal(
            signal.SIGUSR1, lambda: requested_lease_action.__setitem__(0, "pause")
        )
        previous_resume = _install_signal(
            signal.SIGUSR2, lambda: requested_lease_action.__setitem__(0, "resume")
        )
        with args.output.open("x", encoding="utf-8") as output:
            while time.monotonic() - started < args.duration:
                now = time.monotonic()
                if requested_lease_action[0] == "pause":
                    service.pause()
                elif requested_lease_action[0] == "resume":
                    service.resume(now)
                requested_lease_action[0] = None
                state = service.poll(now)
                output.write(
                    json.dumps(
                        state | {"run_elapsed_s": now - started},
                        allow_nan=False,
                    )
                    + "\n"
                )
                output.flush()
                time.sleep(0.01)
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
    finally:
        if previous_pause is not None:
            signal.signal(signal.SIGUSR1, previous_pause)
        if previous_resume is not None:
            signal.signal(signal.SIGUSR2, previous_resume)
        if service is not None:
            service.close()


if __name__ == "__main__":
    main()
