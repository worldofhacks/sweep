"""Estimate one recorded frame with externally pinned camera and map artifacts."""

import argparse
import json

import cv2

from perception.tag_localization import TagLocalizer
from tools.map_common import read_document


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", help="JSON with map, camera, extrinsic and timing inputs")
    parser.add_argument("image")
    args = parser.parse_args()
    try:
        config = read_document(args.config)
        localizer = TagLocalizer(**config["localizer"])
        report = localizer.estimate(cv2.imread(args.image), **config["timing"])
    except (ValueError, KeyError, TypeError, OSError, cv2.error) as exc:
        report = {"accepted": False, "flight_approved": False, "reason": str(exc)}
    print(json.dumps(report, allow_nan=False))
    return 0 if report["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
