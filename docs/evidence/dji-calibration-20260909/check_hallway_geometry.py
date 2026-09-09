"""Offline calls to existing geometry routines; no qualified localizer or control session."""

import collections
import itertools
import json
import pathlib
import sys
import types

import numpy as np

ROOT = pathlib.Path(__file__).parent
sys.path.insert(0, str(ROOT.parents[2]))
from perception.tag_localization import TagLocalizer, tag_corners  # noqa: E402

capture = ROOT / "capture-20260909T043401Z"
fit = json.loads((ROOT / "initial-pinhole-candidate.json").read_text())
mapdoc = json.loads((ROOT.parents[2] / "deployments/real-navigation/tag-map-53.json").read_text())
tags = {t["id"]: t for t in mapdoc["tags"] if t["id"] not in {35, 49}}
geometry = types.SimpleNamespace(
    K=np.array(fit["camera_matrix"]),
    dist=np.array(fit["distortion_coefficients"]),
    world=True,
    tags=tags,
)
geometry._pose_candidates = types.MethodType(TagLocalizer._pose_candidates, geometry)
start = json.loads((capture / "capture-context.json").read_text())["route_authorized_utc"]
obj = tag_corners(0.199898)
rows = []
single_counts = collections.Counter()
joint_counts = collections.Counter()
distances = []
for line in (capture / "frames.jsonl").read_text().splitlines():
    f = json.loads(line)
    if f["saved_utc"] < start:
        continue
    accepted = []
    points = []
    pixels = []
    ids = []
    observations = []
    for ident, corners in zip(f["tag_ids"], f["corners_px"], strict=True):
        if ident not in tags:
            continue
        pix = np.array(corners, float)
        if np.min(np.linalg.norm(pix - np.roll(pix, 1, axis=0), axis=1)) < 60:
            continue
        T = np.array(tags[ident]["T_world_tag"])
        world = obj @ T[:3, :3].T + T[:3, 3]
        pose, error, reason = TagLocalizer._camera_pose(geometry, world, pix, [ident])
        single_counts[reason or "geometry_pass"] += 1
        observations.append(
            {
                "tag_id": ident,
                "reason": reason,
                "rms_px": error,
                "camera_position_m": pose[:3, 3].tolist() if pose is not None else None,
            }
        )
        if pose is not None:
            accepted.append((ident, pose[:3, 3]))
        points.append(world)
        pixels.append(pix)
        ids.append(ident)
    for a, b in itertools.combinations(accepted, 2):
        distances.append(
            {
                "frame_index": f["frame_index"],
                "tag_ids": [a[0], b[0]],
                "disagreement_m": float(np.linalg.norm(a[1] - b[1])),
            }
        )
    row = {"frame_index": f["frame_index"], "single_tag_geometry": observations}
    if len(ids) >= 2:
        pose, error, reason = TagLocalizer._camera_pose(
            geometry, np.concatenate(points), np.concatenate(pixels), ids
        )
        joint_counts[reason or "geometry_pass"] += 1
        row["joint_geometry"] = {
            "tag_ids": ids,
            "reason": reason,
            "rms_px": error,
            "camera_position_m": pose[:3, 3].tolist() if pose is not None else None,
        }
    rows.append(row)
values = np.array([x["disagreement_m"] for x in distances])
report = {
    "kind": "offline_existing_geometry_gate_diagnostic",
    "flight_approved": False,
    "qualified_localizer_created": False,
    "method": (
        "Existing TagLocalizer._camera_pose and _pose_candidates only, fixed "
        "unqualified intrinsics and pinned map; no full localizer, consensus, "
        "timing, extrinsics or control admission. Same >=60 pixel edge filter as "
        "raw diagnostic. Reprojection <=2px and existing ambiguity checks."
    ),
    "single_tag_observation_counts": dict(single_counts),
    "joint_frame_counts": dict(joint_counts),
    "pairwise_geometry_pass_disagreement_m": {
        "count": len(values),
        "median": float(np.median(values)),
        "p95": float(np.percentile(values, 95)),
        "maximum": float(np.max(values)),
    }
    if len(values)
    else None,
    "pairwise_records": distances,
}
(capture / "hallway-geometry-gates.json").write_text(json.dumps(report, indent=2))
(capture / "hallway-geometry-frames.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
print(json.dumps({k: v for k, v in report.items() if k != "pairwise_records"}, indent=2))
