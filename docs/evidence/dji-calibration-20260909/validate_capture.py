import hashlib
import json
import pathlib
import sys

import cv2
import numpy as np

ROOT = pathlib.Path(__file__).parent
sys.path.insert(0, str(ROOT.parents[2]))
from perception.tag_localization import tag_corners  # noqa: E402

fit = json.loads((ROOT / "initial-pinhole-candidate.json").read_text())
K = np.array(fit["camera_matrix"], float)
D = np.array(fit["distortion_coefficients"], float)
obj = tag_corners(0.199898).astype(np.float64)
records = []
for p in sorted(ROOT.glob("capture-*/selected-holds.json")):
    for h in json.loads(p.read_text())["holds"]:
        if h["role"] != "held_out_validation":
            continue
        f = h["frame"]
        path = p.parent / "frames" / f["file"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == f["sha256"]
        pts = np.array(f["corners_px"][f["tag_ids"].index(54)], np.float64)
        success, rs, ts, _ = cv2.solvePnPGeneric(obj, pts, K, D, flags=cv2.SOLVEPNP_IPPE_SQUARE)
        solutions = []
        for r, t in zip(rs, ts, strict=True):
            r, t = cv2.solvePnPRefineLM(obj, pts, K, D, r.copy(), t.copy())
            R = cv2.Rodrigues(r)[0]
            if np.any((obj @ R.T + t.reshape(3))[:, 2] <= 0):
                continue
            proj = cv2.projectPoints(obj, r, t, K, D)[0].reshape(4, 2)
            errors = np.linalg.norm(proj - pts, axis=1)
            solutions.append((float(np.sqrt(np.mean(errors**2))), r, t, errors))
        if not solutions:
            records.append({"hold_id": h["hold_id"], "status": "pose_failed"})
            continue
        rms, r, t, errors = min(solutions, key=lambda x: x[0])
        records.append(
            {
                "hold_id": h["hold_id"],
                "capture": p.parent.name,
                "frame": f["file"],
                "sha256": f["sha256"],
                "rms_px": rms,
                "corner_errors_px": errors.tolist(),
                "tag_translation_camera_m": t.reshape(3).tolist(),
                "status": "pose_estimated",
            }
        )
report = {
    "kind": "held_out_corner_reprojection_check",
    "intrinsics_fixed": True,
    "intrinsics_sha256": hashlib.sha256(
        (ROOT / "initial-pinhole-candidate.json").read_bytes()
    ).hexdigest(),
    "hold_count": len(records),
    "planned_hold_count": 5,
    "coverage_complete": len(records) >= 5,
    "method": (
        "Fix intrinsics/distortion from 20 frozen fitting holds; solve each "
        "separate tag pose with IPPE_SQUARE then refine extrinsics only; report "
        "Euclidean per-corner residual. No independent physical pose ground "
        "truth."
    ),
    "views": records,
    "limitations": [
        "Only 3 of 5 planned independent validation holds acquired.",
        "Three central/front/side views do not cover all pitch directions or image edges.",
        (
            "Four corners supply limited residual degrees of freedom; low "
            "reprojection error alone does not demonstrate metric accuracy."
        ),
        "Independent horizontal/vertical FOV bounds and qualified DJI provenance remain missing.",
    ],
}
if all("rms_px" in x for x in records) and records:
    report["rms_px"] = float(np.sqrt(np.mean([x["rms_px"] ** 2 for x in records])))
    report["max_view_rms_px"] = max(x["rms_px"] for x in records)
(ROOT / "held-out-results.json").write_text(json.dumps(report, indent=2))
print(
    json.dumps(
        {
            k: report.get(k)
            for k in ["hold_count", "coverage_complete", "rms_px", "max_view_rms_px"]
        },
        indent=2,
    ),
    flush=True,
)
# Leave-one-hold-out fits are stability diagnostics only, each using 19 of the 20 fit views.
frozen = json.loads((ROOT / "fitting-holds-frozen.json").read_text())
pixels = [
    np.array(h["frame"]["corners_px"][h["frame"]["tag_ids"].index(54)], np.float32)
    for h in frozen["holds"]
]
objs = [obj.astype(np.float32) for _ in pixels]
loo = []
for i, h in enumerate(frozen["holds"]):
    try:
        rms, k, d, _, _ = cv2.calibrateCamera(
            objs[:i] + objs[i + 1 :], pixels[:i] + pixels[i + 1 :], (1280, 720), None, None
        )
        loo.append(
            {
                "excluded_hold": h["hold_id"],
                "rms_px": float(rms),
                "fx": float(k[0, 0]),
                "fy": float(k[1, 1]),
                "relative_focal_change": [
                    float(abs(k[0, 0] / K[0, 0] - 1)),
                    float(abs(k[1, 1] / K[1, 1] - 1)),
                ],
            }
        )
    except cv2.error:
        loo.append({"excluded_hold": h["hold_id"], "error": "fit_failed"})
stability = {
    "kind": "leave_one_hold_out_diagnostic",
    "training_holds": 20,
    "views_per_refit": 19,
    "note": (
        "Sensitivity check only; 19-view refits do not satisfy the 20-view "
        "acceptance minimum. Validation frames never used."
    ),
    "results": loo,
}
if all("relative_focal_change" in x for x in loo):
    stability["maximum_relative_focal_change"] = np.max(
        [x["relative_focal_change"] for x in loo], axis=0
    ).tolist()
(ROOT / "fit-stability.json").write_text(json.dumps(stability, indent=2))
print(
    "Maximum leave-one-out focal change:",
    stability.get("maximum_relative_focal_change"),
    flush=True,
)
