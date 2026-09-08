"""Build a diagnostic local occupancy PNG from canonical Ohmni range scans."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import tempfile
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from relay.observations import MAX_EVENT_BYTES, Observation, decode_observation
from tools.ohmni_scan_record import FORMAT as RECORDING_FORMAT
from tools.ohmni_scan_record import (
    MAX_BYTES,
    MAX_IDENTIFIER_CHARS,
    MAX_RECORDS,
    MAX_SESSION_CHARS,
    _identifier,
    _open_regular,
)

FORMAT = "ohmni.canonical-occupancy-grid.v1"
DEFAULT_RESOLUTION_M = 0.05
DEFAULT_MAX_CELLS = 2_000_000
MAX_MANIFEST_BYTES = 64 * 1024
MAX_PNG_DIMENSION = 4_096


@dataclass(frozen=True, slots=True)
class GridConfig:
    resolution_m: float = DEFAULT_RESOLUTION_M
    max_cells: int = DEFAULT_MAX_CELLS
    bounds: tuple[float, float, float, float] | None = None

    def __post_init__(self) -> None:
        if (
            isinstance(self.resolution_m, bool)
            or not isinstance(self.resolution_m, int | float)
            or not math.isfinite(self.resolution_m)
            or self.resolution_m <= 0
        ):
            raise ValueError("resolution must be a positive finite number")
        if type(self.max_cells) is not int or not 1 <= self.max_cells <= DEFAULT_MAX_CELLS:
            raise ValueError(f"max cells must be an integer from 1 through {DEFAULT_MAX_CELLS}")
        if self.bounds is not None and (
            len(self.bounds) != 4
            or not all(
                isinstance(value, int | float)
                and not isinstance(value, bool)
                and math.isfinite(value)
                for value in self.bounds
            )
            or self.bounds[0] >= self.bounds[2]
            or self.bounds[1] >= self.bounds[3]
        ):
            raise ValueError("grid bounds must be finite and increasing")


def _json_object(encoded: bytes, name: str) -> dict[str, object]:
    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"{name} contains a duplicate JSON key")
            result[key] = value
        return result

    try:
        value = json.loads(encoded.decode("utf-8"), object_pairs_hook=unique)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError(f"{name} is not valid JSON") from error
    if not isinstance(value, dict):
        raise ValueError(f"{name} must contain an object")
    return value


def _manifest(recording: Path) -> dict[str, object]:
    path = recording / "recording.json"
    with _open_regular(path) as stream:
        encoded = stream.read(MAX_MANIFEST_BYTES + 1)
    if len(encoded) > MAX_MANIFEST_BYTES:
        raise ValueError("recording manifest exceeds 64 KiB")
    manifest = _json_object(encoded, str(path))
    if manifest.get("format") != RECORDING_FORMAT:
        raise ValueError("unsupported scan recording format")
    source = _mapping(manifest.get("source"), "recording source")
    frames = _mapping(manifest.get("frames"), "recording frames")
    observations = _mapping(manifest.get("observations"), "recording observations")
    required_source = {
        "transport",
        "session",
        "device_id",
        "connection_epoch",
        "source_id",
        "node_type",
    }
    if set(source) != required_source or source.get("node_type") != "ground":
        raise ValueError("recording source identity is incomplete")
    if source["transport"] not in {"file_jsonl", "relay_websocket_console_read_only"}:
        raise ValueError("recording transport is unsupported")
    if set(frames) != {"odometry", "lidar", "sensor_pose"}:
        raise ValueError("recording frame declaration is incomplete")
    pose = _mapping(frames["sensor_pose"], "recording sensor pose")
    if pose != {"parent_frame": frames["odometry"], "child_frame": frames["lidar"]}:
        raise ValueError("recording sensor pose does not match its frames")
    if observations.get("path") != "observations.jsonl":
        raise ValueError("recording must name observations.jsonl")
    _identifier(source["session"], "recording session", MAX_SESSION_CHARS)
    _identifier(source["source_id"], "recording source ID", MAX_IDENTIFIER_CHARS)
    _identifier(frames["odometry"], "recording odometry frame", MAX_IDENTIFIER_CHARS)
    _identifier(frames["lidar"], "recording lidar frame", MAX_IDENTIFIER_CHARS)
    _identifier(manifest.get("run_id"), "recording run ID", MAX_IDENTIFIER_CHARS)
    _identifier(manifest.get("mount_id"), "recording mount ID", MAX_IDENTIFIER_CHARS)
    if frames["odometry"] == "world" or frames["lidar"] == "world":
        raise ValueError("recording frames must remain source-scoped local frames")
    if frames["odometry"] == frames["lidar"]:
        raise ValueError("recording odometry and lidar frames must differ")
    if (
        type(source["device_id"]) is not int
        or not 1 <= source["device_id"] <= 2_147_483_647
        or type(source["connection_epoch"]) is not int
        or source["connection_epoch"] <= 0
    ):
        raise ValueError("recording source IDs are invalid")
    return manifest


def _mapping(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value


def _observations(recording: Path, manifest: Mapping[str, object]) -> Iterator[Observation]:
    metadata = _mapping(manifest["observations"], "recording observations")
    expected_bytes = metadata.get("bytes")
    expected_count = metadata.get("count")
    expected_digest = metadata.get("sha256")
    if (
        type(expected_bytes) is not int
        or expected_bytes < 0
        or type(expected_count) is not int
        or expected_count < 0
        or type(expected_digest) is not str
    ):
        raise ValueError("recording observation metadata is invalid")
    source = _mapping(manifest["source"], "recording source")
    frames = _mapping(manifest["frames"], "recording frames")
    mount_id = manifest["mount_id"]
    digest = hashlib.sha256()
    count = 0
    total = 0
    path = recording / "observations.jsonl"
    with _open_regular(path) as stream:
        if os.fstat(stream.fileno()).st_size > MAX_BYTES:
            raise ValueError("observations.jsonl exceeds the recording byte ceiling")
        line_number = 0
        while line := stream.readline(MAX_EVENT_BYTES + 2):
            line_number += 1
            if len(line) > MAX_EVENT_BYTES + 1:
                raise ValueError(f"observations.jsonl:{line_number} exceeds 64 KiB")
            digest.update(line)
            total += len(line)
            if total > MAX_BYTES:
                raise ValueError("observations.jsonl exceeds the recording byte ceiling")
            raw = line.rstrip(b"\r\n")
            if not raw or len(raw) > MAX_EVENT_BYTES:
                raise ValueError(f"observations.jsonl:{line_number} is not a bounded observation")
            try:
                event = decode_observation(raw)
            except ValueError as error:
                raise ValueError(f"observations.jsonl:{line_number} is invalid") from error
            if event.encode() != raw:
                raise ValueError(f"observations.jsonl:{line_number} is not canonical")
            submission = event.submission
            payload = submission.payload
            pose = payload.get("sensor_pose") if payload.get("kind") == "range_scan" else None
            if (
                submission.session != source["session"]
                or submission.device_id != source["device_id"]
                or submission.connection_epoch != source["connection_epoch"]
                or submission.source_id != source["source_id"]
                or submission.node_type != "ground"
                or submission.frame != frames["lidar"]
                or payload.get("mount_id") != mount_id
                or not isinstance(pose, Mapping)
                or pose.get("parent_frame") != frames["odometry"]
                or pose.get("child_frame") != frames["lidar"]
            ):
                raise ValueError(f"observations.jsonl:{line_number} does not match recording scope")
            count += 1
            if count > MAX_RECORDS:
                raise ValueError("observations.jsonl exceeds the recording count ceiling")
            yield event
    if total != expected_bytes or count != expected_count or digest.hexdigest() != expected_digest:
        raise ValueError("recording observation digest, byte count, or count does not match")


def _rays(event: Observation) -> Iterator[tuple[float, float, float, float]]:
    payload = event.submission.payload
    pose = payload["sensor_pose"]
    assert isinstance(pose, Mapping)
    x = _number(pose["x_m"], "sensor pose x")
    y = _number(pose["y_m"], "sensor pose y")
    qx = _number(pose["qx"], "sensor pose qx")
    qy = _number(pose["qy"], "sensor pose qy")
    qz = _number(pose["qz"], "sensor pose qz")
    qw = _number(pose["qw"], "sensor pose qw")
    angle_min = _number(payload["angle_min_rad"], "scan angle minimum")
    increment = _number(payload["angle_increment_rad"], "scan angle increment")
    for index, distance in enumerate(payload["ranges_m"]):
        if distance is None:
            continue
        value = _number(distance, "scan range")
        angle = angle_min + index * increment
        local_x, local_y = value * math.cos(angle), value * math.sin(angle)
        rotated_x = (1 - 2 * (qy * qy + qz * qz)) * local_x + 2 * (qx * qy - qw * qz) * local_y
        rotated_y = 2 * (qx * qy + qw * qz) * local_x + (1 - 2 * (qx * qx + qz * qz)) * local_y
        yield x, y, x + rotated_x, y + rotated_y


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    return float(value)


def _bounds(
    recording: Path, manifest: Mapping[str, object], config: GridConfig
) -> tuple[float, float, int, int, int, dict[str, float | int | None]]:
    confidence = _Confidence()
    rays = 0
    if config.bounds is None:
        minimum_x = minimum_y = math.inf
        maximum_x = maximum_y = -math.inf
        for event in _observations(recording, manifest):
            confidence.add(event.submission.confidence)
            for start_x, start_y, end_x, end_y in _rays(event):
                rays += 1
                minimum_x = min(minimum_x, start_x, end_x)
                minimum_y = min(minimum_y, start_y, end_y)
                maximum_x = max(maximum_x, start_x, end_x)
                maximum_y = max(maximum_y, start_y, end_y)
        if rays == 0:
            raise ValueError(
                "recording has no observed endpoints; supply --bounds for an unknown grid"
            )
        minimum_x -= config.resolution_m
        minimum_y -= config.resolution_m
        maximum_x += config.resolution_m
        maximum_y += config.resolution_m
    else:
        minimum_x, minimum_y, maximum_x, maximum_y = config.bounds
        for event in _observations(recording, manifest):
            confidence.add(event.submission.confidence)
            rays += sum(1 for _ in _rays(event))
    origin_x = math.floor(minimum_x / config.resolution_m) * config.resolution_m
    origin_y = math.floor(minimum_y / config.resolution_m) * config.resolution_m
    width = math.ceil((maximum_x - origin_x) / config.resolution_m)
    height = math.ceil((maximum_y - origin_y) / config.resolution_m)
    if (
        width <= 0
        or height <= 0
        or width > MAX_PNG_DIMENSION
        or height > MAX_PNG_DIMENSION
        or width * height > config.max_cells
    ):
        raise ValueError(f"grid of {width}x{height} exceeds {config.max_cells} cells")
    return origin_x, origin_y, width, height, rays, confidence.to_mapping()


class _Confidence:
    def __init__(self) -> None:
        self.count = 0
        self.total = 0.0
        self.minimum: float | None = None
        self.maximum: float | None = None

    def add(self, value: float) -> None:
        self.count += 1
        self.total += value
        self.minimum = value if self.minimum is None else min(self.minimum, value)
        self.maximum = value if self.maximum is None else max(self.maximum, value)

    def to_mapping(self) -> dict[str, float | int | None]:
        return {
            "count": self.count,
            "minimum": self.minimum,
            "maximum": self.maximum,
            "mean": None if self.count == 0 else self.total / self.count,
        }


def _cell(
    x: float, y: float, origin_x: float, origin_y: float, resolution: float
) -> tuple[int, int]:
    return math.floor((y - origin_y) / resolution), math.floor((x - origin_x) / resolution)


def _render(
    recording: Path,
    manifest: Mapping[str, object],
    origin_x: float,
    origin_y: float,
    width: int,
    height: int,
    resolution: float,
) -> np.ndarray:
    grid = np.full((height, width), 128, dtype=np.uint8)
    for event in _observations(recording, manifest):
        if event.submission.confidence <= 0:
            continue
        for start_x, start_y, end_x, end_y in _rays(event):
            start = _cell(start_x, start_y, origin_x, origin_y, resolution)
            end = _cell(end_x, end_y, origin_x, origin_y, resolution)
            clipped = cv2.clipLine((0, 0, width, height), (start[1], start[0]), (end[1], end[0]))
            if not clipped[0]:
                continue
            _, clipped_start, clipped_end = clipped
            mask = np.zeros_like(grid)
            cv2.line(mask, clipped_start, clipped_end, 255, 1, cv2.LINE_8)
            mask[clipped_start[1], clipped_start[0]] = 0
            grid[(mask == 255) & (grid != 0)] = 255
            if _inside(*end, width, height):
                grid[end[0], end[1]] = 0
    return grid


def _inside(row: int, column: int, width: int, height: int) -> bool:
    return 0 <= row < height and 0 <= column < width


def build_grid(recording: Path, output: Path, config: GridConfig) -> dict[str, object]:
    recording = recording.resolve()
    manifest = _manifest(recording)
    origin_x, origin_y, width, height, rays, confidence = _bounds(recording, manifest, config)
    grid = _render(recording, manifest, origin_x, origin_y, width, height, config.resolution_m)
    output = output.absolute()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=output.parent))
    reserved_output = False
    try:
        png_path = temporary / "occupancy.png"
        encoded, png = cv2.imencode(".png", np.ascontiguousarray(grid[::-1]))
        if not encoded:
            raise ValueError("could not encode occupancy PNG")
        with png_path.open("xb") as stream:
            stream.write(png.tobytes())
            stream.flush()
            os.fsync(stream.fileno())
        counts = {
            "occupied": int(np.count_nonzero(grid == 0)),
            "free": int(np.count_nonzero(grid == 255)),
            "unknown": int(np.count_nonzero(grid == 128)),
        }
        metadata = {
            "format": FORMAT,
            "created_at_ms": int(time.time() * 1000),
            "input": {
                "run_id": manifest["run_id"],
                "source": manifest["source"],
                "observations_sha256": _mapping(manifest["observations"], "recording observations")[
                    "sha256"
                ],
            },
            "grid": {
                "frame": _mapping(manifest["frames"], "recording frames")["odometry"],
                "frame_kind": "source_scoped_local_odometry",
                "registered_to_world": False,
                "resolution_m": config.resolution_m,
                "origin": {"x_m": origin_x, "y_m": origin_y},
                "width": width,
                "height": height,
                "png_row_0": "maximum_y",
                "pixels": {"occupied": 0, "unknown": 128, "free": 255},
            },
            "mount_id": manifest["mount_id"],
            "observations": {
                "records": _mapping(manifest["observations"], "recording observations")["count"],
                "valid_observed_endpoints": rays,
                "confidence": confidence,
                "occupancy_cells": counts,
                "empty_bins_remain_unknown": True,
            },
            "files": {
                "occupancy.png": {
                    "bytes": png_path.stat().st_size,
                    "sha256": _sha256(png_path),
                }
            },
        }
        artifact = json.dumps(
            metadata, allow_nan=False, separators=(",", ":"), sort_keys=True
        ).encode()
        metadata["artifact_sha256"] = hashlib.sha256(artifact).hexdigest()
        with (temporary / "manifest.json").open("x", encoding="utf-8") as stream:
            json.dump(metadata, stream, allow_nan=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.mkdir(output)
        except FileExistsError as error:
            raise ValueError(f"grid output already exists: {output}") from error
        reserved_output = True
        for name in ("occupancy.png", "manifest.json"):
            os.replace(temporary / name, output / name)
        os.rmdir(temporary)
        reserved_output = False
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        if reserved_output:
            shutil.rmtree(output, ignore_errors=True)
        raise
    return metadata


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with _open_regular(path) as stream:
        for chunk in iter(lambda: stream.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recording", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--resolution-m", type=float, default=DEFAULT_RESOLUTION_M)
    parser.add_argument("--max-cells", type=int, default=DEFAULT_MAX_CELLS)
    parser.add_argument(
        "--bounds", type=float, nargs=4, metavar=("MIN_X", "MIN_Y", "MAX_X", "MAX_Y")
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        config = GridConfig(
            args.resolution_m, args.max_cells, tuple(args.bounds) if args.bounds else None
        )
        manifest = build_grid(args.recording, args.output, config)
    except (OSError, ValueError) as error:
        raise SystemExit(f"ohmni occupancy grid failed: {error}") from error
    print(json.dumps({"output": str(args.output), "artifact_sha256": manifest["artifact_sha256"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
