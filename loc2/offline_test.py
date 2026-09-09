"""Offline evidence for task 6: real detection counts and independent-tag position agreement.

Two independent checks against real DJI Mini 3 frames of the actual venue tags:

1. Run perception.tag_localization.TagLocalizer.estimate() -- the real production
   detector and pose solver -- against the 23 committed fitting/held-out PNGs, using
   the calibration and localizer bundle this task built. Reports tags-detected-per-frame.
   These are single-tag calibration-hold close-ups, not nadir floor shots, so multi-tag
   consensus does not apply here -- see the hallway check below for that.
2. Recompute pairwise disagreement between independent single-tag camera-position
   solves from the existing hallway walk evidence
   (capture-20260909T043401Z/hallway-geometry-frames.jsonl), which does have real
   multi-tag frames from a walk under the venue's floor tags. No raw hallway PNGs are
   committed to the repo, so this reuses the already-detected corners on record rather
   than re-detecting; it is not test 1's synthetic path, it is real corner data.
"""
import json
import sys
from itertools import combinations
from pathlib import Path

REPO = Path("/home/gauntlet/sweep-agents/opus5-loc-commission")
SCRATCH = Path(
    "/var/tmp/gauntlet/claude-1001/-home-gauntlet-sweep/478acf4f-e28a-4c47-a655-ea55363dff37"
    "/scratchpad/loc2"
)
sys.path.insert(0, str(REPO))

import cv2
import numpy as np

from perception.tag_localization import TagLocalizer


def run_localizer_on_real_frames():
    accepted = json.loads((SCRATCH / "localizer-bundle-accepted-versions.json").read_text())
    calib_path = SCRATCH / "dji-mini3-real-navigation-calibration.json"
    calib_sha = __import__("hashlib").sha256(calib_path.read_bytes()).hexdigest()
    pipeline = json.loads(calib_path.read_text())["pipeline"]
    localizer = TagLocalizer(
        bundle=str(SCRATCH / "localizer-bundle"),
        accepted_versions=accepted,
        calibration_path=str(calib_path),
        calibration_sha256=calib_sha,
        camera_serial="dji-mini3-real-navigation-venue-unverified",
        pipeline=pipeline,
        T_body_camera=[[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]],
    )
    frames_dir = SCRATCH / "frames_staged"
    results = []
    for path in sorted(frames_dir.iterdir()):
        image = cv2.imread(str(path))
        if image is None:
            continue
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        report = localizer.estimate(gray, capture_time=0.0, decode_time=0.0, now=0.0, max_age=1.0)
        results.append((path.name, report))
    return results


def hallway_agreement():
    path = REPO / "docs/evidence/dji-calibration-20260909/capture-20260909T043401Z/hallway-geometry-frames.jsonl"
    frames = [json.loads(line) for line in path.read_text().splitlines()]
    tag_count_hist: dict[int, int] = {}
    pair_distances = []
    for frame in frames:
        views = [v for v in frame.get("single_tag_geometry", []) if v.get("camera_position_m") is not None]
        tag_count_hist[len(views)] = tag_count_hist.get(len(views), 0) + 1
        for a, b in combinations(views, 2):
            pa = np.array(a["camera_position_m"])
            pb = np.array(b["camera_position_m"])
            pair_distances.append(float(np.linalg.norm(pa - pb)))
    return len(frames), tag_count_hist, pair_distances


def main():
    print("=== 1. TagLocalizer.estimate() on the 23 real committed PNGs ===")
    results = run_localizer_on_real_frames()
    detected_counts = []
    for name, report in results:
        n = len(report.get("tag_ids", []))
        detected_counts.append(n)
        print(f"{name}: accepted={report['accepted']} reason={report.get('reason')} tags={report.get('tag_ids')}")
    print(f"frames: {len(results)}, mean tags/frame: {np.mean(detected_counts):.2f}, "
          f"min: {min(detected_counts)}, max: {max(detected_counts)}")

    print()
    print("=== 2. Hallway walk: independent single-tag camera-position agreement ===")
    n_frames, hist, distances = hallway_agreement()
    print(f"hallway frames: {n_frames}")
    print(f"tags-solved-per-frame histogram: {hist}")
    if distances:
        arr = np.array(distances)
        print(f"pairwise same-frame position disagreements (m), n={len(arr)}:")
        print(f"  median: {np.median(arr):.4f}  mean: {arr.mean():.4f}  "
              f"p90: {np.percentile(arr,90):.4f}  max: {arr.max():.4f}")
    else:
        print("no multi-tag frames with two accepted single-tag solves")


if __name__ == "__main__":
    main()
