from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np

from tools.world_bundle import _validate_obstacles

DEPLOYMENT = Path("deployments/real-navigation")
APPROVAL = DEPLOYMENT / "flight-map-approval-20260909.json"
INPUTS = {
    "tags": DEPLOYMENT / "tag-map-53.json",
    "geometry": DEPLOYMENT / "entrance-wall-geometry.json",
    "obstacles": DEPLOYMENT / "entrance-obstacles.yaml",
    "availability": DEPLOYMENT / "tag-availability-20260909.json",
}


def _read(path: Path) -> tuple[dict, str]:
    payload = path.read_bytes()
    return json.loads(payload), hashlib.sha256(payload).hexdigest()


def transform_obstacles(document: dict, transform: dict, source_frame: str) -> dict:
    """Apply a supplied Z-up rigid transform; this does not verify physical registration."""
    if (
        not isinstance(transform, dict)
        or set(transform)
        != {"sourceFrame", "targetFrame", "transformId", "T_world_source", "evidence"}
        or transform["sourceFrame"] != source_frame
        or transform["targetFrame"] != "world"
        or any(
            not isinstance(transform[key], str) or not transform[key].strip()
            for key in ("transformId", "evidence")
        )
    ):
        raise ValueError("world transform must name the source frame, world target and evidence")
    raw = transform["T_world_source"]
    if (
        not isinstance(raw, list)
        or len(raw) != 4
        or any(
            not isinstance(row, list)
            or len(row) != 4
            or any(type(value) not in (int, float) for value in row)
            for row in raw
        )
    ):
        raise ValueError("T_world_source must be a numeric 4x4 rigid planar transform")
    matrix = np.asarray(raw, dtype=float)
    if (
        not np.isfinite(matrix).all()
        or not np.allclose(matrix[3], [0, 0, 0, 1], atol=1e-9, rtol=0)
        or not np.allclose(matrix[:3, 2], [0, 0, 1], atol=1e-9, rtol=0)
        or not np.allclose(matrix[:3, :3].T @ matrix[:3, :3], np.eye(3), atol=1e-9, rtol=0)
        or not np.isclose(np.linalg.det(matrix[:3, :3]), 1, atol=1e-9, rtol=0)
    ):
        raise ValueError("T_world_source must be a finite rigid planar Z-up transform")
    if document["frame"] != source_frame:
        raise ValueError("obstacle source frame differs from the transform")
    result = {**document, "frame": "world"}
    for key in ("obstacles", "no_fly"):
        result[key] = [
            {
                **obstacle,
                "polygon": [(matrix @ [x, y, 0, 1])[:2].tolist() for x, y in obstacle["polygon"]],
                "z_min_m": obstacle["z_min_m"] + float(matrix[2, 3]),
                "z_max_m": obstacle["z_max_m"] + float(matrix[2, 3]),
            }
            for obstacle in document[key]
        ]
    _validate_obstacles(result)
    return result


def prepare_inputs(root: Path, *, world_transform: Path | None = None) -> dict[str, dict]:
    """Verify the approved bytes and return preparation documents without activating navigation."""
    root = root.resolve()
    approval, approval_digest = _read(root / APPROVAL)
    if (
        approval.get("schemaVersion") != 1
        or approval.get("scope") != "static_map_inputs"
        or approval.get("ownerApproved") is not True
        or not isinstance(approval.get("inputs"), list)
    ):
        raise ValueError("static map inputs need the owner's approval receipt")
    documents, hashes = {}, {}
    for item in approval["inputs"]:
        if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
            raise ValueError("approved inputs require a path and SHA256")
        path = Path(item["path"])
        if path.is_absolute() or not (root / path).resolve().is_relative_to(root):
            raise ValueError("approved input must remain inside the source repository")
        name = path.as_posix()
        if name in documents:
            raise ValueError("approved input paths must be unique")
        document, digest = _read(root / path)
        if digest != item["sha256"]:
            raise ValueError(f"approved input hash mismatch: {name}")
        documents[name], hashes[name] = document, digest
    if not {path.as_posix() for path in INPUTS.values()} <= documents.keys():
        raise ValueError("approval must bind tags, geometry, obstacles and tag availability")
    source = documents[INPUTS["tags"].as_posix()]
    geometry = documents[INPUTS["geometry"].as_posix()]
    obstacles = documents[INPUTS["obstacles"].as_posix()]
    availability = documents[INPUTS["availability"].as_posix()]
    source_frame = source["coordinate_frame"]["id"]
    if (
        approval.get("coordinateFrame") != source_frame
        or geometry["coordinateFrame"] != source_frame
        or geometry["tagMapSha256"] != hashes[INPUTS["tags"].as_posix()]
        or availability["sourceSha256"] != hashes[INPUTS["tags"].as_posix()]
        or geometry["obstacleDocument"] != obstacles
    ):
        raise ValueError("approved walls must bind the same tag map, frame and obstacle document")
    _validate_obstacles(obstacles)
    unavailable = {item["tagId"] for item in availability["unavailableTags"]}
    candidate_tag_ids = sorted(tag["id"] for tag in source["tags"] if tag["id"] not in unavailable)
    source_obstacles = {**obstacles, "frame": source_frame}
    registration = {"status": "missing", "sourceFrame": source_frame, "targetFrame": "world"}
    pending = [
        "world_bundle_registration_and_datum_validation",
        "measured_route_and_free_volume_clearances",
        "camera_visibility_calibration",
        "independent_tape_and_held_out_checkpoint_evidence",
        "control_localization_and_aircraft_bindings",
        "generated_navigation_geometry",
        "fresh_session_navigation_signature",
    ]
    outputs = {}
    if world_transform is not None:
        transform, digest = _read(world_transform)
        outputs["obstacles.yaml"] = transform_obstacles(source_obstacles, transform, source_frame)
        registration = {
            **registration,
            "status": "supplied_pending_bundle_validation",
            "transformSha256": digest,
            "transform": transform,
        }
    else:
        pending.insert(0, "T_world_source")
    common = {
        "schemaVersion": 1,
        "coordinateFrame": source_frame,
        "approval": {"path": APPROVAL.as_posix(), "sha256": approval_digest},
        "worldRegistration": registration,
        "flightAuthorized": False,
    }
    outputs["flight-wall-inputs.json"] = {
        **common,
        "kind": "flight_wall_inputs",
        "status": "prepared_pending_calibration_and_registration",
        "sourceFrame": source["coordinate_frame"],
        "inputs": approval["inputs"],
        "obstacleDocument": source_obstacles,
        "heightLimits": geometry["heightLimits"],
        "extentModel": geometry["extentModel"],
        "unavailableTags": availability["unavailableTags"],
        "localizationCandidateTagIds": candidate_tag_ids,
        "calibrationStatus": approval.get("calibrationStatus", "pending"),
        "pendingInputs": pending,
    }
    outputs["console-wall-features.json"] = {
        **common,
        "kind": "console_wall_features",
        "features": [
            {
                "id": obstacle["id"],
                "kind": "obstacle",
                "name": obstacle["id"].replace("-", " "),
                "aliases": [],
                "points": [{"x": x, "y": y} for x, y in obstacle["polygon"]],
                "widthM": None,
                "flightHeightM": None,
                "heightToleranceM": None,
                "heightEvidence": "",
            }
            for obstacle in source_obstacles["obstacles"]
        ],
    }
    return outputs


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare approved wall inputs without activation.")
    parser.add_argument("output", type=Path, help="new output directory")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--world-transform", type=Path, help="explicit T_world_source JSON record")
    args = parser.parse_args()
    outputs = prepare_inputs(args.root, world_transform=args.world_transform)
    args.output.mkdir(parents=True, exist_ok=False)
    for name, document in outputs.items():
        (args.output / name).write_text(json.dumps(document, indent=2, allow_nan=False) + "\n")
    print(json.dumps({"status": "prepared", "flightAuthorized": False, "files": list(outputs)}))


if __name__ == "__main__":
    main()
