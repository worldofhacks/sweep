import hashlib
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).parent
REPO = ROOT.parents[2]
sys.path.insert(0, str(REPO))
from calibration.tag_intrinsics import TagCandidateRequest, calibrate_tag_candidate  # noqa: E402

frozen = json.loads((ROOT / "fitting-holds-frozen.json").read_text())
pipeline = json.loads((ROOT / "pipeline.json").read_text())
frames = []
offset = 0
offsets = {}
for c in sorted(ROOT.glob("capture-*")):
    if not c.is_dir():
        continue
    offsets[c.name] = offset
    m = c / "capture-manifest.json"
    if m.exists():
        offset += json.loads(m.read_text())["stats"]["decoded_frames"]
    else:
        st = c / "status.json"
        if st.exists():
            offset += json.loads(st.read_text())["decoded_frames"]
for h in frozen["holds"]:
    f = h["frame"]
    source = ROOT / h["capture"] / "frames" / f["file"]
    assert hashlib.sha256(source.read_bytes()).hexdigest() == f["sha256"]
    frames.append(
        {
            **f,
            "frame_index": offsets[h["capture"]] + f["frame_index"],
            "source_capture": h["capture"],
            "source_frame_index": f["frame_index"],
            "source_image": str(source.relative_to(ROOT)),
            "hold_id": h["hold_id"],
        }
    )
evidence = ROOT / "fitting-evidence.json"
evidence.write_text(
    json.dumps(
        {
            "kind": "recorded_dji_diagnostic_corner_evidence",
            "frame_index_definition": (
                "Cumulative decoded frame index across receive segments; original "
                "per-segment indices retained. One selected image per distinct operator "
                "hold."
            ),
            "frames": frames,
        },
        indent=2,
    )
)
report = calibrate_tag_candidate(
    TagCandidateRequest(
        evidence=evidence,
        tag_size_m=0.199898,
        pipeline=pipeline,
        minimum_frame_gap=8,
        maximum_views=30,
        model="pinhole",
    )
)
report["source_evidence"] = "fitting-evidence.json"
report["hold_selection"] = (
    "20 frozen independent operator holds; excludes pre-flat and occluded "
    "attempts, and all future validation holds"
)
(ROOT / "initial-pinhole-candidate.json").write_text(json.dumps(report, indent=2))
print(
    json.dumps(
        {
            k: report.get(k)
            for k in [
                "status",
                "selected_observation_count",
                "pose_constraint_ratio",
                "rms_reprojection_error_px",
                "relative_focal_stddev",
                "pinhole_fov_deg",
                "rejection_reasons",
            ]
        },
        indent=2,
    )
)
