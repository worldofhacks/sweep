"""Validate offline survey bundles before downstream map processing."""

import argparse
import hashlib
import json
import math
import os
import tempfile
from datetime import datetime
from pathlib import Path

from tools.map_common import (
    bundle_document_path,
    finite_number,
    parse_document,
    read_bundle_bytes,
    read_document,
    source_path,
    validate_transform,
)

FILES = {"tags.yaml", "zones.yaml", "obstacles.yaml"}


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _text(value, name):
    _require(isinstance(value, str) and bool(value.strip()), f"{name} must be nonempty text")
    return value


def content_hash(manifest):
    payload = {key: value for key, value in manifest.items() if key != "content_sha256"}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _source_path(bundle, name):
    return source_path(bundle, name)


class ValidatedBundle(dict):
    def __init__(self, manifest, documents, sources):
        super().__init__(manifest)
        self._documents = dict(documents)
        self._sources = {Path(name).as_posix(): payload for name, payload in sources.items()}

    def document(self, name):
        """Return a parsed copy of the exact bytes validated at load, without reopening paths."""
        return parse_document(self._documents[name], name)

    def source_bytes(self, name):
        return self._sources[Path(name).as_posix()]


def seal_manifest(bundle):
    """Recompute and atomically replace hashes; this never creates external acceptance."""
    bundle = Path(bundle).resolve()
    manifest_path = bundle_document_path(bundle, "manifest.yaml")
    manifest = parse_document(read_bundle_bytes(bundle, "manifest.yaml"))
    for source in manifest["sources"]:
        source["sha256"] = hashlib.sha256(
            read_bundle_bytes(bundle, source["path"], source=True)
        ).hexdigest()
    manifest["files"] = {
        name: hashlib.sha256(read_bundle_bytes(bundle, name)).hexdigest() for name in sorted(FILES)
    }
    manifest["content_sha256"] = content_hash(manifest)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=bundle, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(json.dumps(manifest, indent=2, allow_nan=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, manifest_path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
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
    documents = {"manifest.yaml": read_bundle_bytes(bundle, "manifest.yaml")}
    manifest = parse_document(documents["manifest.yaml"], "manifest.yaml")
    _document_header(manifest)
    _text(manifest["bundle_version"], "bundle_version")
    _require(manifest["bundle_version"] in accepted_versions, "bundle version is not accepted")
    _require(
        accepted_versions[manifest["bundle_version"]] == manifest["content_sha256"],
        "accepted version content hash mismatch",
    )
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
    _require(abs(math.remainder(tag0_yaw, 2 * math.pi)) <= 1e-6, "Tag 0 yaw must be zero")
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
    sources = {}
    registrations = {}
    for source in manifest["sources"]:
        path = _source_path(bundle, source["path"])
        _require(path not in source_paths, "duplicate source path")
        source_paths.add(path)
        sources[source["path"]] = read_bundle_bytes(bundle, source["path"], source=True)
        _require(
            source["sha256"] == hashlib.sha256(sources[source["path"]]).hexdigest(),
            "source hash mismatch",
        )
        registrations[path] = source
        validate_transform(source["T_map_scan"])
        _require(finite_number(source["rms_m"], "rms_m") >= 0, "rms_m must be nonnegative")
    _require(set(manifest["files"]) == FILES, "manifest files must list all three documents")
    for name in FILES:
        documents[name] = read_bundle_bytes(bundle, name)
        _require(
            manifest["files"][name] == hashlib.sha256(documents[name]).hexdigest(),
            f"{name} hash mismatch",
        )
    _require(manifest["content_sha256"] == content_hash(manifest), "content hash mismatch")
    tags_doc = parse_document(documents["tags.yaml"], "tags.yaml")
    zones_doc = parse_document(documents["zones.yaml"], "zones.yaml")
    obstacles_doc = parse_document(documents["obstacles.yaml"], "obstacles.yaml")
    for document in (tags_doc, zones_doc, obstacles_doc):
        _document_header(document)
    _require(tags_doc["frame"] == "building", "tag frame must be building")
    tag_source = tags_doc["source"]
    registration = registrations.get(_source_path(bundle, tag_source["path"]))
    _require(registration is not None, "tag source is not registered")
    _require(tag_source["sha256"] == registration["sha256"], "tag source hash mismatch")
    tag_registration = validate_transform(tags_doc["T_map_scan"])
    _require(
        tag_registration == registration["T_map_scan"], "tag registration disagrees with source"
    )
    polygon, z_min, z_max = _volume(zones_doc["geofence"])
    graph = zones_doc["room_graph"]
    _require(isinstance(graph["nodes"], list), "graph nodes must be a list")
    _require(isinstance(graph["edges"], list), "graph edges must be a list")
    nodes = {}
    node_regions = {}
    node_autonomous = {}
    for node in graph["nodes"]:
        node_id = _text(node["id"], "node id")
        _require(node_id not in nodes, "duplicate graph node")
        _require(node["floor_id"] in floors, "graph node has unknown floor")
        nodes[node_id] = node["floor_id"]
        node_regions[node_id] = _text(node["region_id"], "region_id")
        _require(type(node["autonomous"]) is bool, "node autonomous must be boolean")
        node_autonomous[node_id] = node["autonomous"]
    _require({"113", "mezzanine", "north_hallway"} <= nodes.keys(), "missing required graph nodes")
    _require(
        nodes["113"] == nodes["north_hallway"] and nodes["113"] != nodes["mezzanine"],
        "mezzanine and Level 1 must be separate graph floors",
    )
    for node_id in ("mezzanine", "north_hallway"):
        _require(
            node_regions[node_id] == node_id and not node_autonomous[node_id],
            "excluded region cannot be autonomous or relabeled",
        )
    phase_one = {"lobby", "corridor", "kitchen", "atrium", "113", "launch"}
    for node_id in nodes:
        if node_autonomous[node_id]:
            _require(
                nodes[node_id] == nodes["113"] and node_regions[node_id] in phase_one,
                "unaccepted region or floor cannot be autonomous",
            )
    edges = set()
    for edge in graph["edges"]:
        _require(edge["from"] in nodes and edge["to"] in nodes, "unknown graph edge endpoint")
        _require(type(edge["autonomous"]) is bool, "edge autonomous must be boolean")
        key = (edge["from"], edge["to"], edge["side"])
        _require(key not in edges, "duplicate graph edge")
        edges.add(key)
        if edge["autonomous"]:
            _require(
                nodes[edge["from"]] == nodes[edge["to"]] == nodes["113"]
                and all(
                    node_autonomous[n] and node_regions[n] in phase_one
                    for n in (edge["from"], edge["to"])
                ),
                "unaccepted transition cannot be autonomous",
            )
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
        scan_pose = validate_transform(tag["T_scan_tag"])
        mapped_pose = [
            [sum(tag_registration[i][k] * scan_pose[k][j] for k in range(4)) for j in range(4)]
            for i in range(4)
        ]
        _require(
            all(
                abs(transform[i][j] - mapped_pose[i][j]) <= 1e-6 for i in range(4) for j in range(4)
            ),
            "tag pose disagrees with registered scan pose",
        )
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
            _require(tag["floor_id"] == nodes["113"], "Tag 0 must share the Phase-1 origin floor")
            _require(all(abs(item) <= 1e-6 for item in position), "Tag 0 must be at origin")
            _require(abs(math.remainder(yaw, 2 * math.pi)) <= 1e-6, "Tag 0 yaw must be zero")
            _require(
                all(abs(normal[i] - expected) <= 1e-6 for i, expected in enumerate([0, 0, 1])),
                "Tag 0 normal must be +z",
            )
            _require(
                abs(math.remainder(yaw - tag0_yaw, 2 * math.pi)) <= 1e-6,
                "Tag 0 yaw disagrees with frame",
            )
    _require(0 in ids, "missing origin Tag 0")
    return ValidatedBundle(manifest, documents, sources)


def validate_bundle(path, accepted_versions):
    """Return a validated byte snapshot; accepted_versions externally binds versions to SHA-256."""
    try:
        _require(
            isinstance(accepted_versions, dict)
            and bool(accepted_versions)
            and all(
                isinstance(k, str)
                and k
                and isinstance(v, str)
                and len(v) == 64
                and all(c in "0123456789abcdef" for c in v)
                for k, v in accepted_versions.items()
            ),
            "accepted_versions must be a nonempty version-to-content-sha256 mapping",
        )
        return _validate_bundle(Path(path), dict(accepted_versions))
    except (KeyError, TypeError, IndexError, OSError, OverflowError) as exc:
        raise ValueError(f"malformed or missing bundle data: {exc}") from exc


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--accepted-versions", type=Path, required=True)
    args = parser.parse_args()
    try:
        manifest = validate_bundle(args.bundle, read_document(args.accepted_versions))
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
