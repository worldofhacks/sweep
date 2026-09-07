from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from planner.navigation_artifacts import NavigationArtifact
from tools.map_geometry import generate
from tools.map_validate import content_hash

FIXTURE = Path(__file__).parent / "fixtures" / "world_bundle"


def _write(path: Path, document: dict) -> None:
    path.write_text(json.dumps(document, indent=2) + "\n")


def _seal(bundle: Path) -> dict:
    manifest = json.loads((bundle / "manifest.yaml").read_text())
    for name in ("tags.yaml", "zones.yaml", "obstacles.yaml"):
        manifest["files"][name] = hashlib.sha256((bundle / name).read_bytes()).hexdigest()
    for name in ("observed_tags", "known_tags"):
        item = manifest["registration"][name]
        item["sha256"] = hashlib.sha256((bundle / item["path"]).read_bytes()).hexdigest()
    manifest["content_sha256"] = content_hash(manifest)
    _write(bundle / "manifest.yaml", manifest)
    return manifest


def _held_out_world_bundle(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    bundle = tmp_path / "world"
    shutil.copytree(FIXTURE, bundle)
    observed_path = bundle / "evidence" / "observed_tags.json"
    known_path = bundle / "evidence" / "known_tags.json"
    tags_path = bundle / "tags.yaml"
    observed = json.loads(observed_path.read_text())
    known = json.loads(known_path.read_text())
    tags = json.loads(tags_path.read_text())
    observed["tags"].append({"tag_id": 3, "xy_m": [2, -1]})
    known["tags"].append({"tag_id": 3, "xy_m": [2, 0]})
    tag = dict(tags["tags"][1])
    tag.update(
        id=3,
        z_m=1,
        normal=[-1, 0, 0],
        yaw_rad=1.5707963267948966,
        T_world_tag=[[0, 0, -1, 2], [1, 0, 0, 0], [0, -1, 0, 1], [0, 0, 0, 1]],
        tape_verification={
            "source": "independent_tape_measurement",
            "evidence_path": "evidence/tape-3-0.json",
            "evidence_sha256": "",
            "compared_tag_id": 0,
            "measured_distance_m": 2.23606797749979,
            "maximum_error_m": 0.03,
        },
        verified_for_flight=True,
    )
    tape = {
        "schema_version": 1,
        "kind": "independent_tape_measurement",
        "tag_ids": [3, 0],
        "measured_distance_m": 2.23606797749979,
        "maximum_error_m": 0.03,
    }
    tape_path = bundle / "evidence" / "tape-3-0.json"
    _write(tape_path, tape)
    tag["tape_verification"]["evidence_sha256"] = hashlib.sha256(tape_path.read_bytes()).hexdigest()
    observed["tags"][-1]["xy_m"] = [2, -1]
    tags["tags"].append(tag)
    _write(observed_path, observed)
    _write(known_path, known)
    _write(tags_path, tags)
    manifest = json.loads((bundle / "manifest.yaml").read_text())
    manifest["registration"]["held_out_tag_ids"] = [3]
    _write(bundle / "manifest.yaml", manifest)
    manifest = _seal(bundle)
    return bundle, {manifest["bundle_version"]: manifest["content_sha256"]}


def _authoring(tmp_path: Path, bundle: Path) -> Path:
    free_height = {
        "schema_version": 1,
        "kind": "manual_free_volume_clearance",
        "free_volume_id": "lobby-free",
        "floor_id": "level_1",
        "polygon": [[-0.5, -0.5], [0.5, -0.5], [0.5, 0.5], [-0.5, 0.5], [-0.5, -0.5]],
        "z_min_m": 0.8,
        "z_max_m": 1.2,
        "measured_clearance_m": 2.2,
        "maximum_flight_height_m": 1.4,
    }
    camera = {
        "schema_version": 1,
        "kind": "camera_visibility_envelope",
        "camera_model_id": "forward",
        "forward_body": [1, 0, 0],
        "translation_body_m": [0, 0, 0],
        "fov_rad": 1.5707963267948966,
        "min_range_m": 0.5,
        "max_range_m": 3.5,
        "minimum_face_dot": 0.5,
        "focal_length_px": 300,
        "minimum_tag_pixels": 10,
    }
    free_path, camera_path = tmp_path / "free-height.json", tmp_path / "camera-envelope.json"
    _write(free_path, free_height)
    _write(camera_path, camera)
    request = {
        "schema_version": 2,
        "units": "meters",
        "frame": "world",
        "floor_id": "level_1",
        "bundle_content_sha256": json.loads((bundle / "manifest.yaml").read_text())[
            "content_sha256"
        ],
        "evidence_kind": "measured",
        "flight_box_xy": [-1.5, -1, 1.5, 1],
        "cell_m": 0.1,
        "altitude_planes_m": [1],
        "clearance": {"aircraft_radius_m": 0.05, "uncertainty_m": 0, "stopping_m": 0},
        "routes": [
            {
                "id": "lobby-spine",
                "corridor_ids": ["lobby-spine"],
                "centerline": [[-1, 0], [1, 0]],
                "half_width_m": 0.1,
                "z_min_m": 0.9,
                "z_max_m": 1.1,
                "heading_rad": 0,
                "camera_model_id": "forward",
            }
        ],
        "formations": [
            {
                "id": "lobby-pair",
                "polygon": [[-0.4, -0.4], [0.4, -0.4], [0.4, 0.4], [-0.4, 0.4], [-0.4, -0.4]],
                "z_min_m": 0.9,
                "z_max_m": 1.1,
                "free_volume_ids": ["lobby-free"],
                "separation_m": 0.5,
            }
        ],
        "free_volumes": [
            {
                "id": "lobby-free",
                "polygon": [[-0.5, -0.5], [0.5, -0.5], [0.5, 0.5], [-0.5, 0.5], [-0.5, -0.5]],
                "z_min_m": 0.8,
                "z_max_m": 1.2,
                "height_evidence": {
                    "path": free_path.name,
                    "sha256": hashlib.sha256(free_path.read_bytes()).hexdigest(),
                },
            }
        ],
        "held_out_checkpoints": [{"id": "tag-3", "tag_id": 3, "maximum_error_m": 0.03}],
        "camera_models": [
            {
                "id": "forward",
                "forward_body": [1, 0, 0],
                "translation_body_m": [0, 0, 0],
                "fov_rad": 1.5707963267948966,
                "min_range_m": 0.5,
                "max_range_m": 3.5,
                "minimum_face_dot": 0.5,
                "focal_length_px": 300,
                "minimum_tag_pixels": 10,
                "calibration": {
                    "path": camera_path.name,
                    "sha256": hashlib.sha256(camera_path.read_bytes()).hexdigest(),
                },
            }
        ],
    }
    path = tmp_path / "geometry-v2.json"
    _write(path, request)
    return path


def test_measured_world_geometry_requires_registered_checkpoint_and_visibility(
    tmp_path: Path,
) -> None:
    bundle, accepted = _held_out_world_bundle(tmp_path)
    authoring = _authoring(tmp_path, bundle)
    output = tmp_path / "geometry"

    report = generate(bundle, authoring, output, accepted)
    artifact = NavigationArtifact.from_geometry_directory(
        bundle, output, accepted, authoring=authoring
    )

    assert report["held_out_checkpoints"][0]["passes"] is True
    assert report["routes"][0]["tag_coverage"]["covered"] is True
    assert (
        artifact.geometry_pin.content_sha256
        == hashlib.sha256((output / "geometry.json").read_bytes()).hexdigest()
    )
    report["routes"][0]["tag_coverage"]["covered"] = False
    _write(output / "geometry.json", report)
    with pytest.raises(ValueError, match="tag coverage"):
        NavigationArtifact.from_geometry_directory(bundle, output, accepted, authoring=authoring)


def _request(authoring: Path) -> dict:
    return json.loads(authoring.read_text())


def test_measured_geometry_rejects_offgrid_envelopes_and_undersized_route_tubes(
    tmp_path: Path,
) -> None:
    bundle, accepted = _held_out_world_bundle(tmp_path)
    authoring = _authoring(tmp_path, bundle)
    request = _request(authoring)
    request["flight_box_xy"] = [-0.5, -1, 0.5, 1]
    _write(authoring, request)
    report = generate(bundle, authoring, tmp_path / "offgrid", accepted)
    assert report["routes"][0]["geometry_clear"] is False
    with pytest.raises(ValueError, match="route lacks measured clearance"):
        NavigationArtifact.from_geometry_directory(
            bundle, tmp_path / "offgrid", accepted, authoring=authoring
        )

    authoring = _authoring(tmp_path, bundle)
    request = _request(authoring)
    request["clearance"]["aircraft_radius_m"] = 0.2
    _write(authoring, request)
    with pytest.raises(ValueError, match="route half width"):
        generate(bundle, authoring, tmp_path / "undersized", accepted)


def test_measured_geometry_requires_verified_tags_and_exact_occlusion_and_height_scope(
    tmp_path: Path,
) -> None:
    bundle, accepted = _held_out_world_bundle(tmp_path)
    tags = json.loads((bundle / "tags.yaml").read_text())
    tag = next(item for item in tags["tags"] if item["id"] == 3)
    tag["verified_for_flight"] = False
    tag["tape_verification"] = None
    _write(bundle / "tags.yaml", tags)
    # Rebuild the modified fixture's manifest after the deliberate tag mutation.
    manifest = _seal(bundle)
    accepted = {manifest["bundle_version"]: manifest["content_sha256"]}
    authoring = _authoring(tmp_path, bundle)
    with pytest.raises(ValueError, match="checkpoint tag needs independent tape verification"):
        generate(bundle, authoring, tmp_path / "unverified", accepted)

    bundle, accepted = _held_out_world_bundle(tmp_path / "wall")
    obstacles = json.loads((bundle / "obstacles.yaml").read_text())
    obstacles["obstacles"].append(
        {
            "id": "thin-wall",
            "floor_id": "level_1",
            "polygon": [
                [1.5, -0.001],
                [1.501, -0.001],
                [1.501, 0.001],
                [1.5, 0.001],
                [1.5, -0.001],
            ],
            "z_min_m": 0,
            "z_max_m": 3,
        }
    )
    _write(bundle / "obstacles.yaml", obstacles)
    manifest = _seal(bundle)
    accepted = {manifest["bundle_version"]: manifest["content_sha256"]}
    authoring = _authoring(tmp_path / "wall", bundle)
    report = generate(bundle, authoring, tmp_path / "wall-output", accepted)
    assert report["routes"][0]["tag_coverage"]["covered"] is False

    bundle, accepted = _held_out_world_bundle(tmp_path / "height")
    authoring = _authoring(tmp_path / "height", bundle)
    request = _request(authoring)
    evidence_path = (tmp_path / "height") / request["free_volumes"][0]["height_evidence"]["path"]
    evidence = json.loads(evidence_path.read_text())
    evidence["free_volume_id"] = "unrelated"
    _write(evidence_path, evidence)
    request["free_volumes"][0]["height_evidence"]["sha256"] = hashlib.sha256(
        evidence_path.read_bytes()
    ).hexdigest()
    _write(authoring, request)
    with pytest.raises(ValueError, match="exact measured volume"):
        generate(bundle, authoring, tmp_path / "height-output", accepted)


def test_measured_geometry_bounds_authoring_and_loader_rederives_files(tmp_path: Path) -> None:
    bundle, accepted = _held_out_world_bundle(tmp_path)
    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b"{" + b" " * 1_000_000)
    with pytest.raises(ValueError, match="byte limit"):
        generate(bundle, oversized, tmp_path / "oversized-output", accepted)

    authoring = _authoring(tmp_path, bundle)
    output = tmp_path / "geometry"
    generate(bundle, authoring, output, accepted)
    report = json.loads((output / "geometry.json").read_text())
    grid = output / report["grid_files"][0]
    rows = __import__("numpy").load(grid, allow_pickle=False)
    rows[0, 0] = 1 - rows[0, 0]
    __import__("numpy").save(grid, rows, allow_pickle=False)
    report["files"][grid.name] = hashlib.sha256(grid.read_bytes()).hexdigest()
    _write(output / "geometry.json", report)
    with pytest.raises(ValueError, match="not derived"):
        NavigationArtifact.from_geometry_directory(bundle, output, accepted, authoring=authoring)
