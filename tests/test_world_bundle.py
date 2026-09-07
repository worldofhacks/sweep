import hashlib
import json
import shutil
from pathlib import Path

import cv2
import numpy as np
import pytest

from tools.map_validate import content_hash, validate_bundle, validate_candidate


@pytest.fixture
def bundle(tmp_path):
    result = tmp_path / "world-bundle"
    shutil.copytree(Path(__file__).parent / "fixtures" / "world_bundle", result)
    return result


def reseal(bundle):
    manifest_path = bundle / "manifest.yaml"
    manifest = json.loads(manifest_path.read_text())
    manifest["files"] = {
        name: hashlib.sha256((bundle / name).read_bytes()).hexdigest()
        for name in ("tags.yaml", "zones.yaml", "obstacles.yaml")
    }
    manifest["occupancy"]["sha256"] = hashlib.sha256(
        (bundle / manifest["occupancy"]["path"]).read_bytes()
    ).hexdigest()
    manifest["content_sha256"] = content_hash(manifest)
    manifest_path.write_text(json.dumps(manifest))
    return manifest


def mutate(bundle, name, change):
    path = bundle / name
    value = json.loads(path.read_text())
    change(value)
    path.write_text(json.dumps(value))
    reseal(bundle)


def test_candidate_snapshot_is_bounded_unapproved_and_external_registry_controls_acceptance(bundle):
    candidate = validate_candidate(bundle)
    manifest = json.loads((bundle / "manifest.yaml").read_text())
    assert candidate["bundle_version"] == "world-fixture-v2"
    assert candidate.document("tags.yaml")["frame"] == "world"
    assert candidate.occupancy_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert "flight_approved" not in candidate
    with pytest.raises(ValueError, match="accepted version"):
        validate_bundle(bundle, {manifest["bundle_version"]: "0" * 64})
    accepted = validate_bundle(bundle, {manifest["bundle_version"]: manifest["content_sha256"]})
    assert accepted["content_sha256"] == manifest["content_sha256"]


def test_local_grid_cells_use_measured_rotation_translation_and_png_row_order(bundle):
    candidate = validate_candidate(bundle)
    assert candidate.occupancy_cell_world_xy(0, 0) == pytest.approx((1.85, -2.95))
    assert candidate.occupancy_cell_world_xy(0, 1) == pytest.approx((1.95, -2.95))
    assert candidate.occupancy_cell_world_xy(1, 0) == pytest.approx((1.85, -2.85))
    with pytest.raises(ValueError, match="column"):
        candidate.occupancy_cell_world_xy(3, 0)


def test_tape_evidence_cannot_relax_the_ten_centimetre_map_bound(bundle):
    evidence_path = bundle / "evidence/tape-0-1.json"
    evidence = json.loads(evidence_path.read_text())
    evidence["maximum_error_m"] = 100
    evidence_path.write_text(json.dumps(evidence))
    mutate(
        bundle,
        "tags.yaml",
        lambda d: d["tags"][0]["tape_verification"].update(
            maximum_error_m=100,
            evidence_sha256=hashlib.sha256(evidence_path.read_bytes()).hexdigest(),
        ),
    )
    with pytest.raises(ValueError, match="error bound"):
        validate_candidate(bundle)


def test_manifest_and_registration_mutation_cannot_change_validated_snapshot(bundle):
    candidate = validate_candidate(bundle)
    candidate["occupancy"]["origin_xy"][0] = 100
    candidate.registration()["T_target_source"]["dx_m"] = 100
    candidate.document("tags.yaml")["tags"][0]["x_m"] = 100
    with pytest.raises(TypeError):
        candidate["map_id"] = "other"
    assert candidate.occupancy_cell_world_xy(0, 0) == pytest.approx((1.85, -2.95))
    assert candidate.document("tags.yaml")["tags"][0]["x_m"] == 0


def test_registration_source_epoch_and_target_world_pins_are_required(bundle):
    mutate(
        bundle, "manifest.yaml", lambda d: d["occupancy"]["source_scope"].update(connection_epoch=4)
    )
    with pytest.raises(ValueError, match="source does not match"):
        validate_candidate(bundle)
    mutate(
        bundle, "manifest.yaml", lambda d: d["occupancy"]["source_scope"].update(connection_epoch=3)
    )
    mutate(bundle, "manifest.yaml", lambda d: d.update(map_id="different-world"))
    with pytest.raises(ValueError, match="world pins"):
        validate_candidate(bundle)


def test_registration_inputs_are_hashed_and_not_just_claimed_residuals(bundle):
    path = bundle / "evidence" / "observed_tags.json"
    document = json.loads(path.read_text())
    document["tags"][0]["xy_m"][0] += 1
    path.write_text(json.dumps(document))
    with pytest.raises(ValueError, match="hash mismatch"):
        validate_candidate(bundle)
    mutate(
        bundle,
        "manifest.yaml",
        lambda d: d["registration"]["observed_tags"].update(
            sha256=hashlib.sha256(path.read_bytes()).hexdigest()
        ),
    )
    with pytest.raises(ValueError, match="outlier|RMS"):
        validate_candidate(bundle)


@pytest.mark.parametrize(
    "points",
    [
        [[0, 0], [2, 0], [1, 0], [1, 1], [0, 1], [0, 0]],
        [[0, 0], [2, 0], [2, 2], [1, 0], [0, 2], [0, 0]],
        [[0, 0], [1e308, 0], [1e308, 1e308], [0, 0]],
    ],
)
def test_world_polygons_reject_overlapping_touching_and_unbounded_edges(bundle, points):
    mutate(bundle, "zones.yaml", lambda d: d["geofence"].update(polygon=points))
    with pytest.raises(ValueError, match="overlapping|self-intersects|metric bounds"):
        validate_candidate(bundle)


def test_corridor_footprint_and_height_must_stay_inside_geofence(bundle):
    mutate(bundle, "zones.yaml", lambda d: d["corridors"][0].update(width_m=7))
    with pytest.raises(ValueError, match="footprint"):
        validate_candidate(bundle)
    mutate(bundle, "zones.yaml", lambda d: d["corridors"][0].update(width_m=0.8, z_min_m=-1))
    with pytest.raises(ValueError, match="altitude"):
        validate_candidate(bundle)


@pytest.mark.parametrize(
    ("name", "change", "match"),
    [
        (
            "manifest.yaml",
            lambda d: d["frame"].update(axis_convention="east_north_up"),
            "canonical local",
        ),
        ("manifest.yaml", lambda d: d["registration"].update(maximum_residual_m=0), "residual"),
        ("manifest.yaml", lambda d: d["occupancy"].update(width_cells=4), "dimensions"),
        ("tags.yaml", lambda d: d["tags"][2].update(verified_for_flight=True), "tape verification"),
        (
            "tags.yaml",
            lambda d: d["tags"][0]["tape_verification"].update(evidence_path="observation/0"),
            "self-verify",
        ),
        ("tags.yaml", lambda d: d["tags"].append(d["tags"][0]), "unique tag"),
        ("zones.yaml", lambda d: d["corridors"][0]["height_evidence"].pop(), "every segment"),
        (
            "zones.yaml",
            lambda d: d["corridors"][0]["height_evidence"][0].update(maximum_flight_height_m=1),
            "does not match",
        ),
        ("obstacles.yaml", lambda d: d.update(unexpected=True), "does not match schema"),
    ],
)
def test_candidate_refuses_corrupt_or_insufficient_world_evidence(bundle, name, change, match):
    mutate(bundle, name, change)
    with pytest.raises(ValueError, match=match):
        validate_candidate(bundle)


def test_candidate_refuses_oversized_document_before_parsing(bundle):
    (bundle / "tags.yaml").write_bytes(b"{" + b" " * 1_000_000 + b"}")
    with pytest.raises(ValueError, match="size limit"):
        validate_candidate(bundle)


def test_candidate_accepts_gray8_png_encoded_like_the_ohmni_grid_builder(bundle):
    encoded, payload = cv2.imencode(".png", np.asarray([[0, 255, 0], [0, 255, 0]], dtype=np.uint8))
    assert encoded
    path = bundle / "ohmni-grid.png"
    path.write_bytes(payload.tobytes())
    manifest = reseal(bundle)
    assert validate_candidate(bundle).occupancy_bytes() == path.read_bytes()
    assert manifest["occupancy"]["encoding"] == "png-gray8"


def test_candidate_refuses_tampered_tape_evidence_source(bundle):
    (bundle / "evidence" / "tape-0-1.json").write_text('{"kind":"rewritten"}\n')
    with pytest.raises(ValueError, match="tape evidence hash mismatch"):
        validate_candidate(bundle)


def test_candidate_refuses_a_world_datum_that_moves_tag_zero(bundle):
    def move_origin(document):
        document["tags"][0]["x_m"] = 0.1
        document["tags"][0]["T_world_tag"][0][3] = 0.1

    mutate(bundle, "tags.yaml", move_origin)
    with pytest.raises(ValueError, match="world datum"):
        validate_candidate(bundle)


def test_candidate_refuses_corridor_evidence_that_does_not_cover_the_route_band(bundle):
    source = bundle / "evidence" / "corridor-0.json"
    evidence = json.loads(source.read_text())
    evidence["maximum_flight_height_m"] = 1
    source.write_text(json.dumps(evidence))
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    mutate(
        bundle,
        "zones.yaml",
        lambda d: d["corridors"][0]["height_evidence"][0].update(
            maximum_flight_height_m=1, evidence_sha256=digest
        ),
    )
    with pytest.raises(ValueError, match="does not bound"):
        validate_candidate(bundle)


def test_existing_v1_artifact_still_uses_its_accepted_hash_contract():
    fixture = Path(__file__).parent / "fixtures" / "mapping"
    manifest = json.loads((fixture / "manifest.yaml").read_text())
    result = validate_bundle(fixture, {manifest["bundle_version"]: manifest["content_sha256"]})
    assert result["frame"]["name"] == "building"
