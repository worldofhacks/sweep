"""Produce bounded local camera and AprilTag observation submissions from a verified capture."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import tempfile
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from perception.camera_tags import CameraTagDetector, read_calibration
from relay.observations import (
    FrameDeclaration,
    FramedPose,
    FrameRegistry,
    ObservationSubmission,
    SourceBinding,
    TimingPolicy,
    decode_submission,
    ingest,
)
from tools.map_common import finite_number
from tools.ohmni_camera_frames import CameraCapture, CaptureError, open_capture

FORMAT = "ohmni.camera-smoke.v1"
MAX_TAG_SIZES = 64
MAX_SELECTED_FRAMES = 10_000
REPORT_RESERVE_BYTES = 2 * 1024 * 1024
DEFAULT_MAX_OUTPUT_BYTES = 64 * 1024 * 1024
MAX_OUTPUT_BYTES = 256 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class SmokeConfig:
    session: str
    device_id: int
    connection_epoch: int
    source_id: str
    camera_serial: str
    camera_frame: str
    tag_sizes_m: dict[int, float]
    every_nth: int = 1
    max_frames: int = MAX_SELECTED_FRAMES
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES
    allow_synthetic_calibration: bool = False

    def __post_init__(self) -> None:
        for name in ("session", "source_id", "camera_serial", "camera_frame"):
            value = getattr(self, name)
            if type(value) is not str or not value or value != value.strip() or len(value) > 128:
                raise ValueError(f"{name} must be 1 through 128 characters")
        if type(self.device_id) is not int or not 1 <= self.device_id <= 2_147_483_647:
            raise ValueError("device_id must be a positive signed 32-bit integer")
        if type(self.connection_epoch) is not int or self.connection_epoch < 1:
            raise ValueError("connection_epoch must be a positive integer")
        if type(self.every_nth) is not int or self.every_nth < 1:
            raise ValueError("every_nth must be a positive integer")
        if type(self.max_frames) is not int or not 1 <= self.max_frames <= MAX_SELECTED_FRAMES:
            raise ValueError(f"max_frames must be from 1 through {MAX_SELECTED_FRAMES}")
        if (
            type(self.max_output_bytes) is not int
            or not REPORT_RESERVE_BYTES <= self.max_output_bytes <= MAX_OUTPUT_BYTES
        ):
            raise ValueError(
                f"max_output_bytes must be from {REPORT_RESERVE_BYTES} through {MAX_OUTPUT_BYTES}"
            )
        if type(self.allow_synthetic_calibration) is not bool:
            raise ValueError("allow_synthetic_calibration must be boolean")
        if (
            not isinstance(self.tag_sizes_m, dict)
            or not 1 <= len(self.tag_sizes_m) <= MAX_TAG_SIZES
        ):
            raise ValueError(f"declare from one through {MAX_TAG_SIZES} tag sizes")
        normalized: dict[int, float] = {}
        for identifier, size_m in self.tag_sizes_m.items():
            size = finite_number(size_m, "tag size")
            if type(identifier) is not int or not 0 <= identifier <= 586 or not 0 < size <= 10:
                raise ValueError(
                    "tag sizes must use tag36h11 IDs and metric values from 0 through 10"
                )
            normalized[identifier] = size
        if len(normalized) != len(self.tag_sizes_m):
            raise ValueError("tag IDs must be unique")
        object.__setattr__(self, "tag_sizes_m", normalized)


def _canonical(submission: ObservationSubmission) -> bytes:
    encoded = json.dumps(
        submission.to_mapping(), allow_nan=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    if len(encoded) > 65_536:
        raise ValueError("camera observation exceeds 65536 bytes")
    return encoded


def _quaternion(rotation: np.ndarray) -> tuple[float, float, float, float]:
    matrix = np.asarray(rotation, dtype=float)
    if matrix.shape != (3, 3) or not np.isfinite(matrix).all():
        raise ValueError("detector returned an invalid pose rotation")
    trace = float(np.trace(matrix))
    if trace > 0:
        scale = math.sqrt(trace + 1.0) * 2
        qw = 0.25 * scale
        qx = (matrix[2, 1] - matrix[1, 2]) / scale
        qy = (matrix[0, 2] - matrix[2, 0]) / scale
        qz = (matrix[1, 0] - matrix[0, 1]) / scale
    elif matrix[0, 0] > matrix[1, 1] and matrix[0, 0] > matrix[2, 2]:
        scale = math.sqrt(1.0 + matrix[0, 0] - matrix[1, 1] - matrix[2, 2]) * 2
        qw = (matrix[2, 1] - matrix[1, 2]) / scale
        qx = 0.25 * scale
        qy = (matrix[0, 1] + matrix[1, 0]) / scale
        qz = (matrix[0, 2] + matrix[2, 0]) / scale
    elif matrix[1, 1] > matrix[2, 2]:
        scale = math.sqrt(1.0 + matrix[1, 1] - matrix[0, 0] - matrix[2, 2]) * 2
        qw = (matrix[0, 2] - matrix[2, 0]) / scale
        qx = (matrix[0, 1] + matrix[1, 0]) / scale
        qy = 0.25 * scale
        qz = (matrix[1, 2] + matrix[2, 1]) / scale
    else:
        scale = math.sqrt(1.0 + matrix[2, 2] - matrix[0, 0] - matrix[1, 1]) * 2
        qw = (matrix[1, 0] - matrix[0, 1]) / scale
        qx = (matrix[0, 2] + matrix[2, 0]) / scale
        qy = (matrix[1, 2] + matrix[2, 1]) / scale
        qz = 0.25 * scale
    quaternion = np.asarray((qx, qy, qz, qw), dtype=float)
    norm = float(np.linalg.norm(quaternion))
    if not np.isfinite(norm) or norm == 0:
        raise ValueError("detector returned an invalid pose quaternion")
    return tuple((quaternion / norm).tolist())  # type: ignore[return-value]


def _validate_submission(
    submission: ObservationSubmission,
    config: SmokeConfig,
    *,
    tag_id: int | None = None,
) -> None:
    declarations = [
        FrameDeclaration(
            config.camera_frame,
            "camera",
            "right_down_forward",
            "m",
            config.session,
            config.device_id,
            config.connection_epoch,
            config.source_id,
        )
    ]
    frames = [config.camera_frame]
    if tag_id is not None:
        tag_frame = f"tag:{tag_id}"
        declarations.append(
            FrameDeclaration(
                tag_frame,
                "tag",
                "right_up_outward",
                "m",
                config.session,
                config.device_id,
                config.connection_epoch,
                config.source_id,
            )
        )
        frames.append(tag_frame)
    binding = SourceBinding(
        config.session,
        config.device_id,
        config.connection_epoch,
        config.source_id,
        "ground",
        tuple(frames),
        (str(submission.payload["kind"]),),
    )
    ingest(
        submission,
        t_ingest=0,
        frames=FrameRegistry(tuple(declarations)),
        binding=binding,
        mappings={},
        timing=TimingPolicy(0),
    )


def _submission(
    config: SmokeConfig,
    *,
    event_id: str,
    receipt_clock_id: str,
    receipt_ns: int,
    payload: dict[str, object],
) -> ObservationSubmission:
    raw = {
        "v": 1,
        "type": "observation",
        "event_id": event_id,
        "session": config.session,
        "device_id": config.device_id,
        "connection_epoch": config.connection_epoch,
        "source_id": config.source_id,
        "node_type": "ground",
        "frame": config.camera_frame,
        "confidence": 0.0,
        "t_capture": None,
        "t_source_receipt": {"clock_id": receipt_clock_id, "unit": "ns", "value": receipt_ns},
        "clock_mapping_id": None,
        "payload": payload,
    }
    return decode_submission(json.dumps(raw, allow_nan=False, separators=(",", ":")))


def _require_capture_identity(
    capture: CameraCapture, calibration: dict[str, object], config: SmokeConfig
) -> None:
    metadata = capture.metadata
    provenance = metadata.get("provenance")
    image = metadata.get("image")
    if not isinstance(provenance, dict) or not isinstance(image, dict):
        raise ValueError("capture metadata is incomplete")
    if provenance.get("device_id") != str(config.device_id):
        raise ValueError("capture device identity does not match device_id")
    if provenance.get("camera_id") != config.camera_serial:
        raise ValueError("capture camera identity does not match camera_serial")
    size = [image.get("width"), image.get("height")]
    if size != calibration.get("image_size_px") or provenance.get("resolution_px") != size:
        raise ValueError("capture and calibration resolutions do not match")


def _write_bytes(stream, encoded: bytes, *, total: int, maximum: int) -> int:
    line = encoded + b"\n"
    if total + len(line) > maximum - REPORT_RESERVE_BYTES:
        raise ValueError("camera smoke observations exceed the output byte cap")
    stream.write(line)
    return total + len(line)


def _report(
    *,
    capture: CameraCapture,
    calibration_sha256: str,
    config: SmokeConfig,
    run_hash: str,
    observation_count: int,
    observation_bytes: int,
    observation_hash: str,
    selected_frames: int,
    processed_frames: int,
    detected_ids: Counter[int],
    reasons: Counter[str],
    pose_accepted: int,
    processing_started_ns: int,
    processing_cpu_ns: int,
) -> dict[str, object]:
    wall_ns = time.monotonic_ns() - processing_started_ns
    cpu_ns = time.process_time_ns() - processing_cpu_ns
    capture_measurements = capture.metadata.get("measurements")
    inherited = capture_measurements if isinstance(capture_measurements, dict) else {}
    recording = capture.metadata["recording"]
    image = capture.metadata["image"]
    return {
        "format": FORMAT,
        "run_id": capture.metadata["run_id"],
        "run_hash": run_hash,
        "source": {
            "session": config.session,
            "device_id": config.device_id,
            "connection_epoch": config.connection_epoch,
            "source_id": config.source_id,
            "node_type": "ground",
        },
        "camera": {
            "serial": config.camera_serial,
            "frame": config.camera_frame,
            "resolution_px": [image["width"], image["height"]],
            "calibration_sha256": calibration_sha256,
        },
        "input": {
            "raw_frames_sha256": recording["frames_sha256"],
            "frame_index_sha256": recording["frame_index_sha256"],
            "calibration_sha256": calibration_sha256,
        },
        "observations": {
            "path": "observations.jsonl",
            "count": observation_count,
            "bytes": observation_bytes,
            "sha256": observation_hash,
        },
        "frames": {
            "capture_frame_count": recording["frame_count"],
            "selected": selected_frames,
            "processed": processed_frames,
        },
        "detections": {
            "decoded_ids": [
                {"id": identifier, "count": detected_ids[identifier]}
                for identifier in sorted(detected_ids)
            ],
            "reasons": dict(sorted(reasons.items())),
            "pose_admission": {
                "accepted": pose_accepted,
                "rejected": sum(reasons.values()) - pose_accepted,
                "flight_approved": False,
            },
        },
        "measurements": {
            "processing": {
                "wall_duration_ns": wall_ns,
                "cpu_ns": cpu_ns,
                "fps": 0.0 if wall_ns == 0 else processed_frames / (wall_ns / 1_000_000_000),
            },
            "capture": {
                "received_fps_mean": inherited.get("received_fps_mean"),
                "cpu": inherited.get("cpu"),
            },
            "latency": {
                "status": "unavailable",
                "reason": "capture receipt timestamps do not provide exposure time",
            },
        },
        "flight_approved": False,
    }


def run_smoke(
    capture_dir: Path,
    calibration_path: Path,
    calibration_sha256: str,
    output: Path,
    config: SmokeConfig,
) -> dict[str, object]:
    if (
        type(calibration_sha256) is not str
        or len(calibration_sha256) != 64
        or any(character not in "0123456789abcdef" for character in calibration_sha256)
    ):
        raise ValueError("calibration_sha256 must be a lowercase SHA-256")
    output = Path(output).absolute()
    if os.path.lexists(output):
        raise ValueError(f"output already exists: {output}")
    calibration = read_calibration(calibration_path, calibration_sha256)
    detector = CameraTagDetector(
        calibration,
        camera_serial=config.camera_serial,
        tag_sizes_m=config.tag_sizes_m,
        allow_synthetic=config.allow_synthetic_calibration,
    )
    with open_capture(capture_dir) as capture:
        _require_capture_identity(capture, calibration, config)
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=output.parent))
        try:
            run_hash = hashlib.sha256(
                f"{capture.metadata['run_id']}:{capture.metadata['recording']['frames_sha256']}".encode()
            ).hexdigest()
            receipt_clock_id = f"capture-receipt:{run_hash[:32]}"
            observation_hash = hashlib.sha256()
            observation_count = 0
            observation_bytes = 0
            selected_frames = 0
            processed_frames = 0
            detected_ids: Counter[int] = Counter()
            reasons: Counter[str] = Counter()
            pose_accepted = 0
            processing_started_ns = time.monotonic_ns()
            processing_cpu_ns = time.process_time_ns()
            image = capture.metadata["image"]
            with (temporary / "observations.jsonl").open("xb") as stream:
                for frame in capture.frames():
                    if frame.index % config.every_nth:
                        continue
                    if selected_frames == config.max_frames:
                        break
                    selected_frames += 1
                    processed_frames += 1
                    image_id = f"camera:{run_hash[:16]}:{frame.index}"
                    camera_payload = {
                        "kind": "camera_frame",
                        "image_id": image_id,
                        "sha256": hashlib.sha256(frame.gray8).hexdigest(),
                        "width_px": image["width"],
                        "height_px": image["height"],
                        "calibration_id": f"sha256:{calibration_sha256}",
                    }
                    camera_submission = _submission(
                        config,
                        event_id=f"camera:{run_hash[:16]}:{frame.index}",
                        receipt_clock_id=receipt_clock_id,
                        receipt_ns=frame.received_monotonic_ns,
                        payload=camera_payload,
                    )
                    _validate_submission(camera_submission, config)
                    encoded = _canonical(camera_submission)
                    observation_bytes = _write_bytes(
                        stream, encoded, total=observation_bytes, maximum=config.max_output_bytes
                    )
                    observation_hash.update(encoded + b"\n")
                    observation_count += 1
                    pixels = np.frombuffer(frame.gray8, np.uint8).reshape(
                        int(image["height"]), int(image["width"])
                    )
                    for ordinal, detection in enumerate(detector.detect(pixels)):
                        tag_id = detection["tag_id"]
                        reason = detection["reason"]
                        accepted = detection["pose_accepted"]
                        if (
                            type(tag_id) is not int
                            or type(reason) is not str
                            or type(accepted) is not bool
                        ):
                            raise ValueError("detector returned an invalid tag observation")
                        detected_ids[tag_id] += 1
                        reasons[reason] += 1
                        tag_pose = None
                        if accepted:
                            transform = np.asarray(detection["T_camera_tag"], dtype=float)
                            qx, qy, qz, qw = _quaternion(transform[:3, :3])
                            tag_pose = FramedPose(
                                config.camera_frame,
                                f"tag:{tag_id}",
                                *transform[:3, 3].tolist(),
                                qx,
                                qy,
                                qz,
                                qw,
                            ).to_mapping()
                            pose_accepted += 1
                        tag_payload = {
                            "kind": "tag_observation",
                            "family": "tag36h11",
                            "tag_id": tag_id,
                            "image_id": image_id,
                            "pose_accepted": accepted,
                            "tag_pose": tag_pose,
                            "covariance_m2": None,
                            "reason": reason,
                            "size_m": detection["size_m"],
                            "corners_px": detection["corners_px"],
                            "pixel_frame": detection["pixel_frame"],
                            "reprojection_rms_px": detection["reprojection_rms_px"],
                        }
                        tag_submission = _submission(
                            config,
                            event_id=f"tag:{run_hash[:16]}:{frame.index}:{ordinal}",
                            receipt_clock_id=receipt_clock_id,
                            receipt_ns=frame.received_monotonic_ns,
                            payload=tag_payload,
                        )
                        _validate_submission(
                            tag_submission, config, tag_id=tag_id if accepted else None
                        )
                        encoded = _canonical(tag_submission)
                        observation_bytes = _write_bytes(
                            stream,
                            encoded,
                            total=observation_bytes,
                            maximum=config.max_output_bytes,
                        )
                        observation_hash.update(encoded + b"\n")
                        observation_count += 1
                stream.flush()
                os.fsync(stream.fileno())
            report = _report(
                capture=capture,
                calibration_sha256=calibration_sha256,
                config=config,
                run_hash=run_hash,
                observation_count=observation_count,
                observation_bytes=observation_bytes,
                observation_hash=observation_hash.hexdigest(),
                selected_frames=selected_frames,
                processed_frames=processed_frames,
                detected_ids=detected_ids,
                reasons=reasons,
                pose_accepted=pose_accepted,
                processing_started_ns=processing_started_ns,
                processing_cpu_ns=processing_cpu_ns,
            )
            encoded_report = (
                json.dumps(report, allow_nan=False, indent=2, sort_keys=True).encode("utf-8")
                + b"\n"
            )
            if len(encoded_report) > REPORT_RESERVE_BYTES:
                raise ValueError("camera smoke report exceeds its reserved byte budget")
            with (temporary / "report.json").open("xb") as stream:
                stream.write(encoded_report)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, output)
            return report
        except BaseException:
            shutil.rmtree(temporary, ignore_errors=True)
            raise


def _tag_size(value: str) -> tuple[int, float]:
    identifier, separator, raw_size = value.partition(":")
    if not separator or not identifier or not raw_size:
        raise argparse.ArgumentTypeError("tag size must be id:metres")
    try:
        tag_id = int(identifier)
        size_m = finite_number(float(raw_size), "tag size")
    except ValueError as error:
        raise argparse.ArgumentTypeError("tag size must be id:metres") from error
    if not 0 <= tag_id <= 586 or not 0 < size_m <= 10:
        raise argparse.ArgumentTypeError("tag size ID or metres is outside the allowed range")
    return tag_id, size_m


class _TagSizeAction(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        current = list(getattr(namespace, self.dest, None) or [])
        if len(current) == MAX_TAG_SIZES:
            raise argparse.ArgumentError(self, f"at most {MAX_TAG_SIZES} tag sizes are allowed")
        tag_id, size_m = values
        if any(existing_id == tag_id for existing_id, _ in current):
            raise argparse.ArgumentError(self, "tag IDs must be unique")
        current.append((tag_id, size_m))
        setattr(namespace, self.dest, current)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture-dir", required=True, type=Path)
    parser.add_argument("--calibration", required=True, type=Path)
    parser.add_argument("--calibration-sha256", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--session", required=True)
    parser.add_argument("--device-id", required=True, type=int)
    parser.add_argument("--connection-epoch", required=True, type=int)
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--camera-serial", required=True)
    parser.add_argument("--camera-frame", required=True)
    parser.add_argument("--tag-size", action=_TagSizeAction, type=_tag_size, required=True)
    parser.add_argument("--every-nth", type=int, default=1)
    parser.add_argument("--max-frames", type=int, default=MAX_SELECTED_FRAMES)
    parser.add_argument("--max-output-bytes", type=int, default=DEFAULT_MAX_OUTPUT_BYTES)
    parser.add_argument("--allow-synthetic-calibration", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = run_smoke(
            args.capture_dir,
            args.calibration,
            args.calibration_sha256,
            args.output,
            SmokeConfig(
                session=args.session,
                device_id=args.device_id,
                connection_epoch=args.connection_epoch,
                source_id=args.source_id,
                camera_serial=args.camera_serial,
                camera_frame=args.camera_frame,
                tag_sizes_m=dict(args.tag_size),
                every_nth=args.every_nth,
                max_frames=args.max_frames,
                max_output_bytes=args.max_output_bytes,
                allow_synthetic_calibration=args.allow_synthetic_calibration,
            ),
        )
    except (CaptureError, OSError, ValueError) as error:
        print(json.dumps({"valid": False, "error": str(error)}, sort_keys=True))
        return 1
    print(
        json.dumps(
            {
                "valid": True,
                "output": str(args.output),
                "observations": report["observations"]["count"],
                "frames": report["frames"]["processed"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
