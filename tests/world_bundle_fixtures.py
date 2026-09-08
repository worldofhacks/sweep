"""Synthetic map evidence used only by isolated software tests."""

import base64
import hashlib

import cv2
import numpy as np


def fixture_world_draft():
    _, encoded = cv2.imencode(".png", np.full((100, 100), 127, dtype=np.uint8))
    payload = encoded.tobytes()

    def feature(identifier, kind, points, **extra):
        return {
            "id": identifier,
            "kind": kind,
            "name": identifier,
            "aliases": [],
            "points": [{"x": x, "y": y} for x, y in points],
            "widthM": None,
            "flightHeightM": None,
            "heightToleranceM": None,
            "heightEvidence": "",
            **extra,
        }

    return {
        "format": "sweep-map-draft-v1",
        "metadata": {
            "mapVersion": "fixture-v1",
            "floorId": "level-1",
            "frame": "world",
            "resolutionM": 0.1,
            "originXM": 0,
            "originYM": 0,
            "units": "m",
            "createdAt": 1000,
            "creationEvidence": "synthetic software fixture",
            "registration": {
                "sourceFrame": "fixture-slam",
                "transformId": "fixture-transform",
                "residualM": 0.02,
                "thresholdM": 0.1,
                "evidence": "synthetic registration vectors",
            },
        },
        "image": {
            "name": "synthetic.png",
            "width": 100,
            "height": 100,
            "dataUrl": "data:image/png;base64," + base64.b64encode(payload).decode(),
            "sha256": hashlib.sha256(payload).hexdigest(),
        },
        "features": [
            feature("boundary", "geofence", [(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)]),
            feature("lobby", "zone", [(2, 2), (4, 2), (4, 4), (2, 4), (2, 2)], aliases=["entry"]),
            feature(
                "hall",
                "corridor",
                [(3, 4.5), (3, 6), (3, 7)],
                widthM=1,
                flightHeightM=1.5,
                heightToleranceM=0.1,
                heightEvidence="synthetic per-segment tape evidence",
            ),
            feature("glass", "obstacle", [(8, 8), (9, 8), (9, 9), (8, 9), (8, 8)]),
        ],
        "tags": [
            {
                "id": "tag-zero",
                "tagId": 0,
                "family": "tag36h11",
                "sizeM": 0.2,
                "position": {"x": 1, "y": 1},
                "heightM": 1.2,
                "yawRad": 0,
                "source": "measured",
                "confidence": 0.95,
                "observations": ["fixture-observation"],
                "usedForFlight": True,
                "tapeVerified": True,
                "tapeEvidence": "synthetic tape check, not physical acceptance",
            }
        ],
    }
