"""Generate offline grids from ASCII PLY XYZ clouds and explicit free-space evidence."""

import argparse
import hashlib
import io
import json
import math
import os
import stat
from pathlib import Path

import numpy as np

from tools.geometry_math import (
    distance_to_segment,
    inset_cell,
    point_inside,
    polygon,
    polygon_cell_intersects,
    rect_inside_polygon,
    rect_polygon_distance,
    rect_segment_distance,
)
from tools.map_common import (
    finite_number,
    parse_document,
    read_document,
    transform_point,
    write_document,
)
from tools.map_validate import validate_bundle
from tools.ohmni_world_registration import apply_transform

CELL_M = 0.10
HAZARD_MARGIN_M = 0.75
WALL_INSET_M = 1.0
BANDS_M = (0.8, 1.2, 1.6, 2.0, 2.4)
PREVIEW_POINT_LIMIT = 5_000
MAX_V2_GRID_CELLS = 100_000
MAX_V2_PLANES = 64
MAX_V2_ROUTES = 64
MAX_V2_FORMATIONS = 64
MAX_V2_SAMPLES = 100_000
MAX_V2_EVIDENCE_BYTES = 1_000_000


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


def _parse_ply(payload, transform):
    """Parse one immutable ASCII PLY byte snapshot."""
    try:
        text = payload.decode("ascii")
    except (AttributeError, UnicodeDecodeError) as exc:
        raise ValueError("PLY payload must be ASCII bytes") from exc
    with io.StringIO(text) as stream:
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
                _require(element == "vertex", "only a vertex PLY element is supported")
                _require(count is None, "duplicate PLY vertex element")
                count = int(words[2])
                _require(0 <= count <= 1_000_000, "PLY vertex count exceeds authoring limit")
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
        _require(not stream.read().strip(), "unexpected trailing PLY payload")
        return points


def read_ply(path, transform):
    """Read one ASCII PLY snapshot; unsupported or ambiguous layouts raise ValueError."""
    return _parse_ply(Path(path).read_bytes(), transform)


def _preview_sample(points):
    """Return a deterministic bounded sample that includes both ends of the source order."""
    if len(points) <= PREVIEW_POINT_LIMIT:
        return list(points)
    return [
        points[index * (len(points) - 1) // (PREVIEW_POINT_LIMIT - 1)]
        for index in range(PREVIEW_POINT_LIMIT)
    ]


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


def _generate_v1(bundle, authoring, output, accepted_versions):
    manifest = validate_bundle(bundle, accepted_versions)
    authoring_payload = authoring.read_bytes()
    request = parse_document(authoring_payload, str(authoring))
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
    zones_doc = manifest.document("zones.yaml")
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
        points.extend(_parse_ply(manifest.source_bytes(source), sources[source]["T_map_scan"]))
    _require(bool(points), "source clouds contain no observed vertices")
    relevant_points = [
        point
        for point in points
        if flight[0] - HAZARD_MARGIN_M - CELL_M <= point[0] <= flight[2] + HAZARD_MARGIN_M + CELL_M
        and flight[1] - HAZARD_MARGIN_M - CELL_M <= point[1] <= flight[3] + HAZARD_MARGIN_M + CELL_M
    ]
    voxels = sorted({_voxel(point) for point in relevant_points})
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
    obstacles = [_volume(v) for v in manifest.document("obstacles.yaml")["obstacles"]]
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
    route_length = sum(math.dist(a, b) for a, b in segments)
    _require(route_length > 0, "route total length must be positive")
    _require(
        route_length <= 1000,
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
    tags = [t for t in manifest.document("tags.yaml")["tags"] if t["floor_id"] == floor]
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
        stopping = finite_number(item["stopping_m"], "stopping_m")
        p95_error = finite_number(item["p95_error_m"], "p95_error_m")
        drone_radius = finite_number(item["drone_radius_m"], "drone_radius_m")
        envelope = stopping + p95_error + drone_radius
        _require(
            separation > 0 and stopping >= 0 and p95_error >= 0 and drone_radius > 0,
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
        "authoring_sha256": hashlib.sha256(authoring_payload).hexdigest(),
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
        "source_point_count": len(points),
        "geometry_point_count": len(relevant_points),
        "preview_point_limit": PREVIEW_POINT_LIMIT,
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

    preview_points = _preview_sample(relevant_points)
    report["preview_point_count"] = len(preview_points)
    write_preview(
        output / "preview.html",
        report,
        grids,
        preview_points,
        tags,
        [rect for rect, ok in zip(cells, inside, strict=True) if ok],
    )
    report["files"] = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(output.iterdir())
    }
    write_document(output / "geometry.json", report)
    return report


def _v2_text(value, name):
    _require(isinstance(value, str) and value and value == value.strip(), f"{name} must be text")
    return value


def _v2_sha256(value, name):
    _require(
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value),
        f"{name} must be a SHA-256 digest",
    )
    return value


def _v2_volume(value, name):
    _require(isinstance(value, dict), f"{name} must be an object")
    boundary = polygon(value["polygon"])
    low = finite_number(value["z_min_m"], f"{name} z_min_m")
    high = finite_number(value["z_max_m"], f"{name} z_max_m")
    _require(low < high, f"{name} altitude bounds must increase")
    return {"polygon": boundary, "z_min_m": low, "z_max_m": high}


def _v2_positive(value, name):
    number = finite_number(value, name)
    _require(number > 0, f"{name} must be positive")
    return number


def _v2_direct_child(root, name, limit, label):
    path = Path(name)
    _require(path.name == name and not path.is_absolute(), f"{label} path must be a direct child")
    candidate = root / path
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(candidate, flags)
    except OSError as exc:
        raise ValueError(f"{label} must be a regular file") from exc
    try:
        info = os.fstat(descriptor)
        _require(stat.S_ISREG(info.st_mode), f"{label} must be a regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            payload = handle.read(limit + 1)
    finally:
        os.close(descriptor)
    _require(len(payload) <= limit, f"{label} exceeds its byte limit")
    return payload


def _v2_authoring_evidence(authoring, item, label):
    _require(
        isinstance(item, dict) and set(item) == {"path", "sha256"}, f"{label} pin is malformed"
    )
    path = _v2_text(item["path"], f"{label} path")
    digest = _v2_sha256(item["sha256"], f"{label} hash")
    payload = _v2_direct_child(authoring.parent, path, MAX_V2_EVIDENCE_BYTES, label)
    _require(hashlib.sha256(payload).hexdigest() == digest, f"{label} hash mismatch")
    return parse_document(payload, path), {"path": path, "sha256": digest}


def _v2_plane_list(value):
    _require(
        isinstance(value, list) and 1 <= len(value) <= MAX_V2_PLANES, "altitude planes are bounded"
    )
    planes = [finite_number(item, "altitude plane") for item in value]
    _require(planes == sorted(set(planes)), "altitude planes must be unique and increasing")
    return planes


def _v2_cell_domain(rect, low, high, corridors, free_volumes):
    corners = ((rect[0], rect[1]), (rect[2], rect[1]), (rect[2], rect[3]), (rect[0], rect[3]))
    for corridor in corridors:
        if (
            corridor["z_min_m"] <= low
            and high <= corridor["z_max_m"]
            and any(
                all(
                    distance_to_segment(corner, start, end) <= corridor["width_m"] / 2
                    for corner in corners
                )
                and high <= evidence["maximum_flight_height_m"]
                for start, end, evidence in zip(
                    corridor["centerline"][:-1],
                    corridor["centerline"][1:],
                    corridor["height_evidence"],
                    strict=True,
                )
            )
        ):
            return "corridor"
    for volume in free_volumes:
        if (
            volume["z_min_m"] <= low
            and high <= volume["z_max_m"]
            and high <= volume["maximum_flight_height_m"]
            and rect_inside_polygon(rect, volume["polygon"])
        ):
            return volume["id"]
    return None


def _v2_blocked(rect, low, high, geofence, hazards, clearance):
    if not (
        geofence["z_min_m"] <= low <= high <= geofence["z_max_m"]
        and rect_inside_polygon(rect, geofence["polygon"])
    ):
        return "outside_geofence"
    for hazard in hazards:
        if _overlap(low, high, hazard["z_min_m"] - clearance, hazard["z_max_m"] + clearance) and (
            rect_polygon_distance(rect, hazard["polygon"]) <= clearance
        ):
            return "static_hazard"
    return None


def _v2_route_cells(cells, route):
    segments = list(zip(route["centerline"], route["centerline"][1:], strict=False))
    return [
        index
        for index, rect in enumerate(cells)
        if any(
            rect_segment_distance(rect, start, end) <= route["half_width_m"]
            for start, end in segments
        )
    ]


def _v2_formation_fit(volume, separation, envelope):
    boundary = volume["polygon"]
    xs, ys = [point[0] for point in boundary[:-1]], [point[1] for point in boundary[:-1]]
    _require(
        len(boundary) == 5 and len(set(xs)) == len(set(ys)) == 2,
        "formation must be an axis-aligned rectangle",
    )
    span_x, span_y = max(xs) - min(xs), max(ys) - min(ys)
    return (
        separation >= 2 * envelope
        and max(span_x, span_y) >= separation + 2 * envelope
        and min(span_x, span_y, volume["z_max_m"] - volume["z_min_m"]) >= 2 * envelope
    )


def _v2_route_samples(route):
    samples = []
    width = route["half_width_m"]
    for start, end in zip(route["centerline"], route["centerline"][1:], strict=False):
        distance = math.dist(start, end)
        count = max(1, math.ceil(distance / CELL_M))
        dx, dy = end[0] - start[0], end[1] - start[1]
        length = math.hypot(dx, dy)
        lateral = (-dy / length * width, dx / length * width)
        for index in range(count + 1):
            x = start[0] + dx * index / count
            y = start[1] + dy * index / count
            for offset in ((0.0, 0.0), lateral, (-lateral[0], -lateral[1])):
                for z in (route["z_min_m"], route["z_max_m"]):
                    samples.append((x + offset[0], y + offset[1], z))
    _require(len(samples) <= MAX_V2_SAMPLES, "route visibility sampling exceeds the bound")
    return samples


def _v2_direction(heading, forward):
    cosine, sine = math.cos(heading), math.sin(heading)
    return (
        cosine * forward[0] - sine * forward[1],
        sine * forward[0] + cosine * forward[1],
        forward[2],
    )


def _v2_occluded(camera, tag, hazards):
    distance = math.dist(camera, tag)
    count = max(1, math.ceil(distance / 0.05))
    for index in range(1, count):
        ratio = index / count
        point = tuple(camera[axis] + (tag[axis] - camera[axis]) * ratio for axis in range(3))
        if any(
            hazard["z_min_m"] <= point[2] <= hazard["z_max_m"]
            and point_inside(hazard["polygon"], point[:2])
            for hazard in hazards
        ):
            return True
    return False


def _v2_visible(camera, heading, tag, model, hazards):
    target = (tag["x_m"], tag["y_m"], tag["z_m"])
    vector = tuple(target[axis] - camera[axis] for axis in range(3))
    distance = math.dist(camera, target)
    if not model["min_range_m"] <= distance <= model["max_range_m"]:
        return False
    direction = _v2_direction(heading, model["forward_body"])
    forward = sum(vector[axis] * direction[axis] for axis in range(3)) / distance
    if forward < math.cos(model["fov_rad"] / 2):
        return False
    ray_from_tag = tuple(-vector[axis] / distance for axis in range(3))
    if (
        sum(tag["normal"][axis] * ray_from_tag[axis] for axis in range(3))
        < model["minimum_face_dot"]
    ):
        return False
    return not _v2_occluded(camera, target, hazards)


def _v2_visibility(route, tags, model, hazards):
    samples = _v2_route_samples(route)
    uncovered = [
        sample
        for sample in samples
        if not any(_v2_visible(sample, route["heading_rad"], tag, model, hazards) for tag in tags)
    ]
    return {
        "status": "measured_camera_envelope",
        "sample_spacing_max_m": CELL_M,
        "sample_count": len(samples),
        "uncovered_sample_count": len(uncovered),
        "covered": not uncovered,
        "uncovered_samples_xyz": [list(sample) for sample in uncovered],
        "camera_model_id": model["id"],
        "calibration": model["calibration"],
    }


def _v2_checkpoint_report(validated, checkpoints):
    registration = validated.registration()
    observed = parse_document(
        validated.source_bytes(validated["registration"]["observed_tags"]["path"]), "observed tags"
    )
    known = parse_document(
        validated.source_bytes(validated["registration"]["known_tags"]["path"]), "known tags"
    )
    _require(
        observed["scope"] == {key: registration["source"][key] for key in observed["scope"]},
        "observed checkpoint scope disagrees with registration",
    )
    local = {item["tag_id"]: item["xy_m"] for item in observed["tags"]}
    world = {item["tag_id"]: item["xy_m"] for item in known["tags"]}
    tags = {item["id"]: item for item in validated.document("tags.yaml")["tags"]}
    fit_ids = set(registration["fit_tag_ids"])
    reports = []
    for item in checkpoints:
        _require(
            isinstance(item, dict) and set(item) == {"id", "tag_id", "maximum_error_m"},
            "checkpoint schema is invalid",
        )
        ident = _v2_text(item["id"], "checkpoint id")
        tag_id = item["tag_id"]
        bound = _v2_positive(item["maximum_error_m"], "checkpoint maximum_error_m")
        _require(bound <= 0.10, "checkpoint maximum error must be at most 0.10 m")
        _require(
            type(tag_id) is int and tag_id in local and tag_id in world and tag_id in tags,
            "checkpoint tag is unknown",
        )
        _require(tag_id not in fit_ids, "checkpoint tag cannot be a registration fit tag")
        tag = tags[tag_id]
        _require(
            tag["verified_for_flight"] is True, "checkpoint tag needs independent tape verification"
        )
        registered = apply_transform(registration["T_target_source"], local[tag_id])
        measured = (world[tag_id][0], world[tag_id][1], tag["z_m"])
        error = math.dist((*registered, tag["z_m"]), measured)
        reports.append(
            {
                "id": ident,
                "tag_id": tag_id,
                "local_xy_m": local[tag_id],
                "source_scope": observed["scope"],
                "registered_world_xyz_m": [*registered, tag["z_m"]],
                "independent_tape_world_xyz_m": list(measured),
                "maximum_error_m": bound,
                "error_m": error,
                "passes": error <= bound,
            }
        )
    _require(
        reports and all(item["passes"] for item in reports),
        "held-out checkpoints exceed the map bound",
    )
    return reports


def _generate_v2(bundle, authoring, output, accepted_versions):
    validated = validate_bundle(bundle, accepted_versions)
    _require(validated.get("schema_version") == 2, "geometry schema version 2 needs a world bundle")
    payload = authoring.read_bytes()
    request = parse_document(payload, str(authoring))
    _require(
        set(request)
        == {
            "schema_version",
            "units",
            "frame",
            "floor_id",
            "bundle_content_sha256",
            "evidence_kind",
            "flight_box_xy",
            "cell_m",
            "altitude_planes_m",
            "clearance",
            "routes",
            "formations",
            "free_volumes",
            "held_out_checkpoints",
            "camera_models",
        },
        "geometry schema version 2 is not exact",
    )
    _require(
        request["schema_version"] == 2
        and request["units"] == "meters"
        and request["frame"] == "world",
        "geometry v2 uses metres in world frame",
    )
    floor_id = _v2_text(request["floor_id"], "floor id")
    _require(
        request["bundle_content_sha256"] == validated["content_sha256"], "stale geometry input"
    )
    _require(
        request["evidence_kind"] in {"synthetic", "measured"},
        "geometry v2 evidence kind is invalid",
    )
    flight = _point(request["flight_box_xy"], 4)
    _require(flight[0] < flight[2] and flight[1] < flight[3], "invalid flight box")
    cell_m = _v2_positive(request["cell_m"], "cell_m")
    width, height = (math.ceil((flight[index + 2] - flight[index]) / cell_m) for index in range(2))
    _require(width * height <= MAX_V2_GRID_CELLS, "grid exceeds offline authoring limit")
    clearance = request["clearance"]
    _require(
        isinstance(clearance, dict)
        and set(clearance) == {"aircraft_radius_m", "uncertainty_m", "stopping_m"},
        "clearance schema is invalid",
    )
    clearance_values = {
        name: finite_number(value, f"clearance {name}") for name, value in clearance.items()
    }
    _require(
        clearance_values["aircraft_radius_m"] > 0
        and all(value >= 0 for value in clearance_values.values()),
        "clearance must have a positive aircraft radius and nonnegative allowances",
    )
    clearance_m = sum(clearance_values.values())
    zones = validated.document("zones.yaml")
    geofence = _v2_volume(zones["geofence"], "geofence")
    corridor_by_id = {item["id"]: item for item in zones["corridors"]}
    hazards = [
        _v2_volume(item, "static hazard")
        for key in ("obstacles", "no_fly")
        for item in validated.document("obstacles.yaml")[key]
    ]
    free_volumes, free_by_id = [], {}
    _require(
        isinstance(request["free_volumes"], list)
        and len(request["free_volumes"]) <= MAX_V2_FORMATIONS,
        "free volumes are bounded",
    )
    for item in request["free_volumes"]:
        _require(
            isinstance(item, dict)
            and set(item) == {"id", "polygon", "z_min_m", "z_max_m", "height_evidence"},
            "free volume schema is invalid",
        )
        volume = _v2_volume(item, "free volume")
        ident = _v2_text(item["id"], "free volume id")
        _require(
            ident not in free_by_id
            and rect_inside_polygon(
                (
                    min(point[0] for point in volume["polygon"][:-1]),
                    min(point[1] for point in volume["polygon"][:-1]),
                    max(point[0] for point in volume["polygon"][:-1]),
                    max(point[1] for point in volume["polygon"][:-1]),
                ),
                geofence["polygon"],
            ),
            "free volume must be inside the geofence",
        )
        evidence, pin = _v2_authoring_evidence(
            authoring, item["height_evidence"], "free-volume height evidence"
        )
        _require(
            evidence.get("kind") == "manual_corridor_clearance",
            "free-volume height evidence must be hand measured",
        )
        maximum = _v2_positive(
            evidence.get("maximum_flight_height_m"), "free-volume maximum height"
        )
        _require(
            volume["z_max_m"]
            <= maximum
            <= _v2_positive(evidence.get("measured_clearance_m"), "free-volume clearance"),
            "free-volume height evidence does not cover the volume",
        )
        volume.update(id=ident, maximum_flight_height_m=maximum, height_evidence=pin)
        free_volumes.append(volume)
        free_by_id[ident] = volume
    corridors = []
    for item in corridor_by_id.values():
        corridors.append(
            {
                **item,
                "z_min_m": item["z_min_m"],
                "z_max_m": item["z_max_m"],
                "height_evidence": item["height_evidence"],
            }
        )
    planes = _v2_plane_list(request["altitude_planes_m"])
    origin = flight[:2]
    cells = [
        (
            origin[0] + x * cell_m,
            origin[1] + y * cell_m,
            origin[0] + (x + 1) * cell_m,
            origin[1] + (y + 1) * cell_m,
        )
        for y in range(height)
        for x in range(width)
    ]
    grids = {}
    for index, z in enumerate(planes):
        rows = []
        for start in range(0, len(cells), width):
            rows.append(
                [
                    int(
                        _v2_blocked(rect, z, z, geofence, hazards, clearance_m) is not None
                        or _v2_cell_domain(rect, z, z, corridors, free_volumes) is None
                    )
                    for rect in cells[start : start + width]
                ]
            )
        grids[f"grid_world_{index:03d}.npy"] = rows
    models = {}
    _require(
        isinstance(request["camera_models"], list) and request["camera_models"],
        "camera models are required",
    )
    for item in request["camera_models"]:
        _require(
            isinstance(item, dict)
            and set(item)
            == {
                "id",
                "forward_body",
                "fov_rad",
                "min_range_m",
                "max_range_m",
                "minimum_face_dot",
                "calibration",
            },
            "camera model schema is invalid",
        )
        ident = _v2_text(item["id"], "camera model id")
        forward = _point(item["forward_body"], 3)
        norm = math.sqrt(sum(value * value for value in forward))
        _require(abs(norm - 1) <= 1e-6, "camera forward_body must be unit length")
        model = {
            **item,
            "id": ident,
            "forward_body": forward,
            "fov_rad": _v2_positive(item["fov_rad"], "camera fov"),
            "min_range_m": _v2_positive(item["min_range_m"], "camera min range"),
            "max_range_m": _v2_positive(item["max_range_m"], "camera max range"),
            "minimum_face_dot": finite_number(item["minimum_face_dot"], "camera minimum face dot"),
        }
        _require(
            model["fov_rad"] <= math.pi
            and model["min_range_m"] < model["max_range_m"]
            and 0 <= model["minimum_face_dot"] <= 1
            and ident not in models,
            "camera model values are invalid",
        )
        calibration, pin = _v2_authoring_evidence(
            authoring, item["calibration"], "camera calibration"
        )
        _require(
            calibration
            == {
                "schema_version": 1,
                "kind": "camera_visibility_envelope",
                "camera_model_id": ident,
                "forward_body": forward,
                "fov_rad": model["fov_rad"],
                "min_range_m": model["min_range_m"],
                "max_range_m": model["max_range_m"],
                "minimum_face_dot": model["minimum_face_dot"],
            },
            "camera calibration does not bind the visibility envelope",
        )
        model["calibration"] = pin
        models[ident] = model
    tags = validated.document("tags.yaml")["tags"]
    routes = []
    _require(
        isinstance(request["routes"], list) and 1 <= len(request["routes"]) <= MAX_V2_ROUTES,
        "routes are bounded",
    )
    for item in request["routes"]:
        _require(
            isinstance(item, dict)
            and set(item)
            == {
                "id",
                "corridor_ids",
                "centerline",
                "half_width_m",
                "z_min_m",
                "z_max_m",
                "heading_rad",
                "camera_model_id",
            },
            "route schema is invalid",
        )
        route = {
            **item,
            "id": _v2_text(item["id"], "route id"),
            "centerline": [_point(point, 2) for point in item["centerline"]],
            "half_width_m": _v2_positive(item["half_width_m"], "route half width"),
            "z_min_m": finite_number(item["z_min_m"], "route z_min"),
            "z_max_m": finite_number(item["z_max_m"], "route z_max"),
            "heading_rad": finite_number(item["heading_rad"], "route heading"),
        }
        _require(
            2 <= len(route["centerline"]) <= 256
            and route["z_min_m"] < route["z_max_m"]
            and all(
                start != end
                for start, end in zip(route["centerline"], route["centerline"][1:], strict=False)
            ),
            "route bounds are invalid",
        )
        ids = item["corridor_ids"]
        _require(
            isinstance(ids, list)
            and ids
            and len(set(ids)) == len(ids)
            and set(ids) <= set(corridor_by_id),
            "route corridors are invalid",
        )
        route["corridor_ids"] = ids
        _require(
            all(corridor_by_id[ident]["floor_id"] == floor_id for ident in ids),
            "route corridor is on another floor",
        )
        _require(item["camera_model_id"] in models, "route camera model is unknown")
        route_cells = _v2_route_cells(cells, route)
        selected_corridors = [corridor_by_id[ident] for ident in ids]
        clear = bool(route_cells) and all(
            _v2_blocked(
                cells[index], route["z_min_m"], route["z_max_m"], geofence, hazards, clearance_m
            )
            is None
            and _v2_cell_domain(
                cells[index], route["z_min_m"], route["z_max_m"], selected_corridors, ()
            )
            == "corridor"
            for index in route_cells
        )
        route["geometry_clear"] = clear
        route["intersecting_cells"] = len(route_cells)
        route["tag_coverage"] = _v2_visibility(
            route, tags, models[item["camera_model_id"]], hazards
        )
        routes.append(route)
    formations = []
    _require(
        isinstance(request["formations"], list) and len(request["formations"]) <= MAX_V2_FORMATIONS,
        "formations are bounded",
    )
    for item in request["formations"]:
        _require(
            isinstance(item, dict)
            and set(item)
            == {"id", "polygon", "z_min_m", "z_max_m", "free_volume_ids", "separation_m"},
            "formation schema is invalid",
        )
        volume = _v2_volume(item, "formation")
        ids = item["free_volume_ids"]
        _require(
            isinstance(ids, list)
            and ids
            and len(set(ids)) == len(ids)
            and set(ids) <= set(free_by_id),
            "formation free volumes are invalid",
        )
        selected = [
            index
            for index, rect in enumerate(cells)
            if polygon_cell_intersects(volume["polygon"], rect)
        ]
        separation = _v2_positive(item["separation_m"], "formation separation")
        static_fit = _v2_formation_fit(volume, separation, clearance_m)
        clear = bool(selected) and all(
            _v2_blocked(
                cells[index], volume["z_min_m"], volume["z_max_m"], geofence, hazards, clearance_m
            )
            is None
            and _v2_cell_domain(
                cells[index],
                volume["z_min_m"],
                volume["z_max_m"],
                (),
                [free_by_id[ident] for ident in ids],
            )
            is not None
            for index in selected
        )
        formations.append(
            {
                "id": _v2_text(item["id"], "formation id"),
                "volume": volume,
                "free_volume_ids": ids,
                "separation_m": separation,
                "aircraft_envelope_m": clearance_m,
                "two_aircraft_static_fit": static_fit,
                "geometry_clear": clear,
                "candidate": clear and static_fit,
                "blocked_cells": len(selected)
                - sum(
                    _v2_blocked(
                        cells[index],
                        volume["z_min_m"],
                        volume["z_max_m"],
                        geofence,
                        hazards,
                        clearance_m,
                    )
                    is None
                    for index in selected
                ),
            }
        )
    checkpoints = _v2_checkpoint_report(validated, request["held_out_checkpoints"])
    _require(not output.exists(), "output directory already exists; use a new path")
    output.mkdir(parents=True)
    for name, rows in grids.items():
        np.save(output / name, np.asarray(rows, dtype=np.uint8), allow_pickle=False)
    report = {
        "schema_version": 2,
        "status": "offline_authoring",
        "flight_approved": False,
        "evidence_kind": request["evidence_kind"],
        "frame": "world",
        "floor_id": floor_id,
        "bundle_version": validated["bundle_version"],
        "bundle_content_sha256": validated["content_sha256"],
        "authoring_sha256": hashlib.sha256(payload).hexdigest(),
        "units": "meters",
        "cell_m": cell_m,
        "origin_xy": origin,
        "shape_yx": [height, width],
        "blocked_value": 1,
        "candidate_value": 0,
        "altitude_planes_m": planes,
        "clearance": clearance_values,
        "clearance_m": clearance_m,
        "routes": routes,
        "formations": formations,
        "held_out_checkpoints": checkpoints,
        "free_volumes": free_volumes,
        "grid_files": list(grids),
        "static_geometry_only": True,
    }
    preview_report = {
        "origin_xy": origin,
        "shape_yx": [height, width],
        "cell_m": cell_m,
        "floor_elevation_m": 0,
        "evidence_kind": request["evidence_kind"],
        "route": {
            "tube": {
                "centerline": routes[0]["centerline"],
                "half_width_m": routes[0]["half_width_m"],
                "z_min": routes[0]["z_min_m"],
                "z_max": routes[0]["z_max_m"],
            },
            "geometry_clear": routes[0]["geometry_clear"],
        },
        "formations": [
            {
                "id": item["id"],
                "volume": {
                    "polygon": item["volume"]["polygon"],
                    "z_min": item["volume"]["z_min_m"],
                    "z_max": item["volume"]["z_max_m"],
                },
                "candidate": item["candidate"],
            }
            for item in formations
        ],
    }
    from tools.map_geometry_preview import write_preview

    normalized_tags = [
        {
            "id": item["id"],
            "x": item["x_m"],
            "y": item["y_m"],
            "z": item["z_m"],
            "T_map_tag": item["T_world_tag"],
        }
        for item in tags
    ]
    write_preview(output / "preview.html", preview_report, grids, [], normalized_tags, [])
    report["files"] = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(output.iterdir())
    }
    write_document(output / "geometry.json", report)
    return report


def _generate(bundle, authoring, output, accepted_versions):
    request = parse_document(Path(authoring).read_bytes(), str(authoring))
    version = request.get("schema_version") if isinstance(request, dict) else None
    if version == 1:
        return _generate_v1(bundle, authoring, output, accepted_versions)
    if version == 2:
        return _generate_v2(bundle, authoring, output, accepted_versions)
    raise ValueError("unsupported geometry schema")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("authoring", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--accepted-versions", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = generate(
            args.bundle, args.authoring, args.output, read_document(args.accepted_versions)
        )
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
