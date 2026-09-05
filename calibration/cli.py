"""Command-line entry point for reproducible calibration artifacts."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

import cv2

from calibration.intrinsics import CalibrationRequest, calibrate
from calibration.latency import summarize_latency


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read JSON from {path}: {error}") from error


def _write_artifact(path: Path, artifact: dict[str, object]) -> None:
    content = json.dumps(artifact, indent=2, sort_keys=True) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=".calibration-", delete=False
        ) as stream:
            temporary = Path(stream.name)
            if stream.write(content) != len(content):
                raise OSError("incomplete artifact write")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _corners(value: str) -> tuple[int, int]:
    try:
        columns, rows = (int(part) for part in value.lower().split("x", 1))
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "inner corners must be COLUMNSxROWS, for example 9x6"
        ) from error
    if columns < 3 or rows < 3:
        raise argparse.ArgumentTypeError("inner corners must be at least 3x3")
    return columns, rows


def _intrinsics_command(args: argparse.Namespace) -> None:
    pipeline = _read_json(args.pipeline)
    if not isinstance(pipeline, dict):
        raise ValueError("pipeline JSON must be an object")
    artifact = calibrate(
        CalibrationRequest(
            images_dir=args.images,
            inner_corners=args.inner_corners,
            square_size_m=args.square_size_m,
            camera_serial=args.camera_serial,
            pipeline=pipeline,
            evidence_kind=args.evidence_kind,
        )
    )
    _write_artifact(args.output, artifact)


def _latency_command(args: argparse.Namespace) -> None:
    pipeline = _read_json(args.pipeline)
    samples = _read_json(args.samples)
    if not isinstance(pipeline, dict) or not isinstance(samples, dict):
        raise ValueError("pipeline and samples JSON must both be objects")
    artifact = summarize_latency(
        camera_serial=args.camera_serial,
        pipeline=pipeline,
        evidence_kind=args.evidence_kind,
        samples=samples,
    )
    _write_artifact(args.output, artifact)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create offline calibration artifacts from decoded checkerboard image files."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    intrinsics = commands.add_parser(
        "intrinsics", help="calibrate intrinsics from checkerboard images"
    )
    intrinsics.add_argument("--images", type=Path, required=True)
    intrinsics.add_argument("--inner-corners", type=_corners, required=True)
    intrinsics.add_argument("--square-size-m", type=float, required=True)
    intrinsics.add_argument("--camera-serial", required=True)
    intrinsics.add_argument("--pipeline", type=Path, required=True)
    intrinsics.add_argument(
        "--evidence-kind", choices=("synthetic", "recorded_live"), required=True
    )
    intrinsics.add_argument("--output", type=Path, required=True)
    intrinsics.set_defaults(handler=_intrinsics_command)

    latency = commands.add_parser("latency", help="summarize explicitly measured latency samples")
    latency.add_argument("--samples", type=Path, required=True)
    latency.add_argument("--camera-serial", required=True)
    latency.add_argument("--pipeline", type=Path, required=True)
    latency.add_argument("--evidence-kind", choices=("synthetic", "recorded_live"), required=True)
    latency.add_argument("--output", type=Path, required=True)
    latency.set_defaults(handler=_latency_command)
    return parser


def main() -> None:
    args = _parser().parse_args()
    try:
        args.handler(args)
    except (ValueError, OSError, cv2.error) as error:
        raise SystemExit(f"error: {error}") from error
