"""Generate offline grids from ASCII PLY XYZ clouds and explicit free-space evidence."""

import argparse
import hashlib
import json
import math
from pathlib import Path

import numpy as np

from tools.geometry_math import (
    inset_cell,
    polygon,
    polygon_cell_intersects,
    rect_inside_polygon,
    rect_polygon_distance,
    rect_segment_distance,
)
from tools.map_common import finite_number, read_document, transform_point, write_document
from tools.map_validate import validate_bundle

CELL_M = 0.10
HAZARD_MARGIN_M = 0.75
WALL_INSET_M = 1.0
BANDS_M = (0.8, 1.2, 1.6, 2.0, 2.4)


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _point(value, dimensions):
    _require(isinstance(value, list) and len(value) == dimensions, "invalid point dimensions")
    return [finite_number(v, "coordinate") for v in value]


def _volume(value):
    boundary = polygon(value["polygon"])
    low, high = (finite_number(value[k], k) for k in ("z_min", "z_max"))
    _require(low < high, "volume altitude bounds must increase")
    return {**value, "polygon": boundary, "z_min": low, "z_max": high}


def read_ply(path, transform):
    """Read ASCII PLY vertex XYZ into the building frame; unsupported layouts raise ValueError."""
    with Path(path).open(encoding="ascii") as stream:
        _require(stream.readline().strip() == "ply", "expected PLY header")
        _require(stream.readline().strip() == "format ascii 1.0", "only ASCII PLY 1.0 is supported")
        count, properties, element = None, [], None
        for _ in range(1000):
            line = stream.readline().strip()
            _require(bool(line), "truncated PLY header")
            words = line.split()
            if words[0] in ("comment", "obj_info"):
                continue
            if words[0] == "end_header":
                break
            if words[0] == "element":
                _require(len(words) == 3, "invalid PLY element")
                element = words[1]
                if element == "vertex":
                    _require(count is None, "duplicate PLY vertex element")
                    count = int(words[2])
                    _require(0 <= count <= 1_000_000, "PLY vertex count exceeds authoring limit")
                elif count is None:
                    raise ValueError("vertex must be the first PLY element")
            elif words[0] == "property" and element == "vertex":
                _require(len(words) == 3 and words[1] != "list", "unsupported vertex property")
                _require(words[2] not in properties, "duplicate vertex property")
                properties.append(words[2])
            elif words[0] != "property":
                raise ValueError("unsupported PLY header field")
        else:
            raise ValueError("PLY header exceeds authoring limit")
        _require(count is not None and {"x", "y", "z"} <= set(properties), "PLY needs vertex XYZ")
        indices = [properties.index(k) for k in ("x", "y", "z")]
        points = []
        for _ in range(count):
            fields = stream.readline().split()
            _require(len(fields) == len(properties), "truncated or malformed PLY vertex")
            point = [finite_number(float(fields[i]), "PLY coordinate") for i in indices]
            points.append(transform_point(transform, point))
        return points


def _rect(ix, iy, origin):
    x, y = origin[0] + ix * CELL_M, origin[1] + iy * CELL_M
    return (x, y, x + CELL_M, y + CELL_M)


def _overlap(low, high, other_low, other_high):
    return low <= other_high and high >= other_low


def _voxel(point):
    indices = [math.floor(v / CELL_M) for v in point]
    return tuple(v * CELL_M for v in indices)


def _blocked(rect, low, high, domain, obstacles, voxels):
    if not any(
        volume["eligible"]
        and volume["z_min"] <= low - HAZARD_MARGIN_M
        and high + HAZARD_MARGIN_M <= volume["z_max"]
        and rect_inside_polygon(
            (
                rect[0] - HAZARD_MARGIN_M,
                rect[1] - HAZARD_MARGIN_M,
                rect[2] + HAZARD_MARGIN_M,
                rect[3] + HAZARD_MARGIN_M,
            ),
            volume["polygon"],
        )
        for volume in domain
    ):
        return "unknown"
    for obstacle in obstacles:
        if _overlap(
            low, high, obstacle["z_min"] - HAZARD_MARGIN_M, obstacle["z_max"] + HAZARD_MARGIN_M
        ):
            if rect_polygon_distance(rect, obstacle["polygon"]) <= HAZARD_MARGIN_M:
                return "obstacle_or_no_fly"
    for x, y, z in voxels:
        if _overlap(low, high, z - HAZARD_MARGIN_M, z + CELL_M + HAZARD_MARGIN_M):
            dx = max(rect[0] - x - CELL_M, x - rect[2], 0)
            dy = max(rect[1] - y - CELL_M, y - rect[3], 0)
            if math.hypot(dx, dy) <= HAZARD_MARGIN_M + 1e-9:
                return "scan_voxel"
    return None


def _voxel_index(voxels, low, high):
    index = {}
    for x, y, z in voxels:
        if _overlap(low, high, z - HAZARD_MARGIN_M, z + CELL_M + HAZARD_MARGIN_M):
            index.setdefault((math.floor(x), math.floor(y)), []).append((x, y, z))
    return index


def _nearby_voxels(index, rect):
    for ix in range(
        math.floor(rect[0] - HAZARD_MARGIN_M - CELL_M), math.floor(rect[2] + HAZARD_MARGIN_M) + 1
    ):
        for iy in range(
            math.floor(rect[1] - HAZARD_MARGIN_M - CELL_M),
            math.floor(rect[3] + HAZARD_MARGIN_M) + 1,
        ):
            yield from index.get((ix, iy), ())


def _proximity(route, tags):
    samples = []
    for a, b in zip(route["centerline"], route["centerline"][1:], strict=False):
        count = max(1, math.ceil(math.dist(a, b) / CELL_M))
        for i in range(count + 1):
            point = [a[j] + (b[j] - a[j]) * i / count for j in range(2)]
            for z in (route["z_min"], route["z_max"]):
                distance = min(
                    (math.dist([*point, z], [tag["x"], tag["y"], tag["z"]]) for tag in tags),
                    default=None,
                )
                samples.append({"xyz": [*point, z], "nearest_tag_distance_m": distance})
    return {
        "status": "candidate_proximity_only",
        "visibility_verified": False,
        "sample_spacing_max_m": CELL_M,
        "radius_m": 2.5,
        "samples_outside_radius": sum(
            p["nearest_tag_distance_m"] is None or p["nearest_tag_distance_m"] > 2.5
            for p in samples
        ),
        "samples": samples,
    }


def generate(bundle, authoring, output, accepted_versions):
    """Write offline artifacts into a new directory; malformed input raises ValueError."""
    try:
        return _generate(Path(bundle), Path(authoring), Path(output), accepted_versions)
    except (KeyError, TypeError, IndexError, OverflowError, OSError) as exc:
        raise ValueError(f"invalid geometry input: {exc}") from exc


def _generate(bundle, authoring, output, accepted_versions):
    manifest = validate_bundle(bundle, accepted_versions)
    request = read_document(authoring)
    _require(
        type(request["schema_version"]) is int and request["schema_version"] == 1,
        "unsupported geometry schema",
    )
    _require(request["units"] == "meters", "geometry units must be meters")
    _require(request["bundle_content_sha256"] == manifest["content_sha256"], "stale geometry input")
    _require(request["evidence_kind"] in ("synthetic", "surveyed"), "unknown evidence kind")
    floor = request["floor_id"]
    _require(floor in manifest["floor_ids"], "unknown floor")
    _require(isinstance(floor, str) and floor.replace("_", "").isalnum(), "unsafe floor filename")
    floor_z = finite_number(request["floor_elevation_m"], "floor elevation")
    flight = _point(request["flight_box_xy"], 4)
    _require(flight[0] < flight[2] and flight[1] < flight[3], "invalid flight box")
    origin = flight[:2]
    width, height = (math.ceil((flight[i + 2] - flight[i]) / CELL_M) for i in range(2))
    _require(width * height <= 100_000, "grid exceeds offline authoring limit of 100000 cells")
    zones_doc = read_document(bundle / "zones.yaml")
    geofence = _volume(zones_doc["geofence"])
    walls = polygon(request["wall_boundary"])
    sources = {s["path"]: s for s in manifest["sources"]}
    wall_source = request["wall_source"]
    _require(wall_source in sources, "wall boundary must reference a pinned source")
    _require(
        isinstance(request["cloud_sources"], list) and bool(request["cloud_sources"]),
        "cloud_sources must be a nonempty list",
    )
    _require(
        len(set(request["cloud_sources"])) == len(request["cloud_sources"])
        and set(request["cloud_sources"]) == set(sources),
        "cloud_sources must include every pinned source scan exactly once",
    )
    points = []
    for source in request["cloud_sources"]:
        _require(source in sources, "cloud must reference a pinned source")
        points.extend(read_ply(bundle / source, sources[source]["T_map_scan"]))
    _require(bool(points), "source clouds contain no observed vertices")
    voxels = sorted(
        {
            _voxel(p)
            for p in points
            if flight[0] - HAZARD_MARGIN_M - CELL_M <= p[0] <= flight[2] + HAZARD_MARGIN_M + CELL_M
            and flight[1] - HAZARD_MARGIN_M - CELL_M <= p[1] <= flight[3] + HAZARD_MARGIN_M + CELL_M
        }
    )
    _require(isinstance(request["free_space"], list), "free_space must be a list")
    domain = []
    for item in request["free_space"]:
        volume = _volume(item)
        _require(item["source"] in sources, "free-space evidence must reference a pinned source")
        _require(
            type(item["observed"]) is bool and type(item["owner_approved"]) is bool,
            "free-space observation and approval must be explicit booleans",
        )
        volume["eligible"] = item["observed"] and (
            request["evidence_kind"] == "synthetic" or item["owner_approved"]
        )
        domain.append(volume)
    obstacles = [_volume(v) for v in read_document(bundle / "obstacles.yaml")["obstacles"]]
    _require(isinstance(request["no_fly"], list), "no_fly must be a list")
    obstacles.extend(_volume(v) for v in request["no_fly"])
    cells = [_rect(ix, iy, origin) for iy in range(height) for ix in range(width)]
    inside = [
        rect[2] <= flight[2] + 1e-9
        and rect[3] <= flight[3] + 1e-9
        and rect_inside_polygon(rect, geofence["polygon"])
        and inset_cell(rect, walls, WALL_INSET_M)
        for rect in cells
    ]

    def grid(low, high):
        bounds_ok = geofence["z_min"] <= low <= high <= geofence["z_max"]
        index = _voxel_index(voxels, low, high)
        return [
            (
                _blocked(rect, low, high, domain, obstacles, _nearby_voxels(index, rect))
                if ok and bounds_ok
                else "outside_inset_geofence"
            )
            for rect, ok in zip(cells, inside, strict=True)
        ]

    grids = {}
    for band in BANDS_M:
        reasons = grid(floor_z + band, floor_z + band)
        grids[f"grid_{floor}_{band:.1f}.npy"] = [
            [int(reason is not None) for reason in reasons[start : start + width]]
            for start in range(0, len(cells), width)
        ]
    route = request["route"]
    _require(
        isinstance(route["centerline"], list) and 2 <= len(route["centerline"]) <= 1000,
        "route requires 2 to 1000 points",
    )
    route = {**route, "centerline": [_point(p, 2) for p in route["centerline"]]}
    radius = finite_number(route["half_width_m"], "route half width")
    _require(radius > 0, "route half width must be positive")
    low, high = (finite_number(route[k], k) for k in ("z_min", "z_max"))
    _require(low < high, "route altitude bounds must increase")
    route_reasons = grid(low, high)
    segments = list(zip(route["centerline"], route["centerline"][1:], strict=False))
    _require(
        sum(math.dist(a, b) for a, b in segments) <= 1000,
        "route exceeds 1000 m authoring sample budget",
    )
    route_cells = [
        i
        for i, rect in enumerate(cells)
        if any(rect_segment_distance(rect, a, b) <= radius for a, b in segments)
    ]
    route_outside = any(
        p[0] - radius < flight[0]
        or p[1] - radius < flight[1]
        or p[0] + radius > flight[2]
        or p[1] + radius > flight[3]
        for p in route["centerline"]
    )
    route_report = {
        "geometry_clear": bool(route_cells)
        and not route_outside
        and all(route_reasons[i] is None for i in route_cells),
        "outside_grid": route_outside,
        "intersecting_cells": len(route_cells),
        "blocked_cells": sum(route_reasons[i] is not None for i in route_cells),
        "tube": route,
    }
    tags = [t for t in read_document(bundle / "tags.yaml")["tags"] if t["floor_id"] == floor]
    route_report["tag_proximity"] = _proximity(route, tags)
    _require(isinstance(request["formations"], list), "formations must be a list")
    formations, names = [], set()
    for item in request["formations"]:
        volume = _volume(item)
        name = item["id"]
        _require(
            isinstance(name, str) and name in ("kitchen", "atrium") and name not in names,
            "formation IDs must be unique kitchen and atrium",
        )
        names.add(name)
        reasons = grid(volume["z_min"], volume["z_max"])
        selected = [
            i for i, rect in enumerate(cells) if polygon_cell_intersects(volume["polygon"], rect)
        ]
        boundary = volume["polygon"]
        xs, ys = [p[0] for p in boundary[:-1]], [p[1] for p in boundary[:-1]]
        _require(
            len(boundary) == 5 and len(set(xs)) == len(set(ys)) == 2,
            "formation must be an axis-aligned rectangle",
        )
        separation = finite_number(item["separation_m"], "separation")
        envelope = sum(
            finite_number(item[k], k) for k in ("stopping_m", "p95_error_m", "drone_radius_m")
        )
        _require(
            separation > 0
            and all(item[k] >= 0 for k in ("stopping_m", "p95_error_m", "drone_radius_m")),
            "invalid formation envelope",
        )
        span_x, span_y = max(xs) - min(xs), max(ys) - min(ys)
        fits = (
            separation >= 2 * envelope
            and max(span_x, span_y) >= separation + 2 * envelope
            and min(span_x, span_y, volume["z_max"] - volume["z_min"]) >= 2 * envelope
        )
        outside = (
            min(xs) < flight[0] or min(ys) < flight[1] or max(xs) > flight[2] or max(ys) > flight[3]
        )
        zone = next(z for z in zones_doc["zones"] if z["id"] == name)
        in_named_zone = (
            zone["floor_id"] == floor
            and zone["z_min"] <= volume["z_min"] < volume["z_max"] <= zone["z_max"]
            and rect_inside_polygon((min(xs), min(ys), max(xs), max(ys)), zone["polygon"])
        )
        clear = (
            bool(selected)
            and not outside
            and in_named_zone
            and all(reasons[i] is None for i in selected)
        )
        formations.append(
            {
                "id": name,
                "geometry_clear": clear,
                "two_drone_static_fit": fits,
                "inside_named_zone": in_named_zone,
                "candidate": clear and fits,
                "owner_acceptance": "pending",
                "blocked_cells": sum(reasons[i] is not None for i in selected),
                "volume": volume,
            }
        )
    _require(names == {"kitchen", "atrium"}, "both kitchen and atrium formations required")
    common = {
        "schema_version": 1,
        "status": "offline_authoring",
        "flight_approved": False,
        "evidence_kind": request["evidence_kind"],
        "bundle_version": manifest["bundle_version"],
        "bundle_content_sha256": manifest["content_sha256"],
        "authoring_sha256": hashlib.sha256(authoring.read_bytes()).hexdigest(),
        "floor_id": floor,
        "floor_elevation_m": floor_z,
        "units": "meters",
        "cell_m": CELL_M,
        "origin_xy": origin,
        "shape_yx": [height, width],
        "row_direction": "+y",
        "column_direction": "+x",
        "blocked_value": 1,
        "candidate_value": 0,
        "hazard_margin_m": HAZARD_MARGIN_M,
        "wall_inset_m": WALL_INSET_M,
    }
    report = {
        **common,
        "bands_above_floor_m": BANDS_M,
        "route": route_report,
        "formations": formations,
    }
    atrium = next(f for f in formations if f["id"] == "atrium")
    report["atrium_recommendation"] = (
        "candidate_pending_measurements" if atrium["candidate"] else "use_kitchen_only_if_accepted"
    )
    _require(not output.exists(), "output directory already exists; use a new path")
    output.mkdir(parents=True)
    for name, rows in grids.items():
        np.save(output / name, np.asarray(rows, dtype=np.uint8), allow_pickle=False)
    write_document(
        output / f"geofence_{floor}.json",
        {
            **common,
            "source_wall_polygon": walls,
            "candidate_cells_xyxy": [rect for rect, ok in zip(cells, inside, strict=True) if ok],
            "z_min": geofence["z_min"],
            "z_max": geofence["z_max"],
        },
    )
    from tools.map_geometry_preview import write_preview

    write_preview(
        output / "preview.html",
        report,
        grids,
        points,
        tags,
        [rect for rect, ok in zip(cells, inside, strict=True) if ok],
    )
    report["files"] = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(output.iterdir())
    }
    write_document(output / "geometry.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("authoring", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--accepted-version", action="append", required=True)
    args = parser.parse_args()
    try:
        report = generate(args.bundle, args.authoring, args.output, args.accepted_version)
    except ValueError as exc:
        print(json.dumps({"valid": False, "error": str(exc)}))
        return 1
    print(
        json.dumps(
            {
                "valid": True,
                "status": report["status"],
                "flight_approved": False,
                "route_geometry_clear": report["route"]["geometry_clear"],
                "atrium_recommendation": report["atrium_recommendation"],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
