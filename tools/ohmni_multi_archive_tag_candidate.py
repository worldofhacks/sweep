"""Fuse a bounded, continuous series of accepted mapper archives in one local odometry frame."""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path

from relay.observations import Observation, decode_observation
from tools.ohmni_live_tag_mapper import (
    MAX_ARCHIVE_BYTES,
    MAX_ARCHIVE_DURATION_S,
    MAX_ARCHIVE_RECORDS,
)
from tools.ohmni_local_map_candidate import (
    MAX_MANIFEST_BYTES,
    _archive,
    _digest,
    _lidar_config,
    _mapping,
    _object,
    _pin,
    _read_regular,
)
from tools.ohmni_occupancy_grid import build_grid
from tools.ohmni_scan_record import MANIFEST_RESERVE_BYTES, record_events
from tools.ohmni_tag_candidate_fusion import (
    MAX_MULTI_ARCHIVE_OBSERVATIONS,
    _calibration,
    _mount,
    _pose_matrix,
    _request,
    _rotation_distance,
    _translation,
    fuse_observations,
)

MAX_ARCHIVES = 64
MAX_AGGREGATE_BYTES = 64 * 1024 * 1024
MAX_CONTINUITY_GAP_NS = 100_000_000


def _write_snapshot(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _capture(event: Observation) -> tuple[str, int]:
    captured = event.submission.t_capture
    if captured is None or captured.unit != "ns":
        raise ValueError("archive pose is missing a nanosecond capture time")
    return captured.clock_id, captured.value


def _boundary_pose(
    events: Sequence[Observation], first: bool
) -> tuple[str, int, list[list[float]]]:
    poses = [event for event in events if event.submission.payload["kind"] == "pose"]
    if not poses:
        raise ValueError("archive has no body poses for continuity")
    captures = [_capture(event) for event in poses]
    if len({clock for clock, _ in captures}) != 1:
        raise ValueError("archive pose continuity requires one capture clock")
    if any(
        later < earlier for (_, earlier), (_, later) in zip(captures, captures[1:], strict=False)
    ):
        raise ValueError("archive pose capture times are not monotonic")
    event = poses[0 if first else -1]
    clock, timestamp = _capture(event)
    return clock, timestamp, _pose_matrix(event.submission.payload["pose"])


def _shared_tag_ids(events: Sequence[Observation]) -> set[int]:
    return {
        event.submission.payload["tag_id"]
        for event in events
        if event.submission.payload["kind"] == "tag_observation"
        and event.submission.payload["pose_accepted"] is True
        and event.submission.payload["tag_pose"] is not None
    }


def _archive_limits(manifest: Mapping[str, object]) -> None:
    limits = manifest.get("limits")
    if not isinstance(limits, Mapping) or set(limits) != {"max_records", "max_bytes", "duration_s"}:
        raise ValueError("archive limits are incomplete")
    records = limits["max_records"]
    maximum_bytes = limits["max_bytes"]
    duration = limits["duration_s"]
    if (
        type(records) is not int
        or not 1 <= records <= MAX_ARCHIVE_RECORDS
        or type(maximum_bytes) is not int
        or not 1 <= maximum_bytes <= MAX_ARCHIVE_BYTES
        or isinstance(duration, bool)
        or not isinstance(duration, int | float)
        or not math.isfinite(duration)
        or not 0 < duration <= MAX_ARCHIVE_DURATION_S
        or manifest["observations"]["count"] > records
        or manifest["observations"]["bytes"] > maximum_bytes
    ):
        raise ValueError("archive exceeds the accepted mapper limits")


def _safe_evidence_path(root: Path, pin: Mapping[str, str], name: str) -> Path:
    relative = Path(pin["path"])
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise ValueError(f"{name} pin path must stay below evidence root")
    return root / relative


def _read_evidence(
    request: Mapping[str, object], evidence_root: Path
) -> tuple[
    dict[str, object],
    dict[str, object],
    dict[str, str],
    dict[str, str],
    bytes,
    bytes,
]:
    calibration_pin = _pin(request["calibration"], "calibration")
    mount_pin = _pin(request["mount"], "mount")
    calibration_payload = _read_regular(
        _safe_evidence_path(evidence_root, calibration_pin, "calibration"), 1024 * 1024
    )
    mount_payload = _read_regular(
        _safe_evidence_path(evidence_root, mount_pin, "mount"), 1024 * 1024
    )
    if _digest(calibration_payload) != calibration_pin["sha256"]:
        raise ValueError("calibration pin does not match evidence bytes")
    if _digest(mount_payload) != mount_pin["sha256"]:
        raise ValueError("mount pin does not match evidence bytes")
    calibration = _calibration(_object(calibration_payload, "calibration"), calibration_pin)
    mount = _mount(_object(mount_payload, "mount"), calibration)
    return calibration, mount, calibration_pin, mount_pin, calibration_payload, mount_payload


def _snapshot_pinned(inputs: Path, pin: Mapping[str, str], payload: bytes, name: str) -> None:
    target = _safe_evidence_path(inputs, pin, name)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _sha256(value: object, name: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _metadata(value: object, name: str) -> dict[str, object]:
    metadata = _mapping(value, name)
    if set(metadata) != {"count", "bytes", "sha256"} or (
        type(metadata["count"]) is not int
        or metadata["count"] < 1
        or type(metadata["bytes"]) is not int
        or metadata["bytes"] < 1
    ):
        raise ValueError(f"{name} is invalid")
    _sha256(metadata["sha256"], name)
    return metadata


def _timestamp(value: object, name: str) -> dict[str, object]:
    timestamp = _mapping(value, name)
    if (
        set(timestamp) != {"clock_id", "unit", "value"}
        or type(timestamp["clock_id"]) is not str
        or timestamp["unit"] != "ns"
        or type(timestamp["value"]) is not int
    ):
        raise ValueError(f"{name} is invalid")
    return timestamp


def _collection(
    path: Path,
    archives: Sequence[Path],
    manifests: Sequence[Mapping[str, object]],
    manifest_payloads: Sequence[bytes],
    observation_payloads: Sequence[bytes],
    event_chunks: Sequence[Sequence[Observation]],
) -> tuple[bytes, dict[str, object], list[Observation], bytes]:
    payload = _read_regular(path, MAX_MANIFEST_BYTES)
    document = _object(payload, "continuous mapper collection")
    if (
        set(document)
        != {
            "schema_version",
            "kind",
            "archives",
            "handoffs",
            "raw_observations",
            "unique_observations",
        }
        or document["schema_version"] != 1
        or document["kind"] != "ohmni_continuous_mapper_collection"
    ):
        raise ValueError("continuous mapper collection schema is invalid")
    archive_pins = document["archives"]
    handoffs = document["handoffs"]
    if not isinstance(archive_pins, list) or len(archive_pins) != len(archives):
        raise ValueError("collection archives do not match supplied archives")
    if not isinstance(handoffs, list) or len(handoffs) != len(archives) - 1:
        raise ValueError("collection must declare one handoff per archive boundary")
    for index, pin in enumerate(archive_pins):
        item = _mapping(pin, "collection archive")
        if (
            set(item) != {"path", "manifest_sha256", "observations_sha256"}
            or type(item["path"]) is not str
        ):
            raise ValueError("collection archive pin is invalid")
        relative = Path(item["path"])
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise ValueError("collection archive path must stay below collection root")
        if (path.parent / relative).resolve() != archives[index].resolve():
            raise ValueError("collection archive order does not match supplied archives")
        if _sha256(item["manifest_sha256"], "collection archive manifest") != _digest(
            manifest_payloads[index]
        ):
            raise ValueError("collection manifest pin does not match archive")
        if _sha256(item["observations_sha256"], "collection archive observations") != _digest(
            observation_payloads[index]
        ):
            raise ValueError("collection observation pin does not match archive")
    raw = b"".join(observation_payloads)
    raw_metadata = _metadata(document["raw_observations"], "collection raw observations")
    if raw_metadata != {
        "count": sum(map(len, event_chunks)),
        "bytes": len(raw),
        "sha256": _digest(raw),
    }:
        raise ValueError("collection raw observation summary does not match archives")

    dropped: set[tuple[int, int]] = set()
    allowed: set[tuple[str, str]] = set()
    for boundary, handoff in enumerate(handoffs):
        item = _mapping(handoff, "collection handoff")
        if (
            set(item)
            != {
                "from_archive",
                "to_archive",
                "source_id",
                "event_id",
                "observation_sha256",
                "t_capture",
                "t_source_receipt",
            }
            or item["from_archive"] != boundary
            or item["to_archive"] != boundary + 1
            or not all(type(item[key]) is str for key in ("source_id", "event_id"))
        ):
            raise ValueError("collection handoff must name its adjacent archive boundary")
        _sha256(item["observation_sha256"], "collection handoff observation")
        capture = _timestamp(item["t_capture"], "collection handoff capture time")
        receipt = _timestamp(item["t_source_receipt"], "collection handoff receipt time")
        key = (item["source_id"], item["event_id"])
        if key in allowed or item["source_id"] != manifests[boundary]["sources"]["pose"]:
            raise ValueError("collection handoff duplicates or misidentifies its pose source")
        matches = []
        for chunk_index in (boundary, boundary + 1):
            matches.extend(
                (chunk_index, event_index, event)
                for event_index, event in enumerate(event_chunks[chunk_index])
                if (event.submission.source_id, event.submission.event_id) == key
            )
        if len(matches) != 2 or {match[0] for match in matches} != {boundary, boundary + 1}:
            raise ValueError("collection handoff pose must occur once in each adjacent archive")
        first, second = matches
        captured = first[2].submission.t_capture
        received = first[2].submission.t_source_receipt
        if captured is None or received is None:
            raise ValueError("collection handoff pose must carry capture and receipt times")
        encoded = first[2].encode()
        if (
            first[2].submission.payload["kind"] != "pose"
            or second[2].encode() != encoded
            or _digest(encoded) != item["observation_sha256"]
            or {
                "clock_id": captured.clock_id,
                "unit": captured.unit,
                "value": captured.value,
            }
            != capture
            or {
                "clock_id": received.clock_id,
                "unit": received.unit,
                "value": received.value,
            }
            != receipt
        ):
            raise ValueError("collection handoff does not bind one exact accepted pose")
        allowed.add(key)
        dropped.add((second[0], second[1]))
    occurrences: dict[tuple[str, str], int] = {}
    for chunk in event_chunks:
        for event in chunk:
            key = (event.submission.source_id, event.submission.event_id)
            occurrences[key] = occurrences.get(key, 0) + 1
    if any(count > 1 and key not in allowed for key, count in occurrences.items()):
        raise ValueError("collection contains a duplicate event outside a declared handoff")
    unique_events = [
        event
        for chunk_index, chunk in enumerate(event_chunks)
        for event_index, event in enumerate(chunk)
        if (chunk_index, event_index) not in dropped
    ]
    unique = b"".join(event.encode() + b"\n" for event in unique_events)
    unique_metadata = _metadata(document["unique_observations"], "collection unique observations")
    if unique_metadata != {
        "count": len(unique_events),
        "bytes": len(unique),
        "sha256": _digest(unique),
    }:
        raise ValueError("collection unique observation summary does not match its handoffs")
    return payload, document, unique_events, unique


def build(
    archives: Sequence[Path],
    request_path: Path,
    evidence_root: Path,
    output: Path,
    maximum_continuity_gap_ns: int,
    collection_path: Path | None = None,
) -> dict[str, object]:
    if not 1 <= len(archives) <= MAX_ARCHIVES:
        raise ValueError(f"archive count must be from 1 through {MAX_ARCHIVES}")
    if (
        type(maximum_continuity_gap_ns) is not int
        or not 0 <= maximum_continuity_gap_ns <= MAX_CONTINUITY_GAP_NS
    ):
        raise ValueError("maximum continuity gap must be from 0 through 100000000 ns")
    request_payload = _read_regular(request_path, MAX_MANIFEST_BYTES)
    request = _request(_object(request_payload, "fusion request"))
    if request["candidate_mode"] != "local_odom":
        raise ValueError("multi-archive candidate requires a closed local_odom fusion request")
    (
        calibration,
        mount,
        calibration_pin,
        mount_pin,
        calibration_payload,
        mount_payload,
    ) = _read_evidence(request, evidence_root)

    baseline: tuple[object, object, object] | None = None
    event_chunks: list[list[Observation]] = []
    manifests: list[Mapping[str, object]] = []
    manifest_payloads: list[bytes] = []
    observation_payloads: list[bytes] = []
    previous: tuple[str, int, list[list[float]]] | None = None
    shared: set[int] = set()
    seen_tags: set[int] = set()
    all_events: list[Observation] = []
    for archive in archives:
        manifest, manifest_payload, observations_payload, events = _archive(archive)
        _archive_limits(manifest)
        identity = (manifest["scope"], manifest["sources"], manifest["frames"])
        if baseline is None:
            baseline = identity
            frames = manifest["frames"]
            sources = manifest["sources"]
            for name in ("pose", "camera", "tag"):
                expected = {
                    "session": manifest["scope"]["session"],
                    "device_id": manifest["scope"]["device_id"],
                    "connection_epoch": manifest["scope"]["connection_epoch"],
                    "source_id": sources[name],
                }
                if request["source_scopes"][name] != expected:
                    raise ValueError(f"fusion request {name} scope does not match archives")
            if request["odom_frame"] != frames["odom"]:
                raise ValueError("fusion request odometry frame does not match archives")
        elif identity != baseline:
            raise ValueError(
                "archives must share an exact session, epoch, source, and frame identity"
            )
        start = _boundary_pose(events, True) if collection_path is None else None
        if previous is not None and start is not None:
            if (
                start[0] != previous[0]
                or start[1] < previous[1]
                or start[1] - previous[1] > maximum_continuity_gap_ns
            ):
                raise ValueError("archive pose continuity time proof failed")
            if (
                math.dist(_translation(previous[2]), _translation(start[2]))
                > request["maximum_translation_spread_m"]
            ):
                raise ValueError("archive pose continuity translation proof failed")
            if _rotation_distance(previous[2], start[2]) > request["maximum_rotation_spread_rad"]:
                raise ValueError("archive pose continuity rotation proof failed")
        previous = _boundary_pose(events, False) if collection_path is None else None
        tags = _shared_tag_ids(events)
        shared |= seen_tags.intersection(tags)
        seen_tags |= tags
        event_chunks.append(events)
        manifests.append(manifest)
        manifest_payloads.append(manifest_payload)
        observation_payloads.append(observations_payload)
        all_events.extend(events)
    collection_payload: bytes | None = None
    collection: dict[str, object] | None = None
    if collection_path is not None:
        collection_payload, collection, all_events, combined = _collection(
            collection_path,
            archives,
            manifests,
            manifest_payloads,
            observation_payloads,
            event_chunks,
        )
    else:
        combined = b"".join(observation_payloads)
    if len(all_events) > MAX_MULTI_ARCHIVE_OBSERVATIONS or len(combined) > MAX_AGGREGATE_BYTES:
        raise ValueError("combined archives exceed the aggregate fusion limit")
    observation_pin = _pin(request["observations"], "fusion observations")
    _safe_evidence_path(evidence_root, observation_pin, "fusion observations")
    if observation_pin["sha256"] != _digest(combined):
        raise ValueError("fusion observation pin does not match combined archives")
    result = fuse_observations(
        all_events,
        request=request,
        calibration=calibration,
        mount=mount,
        registration=None,
        input_pins={
            "observations": {
                "path": "inputs/combined-observations.jsonl",
                "sha256": observation_pin["sha256"],
            },
            "calibration": {
                "path": str(Path("inputs") / calibration_pin["path"]),
                "sha256": calibration_pin["sha256"],
            },
            "mount": {
                "path": str(Path("inputs") / mount_pin["path"]),
                "sha256": mount_pin["sha256"],
            },
            "registration": None,
            "vertical_datum": None,
        },
        maximum_observations=MAX_MULTI_ARCHIVE_OBSERVATIONS,
    )
    candidate_ids = {item["tag_id"] for item in result["candidates"]}
    if shared - candidate_ids:
        raise ValueError("shared tag observations do not agree across archives")
    if result["candidate_frame"] != request["odom_frame"]:
        raise ValueError("generated tag candidates do not match archive odometry frame")
    if not result["candidates"]:
        raise ValueError("archives cannot produce qualified local tag candidates")

    output = output.absolute()
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"candidate output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=output.parent))
    published = False
    try:
        inputs = temporary / "inputs"
        inputs.mkdir()
        (inputs / "combined-observations.jsonl").write_bytes(combined)
        (inputs / "request.json").write_bytes(request_payload)
        _snapshot_pinned(inputs, calibration_pin, calibration_payload, "calibration")
        _snapshot_pinned(inputs, mount_pin, mount_payload, "mount")
        for index, payload in enumerate(manifest_payloads):
            (inputs / f"archive-{index:03d}-manifest.json").write_bytes(payload)
        if collection_payload is not None and collection is not None:
            (inputs / "collection.json").write_bytes(collection_payload)
            for index, payload in enumerate(observation_payloads):
                (inputs / f"archive-{index:03d}-observations.jsonl").write_bytes(payload)
        result["request_sha256"] = _digest(request_payload)
        result["requested_observations"] = observation_pin
        result["archive_count"] = len(archives)
        result["archives"] = [
            {"path": f"inputs/archive-{index:03d}-manifest.json", "sha256": _digest(payload)}
            for index, payload in enumerate(manifest_payloads)
        ]
        result["aggregate_observations"] = result.pop("observations")
        result["continuity"] = {
            "maximum_gap_ns": maximum_continuity_gap_ns,
            "checked_boundaries": max(0, len(archives) - 1),
        }
        if collection_payload is not None and collection is not None:
            result["collection"] = {
                "path": "inputs/collection.json",
                "sha256": _digest(collection_payload),
            }
            result["raw_observations"] = collection["raw_observations"]
            result["unique_observations"] = collection["unique_observations"]
            result["raw_archives"] = [
                {
                    "path": f"inputs/archive-{index:03d}-observations.jsonl",
                    "sha256": _digest(payload),
                }
                for index, payload in enumerate(observation_payloads)
            ]
        encoded = json.dumps(result, allow_nan=False, indent=2, sort_keys=True).encode() + b"\n"
        (temporary / "candidate.json").write_bytes(encoded)
        os.rename(temporary, output)
        published = True
        return result
    finally:
        if not published:
            shutil.rmtree(temporary, ignore_errors=True)


def build_map(
    archives: Sequence[Path],
    lidar_config_path: Path,
    lidar_mount_id: str,
    request_path: Path,
    evidence_root: Path,
    output: Path,
    maximum_continuity_gap_ns: int,
    collection_path: Path | None = None,
) -> dict[str, object]:
    output = output.absolute()
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"candidate output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=output.parent))
    published = False
    try:
        tag_output = temporary / "tag-candidate"
        tags = build(
            archives,
            request_path,
            evidence_root,
            tag_output,
            maximum_continuity_gap_ns,
            collection_path,
        )
        inputs = tag_output / "inputs"
        combined = _read_regular(inputs / "combined-observations.jsonl", MAX_AGGREGATE_BYTES)
        events = [decode_observation(line) for line in combined.splitlines()]
        lidar_events = [
            event for event in events if event.submission.payload["kind"] == "range_scan"
        ]
        if not lidar_events:
            raise ValueError("archives have no accepted lidar observations")
        archive_manifest_payload = _read_regular(
            inputs / "archive-000-manifest.json", MAX_MANIFEST_BYTES
        )
        archive_manifest = _object(archive_manifest_payload, "archive manifest snapshot")
        recording_config, grid_config, lidar_config_payload, lidar_pin = _lidar_config(
            lidar_config_path, lidar_mount_id, archive_manifest
        )
        lidar_bytes = sum(len(event.encode()) + 1 for event in lidar_events)
        if len(lidar_events) > recording_config.max_records:
            raise ValueError("reviewed lidar recording record budget cannot contain combined scans")
        if lidar_bytes > recording_config.max_bytes - MANIFEST_RESERVE_BYTES:
            raise ValueError("reviewed lidar recording byte budget cannot contain combined scans")

        os.rename(inputs, temporary / "inputs")
        _write_snapshot(temporary / "inputs" / "lidar-config.json", lidar_config_payload)
        recording = record_events(
            (event.encode() for event in events), temporary / "recording", recording_config
        )
        if recording["observations"]["stop_reason"] != "input_exhausted":
            raise ValueError("reviewed lidar recording budget did not retain every combined scan")
        grid = build_grid(temporary / "recording", temporary / "occupancy", grid_config)
        if grid["grid"]["frame"] != tags["candidate_frame"]:
            raise ValueError("generated map artifacts do not match archive odometry frame")
        if not tags["candidates"]:
            raise ValueError("archives cannot produce qualified local tag candidates")
        _write_snapshot(
            temporary / "tag_candidates.json",
            json.dumps(tags, allow_nan=False, indent=2, sort_keys=True).encode() + b"\n",
        )
        shutil.rmtree(tag_output)

        calibration_pin = tags["calibration"]
        mount_pin = tags["mount"]
        files: dict[str, dict[str, int | str]] = {}
        paths = [
            "recording/recording.json",
            "recording/observations.jsonl",
            "occupancy/manifest.json",
            "occupancy/occupancy.png",
            "tag_candidates.json",
            "inputs/combined-observations.jsonl",
            "inputs/request.json",
            "inputs/lidar-config.json",
            *(archive["path"] for archive in tags["archives"]),
            *(archive["path"] for archive in tags.get("raw_archives", [])),
            *([tags["collection"]["path"]] if "collection" in tags else []),
            calibration_pin["path"],
            mount_pin["path"],
        ]
        for relative in paths:
            payload = _read_regular(temporary / relative, MAX_AGGREGATE_BYTES)
            files[relative] = {"bytes": len(payload), "sha256": _digest(payload)}
        manifest = {
            "schema_version": 1,
            "kind": "ohmni_multi_archive_local_map_candidate",
            "approval_status": "unapproved",
            "candidate_mode": "local_odom",
            "candidate_frame": tags["candidate_frame"],
            "claim_scope": (
                "Offline local occupancy and tag estimates. This candidate does not approve "
                "control, flight, or autonomous movement."
            ),
            "archive_count": tags["archive_count"],
            "archives": tags["archives"],
            "continuity": tags["continuity"],
            "inputs": {
                "lidar_config": lidar_pin,
                "fusion_request": {
                    "path": "inputs/request.json",
                    "sha256": tags["request_sha256"],
                },
                "aggregate_observations": tags["aggregate_observations"],
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
        if "collection" in tags:
            manifest["collection"] = tags["collection"]
            manifest["raw_observations"] = tags["raw_observations"]
            manifest["unique_observations"] = tags["unique_observations"]
            manifest["raw_archives"] = tags["raw_archives"]
        _write_snapshot(
            temporary / "manifest.json",
            json.dumps(manifest, allow_nan=False, indent=2, sort_keys=True).encode() + b"\n",
        )
        os.rename(temporary, output)
        published = True
        return manifest
    finally:
        if not published:
            shutil.rmtree(temporary, ignore_errors=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fusion-request", required=True, type=Path)
    parser.add_argument("--evidence-root", required=True, type=Path)
    parser.add_argument("--lidar-config", required=True, type=Path)
    parser.add_argument("--lidar-mount-id", required=True)
    parser.add_argument("--collection", required=True, type=Path)
    parser.add_argument("--maximum-continuity-gap-ns", required=True, type=int)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("archives", nargs="+", type=Path)
    args = parser.parse_args(argv)
    try:
        result = build_map(
            args.archives,
            args.lidar_config,
            args.lidar_mount_id,
            args.fusion_request,
            args.evidence_root,
            args.output,
            args.maximum_continuity_gap_ns,
            args.collection,
        )
    except (OSError, TypeError, ValueError) as error:
        raise SystemExit(f"ohmni multi-archive tag candidate failed: {error}") from error
    print(
        json.dumps(
            {"output": str(args.output), "tag_count": len(result["candidates"])}, sort_keys=True
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
