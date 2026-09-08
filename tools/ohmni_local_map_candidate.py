"""Build one unapproved local Ohmni map candidate from an accepted mapper archive."""

# ruff: noqa: E501

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import tempfile
from collections.abc import Mapping
from pathlib import Path

from relay.observations import Observation, decode_observation
from tools.ohmni_live_tag_mapper import ARCHIVE_FORMAT
from tools.ohmni_occupancy_grid import GridConfig, build_grid
from tools.ohmni_scan_record import RecordingConfig, record_events
from tools.ohmni_tag_candidate_fusion import _request, run

MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_MANIFEST_BYTES = 64 * 1024
MAX_OUTPUT_BYTES = 128 * 1024 * 1024


def _object(raw: bytes, name: str) -> dict[str, object]:
    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"{name} contains a duplicate JSON key")
            result[key] = value
        return result

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError(f"{name} is not valid JSON") from error
    if not isinstance(value, dict):
        raise ValueError(f"{name} must contain an object")
    return value


def _read_regular(path: Path, maximum: int) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or details.st_size > maximum:
            raise ValueError(f"{path} must be a bounded regular file")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            payload = stream.read(maximum + 1)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(payload) > maximum:
        raise ValueError(f"{path} exceeds its byte limit")
    return payload


def _mapping(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value


def _scope(value: object, name: str) -> dict[str, object]:
    scope = _mapping(value, name)
    if set(scope) != {"session", "device_id", "connection_epoch", "source_id"}:
        raise ValueError(f"{name} is incomplete")
    if (
        not isinstance(scope["session"], str)
        or not scope["session"]
        or type(scope["device_id"]) is not int
        or type(scope["connection_epoch"]) is not int
        or scope["connection_epoch"] <= 0
        or not isinstance(scope["source_id"], str)
        or not scope["source_id"]
    ):
        raise ValueError(f"{name} is invalid")
    return scope


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _pin(value: object, name: str) -> dict[str, str]:
    pin = _mapping(value, name)
    if (
        set(pin) != {"path", "sha256"}
        or not isinstance(pin["path"], str)
        or not isinstance(pin["sha256"], str)
    ):
        raise ValueError(f"{name} must be a hash pin")
    return {"path": pin["path"], "sha256": pin["sha256"]}


def _archive(archive: Path) -> tuple[dict[str, object], bytes, list[Observation]]:
    manifest = _object(
        _read_regular(archive / "manifest.json", MAX_MANIFEST_BYTES), "archive manifest"
    )
    if manifest.get("format") != ARCHIVE_FORMAT:
        raise ValueError("unsupported accepted mapper archive format")
    scope = _mapping(manifest.get("scope"), "archive scope")
    sources = _mapping(manifest.get("sources"), "archive sources")
    frames = _mapping(manifest.get("frames"), "archive frames")
    observations = _mapping(manifest.get("observations"), "archive observations")
    if (
        set(scope) != {"session", "device_id", "connection_epoch"}
        or set(sources) != {"camera", "tag", "pose", "lidar"}
        or set(frames) != {"odom", "body", "camera", "lidar"}
    ):
        raise ValueError("archive scope, sources, or frames are incomplete")
    if "world" in frames.values() or len(set(frames.values())) != 4:
        raise ValueError("archive must describe distinct local frames")
    if len(set(sources.values())) != 4:
        raise ValueError("archive sources must be distinct")
    source_scopes = {
        name: _scope({**scope, "source_id": source}, f"archive {name} source")
        for name, source in sources.items()
    }
    if (
        observations.get("path") != "observations.jsonl"
        or type(observations.get("count")) is not int
        or type(observations.get("bytes")) is not int
        or type(observations.get("sha256")) is not str
    ):
        raise ValueError("archive observation manifest is invalid")
    payload = _read_regular(archive / "observations.jsonl", MAX_ARCHIVE_BYTES)
    if len(payload) != observations["bytes"] or _digest(payload) != observations["sha256"]:
        raise ValueError("archive observation digest or byte count does not match")
    lines = payload.splitlines()
    if len(lines) != observations["count"] or not lines:
        raise ValueError("archive observation count is invalid")
    actual_kinds = {name: 0 for name in ("camera_frame", "tag_observation", "pose", "range_scan")}
    events: list[Observation] = []
    for index, line in enumerate(lines, 1):
        try:
            event = decode_observation(line)
        except ValueError as error:
            raise ValueError(f"archive observation {index} is invalid") from error
        if event.encode() != line:
            raise ValueError(f"archive observation {index} is not canonical")
        submission = event.submission
        kind = submission.payload["kind"]
        if kind not in actual_kinds or submission.node_type != "ground":
            raise ValueError(f"archive observation {index} has an unsupported kind")
        expected = source_scopes[
            {
                "camera_frame": "camera",
                "tag_observation": "tag",
                "pose": "pose",
                "range_scan": "lidar",
            }[kind]
        ]
        if any(getattr(submission, key) != value for key, value in expected.items()):
            raise ValueError(f"archive observation {index} does not match archive scope")
        if kind in {"camera_frame", "tag_observation"} and submission.frame != frames["camera"]:
            raise ValueError(f"archive observation {index} has a camera frame mismatch")
        if kind == "pose":
            pose = submission.payload["pose"]
            if (
                submission.frame != frames["odom"]
                or pose["parent_frame"] != frames["odom"]
                or pose["child_frame"] != frames["body"]
            ):
                raise ValueError(f"archive observation {index} has a pose frame mismatch")
        if kind == "range_scan":
            pose = submission.payload["sensor_pose"]
            if (
                submission.frame != frames["lidar"]
                or pose["parent_frame"] != frames["odom"]
                or pose["child_frame"] != frames["lidar"]
            ):
                raise ValueError(f"archive observation {index} has a lidar frame mismatch")
        actual_kinds[kind] += 1
        events.append(event)
    if observations.get("kinds") != actual_kinds:
        raise ValueError("archive observation kind counts do not match")
    return manifest, payload, events


def _lidar_config(
    path: Path, mount_id: str, archive: Mapping[str, object]
) -> tuple[RecordingConfig, GridConfig, dict[str, str]]:
    payload = _read_regular(path, MAX_MANIFEST_BYTES)
    document = _object(payload, "lidar config")
    required = {
        "schema_version",
        "kind",
        "source_scope",
        "odom_frame",
        "lidar_frame",
        "mount_id",
        "recording",
        "grid",
    }
    if (
        set(document) != required
        or document.get("schema_version") != 1
        or document.get("kind") != "ohmni_local_map_lidar_config"
    ):
        raise ValueError("lidar config schema is invalid")
    sources = _mapping(archive["sources"], "archive sources")
    frames = _mapping(archive["frames"], "archive frames")
    scope = _scope(document["source_scope"], "lidar config source scope")
    expected = {
        "session": archive["scope"]["session"],
        "device_id": archive["scope"]["device_id"],
        "connection_epoch": archive["scope"]["connection_epoch"],
        "source_id": sources["lidar"],
    }
    if (
        scope != expected
        or document["odom_frame"] != frames["odom"]
        or document["lidar_frame"] != frames["lidar"]
    ):
        raise ValueError("lidar config does not agree with archive scope or frames")
    if document["mount_id"] != mount_id or not isinstance(mount_id, str) or not mount_id:
        raise ValueError("explicit reviewed lidar mount ID does not match config")
    recording = _mapping(document["recording"], "lidar recording")
    grid = _mapping(document["grid"], "lidar grid")
    if set(recording) != {"max_records", "max_bytes", "duration_s"} or set(grid) != {
        "resolution_m",
        "max_cells",
        "bounds",
    }:
        raise ValueError("lidar config recording or grid settings are incomplete")
    return (
        RecordingConfig(
            **scope,
            odom_frame=document["odom_frame"],
            lidar_frame=document["lidar_frame"],
            mount_id=mount_id,
            run_id=f"local-map-{_digest(payload)[:16]}",
            **recording,
        ),
        GridConfig(
            grid["resolution_m"],
            grid["max_cells"],
            tuple(grid["bounds"]) if grid["bounds"] is not None else None,
        ),
        {"path": path.name, "sha256": _digest(payload)},
    )


def _copy_pinned(root: Path, temporary: Path, pin: Mapping[str, str], name: str) -> bytes:
    relative = Path(pin["path"])
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise ValueError(f"{name} pin path must stay below evidence root")
    payload = _read_regular(root / relative, 1024 * 1024)
    if _digest(payload) != pin["sha256"]:
        raise ValueError(f"{name} pin does not match evidence bytes")
    target = temporary / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    return payload


def build(
    archive: Path,
    lidar_config: Path,
    lidar_mount_id: str,
    request_path: Path,
    evidence_root: Path,
    output: Path,
) -> dict[str, object]:
    archive_manifest, archive_observations, events = _archive(archive)
    recording_config, grid_config, lidar_pin = _lidar_config(
        lidar_config, lidar_mount_id, archive_manifest
    )
    request_payload = _read_regular(request_path, MAX_MANIFEST_BYTES)
    request = _request(_object(request_payload, "fusion request"))
    if request["candidate_mode"] != "local_odom":
        raise ValueError("map candidate requires a closed local_odom fusion request")
    frames = _mapping(archive_manifest["frames"], "archive frames")
    sources = _mapping(archive_manifest["sources"], "archive sources")
    if request["odom_frame"] != frames["odom"]:
        raise ValueError("fusion request odometry frame does not match archive")
    for name in ("pose", "camera", "tag"):
        expected = {
            "session": archive_manifest["scope"]["session"],
            "device_id": archive_manifest["scope"]["device_id"],
            "connection_epoch": archive_manifest["scope"]["connection_epoch"],
            "source_id": sources[name],
        }
        if request["source_scopes"][name] != expected:
            raise ValueError(f"fusion request {name} scope does not match archive")
    observation_pin = _pin(request["observations"], "fusion observations")
    if observation_pin["sha256"] != _digest(archive_observations):
        raise ValueError("fusion observation pin does not match archive")
    observation_path = Path(observation_pin["path"])
    if (
        observation_path.is_absolute()
        or ".." in observation_path.parts
        or not observation_path.parts
    ):
        raise ValueError("fusion observation pin path must stay below evidence root")
    if not any(event.submission.payload["kind"] == "range_scan" for event in events):
        raise ValueError("archive has no accepted lidar observations")
    output = output.absolute()
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"map candidate output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=output.parent))
    published = False
    try:
        snapshots = temporary / "inputs"
        snapshots.mkdir()
        snapshot_observations = snapshots / observation_path
        snapshot_observations.parent.mkdir(parents=True, exist_ok=True)
        with snapshot_observations.open("xb") as stream:
            stream.write(archive_observations)
            stream.flush()
            os.fsync(stream.fileno())
        with (snapshots / "request.json").open("xb") as stream:
            stream.write(request_payload)
            stream.flush()
            os.fsync(stream.fileno())
        calibration_pin = _pin(request["calibration"], "calibration")
        mount_pin = _pin(request["mount"], "mount")
        _copy_pinned(evidence_root, snapshots, calibration_pin, "calibration")
        _copy_pinned(evidence_root, snapshots, mount_pin, "mount")
        record_events(
            (event.encode() for event in events), temporary / "recording", recording_config
        )
        grid = build_grid(temporary / "recording", temporary / "occupancy", grid_config)
        tags = run(snapshots / "request.json", snapshots, temporary / "tag_candidates.json")
        if not tags["candidates"]:
            raise ValueError("archive cannot produce qualified local tag candidates")
        files = {}
        for relative in (
            "recording/recording.json",
            "recording/observations.jsonl",
            "occupancy/manifest.json",
            "occupancy/occupancy.png",
            "tag_candidates.json",
            str(Path("inputs") / observation_path),
            "inputs/request.json",
            str(Path("inputs") / calibration_pin["path"]),
            str(Path("inputs") / mount_pin["path"]),
        ):
            payload = _read_regular(temporary / relative, MAX_OUTPUT_BYTES)
            files[relative] = {"bytes": len(payload), "sha256": _digest(payload)}
        manifest = {
            "schema_version": 1,
            "kind": "ohmni_local_map_candidate",
            "approval_status": "unapproved",
            "candidate_mode": "local_odom",
            "candidate_frame": frames["odom"],
            "claim_scope": "Offline local occupancy and tag estimates. This candidate does not approve control, flight, or autonomous movement.",
            "archive": {
                "manifest_sha256": _digest(
                    _read_regular(archive / "manifest.json", MAX_MANIFEST_BYTES)
                ),
                "observations": observation_pin,
            },
            "inputs": {
                "lidar_config": lidar_pin,
                "fusion_request": {
                    "path": "inputs/request.json",
                    "sha256": _digest(request_payload),
                },
                "calibration": calibration_pin,
                "mount": mount_pin,
            },
            "artifacts": {
                "recording": "recording/recording.json",
                "occupancy": "occupancy/manifest.json",
                "tags": "tag_candidates.json",
            },
            "files": files,
            "occupancy": {
                "frame": grid["grid"]["frame"],
                "artifact_sha256": grid["artifact_sha256"],
            },
            "tag_count": len(tags["candidates"]),
        }
        encoded = json.dumps(manifest, allow_nan=False, indent=2, sort_keys=True).encode() + b"\n"
        with (temporary / "manifest.json").open("xb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.rename(temporary, output)
        published = True
        return manifest
    finally:
        if not published:
            shutil.rmtree(temporary, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--lidar-config", type=Path, required=True)
    parser.add_argument("--lidar-mount-id", required=True)
    parser.add_argument("--fusion-request", type=Path, required=True)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = build(
            args.archive,
            args.lidar_config,
            args.lidar_mount_id,
            args.fusion_request,
            args.evidence_root,
            args.output,
        )
    except (OSError, ValueError, TypeError) as error:
        raise SystemExit(f"ohmni local map candidate failed: {error}") from error
    print(
        json.dumps({"output": str(args.output), "tag_count": result["tag_count"]}, sort_keys=True)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
