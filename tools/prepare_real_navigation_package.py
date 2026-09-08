"""Stage retained tag geometry and optional owner approval for field preparation."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Mapping, Sequence
from math import isfinite
from pathlib import Path

_EXPECTED_TAG_IDS = frozenset(set(range(54)) - {29})
_SOURCE_FRAME = "unit11_atrium_38_to_39_v1"
_AREAS = (
    ("atrium-front", "Atrium Front", (32, 34, 41, 43), ("atrium front",)),
    ("carpet", "Carpet", (0, 2, 9, 11), ()),
)


def _load(path: Path) -> dict[str, object]:
    raw = json.loads(path.read_text())
    if not isinstance(raw, dict):
        raise ValueError("tag map must be an object")
    return raw


def _tag_points(raw: Mapping[str, object]) -> dict[int, tuple[float, float]]:
    frame = raw.get("coordinate_frame")
    if not isinstance(frame, Mapping) or frame.get("id") != _SOURCE_FRAME:
        raise ValueError("tag map coordinate frame does not match the retained 38-to-39 frame")
    coverage = raw.get("coverage")
    if (
        not isinstance(coverage, Mapping)
        or set(coverage.get("actual_tag_ids", ())) != _EXPECTED_TAG_IDS
    ):
        raise ValueError("tag map must contain exactly the 53 retained tag IDs")
    if coverage.get("intentionally_absent_tag_ids") != [29]:
        raise ValueError("tag 29 must remain the only intentionally absent tag")
    values = raw.get("tags")
    if not isinstance(values, list):
        raise ValueError("tag map tags must be a list")
    points: dict[int, tuple[float, float]] = {}
    for value in values:
        if not isinstance(value, Mapping):
            raise ValueError("tag map entry must be an object")
        tag_id, center = value.get("id"), value.get("center_m")
        if (
            type(tag_id) is not int
            or not isinstance(center, list)
            or len(center) != 3
            or not all(type(item) in {int, float} and isfinite(item) for item in center)
        ):
            raise ValueError("tag map entry has an invalid center")
        if tag_id in points:
            raise ValueError("tag map contains a duplicate tag ID")
        points[tag_id] = (float(center[0]), float(center[1]))
    if set(points) != _EXPECTED_TAG_IDS:
        raise ValueError("tag map IDs do not match its retained coverage")
    return points


def _convex_outline(points: Sequence[tuple[float, float]]) -> list[dict[str, float]]:
    ordered = sorted(set(points))
    if len(ordered) != 4:
        raise ValueError("each formation area needs four distinct measured corner tags")

    def cross(
        origin: tuple[float, float], left: tuple[float, float], right: tuple[float, float]
    ) -> float:
        return (left[0] - origin[0]) * (right[1] - origin[1]) - (left[1] - origin[1]) * (
            right[0] - origin[0]
        )

    lower: list[tuple[float, float]] = []
    for point in ordered:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], point) <= 0:
            lower.pop()
        lower.append(point)
    upper: list[tuple[float, float]] = []
    for point in reversed(ordered):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], point) <= 0:
            upper.pop()
        upper.append(point)
    hull = lower[:-1] + upper[:-1]
    if len(hull) != 4:
        raise ValueError("measured formation corners must form a non-degenerate quadrilateral")
    return [{"x": x, "y": y} for x, y in (*hull, hull[0])]


def _area(
    zone_id: str,
    name: str,
    corner_tag_ids: tuple[int, ...],
    aliases: tuple[str, ...],
    tag_points: Mapping[int, tuple[float, float]],
) -> dict[str, object]:
    corners = [tag_points[tag_id] for tag_id in corner_tag_ids]
    return {
        "zoneId": zone_id,
        "name": name,
        "aliases": list(aliases),
        "cornerTagIds": list(corner_tag_ids),
        "tagGridOutline": _convex_outline(corners),
        "arrivalCandidate": {
            "x": sum(point[0] for point in corners) / len(corners),
            "y": sum(point[1] for point in corners) / len(corners),
            "basis": "measured tag-grid corner centroid; not a dispatchable arrival slot",
        },
        "flightAuthorization": {
            "ownerApproved": False,
            "enabled": False,
            "verticalClearanceM": None,
            "horizontalClearanceM": None,
            "speedMps": None,
            "altitudeOffsetsM": None,
        },
    }


def _map_approval(path: Path | None, source_digest: str) -> dict[str, object]:
    if path is None:
        return {"status": "pending", "scope": "tag_map_baseline"}
    approval = _load(path)
    if (
        approval.get("schemaVersion") != 1
        or approval.get("scope") != "tag_map_baseline"
        or approval.get("ownerApproved") is not True
        or approval.get("sourceSha256") != source_digest
        or approval.get("coordinateFrame") != _SOURCE_FRAME
        or not isinstance(approval.get("recordedAt"), str)
        or not approval["recordedAt"].strip()
    ):
        raise ValueError("map approval must bind the exact source and coordinate frame")
    return {**approval, "status": "accepted"}


def build_package(source: Path, *, map_approval: Path | None = None) -> dict[str, object]:
    payload = source.read_bytes()
    raw = json.loads(payload)
    if not isinstance(raw, dict):
        raise ValueError("tag map must be an object")
    if raw.get("kind") != "complete_private_provisional_53_tag_map":
        raise ValueError("tag map is not the retained final 53-tag map")
    if raw.get("status") != "private_provisional_offline_map_not_for_registry_acceptance":
        raise ValueError("tag map status must remain provisional and unaccepted")
    tag_points = _tag_points(raw)
    digest = hashlib.sha256(payload).hexdigest()
    approval = _map_approval(map_approval, digest)
    accepted = approval["status"] == "accepted"
    areas = [_area(*specification, tag_points) for specification in _AREAS]
    return {
        "schemaVersion": 1,
        "kind": "real_navigation_staging_package",
        "activation": "blocked",
        "source": {
            "path": str(source),
            "sha256": digest,
            "coordinateFrame": _SOURCE_FRAME,
            "retainedTagIds": sorted(_EXPECTED_TAG_IDS),
            "intentionallyAbsentTagIds": [29],
        },
        "mapApproval": approval,
        "semanticCatalogSeed": {
            "destinations": [
                {"destinationId": area["zoneId"], "name": area["name"], "aliases": area["aliases"]}
                for area in areas
            ],
            "searchTargetClasses": [],
        },
        "formationAreaDrafts": areas,
        "formationBindingContract": {
            "selector": "zone_id",
            "intentArgs": ["name", "zone_id"],
            "selectionRule": "The signed binding must match both shape and zone_id.",
            "bindings": [
                {"shape": shape, "zoneId": area["zoneId"]}
                for area in areas
                for shape in ("line", "column")
            ],
        },
        "activationChecks": [
            {
                "id": "accepted-tag-map",
                "status": "passed" if accepted else "blocked",
                "evidence": (
                    "Owner approved this exact tag-map baseline for hardware-test preparation."
                    if accepted
                    else "No owner approval record accompanies the retained source."
                ),
            },
            {
                "id": "measured-route-geometry",
                "status": "blocked",
                "evidence": (
                    "Wall and height measurements and replacement-tag verification remain "
                    "outstanding; tag-map approval does not supply route clearance."
                ),
            },
            {
                "id": "formation-area-clearance",
                "status": "blocked",
                "evidence": (
                    "Horizontal and vertical clearances, speed limits, and altitude offsets "
                    "are unmeasured."
                ),
            },
            {
                "id": "two-qualified-aircraft",
                "status": "blocked",
                "evidence": (
                    "Register two aircraft with distinct current epochs, "
                    "control-localization pins, and flight qualification; ground robots do not "
                    "satisfy this check."
                ),
            },
            {
                "id": "signed-navigation-approval",
                "status": "blocked",
                "evidence": (
                    "No signed execution configuration is present in this staging package."
                ),
            },
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--map-approval", type=Path)
    arguments = parser.parse_args()
    package = build_package(arguments.source, map_approval=arguments.map_approval)
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(package, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
