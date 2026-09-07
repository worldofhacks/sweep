"""Convert a bounded operator measurement worksheet into world-map evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import stat
from pathlib import Path

from tools.geometry_math import point_inside, polygon
from tools.map_common import finite_number, parse_document, write_document

MAX_INPUT_BYTES = 1_000_000
MAX_OUTPUT_FILES = 128
MAX_COORDINATE_M = 1_000_000
MAX_TAGS = 128
MAX_TAPE_CHECKS = 48
MAX_POLYGON_POINTS = 128
MAX_IDENTIFIER_CHARS = 128
METERS_PER_FOOT = 0.3048
METERS_PER_INCH = 0.0254


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _text(value, name, maximum=MAX_IDENTIFIER_CHARS):
    _require(
        type(value) is str
        and bool(value)
        and value == value.strip()
        and value.isprintable()
        and len(value) <= maximum,
        f"{name} must be canonical printable text",
    )
    return value


def _number(value, name, *, lower=None, upper=None):
    value = finite_number(value, name)
    _require(abs(value) <= MAX_COORDINATE_M, f"{name} exceeds metric bounds")
    if lower is not None:
        _require(value >= lower, f"{name} must be at least {lower:g}")
    if upper is not None:
        _require(value <= upper, f"{name} must be at most {upper:g}")
    return value


def _measure(value, name, *, positive=False, allow_zero=True):
    _require(
        isinstance(value, dict) and set(value) <= {"feet", "inches", "sign"},
        f"{name} must use feet and inches",
    )
    _require("feet" in value and "inches" in value, f"{name} must use feet and inches")
    feet = value["feet"]
    _require(
        type(feet) is int and 0 <= feet <= 3_280_839,
        f"{name}.feet must be a bounded nonnegative integer",
    )
    inches = _number(value["inches"], f"{name}.inches", lower=0, upper=11.999999)
    sign = value.get("sign", 1)
    _require(type(sign) is int and sign in {-1, 1}, f"{name}.sign must be -1 or 1")
    meters = sign * (feet * METERS_PER_FOOT + inches * METERS_PER_INCH)
    if positive:
        _require(meters > 0, f"{name} must be positive")
    elif not allow_zero:
        _require(meters != 0, f"{name} must be nonzero")
    return meters


def _xy(value, name):
    _require(isinstance(value, dict) and set(value) == {"x", "y"}, f"{name} requires x and y")
    return (_measure(value["x"], f"{name}.x"), _measure(value["y"], f"{name}.y"))


def _axis(value, name):
    _require(value in {"+x", "-x", "+y", "-y"}, f"{name} must be one of +x, -x, +y, -y")
    return {"+x": (1.0, 0.0), "-x": (-1.0, 0.0), "+y": (0.0, 1.0), "-y": (0.0, -1.0)}[value]


def _axis_name(value, name):
    _axis(value, name)
    return value


def _add(left, right):
    return (left[0] + right[0], left[1] + right[1])


def _scale(vector, scale):
    return (vector[0] * scale, vector[1] * scale)


def _distance(left, right):
    return math.hypot(left[0] - right[0], left[1] - right[1])


def _transform(x_axis, y_axis, normal, translation):
    return [
        [x_axis[0], y_axis[0], normal[0], translation[0]],
        [x_axis[1], y_axis[1], normal[1], translation[1]],
        [x_axis[2], y_axis[2], normal[2], translation[2]],
        [0, 0, 0, 1],
    ]


def _tag(tag_id, floor_id, size, point, x_axis, y_axis, normal, source, tape=None):
    yaw = math.atan2(x_axis[1], x_axis[0])
    return {
        "id": tag_id,
        "family": "tag36h11",
        "floor_id": floor_id,
        "size_m": size,
        "x_m": point[0],
        "y_m": point[1],
        "z_m": point[2],
        "yaw_rad": yaw,
        "normal": list(normal),
        "T_world_tag": _transform(x_axis, y_axis, normal, point),
        "source": source,
        "confidence": 0.8,
        "observation_refs": ["worksheet-v1"],
        "verified_for_flight": tape is not None,
        "tape_verification": tape,
    }


def _read_snapshot(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        _require(
            stat.S_ISREG(info.st_mode) and info.st_size <= MAX_INPUT_BYTES,
            "worksheet must be a bounded regular file",
        )
        with os.fdopen(descriptor, "rb") as source:
            descriptor = None
            payload = source.read(MAX_INPUT_BYTES + 1)
        _require(len(payload) <= MAX_INPUT_BYTES, "worksheet exceeds byte limit")
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return payload, parse_document(payload, str(path))


def _metadata(document, digest):
    _require(
        set(document)
        == {
            "schema_version",
            "worksheet_kind",
            "map",
            "floor",
            "tag_black_size",
            "grid",
            "moved_tags",
            "wall_tags",
            "independent_tape_checks",
            "registration_tie_ids",
            "floor_zone",
            "authored_hazards",
            "connector_lines",
        },
        "worksheet does not match worksheet-v1 schema",
    )
    _require(
        type(document["schema_version"]) is int and document["schema_version"] == 1,
        "worksheet requires schema_version 1",
    )
    _require(
        document["worksheet_kind"] == "ohmni-measurement-worksheet", "worksheet_kind is unsupported"
    )
    map_info = document["map"]
    _require(
        isinstance(map_info, dict)
        and set(map_info) == {"map_id", "map_version", "physical_datum", "axes", "provenance"},
        "map does not match worksheet-v1 schema",
    )
    axes = map_info["axes"]
    _require(
        axes == {"x": "toward_elevator", "y": "toward_street_wall", "z": "up"},
        "map axes must explicitly name the canonical world directions",
    )
    _require(map_info["physical_datum"] == "tag_0_center", "physical datum must be tag_0_center")
    provenance = map_info["provenance"]
    _require(
        isinstance(provenance, dict) and set(provenance) == {"name", "captured_at"},
        "map provenance requires name and captured_at",
    )
    floor = document["floor"]
    _require(
        isinstance(floor, dict) and set(floor) == {"id", "site_datum", "elevation_from_site_datum"},
        "floor requires id, site_datum, and elevation_from_site_datum",
    )
    return {
        "map_id": _text(map_info["map_id"], "map_id"),
        "map_version": _text(map_info["map_version"], "map_version"),
        "physical_datum": map_info["physical_datum"],
        "provenance": {
            "name": _text(provenance["name"], "provenance.name"),
            "captured_at": _text(provenance["captured_at"], "provenance.captured_at"),
        },
        "floor_id": _text(floor["id"], "floor.id"),
        "site_datum": _text(floor["site_datum"], "floor.site_datum"),
        "floor_elevation_m": _measure(
            floor["elevation_from_site_datum"], "floor.elevation_from_site_datum"
        ),
        "worksheet_sha256": digest,
    }


def _grid(document, floor_id, size):
    grid = document["grid"]
    _require(
        isinstance(grid, dict)
        and set(grid)
        == {
            "tag_ids",
            "top_left_black_corner_xy",
            "across_axis",
            "down_axis",
            "center_spacing_across",
            "center_spacing_down",
        },
        "grid does not match worksheet-v1 schema",
    )
    rows = grid["tag_ids"]
    _require(
        isinstance(rows, list)
        and len(rows) == 3
        and all(isinstance(row, list) and len(row) == 4 for row in rows),
        "grid needs exactly 3 rows of 4 tag IDs",
    )
    ids = [tag_id for row in rows for tag_id in row]
    _require(
        all(type(tag_id) is int and 0 <= tag_id <= 586 for tag_id in ids)
        and len(set(ids)) == len(ids),
        "grid tag IDs must be unique tag36h11 IDs",
    )
    _require(0 in ids, "grid needs world datum tag ID 0")
    across_name = _axis_name(grid["across_axis"], "grid.across_axis")
    down_name = _axis_name(grid["down_axis"], "grid.down_axis")
    _require(
        across_name == "+x" and down_name == "+y",
        "grid orientation must explicitly align across with +x and down with +y",
    )
    corner = _xy(grid["top_left_black_corner_xy"], "grid.top_left_black_corner_xy")
    across = _axis(across_name, "grid.across_axis")
    down = _axis(down_name, "grid.down_axis")
    spacing_across = _measure(
        grid["center_spacing_across"], "grid.center_spacing_across", positive=True
    )
    spacing_down = _measure(grid["center_spacing_down"], "grid.center_spacing_down", positive=True)
    _require(
        spacing_across >= size and spacing_down >= size,
        "grid center spacing cannot overlap physical tags",
    )
    points = {}
    for row_index, row in enumerate(rows):
        for column_index, tag_id in enumerate(row):
            offset = _add(
                _scale(across, column_index * spacing_across + size / 2),
                _scale(down, row_index * spacing_down + size / 2),
            )
            points[tag_id] = _add(corner, offset)
    datum = points[0]
    tags = {
        tag_id: _tag(
            tag_id,
            floor_id,
            size,
            (point[0] - datum[0], point[1] - datum[1], 0.0),
            (1, 0, 0),
            (0, 1, 0),
            (0, 0, 1),
            "measured",
        )
        for tag_id, point in points.items()
    }
    return tags, datum


def _moved_tags(document, tags):
    moves = document["moved_tags"]
    _require(isinstance(moves, list) and len(moves) <= MAX_TAGS, "moved_tags must be bounded")
    seen = set()
    for move in moves:
        _require(
            isinstance(move, dict) and set(move) == {"tag_id", "dx", "dy"},
            "moved tag requires tag_id, dx, and dy",
        )
        tag_id = move["tag_id"]
        _require(tag_id in tags and tag_id not in seen, "moved tag must name one grid tag once")
        seen.add(tag_id)
        dx = _measure(move["dx"], f"moved tag {tag_id}.dx")
        dy = _measure(move["dy"], f"moved tag {tag_id}.dy")
        tag = tags[tag_id]
        point = (tag["x_m"] + dx, tag["y_m"] + dy, 0.0)
        _require(
            max(abs(value) for value in point) <= MAX_COORDINATE_M,
            "moved tag exceeds metric bounds",
        )
        tag.update(
            x_m=point[0],
            y_m=point[1],
            z_m=point[2],
            T_world_tag=_transform((1, 0, 0), (0, 1, 0), (0, 0, 1), point),
        )
    datum = tags[0]
    _require(datum["x_m"] == datum["y_m"] == 0, "datum tag 0 cannot be moved")


def _wall_tags(document, tags, floor_id, size, datum):
    walls = document["wall_tags"]
    _require(
        isinstance(walls, list) and len(walls) <= MAX_TAGS - len(tags),
        "wall_tags exceed the tag limit",
    )
    for wall in walls:
        _require(
            isinstance(wall, dict)
            and set(wall) == {"id", "center_xy", "center_height_above_floor", "wall_normal"},
            "wall tag requires id, center_xy, center_height_above_floor, and wall_normal",
        )
        tag_id = wall["id"]
        _require(
            type(tag_id) is int and 0 <= tag_id <= 586 and tag_id not in tags,
            "wall tag ID is ambiguous",
        )
        xy = _xy(wall["center_xy"], f"wall tag {tag_id}.center_xy")
        normal_2d = _axis(wall["wall_normal"], f"wall tag {tag_id}.wall_normal")
        height = _measure(
            wall["center_height_above_floor"],
            f"wall tag {tag_id}.center_height_above_floor",
            positive=True,
        )
        normal = (normal_2d[0], normal_2d[1], 0.0)
        x_axis = (-normal[1], normal[0], 0.0)
        y_axis = (0.0, 0.0, 1.0)
        tags[tag_id] = _tag(
            tag_id,
            floor_id,
            size,
            (xy[0] - datum[0], xy[1] - datum[1], height),
            x_axis,
            y_axis,
            normal,
            "surveyed",
        )


def _checks(document, tags):
    checks = document["independent_tape_checks"]
    _require(
        isinstance(checks, list) and 1 <= len(checks) <= MAX_TAPE_CHECKS,
        "at least one bounded independent tape check is required",
    )
    tapes = {}
    for index, check in enumerate(checks):
        _require(
            isinstance(check, dict)
            and set(check) == {"name", "tag_ids", "measured_distance", "maximum_error"},
            "independent tape check does not match worksheet-v1 schema",
        )
        name = _text(check["name"], f"tape check {index}.name")
        pair = check["tag_ids"]
        _require(
            isinstance(pair, list) and len(pair) == 2 and all(type(item) is int for item in pair),
            "tape check tag_ids must contain two IDs",
        )
        left, right = pair
        _require(
            left != right and left in tags and right in tags,
            "tape check names unknown or repeated tag IDs",
        )
        measured = _measure(
            check["measured_distance"], f"tape check {name}.measured_distance", positive=True
        )
        maximum = _measure(
            check["maximum_error"], f"tape check {name}.maximum_error", positive=True
        )
        _require(maximum <= 0.1, "tape maximum_error cannot exceed 0.1 m")
        actual = _distance(
            (tags[left]["x_m"], tags[left]["y_m"]), (tags[right]["x_m"], tags[right]["y_m"])
        )
        _require(
            abs(actual - measured) <= maximum + 1e-9,
            f"tape check {name} disagrees with derived tag distance",
        )
        for endpoint, other, suffix in ((left, right, "a"), (right, left, "b")):
            tapes[f"evidence/tape-{index:02d}-{suffix}.json"] = {
                "schema_version": 1,
                "kind": "independent_tape_measurement",
                "tag_ids": [endpoint, other],
                "measured_distance_m": measured,
                "maximum_error_m": maximum,
            }
    return tapes


def _attach_tapes(tags, tapes):
    for path, evidence in tapes.items():
        tag_id, other_id = evidence["tag_ids"]
        tag = tags[tag_id]
        if tag["tape_verification"] is not None:
            continue
        payload = json.dumps(evidence, indent=2, allow_nan=False).encode() + b"\n"
        tag.update(
            verified_for_flight=True,
            tape_verification={
                "source": "independent_tape_measurement",
                "evidence_path": path,
                "evidence_sha256": hashlib.sha256(payload).hexdigest(),
                "compared_tag_id": other_id,
                "measured_distance_m": evidence["measured_distance_m"],
                "maximum_error_m": evidence["maximum_error_m"],
            },
        )


def _registration_ties(document, tags, metadata):
    ids = document["registration_tie_ids"]
    _require(
        isinstance(ids, list)
        and 3 <= len(ids) <= MAX_TAGS
        and all(type(tag_id) is int for tag_id in ids),
        "registration_tie_ids requires at least three tag IDs",
    )
    _require(
        len(set(ids)) == len(ids) and all(tag_id in tags for tag_id in ids),
        "registration_tie_ids are ambiguous",
    )
    points = [(tags[tag_id]["x_m"], tags[tag_id]["y_m"]) for tag_id in ids]
    noncollinear = any(
        abs(
            (middle[0] - first[0]) * (last[1] - first[1])
            - (middle[1] - first[1]) * (last[0] - first[0])
        )
        > 1e-6
        for first_index, first in enumerate(points)
        for middle_index, middle in enumerate(points)
        for last in points
        if first_index < middle_index
    )
    _require(noncollinear, "registration_tie_ids cannot be collinear")
    return {
        "schema_version": 1,
        "frame": "world",
        "map": {
            "map_id": metadata["map_id"],
            "map_version": metadata["map_version"],
            "physical_datum": metadata["physical_datum"],
        },
        "provenance": {"name": "worksheet-v1", "sha256": metadata["worksheet_sha256"]},
        "tags": [
            {"tag_id": tag_id, "xy_m": list(points[index])} for index, tag_id in enumerate(ids)
        ],
    }


def _polygon(value, name):
    _require(
        isinstance(value, list) and 3 <= len(value) <= MAX_POLYGON_POINTS,
        f"{name} must have bounded vertices",
    )
    points = [_xy(point, name) for point in value]
    _require(len(set(points)) == len(points), f"{name} has repeated vertices")
    return polygon([list(point) for point in points + [points[0]]])


def _rebase_polygon(points, datum):
    return [[point[0] - datum[0], point[1] - datum[1]] for point in points]


def _zones_and_hazards(document, metadata, datum, tags):
    zone = document["floor_zone"]
    hazards = document["authored_hazards"]
    _require(
        hazards is None or isinstance(hazards, list) and len(hazards) <= MAX_TAGS,
        "authored_hazards must be bounded",
    )
    if zone is None:
        _require(not hazards, "authored hazards need a measured floor_zone")
        return {}
    _require(
        isinstance(zone, dict) and set(zone) == {"id", "polygon_xy", "z_min", "z_max"},
        "floor_zone needs id, polygon_xy, z_min, and z_max",
    )
    polygon = _rebase_polygon(_polygon(zone["polygon_xy"], "floor_zone.polygon_xy"), datum)
    low = _measure(zone["z_min"], "floor_zone.z_min")
    high = _measure(zone["z_max"], "floor_zone.z_max")
    _require(low < high, "floor_zone vertical bounds must increase")
    zone_id = _text(zone["id"], "floor_zone.id")
    for tag in tags.values():
        _require(
            point_inside(polygon, (tag["x_m"], tag["y_m"])) and low <= tag["z_m"] <= high,
            "measured tag lies outside floor_zone",
        )
    zones = {
        "schema_version": 2,
        "units": "meters",
        "frame": "world",
        "geofence": {"polygon": polygon, "z_min_m": low, "z_max_m": high},
        "zones": [
            {
                "id": zone_id,
                "floor_id": metadata["floor_id"],
                "polygon": polygon,
                "z_min_m": low,
                "z_max_m": high,
            }
        ],
        "corridors": [],
    }
    obstacles = []
    for hazard in hazards or []:
        _require(
            isinstance(hazard, dict) and set(hazard) == {"id", "polygon_xy", "z_min", "z_max"},
            "authored hazard needs id, polygon_xy, z_min, and z_max",
        )
        hazard_low = _measure(hazard["z_min"], f"hazard {hazard.get('id')}.z_min")
        hazard_high = _measure(hazard["z_max"], f"hazard {hazard.get('id')}.z_max")
        _require(
            low <= hazard_low < hazard_high <= high,
            "authored hazard must fit floor_zone vertical bounds",
        )
        hazard_polygon = _rebase_polygon(_polygon(hazard["polygon_xy"], "hazard.polygon_xy"), datum)
        _require(
            all(point_inside(polygon, point) for point in hazard_polygon[:-1]),
            "authored hazard lies outside floor_zone",
        )
        obstacles.append(
            {
                "id": _text(hazard["id"], "hazard.id"),
                "floor_id": metadata["floor_id"],
                "polygon": hazard_polygon,
                "z_min_m": hazard_low,
                "z_max_m": hazard_high,
            }
        )
    return {
        "zones.yaml": zones,
        "obstacles.yaml": {
            "schema_version": 2,
            "units": "meters",
            "frame": "world",
            "obstacles": obstacles,
            "no_fly": [],
        },
    }


def _connector_lines(document, tags):
    lines = document["connector_lines"]
    _require(
        lines is None or isinstance(lines, list) and len(lines) <= MAX_TAGS,
        "connector_lines must be bounded",
    )
    result = []
    for line in lines or []:
        _require(
            isinstance(line, dict)
            and set(line) == {"id", "direction_axis", "near_wall", "far_wall", "end_ties"},
            "connector line needs direction_axis, wall offsets, and end_ties",
        )
        direction_name = _axis_name(line["direction_axis"], "connector.direction_axis")
        direction = _axis(direction_name, "connector.direction_axis")
        wall_offsets = []
        for side in ("near_wall", "far_wall"):
            wall = line[side]
            _require(
                isinstance(wall, dict) and set(wall) == {"parallel_axis", "offset"},
                f"connector {side} needs parallel_axis and offset",
            )
            parallel_name = _axis_name(wall["parallel_axis"], f"connector {side}.parallel_axis")
            parallel = _axis(parallel_name, f"connector {side}.parallel_axis")
            _require(
                abs(direction[0] * parallel[1] - direction[1] * parallel[0]) <= 1e-9,
                "connector wall offsets must be parallel to the declared direction",
            )
            wall_offsets.append(_measure(wall["offset"], f"connector {side}.offset"))
        _require(
            abs(wall_offsets[0] - wall_offsets[1]) > 1e-9,
            "connector wall offsets must describe two distinct walls",
        )
        ties = line["end_ties"]
        _require(
            isinstance(ties, dict) and set(ties) == {"start_tag_id", "end_tag_id"},
            "connector end_ties requires start_tag_id and end_tag_id",
        )
        start, end = ties["start_tag_id"], ties["end_tag_id"]
        _require(
            type(start) is int
            and type(end) is int
            and start != end
            and start in tags
            and end in tags,
            "connector end_ties must identify two known tags",
        )
        delta = (tags[end]["x_m"] - tags[start]["x_m"], tags[end]["y_m"] - tags[start]["y_m"])
        _require(
            abs(delta[0] * direction[1] - delta[1] * direction[0]) <= 1e-6
            and delta[0] * direction[0] + delta[1] * direction[1] > 0,
            "connector end ties must follow the declared direction",
        )
        result.append(
            {
                "id": _text(line["id"], "connector.id"),
                "direction_axis": direction_name,
                "wall_offsets_m": wall_offsets,
                "end_tie_tag_ids": [start, end],
            }
        )
    return result


def import_worksheet(document, payload):
    digest = hashlib.sha256(payload).hexdigest()
    metadata = _metadata(document, digest)
    size = _measure(document["tag_black_size"], "tag_black_size", positive=True)
    tags, datum = _grid(document, metadata["floor_id"], size)
    _moved_tags(document, tags)
    _wall_tags(document, tags, metadata["floor_id"], size, datum)
    tapes = _checks(document, tags)
    _attach_tapes(tags, tapes)
    known = _registration_ties(document, tags, metadata)
    documents = {
        "tags.yaml": {
            "schema_version": 2,
            "units": "meters",
            "frame": "world",
            "tags": [tags[tag_id] for tag_id in sorted(tags)],
        },
        "evidence/known_tags.json": known,
        **tapes,
        **_zones_and_hazards(document, metadata, datum, tags),
        "worksheet-source.yaml": payload,
    }
    metadata["connector_lines"] = _connector_lines(document, tags)
    metadata["tags"] = [{"id": tag_id, "source": tags[tag_id]["source"]} for tag_id in sorted(tags)]
    documents["worksheet-metadata.json"] = {
        "schema_version": 1,
        "kind": "ohmni-measurement-worksheet-import",
        **metadata,
    }
    return documents


def _write_directory(output, documents):
    output.mkdir(parents=True, exist_ok=False)
    _require(len(documents) <= MAX_OUTPUT_FILES, "worksheet output exceeds file limit")
    for name, value in documents.items():
        path = output / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(value, bytes):
            path.write_bytes(value)
        else:
            write_document(path, value)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("worksheet", type=Path, help="worksheet-v1 JSON/YAML-subset input")
    parser.add_argument("output", type=Path, help="new evidence directory")
    args = parser.parse_args()
    try:
        payload, document = _read_snapshot(args.worksheet)
        documents = import_worksheet(document, payload)
        _write_directory(args.output, documents)
    except (OSError, ValueError) as exc:
        print(json.dumps({"valid": False, "error": str(exc)}))
        return 1
    print(json.dumps({"valid": True, "output": str(args.output), "files": sorted(documents)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
