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

from relay.observations import Observation
from tools.ohmni_live_tag_mapper import (
    MAX_ARCHIVE_BYTES,
    MAX_ARCHIVE_DURATION_S,
    MAX_ARCHIVE_RECORDS,
)
from tools.ohmni_local_map_candidate import (
    MAX_MANIFEST_BYTES,
    _archive,
    _digest,
    _object,
    _pin,
    _read_regular,
)
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


def build(
    archives: Sequence[Path],
    request_path: Path,
    evidence_root: Path,
    output: Path,
    maximum_continuity_gap_ns: int,
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
        start = _boundary_pose(events, True)
        if previous is not None:
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
        previous = _boundary_pose(events, False)
        tags = _shared_tag_ids(events)
        shared |= seen_tags.intersection(tags)
        seen_tags |= tags
        event_chunks.append(events)
        manifest_payloads.append(manifest_payload)
        observation_payloads.append(observations_payload)
        all_events.extend(events)
    if (
        len(all_events) > MAX_MULTI_ARCHIVE_OBSERVATIONS
        or sum(map(len, observation_payloads)) > MAX_AGGREGATE_BYTES
    ):
        raise ValueError("combined archives exceed the aggregate fusion limit")

    combined = b"".join(observation_payloads)
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
        encoded = json.dumps(result, allow_nan=False, indent=2, sort_keys=True).encode() + b"\n"
        (temporary / "candidate.json").write_bytes(encoded)
        os.rename(temporary, output)
        published = True
        return result
    finally:
        if not published:
            shutil.rmtree(temporary, ignore_errors=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fusion-request", required=True, type=Path)
    parser.add_argument("--evidence-root", required=True, type=Path)
    parser.add_argument("--maximum-continuity-gap-ns", required=True, type=int)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("archives", nargs="+", type=Path)
    args = parser.parse_args(argv)
    try:
        result = build(
            args.archives,
            args.fusion_request,
            args.evidence_root,
            args.output,
            args.maximum_continuity_gap_ns,
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
