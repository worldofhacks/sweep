from __future__ import annotations

import argparse
import hashlib
import json
from math import isfinite
from pathlib import Path

import numpy as np

from tools.world_bundle import _validate_obstacles


def _positive(value: object, name: str) -> float:
    if type(value) not in (int, float) or not isfinite(value) or not 0 < value <= 100:
        raise ValueError(f"{name} must be a positive finite metric value")
    return float(value)


def build_wall_geometry(tag_map: Path, measurements: Path) -> dict[str, object]:
    source_bytes, measurement_bytes = tag_map.read_bytes(), measurements.read_bytes()
    source, survey = json.loads(source_bytes), json.loads(measurement_bytes)
    source_digest = hashlib.sha256(source_bytes).hexdigest()
    if survey["tagMapSha256"] != source_digest:
        raise ValueError("wall measurements must bind the exact tag map")
    if survey["referenceCorner"] != "black_top_left":
        raise ValueError("wall measurements require the black top-left reference corner")
    half_size = _positive(survey["tagBlackSizeM"], "tag black size") / 2
    length = _positive(survey["wallLengthM"], "wall length")
    depth = _positive(survey["occupiedDepthM"], "occupied depth")
    ceiling = _positive(survey["heightLimits"]["hardCeilingM"], "hard ceiling")
    soft_ceiling = _positive(survey["heightLimits"]["softCeilingM"], "soft ceiling")
    if soft_ceiling >= ceiling:
        raise ValueError("soft ceiling must be below hard ceiling")
    tags = {tag["id"]: tag for tag in source["tags"]}
    readings = survey["measurements"]
    if not isinstance(readings, list) or not 1 <= len(readings) <= 128:
        raise ValueError("wall measurements must be a nonempty bounded list")
    segments, obstacles, seen = [], [], set()
    for reading in readings:
        tag_id, direction = reading["tagId"], reading["wallDirection"]
        wall_id = f"tag-{tag_id}-{direction}-wall"
        if wall_id in seen:
            raise ValueError("wall measurements contain a duplicate wall")
        seen.add(wall_id)
        tag = tags[tag_id]
        transform = np.asarray(tag["T_world_tag"], dtype=float)
        if (
            transform.shape != (4, 4)
            or not np.isfinite(transform).all()
            or not np.allclose(transform[3], [0, 0, 0, 1], atol=1e-8, rtol=0)
            or not np.allclose(transform[:3, :3].T @ transform[:3, :3], np.eye(3), atol=1e-8)
            or not np.isclose(np.linalg.det(transform[:3, :3]), 1, atol=1e-8)
            or not np.allclose(transform[:3, 2], [0, 0, 1], atol=1e-8, rtol=0)
            or not np.allclose(transform[:3, 3], tag["center_m"], atol=1e-8, rtol=0)
        ):
            raise ValueError(
                "entrance wall anchors require consistent upright floor-tag transforms"
            )
        normal_tag = np.asarray(reading["normalTagXY"], dtype=float)
        if normal_tag.shape != (2,) or tuple(normal_tag) not in {(1, 0), (-1, 0), (0, 1), (0, -1)}:
            raise ValueError("wall normal must name one signed tag-plane axis")
        distance = _positive(reading["distanceM"], "wall distance")
        inches = _positive(reading["distanceIn"], "wall distance in inches")
        if not np.isclose(distance, inches * 0.0254, atol=1e-9, rtol=0):
            raise ValueError("inch and meter wall distances disagree")
        corner = (transform @ [-half_size, half_size, 0, 1])[:3]
        normal = transform[:2, :2] @ normal_tag
        point = corner[:2] + distance * normal
        tangent = np.array([-normal[1], normal[0]])
        start, end = point - length / 2 * tangent, point + length / 2 * tangent
        placement = "centered_on_measured_wall_point"
        if "joinsWallDirection" in reading:
            mate = next(
                (
                    item
                    for item in readings
                    if item["tagId"] == tag_id
                    and item["wallDirection"] == reading["joinsWallDirection"]
                ),
                None,
            )
            if mate is None or mate.get("joinsWallDirection") != direction:
                raise ValueError("corner walls must name each other")
            mate_axis = np.asarray(mate["normalTagXY"], dtype=float)
            if (
                mate_axis.shape != (2,)
                or not np.isclose(np.linalg.norm(mate_axis), 1)
                or not np.isclose(mate_axis @ normal_tag, 0)
            ):
                raise ValueError("joined corner walls require perpendicular unit normals")
            mate_distance = _positive(mate["distanceM"], "corner wall distance")
            if mate_distance > length:
                raise ValueError("wall extent does not reach its measured point from the corner")
            mate_normal = transform[:2, :2] @ mate_axis
            start = point + mate_distance * mate_normal
            end = start - length * mate_normal
            placement = "from_measured_wall_intersection"
        polygon = [start, end, end + depth * normal, start + depth * normal, start]
        floor = float(corner[2])
        segments.append(
            {
                "id": wall_id,
                "tagId": tag_id,
                "entranceId": reading["entranceId"],
                "wallDirection": direction,
                "referenceCornerM": corner.tolist(),
                "wallPointXYM": point.tolist(),
                "outwardNormalXY": normal.tolist(),
                "startXYM": start.tolist(),
                "endXYM": end.tolist(),
                "lengthM": length,
                "extentPlacement": placement,
            }
        )
        obstacles.append(
            {
                "id": wall_id,
                "floor_id": survey["floorId"],
                "polygon": [vertex.tolist() for vertex in polygon],
                "z_min_m": floor,
                "z_max_m": floor + ceiling,
            }
        )
    obstacle_document = {
        "schema_version": 2,
        "units": "meters",
        "frame": "world",
        "obstacles": obstacles,
        "no_fly": [],
    }
    _validate_obstacles(obstacle_document)
    return {
        "schemaVersion": 1,
        "kind": "entrance_wall_geometry",
        "coordinateFrame": source["coordinate_frame"]["id"],
        "tagMapSha256": source_digest,
        "measurementsSha256": hashlib.sha256(measurement_bytes).hexdigest(),
        "heightLimits": survey["heightLimits"],
        "extentModel": {
            "lengthM": length,
            "placement": "centered_entrances_and_joined_atrium_corner",
            "lengthBasis": "owner_estimate",
            "occupiedDepthM": depth,
            "depthBasis": "outward_collision_strip_not_measured_wall_thickness",
            "verticalBasis": "block_the_retained_flight_height_range",
        },
        "wallSegments": segments,
        "obstacleDocument": obstacle_document,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag-map", type=Path, required=True)
    parser.add_argument("--measurements", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--obstacles-output", type=Path, required=True)
    args = parser.parse_args()
    geometry = build_wall_geometry(args.tag_map, args.measurements)
    for path, document in (
        (args.output, geometry),
        (args.obstacles_output, geometry["obstacleDocument"]),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document, indent=2) + "\n")


if __name__ == "__main__":
    main()
