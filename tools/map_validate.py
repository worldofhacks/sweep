"""Validate offline survey bundles before downstream map processing."""

import argparse
import hashlib
import json
import math
from datetime import datetime
from pathlib import Path

from tools.map_common import finite_number, read_document, validate_transform, write_document

FILES = {"tags.yaml", "zones.yaml", "obstacles.yaml"}


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _text(value, name):
    _require(isinstance(value, str) and bool(value.strip()), f"{name} must be nonempty text")
    return value


def _hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def content_hash(manifest):
    payload = {key: value for key, value in manifest.items() if key != "content_sha256"}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _source_path(bundle, name):
    _text(name, "source path")
    relative = Path(name)
    _require(not relative.is_absolute() and ".." not in relative.parts, "unsafe source path")
    path = (bundle / relative).resolve()
    _require(path.is_relative_to(bundle.resolve()) and path.is_file(), "source missing or unsafe")
    _require(name not in FILES | {"manifest.yaml"}, "source cannot be a bundle document")
    return path


def seal_manifest(bundle):
    """Recompute hashes and write manifest.yaml; this does not validate or approve the bundle."""
    bundle = Path(bundle)
    manifest = read_document(bundle / "manifest.yaml")
    for source in manifest["sources"]:
        source["sha256"] = _hash(_source_path(bundle, source["path"]))
    manifest["files"] = {name: _hash(bundle / name) for name in sorted(FILES)}
    manifest["content_sha256"] = content_hash(manifest)
    write_document(bundle / "manifest.yaml", manifest)
    return manifest


def _cross(a, b, c):
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _on_segment(a, b, p):
    return abs(_cross(a, b, p)) <= 1e-9 and all(
        min(a[i], b[i]) - 1e-9 <= p[i] <= max(a[i], b[i]) + 1e-9 for i in range(2)
    )


def _intersects(a, b, c, d):
    ab_c, ab_d, cd_a, cd_b = _cross(a, b, c), _cross(a, b, d), _cross(c, d, a), _cross(c, d, b)
    return (ab_c * ab_d < 0 and cd_a * cd_b < 0) or any(
        (_on_segment(a, b, c), _on_segment(a, b, d), _on_segment(c, d, a), _on_segment(c, d, b))
    )


def _polygon(value):
    _require(
        isinstance(value, list) and len(value) >= 4, "polygon needs three vertices and closure"
    )
    points = []
    for point in value:
        _require(isinstance(point, list) and len(point) == 2, "polygon point must be [x,y]")
        points.append([finite_number(item, "polygon coordinate") for item in point])
    _require(points[0] == points[-1], "polygon must be closed")
    n = len(points) - 1
    _require(len({tuple(p) for p in points[:-1]}) == n, "polygon repeats a vertex")
    area = sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(points, points[1:], strict=False))
    _require(abs(area) > 1e-9, "polygon has zero area")
    for i in range(n):
        _require(points[i] != points[i + 1], "polygon has zero-length edge")
        for j in range(i + 1, n):
            if j == i + 1 or (i == 0 and j == n - 1):
                continue
            _require(
                not _intersects(points[i], points[i + 1], points[j], points[j + 1]),
                "polygon self-intersects",
            )
    return points


def _inside(polygon, point):
    inside = False
    for a, b in zip(polygon, polygon[1:], strict=False):
        if _on_segment(a, b, point):
            return True
        if (a[1] > point[1]) != (b[1] > point[1]):
            x = a[0] + (point[1] - a[1]) * (b[0] - a[0]) / (b[1] - a[1])
            if x > point[0]:
                inside = not inside
    return inside


def _volume(value):
    polygon = _polygon(value["polygon"])
    low = finite_number(value["z_min"], "z_min")
    high = finite_number(value["z_max"], "z_max")
    _require(low < high, "altitude bounds must increase")
    return polygon, low, high


def _document_header(document):
    _require(
        type(document["schema_version"]) is int and document["schema_version"] == 1,
        "unsupported schema_version",
    )
    _require(document["units"] == "meters", "units must be meters")


def _validate_bundle(bundle, accepted_versions):
    manifest = read_document(bundle / "manifest.yaml")
    _document_header(manifest)
    _text(manifest["bundle_version"], "bundle_version")
    _require(manifest["bundle_version"] in accepted_versions, "bundle version is not accepted")
    timestamp = _text(manifest["created_at"], "created_at")
    parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    _require(
        parsed.utcoffset() is not None and parsed.utcoffset().total_seconds() == 0,
        "created_at must include UTC timezone",
    )
    frame = manifest["frame"]
    for key, expected in {
        "name": "building",
        "x": "toward_elevator",
        "y": "toward_street_wall",
        "z": "up",
    }.items():
        _require(frame[key] == expected, f"frame.{key} must be {expected}")
    _require(
        type(frame["origin_tag_id"]) is int and frame["origin_tag_id"] == 0,
        "origin_tag_id must be 0",
    )
    tag0_yaw = finite_number(frame["tag0_yaw_rad"], "tag0_yaw_rad")
    floors = manifest["floor_ids"]
    _require(isinstance(floors, list) and bool(floors), "floor_ids must be nonempty")
    for floor in floors:
        _text(floor, "floor_id")
    _require(len(set(floors)) == len(floors), "duplicate floor_id")
    _require(
        isinstance(manifest["sources"], list) and bool(manifest["sources"]),
        "sources must be nonempty",
    )
    source_paths = set()
    for source in manifest["sources"]:
        path = _source_path(bundle, source["path"])
        _require(path not in source_paths, "duplicate source path")
        source_paths.add(path)
        _require(source["sha256"] == _hash(path), "source hash mismatch")
        validate_transform(source["T_map_scan"])
        _require(finite_number(source["rms_m"], "rms_m") >= 0, "rms_m must be nonnegative")
    _require(set(manifest["files"]) == FILES, "manifest files must list all three documents")
    for name in FILES:
        _require(manifest["files"][name] == _hash(bundle / name), f"{name} hash mismatch")
    _require(manifest["content_sha256"] == content_hash(manifest), "content hash mismatch")
    tags_doc = read_document(bundle / "tags.yaml")
    zones_doc = read_document(bundle / "zones.yaml")
    obstacles_doc = read_document(bundle / "obstacles.yaml")
    for document in (tags_doc, zones_doc, obstacles_doc):
        _document_header(document)
    _require(tags_doc["frame"] == "building", "tag frame must be building")
    polygon, z_min, z_max = _volume(zones_doc["geofence"])
    graph = zones_doc["room_graph"]
    _require(isinstance(graph["nodes"], list), "graph nodes must be a list")
    _require(isinstance(graph["edges"], list), "graph edges must be a list")
    nodes = {}
    for node in graph["nodes"]:
        node_id = _text(node["id"], "node id")
        _require(node_id not in nodes, "duplicate graph node")
        _require(node["floor_id"] in floors, "graph node has unknown floor")
        nodes[node_id] = node["floor_id"]
    _require({"113", "mezzanine", "north_hallway"} <= nodes.keys(), "missing required graph nodes")
    _require(
        nodes["113"] == nodes["north_hallway"] and nodes["113"] != nodes["mezzanine"],
        "mezzanine and Level 1 must be separate graph floors",
    )
    edges = set()
    for edge in graph["edges"]:
        _require(edge["from"] in nodes and edge["to"] in nodes, "unknown graph edge endpoint")
        _require(type(edge["autonomous"]) is bool, "edge autonomous must be boolean")
        key = (edge["from"], edge["to"], edge["side"])
        _require(key not in edges, "duplicate graph edge")
        edges.add(key)
        if {"mezzanine", "north_hallway"} & {edge["from"], edge["to"]}:
            _require(edge["autonomous"] is False, "unaccepted transition cannot be autonomous")
    _require(
        {("113", "mezzanine", "west"), ("113", "north_hallway", "east")} <= edges,
        "missing corrected west/east graph edges",
    )
    for key in ("zones", "obstacles"):
        document = zones_doc if key == "zones" else obstacles_doc
        _require(isinstance(document[key], list), f"{key} must be a list")
        seen = set()
        for volume in document[key]:
            ident = _text(volume["id"], f"{key} id")
            _require(ident not in seen, f"duplicate {key} id")
            seen.add(ident)
            _require(volume["floor_id"] in floors, f"{key} has unknown floor")
            _volume(volume)
            if key == "zones":
                _require(type(volume["owner_approved"]) is bool, "owner_approved must be boolean")
        if key == "zones":
            _require(
                {"lobby", "kitchen", "atrium", "launch", "corridor"} <= seen,
                "missing required zones",
            )
    _require(isinstance(tags_doc["tags"], list), "tags must be a list")
    ids = set()
    for tag in tags_doc["tags"]:
        ident = tag["id"]
        _require(type(ident) is int and 0 <= ident <= 586, "tag36h11 id must be 0..586")
        _require(ident not in ids, "duplicate tag id")
        ids.add(ident)
        _require(tag["floor_id"] in nodes.values(), "tag has unknown graph floor")
        _require(finite_number(tag["size"], "tag size") > 0, "tag size must be positive")
        _require(tag["orientation_confirmed"] is True, "printed orientation must be confirmed")
        position = [finite_number(tag[axis], axis) for axis in ("x", "y", "z")]
        yaw = finite_number(tag["yaw"], "yaw")
        transform = validate_transform(tag["T_map_tag"])
        normal = tag["normal"]
        _require(isinstance(normal, list) and len(normal) == 3, "normal needs three coordinates")
        normal = [finite_number(item, "normal coordinate") for item in normal]
        _require(
            all(abs(position[i] - transform[i][3]) <= 1e-6 for i in range(3)),
            "tag position disagrees with transform",
        )
        _require(
            all(abs(normal[i] - transform[i][2]) <= 1e-6 for i in range(3)),
            "tag normal disagrees with transform",
        )
        _require(
            math.hypot(transform[0][0], transform[1][0]) > 1e-6,
            "tag x axis must have a horizontal projection for yaw",
        )
        transform_yaw = math.atan2(transform[1][0], transform[0][0])
        _require(
            abs(math.remainder(yaw - transform_yaw, 2 * math.pi)) <= 1e-6,
            "tag yaw disagrees with transform",
        )
        _require(
            _inside(polygon, position) and z_min <= position[2] <= z_max, "tag outside geofence"
        )
        if ident == 0:
            _require(all(abs(item) <= 1e-6 for item in position), "Tag 0 must be at origin")
            _require(
                abs(math.remainder(yaw - tag0_yaw, 2 * math.pi)) <= 1e-6,
                "Tag 0 yaw disagrees with frame",
            )
    _require(0 in ids, "missing origin Tag 0")
    return manifest


def validate_bundle(path, accepted_versions):
    """Return a valid manifest or raise ValueError; acceptance is an explicit version allowlist."""
    try:
        _require(
            not isinstance(accepted_versions, str) and bool(accepted_versions),
            "accepted_versions must be a nonempty collection",
        )
        return _validate_bundle(Path(path), accepted_versions)
    except (KeyError, TypeError, IndexError, OSError, OverflowError) as exc:
        raise ValueError(f"malformed or missing bundle data: {exc}") from exc


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--accepted-version", action="append", required=True)
    args = parser.parse_args()
    try:
        manifest = validate_bundle(args.bundle, args.accepted_version)
    except ValueError as exc:
        print(json.dumps({"valid": False, "error": str(exc)}))
        return 1
    print(
        json.dumps(
            {
                "valid": True,
                "bundle_version": manifest["bundle_version"],
                "content_sha256": manifest["content_sha256"],
                "flight_approved": False,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
