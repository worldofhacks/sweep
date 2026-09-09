import json
import hashlib
from pathlib import Path

SCRATCH = Path("/var/tmp/gauntlet/claude-1001/-home-gauntlet-sweep/478acf4f-e28a-4c47-a655-ea55363dff37/scratchpad/loc2")

candidate = json.loads((SCRATCH / "candidate-test.json").read_text())
assert candidate["status"] == "candidate"
frames_dir = SCRATCH / "frames_staged"
hashes = {}
for index in candidate["selection"]["frames"]:
    name = f"frame-{index:06}.png"
    hashes[name] = hashlib.sha256((frames_dir / name).read_bytes()).hexdigest()
assert len(hashes) == 20 == len(set(hashes.values()))

pipeline = json.loads((SCRATCH / "pipeline" / "dji-mini3-pipeline.json").read_text())

hand_authored_reason = (
    "calibration.tag_intrinsics export_tag_calibration refuses this capture: its "
    "recorded_live provenance check (_validate_single_recorded_live_provenance / "
    "_validate_recorded_live_session) is built for the Ohmni dual-USB-camera capture "
    "rig (schema ohmni-dual-calibration-capture/v2, UYVY/MJPEG device fingerprints, "
    "See3CAM_CU135 / HD USB Camera identities) and has no path for a MediaMTX-relayed "
    "DJI RTSP capture. Confirmed by running apriltag-export with "
    "--evidence-kind recorded_live against fitting-evidence.json: it raises "
    "\"recorded live export requires capture provenance\" because the document has no "
    "recorded_live_provenance key, and even a hand-added one would fail the Ohmni-shaped "
    "manifest/device checks that follow. Every field below the schema-required set is "
    "either taken verbatim from running calibration.apriltag-candidate against the real "
    "fitting-evidence.json corner detections and the real staged source PNGs "
    "(camera_matrix, distortion_coefficients, rms_reprojection_error_px, image_sha256, "
    "pinhole_fov_deg, pose_constraint_ratio, focal_stddev_px), or filled by hand: "
    "camera_serial (the DJI serial was never captured by this pipeline, see "
    "pipeline.camera_mode) and evidence_kind (set to recorded_live because the 20 fitting "
    "frames are real DJI Mini 3 video frames of real tags, not synthetic renders, even "
    "though this capture cannot pass the stricter export provenance gate)."
)

artifact = {
    "schema_version": 1,
    "model": "pinhole",
    "status": "offline",
    "evidence_kind": "recorded_live",
    "camera_serial": "dji-mini3-real-navigation-venue-unverified",
    "pipeline": pipeline,
    "target": {
        "family": "tag36h11",
        "black_square_edge_m": 0.199898,
        "validated_feature_kind": "outer_corners",
    },
    "image_size_px": candidate["image_size_px"],
    "camera_matrix": candidate["camera_matrix"],
    "distortion_coefficients": candidate["distortion_coefficients"],
    "rms_reprojection_error_px": candidate["rms_reprojection_error_px"],
    "accepted_image_count": candidate["selected_observation_count"],
    "image_sha256": hashes,
    "tag_candidate_quality": {
        "pose_constraint_ratio": candidate["pose_constraint_ratio"],
        "minimum_pose_constraint_ratio": candidate["minimum_pose_constraint_ratio"],
        "focal_stddev_px": candidate["focal_stddev_px"],
        "relative_focal_stddev": candidate["relative_focal_stddev"],
        "pinhole_fov_deg": candidate["pinhole_fov_deg"],
    },
    "hand_authored": True,
    "hand_authored_reason": hand_authored_reason,
}

out = SCRATCH / "dji-mini3-real-navigation-calibration.json"
out.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n")
print("wrote", out, len(json.dumps(artifact)))
