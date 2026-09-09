import collections
import hashlib
import itertools
import json
import pathlib
import sys
from datetime import datetime

import cv2
import numpy as np

ROOT = pathlib.Path(__file__).parent
capture = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "capture-20260909T043401Z"
map_path = ROOT.parents[2] / "deployments/real-navigation/tag-map-53.json"
map_doc = json.loads(map_path.read_text())
tags = {t["id"]: t for t in map_doc["tags"] if t["id"] not in {35, 49}}
fit = json.loads((ROOT / "initial-pinhole-candidate.json").read_text())
K = np.array(fit["camera_matrix"], float)
D = np.array(fit["distortion_coefficients"], float)
size = 0.199898
obj = np.array([[-1, 1, 0], [1, 1, 0], [1, -1, 0], [-1, -1, 0]], float) * size / 2
ctx = json.loads((capture / "capture-context.json").read_text())
start = ctx.get("route_authorized_utc", ctx.get("ready_prompt_utc", ""))
rows = []
for line in (capture / "frames.jsonl").read_text().splitlines():
    try:
        f = json.loads(line)
        if not start or datetime.fromisoformat(f["saved_utc"]) >= datetime.fromisoformat(start):
            rows.append(f)
    except (ValueError, KeyError):
        pass


def solve(objp, imgp, flag, world=False, ids=()):
    try:
        ok, rs, ts, _ = cv2.solvePnPGeneric(objp, imgp, K, D, flags=flag)
    except cv2.error:
        return []
    sol = []
    for r, t in zip(rs, ts, strict=True):
        try:
            r, t = cv2.solvePnPRefineLM(objp, imgp, K, D, r.copy(), t.copy())
        except cv2.error:
            continue
        R = cv2.Rodrigues(r)[0]
        t = t.reshape(3)
        cam = -R.T @ t
        if np.any((objp @ R.T + t)[:, 2] <= 0):
            continue
        if world:
            if any(
                np.dot(cam - np.array(tags[i]["center_m"]), np.array(tags[i]["normal_unit"])) <= 0
                for i in ids
            ):
                continue
        elif cam[2] <= 0:
            continue
        project = cv2.projectPoints(objp, r, t, K, D)[0].reshape(-1, 2)
        rms = float(np.sqrt(np.mean(np.sum((project - imgp) ** 2, axis=1))))
        T = np.eye(4)
        T[:3, :3] = R.T
        T[:3, 3] = cam
        sol.append({"rms_px": rms, "T_reference_camera": T, "position": cam})
    return sorted(sol, key=lambda v: v["rms_px"])


results = []
counts = collections.Counter()
excluded = collections.Counter()
pair_records = []
for f in rows:
    per = []
    worldpts = []
    images = []
    ids = []
    for ident, corners in zip(f["tag_ids"], f["corners_px"], strict=True):
        counts[ident] += 1
        if ident not in tags:
            excluded[str(ident)] += 1
            continue
        pix = np.array(corners, np.float64)
        edge = float(np.min(np.linalg.norm(pix - np.roll(pix, 1, axis=0), axis=1)))
        if edge < 60:
            excluded["short_edge_under_60px"] += 1
            continue
        Twt = np.array(tags[ident]["T_world_tag"], float)
        solutions = solve(obj, pix, cv2.SOLVEPNP_IPPE_SQUARE)
        if not solutions:
            continue
        best = solutions[0]
        Twc = Twt @ best["T_reference_camera"]
        per.append(
            {
                "tag_id": ident,
                "position_m": Twc[:3, 3].tolist(),
                "rms_px": best["rms_px"],
                "shortest_edge_px": edge,
                "alternative_rms_px": solutions[1]["rms_px"] if len(solutions) > 1 else None,
            }
        )
        ids.append(ident)
        worldpts.append(obj @ Twt[:3, :3].T + Twt[:3, 3])
        images.append(pix)
    out = {
        "frame_index": f["frame_index"],
        "saved_utc": f["saved_utc"],
        "source_sha256": f["sha256"],
        "tag_ids": f["tag_ids"],
        "single_tag_poses": per,
    }
    pairs = []
    for a, b in itertools.combinations(per, 2):
        distance = float(np.linalg.norm(np.array(a["position_m"]) - np.array(b["position_m"])))
        pair = {
            "tags": [a["tag_id"], b["tag_id"]],
            "camera_position_disagreement_m": distance,
            "max_single_tag_rms_px": max(a["rms_px"], b["rms_px"]),
        }
        pairs.append(pair)
        pair_records.append({"frame_index": f["frame_index"], **pair})
    out["pairwise_consistency"] = pairs
    if len(ids) >= 2:
        sols = solve(
            np.concatenate(worldpts),
            np.concatenate(images),
            cv2.SOLVEPNP_SQPNP,
            world=True,
            ids=ids,
        )
        if sols:
            out["joint_pose"] = {
                "tag_ids": ids,
                "camera_position_m": sols[0]["position"].tolist(),
                "rms_px": sols[0]["rms_px"],
                "used_corners": 4 * len(ids),
            }
    elif per:
        out["single_tag_camera_position_m"] = per[0]["position_m"]
    results.append(out)


def summary(values):
    if not values:
        return None
    a = np.array(values, float)
    return {
        "count": len(a),
        "median": float(np.median(a)),
        "p95": float(np.percentile(a, 95)),
        "maximum": float(np.max(a)),
    }


report = {
    "kind": "handheld_hallway_diagnostic",
    "flight_approved": False,
    "calibration_qualified": False,
    "capture": str(capture.relative_to(ROOT)),
    "map_sha256": hashlib.sha256(map_path.read_bytes()).hexdigest(),
    "intrinsics_sha256": hashlib.sha256(
        (ROOT / "initial-pinhole-candidate.json").read_bytes()
    ).hexdigest(),
    "map_status": map_doc["status"],
    "route_start_utc": start,
    "sampled_frames": len(results),
    "frames_with_detected_tags": sum(bool(x["tag_ids"]) for x in results),
    "frames_with_mapped_pose": sum(bool(x["single_tag_poses"]) for x in results),
    "frames_with_multiple_mapped_poses": sum(len(x["single_tag_poses"]) >= 2 for x in results),
    "tag_detection_counts": dict(counts),
    "excluded_observations": dict(excluded),
    "pairwise_position_disagreement_m": summary(
        [x["camera_position_disagreement_m"] for x in pair_records]
    ),
    "joint_reprojection_rms_px": summary(
        [x["joint_pose"]["rms_px"] for x in results if "joint_pose" in x]
    ),
    "single_tag_reprojection_rms_px": summary(
        [p["rms_px"] for x in results for p in x["single_tag_poses"]]
    ),
    "limitations": [
        (
            "Pose disagreement measures consistency of intrinsics, map, corners and "
            "planar-pose choice together; it is not absolute position error."
        ),
        (
            "Camera position only; no measured gimbal/body transform, sensor exposure"
            " timing, motion latency or flight control admission."
        ),
        (
            "No independent camera-position ground truth or measured same-position "
            "loop endpoint was supplied."
        ),
        (
            "Frames sampled approximately once per second, so detection counts are "
            "not full video frame-rate statistics."
        ),
        (
            "Tag poses are diagnostic low-level PnP outputs; ambiguity and control "
            "admission not qualified."
        ),
        (
            "Map is owner-approved snapshot with retained provisional mapping "
            "provenance. Tags 35 and 49 excluded."
        ),
        "Walk by hand, motors off; no aircraft commands sent.",
    ],
}
(capture / "hallway-poses.jsonl").write_text("".join(json.dumps(x) + "\n" for x in results))
(capture / "hallway-diagnostic.json").write_text(json.dumps(report, indent=2))
print(
    json.dumps(
        {
            k: report[k]
            for k in [
                "sampled_frames",
                "frames_with_detected_tags",
                "frames_with_mapped_pose",
                "frames_with_multiple_mapped_poses",
                "tag_detection_counts",
                "pairwise_position_disagreement_m",
                "joint_reprojection_rms_px",
            ]
        },
        indent=2,
    )
)
