"""Report expected AprilTag coverage across accepted mapper archives."""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from collections.abc import Sequence
from pathlib import Path

from relay.observations import Observation
from tools.ohmni_local_map_candidate import _archive

MAX_TAG_ID = 586


def _expected(path: Path) -> list[int]:
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("expected tag file is not valid JSON") from error
    if isinstance(raw, dict):
        raw = raw.get("expected_tag_ids")
    if not isinstance(raw, list) or not raw:
        raise ValueError("expected tags must be a non-empty JSON array or expected_tag_ids object field")
    if any(type(identifier) is not int or not 0 <= identifier <= MAX_TAG_ID for identifier in raw):
        raise ValueError(f"expected tag IDs must be integers from 0 through {MAX_TAG_ID}")
    if len(set(raw)) != len(raw):
        raise ValueError("expected tag IDs must be unique")
    return sorted(raw)


def _capture_key(event: Observation) -> tuple[str, int] | None:
    captured = event.submission.t_capture
    if captured is None or captured.unit != "ns":
        return None
    return captured.clock_id, captured.value


def report(
    archives: Sequence[Path], expected_tags: Sequence[int], maximum_association_error_ns: int
) -> dict[str, object]:
    if not archives:
        raise ValueError("at least one accepted archive is required")
    if type(maximum_association_error_ns) is not int or not 0 <= maximum_association_error_ns <= 100_000_000:
        raise ValueError("maximum association error must be from 0 through 100000000 ns")

    baseline: tuple[object, object, object] | None = None
    raw_counts: Counter[int] = Counter()
    preliminary_counts: Counter[int] = Counter()
    reasons: dict[int, Counter[str]] = defaultdict(Counter)
    chunks: list[dict[str, object]] = []

    for archive in archives:
        manifest, _, _, events = _archive(archive)
        identity = (manifest["scope"], manifest["sources"], manifest["frames"])
        if baseline is None:
            baseline = identity
        elif identity != baseline:
            raise ValueError("archives must share an exact session, epoch, source, and frame identity")

        cameras: set[tuple[str, str, int]] = set()
        poses: dict[str, list[int]] = defaultdict(list)
        tags: list[Observation] = []
        for event in events:
            kind = event.submission.payload["kind"]
            captured = _capture_key(event)
            if kind == "camera_frame" and captured is not None:
                image_id = event.submission.payload["image_id"]
                cameras.add((image_id, *captured))
            elif kind == "pose" and captured is not None:
                poses[captured[0]].append(captured[1])
            elif kind == "tag_observation":
                tags.append(event)
        for values in poses.values():
            values.sort()

        chunk_raw: Counter[int] = Counter()
        chunk_preliminary: Counter[int] = Counter()
        for event in tags:
            payload = event.submission.payload
            identifier = payload["tag_id"]
            if identifier not in expected_tags:
                continue
            raw_counts[identifier] += 1
            chunk_raw[identifier] += 1
            captured = _capture_key(event)
            if captured is None:
                reasons[identifier]["missing_capture_time"] += 1
                continue
            if payload["pose_accepted"] is not True or payload["tag_pose"] is None:
                reasons[identifier]["tag_pose_not_accepted"] += 1
                continue
            if (payload["image_id"], *captured) not in cameras:
                reasons[identifier]["camera_frame_not_capture_associated"] += 1
                continue
            samples = poses[captured[0]]
            if not samples or min(abs(value - captured[1]) for value in samples) > maximum_association_error_ns:
                reasons[identifier]["body_pose_not_capture_associated"] += 1
                continue
            preliminary_counts[identifier] += 1
            chunk_preliminary[identifier] += 1
        chunks.append(
            {
                "archive": str(archive),
                "stop_reason": manifest["observations"]["stop_reason"],
                "raw_tag_observations": dict(sorted(chunk_raw.items())),
                "preliminary_capture_associated_observations": dict(sorted(chunk_preliminary.items())),
            }
        )

    tags_report = []
    for identifier in expected_tags:
        preliminary = preliminary_counts[identifier]
        raw = raw_counts[identifier]
        status = "coverage_candidate" if preliminary >= 2 else "revisit"
        reasons_for_tag = reasons[identifier]
        tags_report.append(
            {
                "tag_id": identifier,
                "status": status,
                "raw_observations": raw,
                "preliminary_capture_associated_observations": preliminary,
                "revisit_reasons": dict(sorted(reasons_for_tag.items())),
            }
        )
    return {
        "schema_version": 1,
        "kind": "ohmni_map_coverage_report",
        "claim_scope": "Archive coverage only. Each candidate still requires calibrated fusion, shared-frame review, and cross-chunk drift evaluation.",
        "expected_tag_count": len(expected_tags),
        "archive_count": len(archives),
        "maximum_association_error_ns": maximum_association_error_ns,
        "chunks": chunks,
        "tags": tags_report,
        "revisit_tag_ids": [item["tag_id"] for item in tags_report if item["status"] == "revisit"],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-tags", required=True, type=Path)
    parser.add_argument("--maximum-association-error-ns", required=True, type=int)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("archives", nargs="+", type=Path)
    args = parser.parse_args(argv)
    if args.output.exists() or args.output.is_symlink():
        raise SystemExit(f"coverage report output already exists: {args.output}")
    try:
        result = report(args.archives, _expected(args.expected_tags), args.maximum_association_error_ns)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, allow_nan=False, indent=2, sort_keys=True) + "\n")
    except (OSError, ValueError, TypeError) as error:
        raise SystemExit(f"ohmni map coverage failed: {error}") from error
    print(json.dumps({"output": str(args.output), "revisit_tag_count": len(result["revisit_tag_ids"])}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
