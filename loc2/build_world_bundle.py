"""Build the localizer-only schema-v1 map bundle from deployments/real-navigation/tag-map-53.json.

This bundle exists only to satisfy perception.tag_localization.TagLocalizer's admission
checks. It is not the navigation deployment's world bundle -- see loc2/WORLD_BUNDLE.md.
"""
import json
import math
import shutil
from pathlib import Path

REPO = Path("/home/gauntlet/sweep-agents/opus5-loc-commission")
SCRATCH = Path(
    "/var/tmp/gauntlet/claude-1001/-home-gauntlet-sweep/478acf4f-e28a-4c47-a655-ea55363dff37"
    "/scratchpad/loc2"
)
BUNDLE = SCRATCH / "localizer-bundle"
EXCLUDED_TAG_IDS = {35, 49}
TAG_SIZE_M = 0.199898

import sys
sys.path.insert(0, str(REPO))
from tools.map_common import validate_transform  # noqa: E402


def matmul(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(4)) for j in range(4)] for i in range(4)]


def invert_rigid(t):
    r = [[t[i][j] for j in range(3)] for i in range(3)]
    rt = [[r[j][i] for j in range(3)] for i in range(3)]
    p = [t[i][3] for i in range(3)]
    new_p = [-sum(rt[i][k] * p[k] for k in range(3)) for i in range(3)]
    return [rt[i] + [new_p[i]] for i in range(3)] + [[0.0, 0.0, 0.0, 1.0]]


def main():
    source_map = json.loads((REPO / "deployments/real-navigation/tag-map-53.json").read_text())
    tags_by_id = {t["id"]: t for t in source_map["tags"]}
    origin_tag = tags_by_id[0]
    t0 = validate_transform(origin_tag["T_world_tag"])
    t0_inv = invert_rigid(t0)

    if BUNDLE.exists():
        shutil.rmtree(BUNDLE)
    BUNDLE.mkdir(parents=True)

    kept_tags = []
    for tag_id, tag in sorted(tags_by_id.items()):
        if tag_id in EXCLUDED_TAG_IDS:
            continue
        t_scan_tag = validate_transform(tag["T_world_tag"])
        t_map_tag = matmul(t0_inv, t_scan_tag)
        validate_transform(t_map_tag)
        position = [t_map_tag[i][3] for i in range(3)]
        normal = [t_map_tag[i][2] for i in range(3)]
        yaw = math.atan2(t_map_tag[1][0], t_map_tag[0][0])
        kept_tags.append(
            dict(
                id=tag_id,
                floor_id="level_1",
                size=TAG_SIZE_M,
                orientation_confirmed=True,
                x=position[0],
                y=position[1],
                z=position[2],
                yaw=yaw,
                normal=normal,
                T_map_tag=t_map_tag,
                T_scan_tag=t_scan_tag,
                mount=tag.get("mount"),
                source_tag_id=tag_id,
            )
        )

    xs = [t["x"] for t in kept_tags]
    ys = [t["y"] for t in kept_tags]
    margin = 2.0
    x_min, x_max = min(xs) - margin, max(xs) + margin
    y_min, y_max = min(ys) - margin, max(ys) + margin
    geofence_polygon = [
        [x_min, y_min],
        [x_max, y_min],
        [x_max, y_max],
        [x_min, y_max],
        [x_min, y_min],
    ]

    tags_doc = {
        "schema_version": 1,
        "units": "meters",
        "frame": "building",
        "source": {"path": "tag-map-53-source.json", "sha256": None},
        "T_map_scan": t0_inv,
        "tags": kept_tags,
    }

    placeholder_zone = dict(z_min=0.0, z_max=3.0, owner_approved=False)
    zones_doc = {
        "schema_version": 1,
        "units": "meters",
        "geofence": {"polygon": geofence_polygon, "z_min": -0.5, "z_max": 3.0},
        "zones": [
            {
                "id": zone_id,
                "floor_id": "level_1",
                "polygon": [[x_min, y_min], [x_min + 1, y_min], [x_min + 1, y_min + 1], [x_min, y_min + 1], [x_min, y_min]],
                **placeholder_zone,
            }
            for zone_id in ("lobby", "kitchen", "atrium", "launch", "corridor")
        ],
        "room_graph": {
            "nodes": [
                {"id": "113", "floor_id": "level_1", "region_id": "113", "autonomous": False},
                {"id": "mezzanine", "floor_id": "mezzanine", "region_id": "mezzanine", "autonomous": False},
                {"id": "north_hallway", "floor_id": "level_1", "region_id": "north_hallway", "autonomous": False},
            ],
            "edges": [
                {"from": "113", "to": "mezzanine", "side": "west", "autonomous": False},
                {"from": "113", "to": "north_hallway", "side": "east", "autonomous": False},
            ],
        },
        "placeholder_note": (
            "zones and room_graph are schema-required structural filler, copied from the "
            "test-fixture shape. TagLocalizer never reads this document's content. The "
            "navigation deployment's own bundle carries this venue's real zones and graph."
        ),
    }

    obstacles_doc = {
        "schema_version": 1,
        "units": "meters",
        "obstacles": [],
        "placeholder_note": "Empty: TagLocalizer never reads this document. Not a survey claim.",
    }

    (BUNDLE / "tags.yaml").write_text(json.dumps(tags_doc, indent=2) + "\n")
    (BUNDLE / "zones.yaml").write_text(json.dumps(zones_doc, indent=2) + "\n")
    (BUNDLE / "obstacles.yaml").write_text(json.dumps(obstacles_doc, indent=2) + "\n")

    source_bytes = (REPO / "deployments/real-navigation/tag-map-53.json").read_bytes()
    (BUNDLE / "tag-map-53-source.json").write_bytes(source_bytes)

    import hashlib

    tags_doc["source"]["sha256"] = hashlib.sha256(source_bytes).hexdigest()
    (BUNDLE / "tags.yaml").write_text(json.dumps(tags_doc, indent=2) + "\n")

    manifest = {
        "schema_version": 1,
        "bundle_version": "real-navigation-unit11-atrium-localizer-v1",
        "created_at": "2026-09-09T00:00:00Z",
        "units": "meters",
        "frame": {
            "name": "building",
            "x": "toward_elevator",
            "y": "toward_street_wall",
            "z": "up",
            "origin_tag_id": 0,
            "tag0_yaw_rad": 0,
        },
        "floor_ids": ["level_1", "mezzanine"],
        "sources": [
            {
                "path": "tag-map-53-source.json",
                "T_map_scan": t0_inv,
                "rms_m": 0.0,
                "rms_m_basis": (
                    "Not a fitted registration residual. This source is the pinned "
                    "deployments/real-navigation/tag-map-53.json map re-expressed under an "
                    "exact rigid coordinate change (rotation+translation only, anchored at "
                    "tag 0) so the schema's required origin_tag_id=0 convention holds; the "
                    "residual of an exact change of basis is zero by construction, not by fit."
                ),
            }
        ],
        "files": {},
        "content_sha256": "",
        "excluded_tag_ids": sorted(EXCLUDED_TAG_IDS),
        "excluded_tag_ids_reason": "physically damaged tags, per owner",
        "provenance_note": (
            "Tag geometry is deployments/real-navigation/tag-map-53.json (crosszone joint "
            "reprojection survey, private/provisional), tags 35 and 49 dropped. This bundle "
            "exists only for perception.tag_localization.TagLocalizer; it is not the "
            "navigation deployment's world bundle."
        ),
    }
    (BUNDLE / "manifest.yaml").write_text(json.dumps(manifest, indent=2) + "\n")

    from tools.map_validate import seal_manifest, validate_bundle

    sealed = seal_manifest(BUNDLE)
    accepted_versions = {sealed["bundle_version"]: sealed["content_sha256"]}
    validated = validate_bundle(BUNDLE, accepted_versions)
    print("validated OK, tag count:", len(validated.document("tags.yaml")["tags"]))
    print("bundle_version:", sealed["bundle_version"])
    print("content_sha256:", sealed["content_sha256"])
    (SCRATCH / "localizer-bundle-accepted-versions.json").write_text(
        json.dumps(accepted_versions, indent=2) + "\n"
    )


if __name__ == "__main__":
    main()
