import base64
import copy
import hashlib
import json
import struct

import pytest

from tests.world_bundle_fixtures import fixture_world_draft
from tools.world_bundle import (
    BundleError,
    build_world_bundle,
    canonical_json,
    content_hash,
    validate_draft,
    validate_world_bundle,
    world_bundle_hash,
)


def test_canonical_world_bundle_is_deterministic_and_round_trips():
    draft = fixture_world_draft()
    assert validate_draft(draft) == []
    bundle = build_world_bundle(draft)
    assert validate_world_bundle(bundle) == []
    assert canonical_json(build_world_bundle(json.loads(canonical_json(draft)))) == canonical_json(
        bundle
    )
    assert bundle["manifest"]["frame"] == "world"
    assert bundle["tags"][0]["verifiedForFlight"] is True
    assert len(bundle["corridors"][0]["segments"]) == 2
    assert bundle["derivedArtifacts"] == []
    assert "flightHeightM" not in bundle["manifest"]["image"]


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("metadata", "frame"), "building"),
        (("metadata", "frame"), "unknown"),
        (("metadata", "units"), "feet"),
        (("metadata", "mapVersion"), ""),
        (("metadata", "mapVersion"), "map version with spaces"),
        (("metadata", "floorId"), ""),
        (("metadata", "createdAt"), True),
        (("metadata", "createdAt"), -1),
        (("metadata", "creationEvidence"), ""),
        (("metadata", "registration", "residualM"), 0.11),
        (("metadata", "registration", "residualM"), -0.1),
        (("metadata", "registration", "thresholdM"), 0),
        (("metadata", "registration", "transformId"), ""),
        (("metadata", "registration", "evidence"), ""),
        (("metadata", "resolutionM"), 0),
        (("metadata", "originXM"), float("nan")),
        (("tags", 0, "source"), "guessed"),
        (("tags", 0, "confidence"), 1.1),
        (("tags", 0, "tagId"), True),
        (("tags", 0, "yawRad"), None),
        (("tags", 0, "tapeVerified"), False),
        (("tags", 0, "tapeEvidence"), ""),
        (("tags", 0, "observations"), []),
        (("features", 2, "heightEvidence"), ""),
        (("features", 2, "heightToleranceM"), 2),
        (("features", 2, "widthM"), 20),
        (("features", 1, "name"), "n" * 129),
        (("features", 1, "name"), "😀" * 65),
        (("features", 1, "aliases"), [f"alias-{i}" for i in range(17)]),
        (("features", 1, "aliases"), ["Front Hall", "Front  Hall"]),
    ],
)
def test_every_evidence_refusal_is_fail_closed(path, value):
    draft = fixture_world_draft()
    target = draft
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    assert validate_draft(draft)
    with pytest.raises(BundleError):
        build_world_bundle(draft)


def test_old_drafts_are_invalid_without_required_world_evidence():
    draft = fixture_world_draft()
    for key in ("units", "createdAt", "creationEvidence", "registration"):
        draft["metadata"].pop(key)
    assert validate_draft(draft)[0]["path"] == "metadata"


def test_duplicate_tag_identity_and_object_ids_refuse():
    draft = fixture_world_draft()
    other = copy.deepcopy(draft["tags"][0])
    other["id"] = "another-object"
    draft["tags"].append(other)
    assert any("duplicate tag id" in i["message"] for i in validate_draft(draft))
    other["tagId"] = 1
    other["id"] = "lobby"
    assert any("duplicate object id" in i["message"] for i in validate_draft(draft))


@pytest.mark.parametrize(
    "points",
    [
        [(2, 2), (4, 2), (4, 4), (2, 4)],
        [(2, 2), (4, 4), (4, 2), (2, 4), (2, 2)],
        [(2, 2), (4, 2), (3, 2), (2, 2)],
        [(2, 2), (4, 2), (4, 2), (2, 4), (2, 2)],
        [(2, 2), (14, 2), (14, 4), (2, 4), (2, 2)],
    ],
)
def test_bad_polygon_and_extent_refuse(points):
    draft = fixture_world_draft()
    draft["features"][1]["points"] = [{"x": x, "y": y} for x, y in points]
    assert validate_draft(draft)


def test_concave_boundary_crossing_and_corridor_width_refuse():
    draft = fixture_world_draft()
    draft["features"][0]["points"] = [
        {"x": x, "y": y}
        for x, y in [(0, 0), (10, 0), (10, 10), (6, 10), (6, 5), (4, 5), (4, 10), (0, 10), (0, 0)]
    ]
    draft["features"][2]["points"] = [{"x": 3, "y": 7}, {"x": 7, "y": 7}]
    assert any("leaves the geofence" in i["message"] for i in validate_draft(draft))
    draft["features"][2]["points"] = [{"x": 3.8, "y": 6}, {"x": 3.8, "y": 7}]
    assert any("leaves the geofence" in i["message"] for i in validate_draft(draft))


@pytest.mark.parametrize("kind", ["obstacle", "no_fly"])
def test_zone_and_corridor_must_avoid_static_forbidden_geometry(kind):
    draft = fixture_world_draft()
    draft["features"][3]["kind"] = kind
    draft["features"][3]["points"] = [
        {"x": x, "y": y} for x, y in [(2.5, 3), (3.5, 3), (3.5, 6), (2.5, 6), (2.5, 3)]
    ]
    issues = validate_draft(draft)
    assert {i["path"] for i in issues if "intersects a static" in i["message"]} == {
        "features.lobby",
        "features.hall",
    }


@pytest.mark.parametrize("mutation", ["hash", "dimensions", "bytes", "allocation"])
def test_image_bytes_hash_dimensions_and_allocation_are_validated(mutation):
    draft = fixture_world_draft()
    image = draft["image"]
    if mutation == "hash":
        image["sha256"] = "0" * 64
    elif mutation == "dimensions":
        image["width"] += 1
    else:
        payload = bytearray(base64.b64decode(image["dataUrl"].split(",")[1]))
        if mutation == "allocation":
            payload[16:24] = struct.pack(">II", 100_000, 100_000)
            image["width"] = image["height"] = 100_000
        else:
            payload = payload[:40]
        image["dataUrl"] = "data:image/png;base64," + base64.b64encode(payload).decode()
        image["sha256"] = hashlib.sha256(payload).hexdigest()
    assert any(i["path"] == "image" for i in validate_draft(draft))


def test_auto_registered_tag_never_gains_flight_verification_without_tape_evidence():
    draft = fixture_world_draft()
    draft["tags"][0].update(
        source="auto_registered",
        usedForFlight=False,
        tapeVerified=False,
        tapeEvidence="",
        yawRad=None,
    )
    bundle = build_world_bundle(draft)
    assert bundle["tags"][0]["verifiedForFlight"] is False
    draft["tags"][0]["usedForFlight"] = True
    assert validate_draft(draft)


def test_corridor_adjacent_backtracking_and_oversized_geometry_refuse():
    draft = fixture_world_draft()
    draft["features"][2]["points"] = [{"x": 3, "y": y} for y in (4.5, 7, 6)]
    assert any("doubles back" in i["message"] for i in validate_draft(draft))
    draft = fixture_world_draft()
    draft["features"][1]["points"] *= 1000
    assert "computation budget" in validate_draft(draft)[0]["message"]


@pytest.mark.parametrize("value", [None, True, 1, "", [], {}, [1], {"unknown": True}])
def test_malformed_json_field_shapes_refuse_without_crashing(value):
    for path in [
        ("metadata",),
        ("image",),
        ("features",),
        ("tags",),
        ("features", 0, "kind"),
        ("features", 1, "points"),
        ("tags", 0, "observations"),
        ("metadata", "registration"),
    ]:
        draft = fixture_world_draft()
        target = draft
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
        issues = validate_draft(draft)
        if path == ("tags",) and value == []:
            assert issues == []  # A ground-only registry may start empty.
        else:
            assert issues


def test_world_hashes_and_per_segment_height_bindings_cannot_be_relabelled():
    bundle = build_world_bundle(fixture_world_draft())
    bundle["manifest"]["mapVersion"] = "renamed"
    assert "hash mismatch" in validate_world_bundle(bundle)[0]["message"]
    bundle = build_world_bundle(fixture_world_draft())
    bundle["corridors"][0]["segments"][0]["flightHeightM"] = 3
    bundle["manifest"]["documentHashes"]["corridors"] = content_hash(bundle["corridors"])
    bundle["manifest"]["contentHash"] = world_bundle_hash(bundle)
    assert "segment height" in validate_world_bundle(bundle)[0]["message"]


@pytest.mark.parametrize("field", ["frame", "mapVersion", "sourceHash", "contentHash"])
def test_derived_artifacts_bind_exact_frame_map_hash_and_content(field):
    bundle = build_world_bundle(fixture_world_draft())
    artifact = {
        "artifactId": "grid",
        "kind": "static-grid",
        "frame": "world",
        "mapVersion": bundle["manifest"]["mapVersion"],
        "sourceHash": world_bundle_hash(bundle, static_only=True),
        "content": {"fixture": True},
        "contentHash": content_hash({"fixture": True}),
    }
    bundle["derivedArtifacts"] = [artifact]
    bundle["manifest"]["contentHash"] = world_bundle_hash(bundle)
    assert validate_world_bundle(bundle) == []
    artifact[field] = "stale"
    bundle["manifest"]["contentHash"] = world_bundle_hash(bundle)
    assert validate_world_bundle(bundle)
