"""Extract metric tag poses from operator-confirmed scan corner picks."""

import argparse
import html
import json
import math
from pathlib import Path

from tools.map_common import (
    finite_number,
    read_document,
    transform_point,
    validate_transform,
    write_document,
)


def _vector(value):
    if not isinstance(value, list) or len(value) != 3:
        raise ValueError("expected three finite coordinates")
    return [finite_number(v, "coordinate") for v in value]


def _dot(a, b):
    return sum(x * y for x, y in zip(a, b, strict=True))


def _unit(v):
    length = math.hypot(*v)
    if length < 1e-9:
        raise ValueError("degenerate tag edge or normal")
    return [x / length for x in v]


def _cross(a, b):
    return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]


def extract_tags(document):
    """Return building-frame poses; inconsistent geometry raises ValueError."""
    if not isinstance(document, dict):
        raise ValueError("survey must be an object")
    if (
        type(document.get("schema_version")) is not int
        or document["schema_version"] != 1
        or document.get("units") != "meters"
    ):
        raise ValueError("survey requires schema_version 1 and units meters")
    transform = document.get("T_map_scan")
    validate_transform(transform)
    records = document.get("tags")
    if not isinstance(records, list) or not records:
        raise ValueError("survey requires tags")
    result = []
    ids = set()
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("tag must be an object")
        tag_id = record.get("id")
        if type(tag_id) is not int or not 0 <= tag_id <= 586 or tag_id in ids:
            raise ValueError("tag36h11 IDs must be unique integers in 0..586")
        ids.add(tag_id)
        floor = record.get("floor_id")
        if not isinstance(floor, str) or not floor.strip():
            raise ValueError("tag requires floor_id")
        if record.get("orientation_confirmed") is not True:
            raise ValueError("operator must confirm decoded TL, TR, BR, BL orientation")
        size = finite_number(record.get("size"), "size")
        if size <= 0:
            raise ValueError("size must be the positive measured black-square width in meters")
        corners = record.get("corners")
        if not isinstance(corners, list) or len(corners) != 4:
            raise ValueError("exactly four TL, TR, BR, BL corners are required")
        tl, tr, br, bl = [_vector(p) for p in corners]
        center = [sum(p[i] / 4 for p in corners) for i in range(3)]
        right = _unit([((tr[i] - tl[i]) + (br[i] - bl[i])) / 2 for i in range(3)])
        up_raw = [((tl[i] - bl[i]) + (tr[i] - br[i])) / 2 for i in range(3)]
        up = _unit([up_raw[i] - _dot(up_raw, right) * right[i] for i in range(3)])
        normal = _unit(_cross(right, up))
        front = _unit(_vector(record.get("front_normal_scan")))
        if _dot(normal, front) < math.cos(math.radians(15)):
            raise ValueError("corner winding contradicts confirmed printed-front normal")
        tolerance = min(0.01, size * 0.05)
        for corner, (sx, sy) in zip(corners, [(-1, 1), (1, 1), (1, -1), (-1, -1)], strict=True):
            fitted = [center[i] + size / 2 * (sx * right[i] + sy * up[i]) for i in range(3)]
            if math.dist(corner, fitted) > tolerance:
                raise ValueError("corners disagree with a planar measured square or corner order")
        mapped_center = transform_point(transform, center)
        axes = [
            [sum(transform[i][j] * axis[j] for j in range(3)) for i in range(3)]
            for axis in (right, up, normal)
        ]
        pose = [[axes[j][i] for j in range(3)] + [mapped_center[i]] for i in range(3)]
        pose.append([0, 0, 0, 1])
        validate_transform(pose)
        # A vertical printed right edge has no building-plane yaw.
        if math.hypot(axes[0][0], axes[0][1]) < 1e-6:
            raise ValueError("printed right edge is vertical; yaw is undefined")
        result.append(
            {
                "id": tag_id,
                "floor_id": floor,
                "size": size,
                "x": mapped_center[0],
                "y": mapped_center[1],
                "z": mapped_center[2],
                "yaw": math.atan2(axes[0][1], axes[0][0]),
                "normal": axes[2],
                "T_map_tag": pose,
                "orientation_confirmed": True,
            }
        )
    tag0 = next((t for t in result if t["id"] == 0), None)
    if tag0 is None or math.hypot(tag0["x"], tag0["y"], tag0["z"]) > 1e-6:
        raise ValueError("T_map_scan must place Tag 0 at the building origin")
    return {"schema_version": 1, "units": "meters", "frame": "building", "tags": result}


def write_preview(path, document):
    """Write a static two-projection preview of tag centers and orientation axes."""
    tags = document["tags"]
    views = []
    for a, b, name in [(0, 1, "Plan: X / Y"), (0, 2, "Elevation: X / Z")]:
        points = [[t["x"], t["y"], t["z"]] for t in tags]
        lo = [min(p[d] for p in points) - 0.6 for d in (a, b)]
        hi = [max(p[d] for p in points) + 0.6 for d in (a, b)]
        scale = min(600 / (hi[0] - lo[0]), 400 / (hi[1] - lo[1]))

        def project(p, a=a, b=b, lo=lo, scale=scale):
            return (30 + (p[a] - lo[0]) * scale, 450 - (p[b] - lo[1]) * scale)

        elements = []
        for tag, point in zip(tags, points, strict=True):
            x, y = project(point)
            elements.append(f'<circle cx="{x}" cy="{y}" r="4"/>')
            label = html.escape(f"{tag['id']} ({tag['floor_id']})")
            elements.append(f'<text x="{x + 6}" y="{y - 8}">{label}</text>')
            for j, color in enumerate(("#b42318", "#067647", "#175cd3")):
                end = [point[i] + 0.3 * tag["T_map_tag"][i][j] for i in range(3)]
                ex, ey = project(end)
                elements.append(
                    f'<path d="M{x},{y} L{ex},{ey}" stroke="{color}" stroke-width="3"/>'
                )
        views.append(f'<h2>{name}</h2><svg viewBox="0 0 660 500">{"".join(elements)}</svg>')
    Path(path).write_text(
        '<!doctype html><meta charset="utf-8"><title>Survey tag axes</title>'
        "<h1>Survey tag axes</h1><p>Red: printed right; green: printed up; "
        "blue: printed front. Axes are 0.3 m. Synthetic previews do not "
        "approve a site map.</p>" + "".join(views),
        encoding="utf-8",
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("survey", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--preview", type=Path)
    args = parser.parse_args()
    try:
        output = extract_tags(read_document(args.survey))
        write_document(args.output, output)
        if args.preview:
            write_preview(args.preview, output)
    except (ValueError, OSError) as exc:
        print(json.dumps({"valid": False, "error": str(exc)}))
        return 1
    print(json.dumps({"valid": True, "tags": len(output["tags"]), "output": str(args.output)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
