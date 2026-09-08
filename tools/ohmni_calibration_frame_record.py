"""Write a raw AprilTag calibration frame record beside a retained PNG."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from time import monotonic_ns

import cv2


def record(image_path: Path, output: Path, frame_index: int, boot_id: str) -> None:
    image = cv2.imread(str(image_path))
    if image is None or frame_index < 0 or not boot_id:
        raise ValueError("image, nonnegative frame index, and boot ID are required")
    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_APRILTAG_36h11)
    corners, identifiers, _ = cv2.aruco.ArucoDetector(dictionary, cv2.aruco.DetectorParameters()).detectMarkers(image)
    payload = {
        "frame_index": frame_index,
        "captured_monotonic_ns": monotonic_ns(),
        "boot_id": boot_id,
        "shape_px": [image.shape[1], image.shape[0]],
        "image_file": image_path.name,
        "image_sha256": hashlib.sha256(image_path.read_bytes()).hexdigest(),
        "tag_ids": [] if identifiers is None else identifiers.reshape(-1).tolist(),
        "corners_px": [] if identifiers is None else [item.reshape(4, 2).tolist() for item in corners],
    }
    output.write_text(json.dumps(payload, sort_keys=True) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--frame-index", type=int, required=True)
    parser.add_argument("--boot-id", required=True)
    args = parser.parse_args()
    record(args.image, args.output, args.frame_index, args.boot_id)


if __name__ == "__main__":
    main()
