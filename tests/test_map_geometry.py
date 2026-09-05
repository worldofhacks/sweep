import hashlib
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

import tools.map_geometry as map_geometry
from tools.map_common import read_document, write_document
from tools.map_geometry import _blocked, _proximity, generate, read_ply

FIXTURE = Path(__file__).parent / "fixtures" / "geometry"
IDENTITY = [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]


def box(x0, y0, x1, y1):
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]


def read_npy(path):
    array = np.load(path, allow_pickle=False)
    assert array.dtype == np.uint8
    assert array.ndim == 2
    return array.tolist()


def accepted_versions(bundle=FIXTURE):
    manifest = read_document(Path(bundle) / "manifest.yaml")
    return {manifest["bundle_version"]: manifest["content_sha256"]}


def seal_changed_source(bundle):
    from tools.map_validate import seal_manifest

    tags = read_document(Path(bundle) / "tags.yaml")
    tags["source"]["sha256"] = hashlib.sha256(Path(bundle, "scan.ply").read_bytes()).hexdigest()
    write_document(Path(bundle) / "tags.yaml", tags)
    return seal_manifest(bundle)


@pytest.fixture(scope="module")
def generated(tmp_path_factory):
    output = tmp_path_factory.mktemp("geometry") / "output"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tools.map_geometry",
            str(FIXTURE),
            str(FIXTURE / "geometry_authoring.json"),
            str(output),
            "--accepted-versions",
            str(FIXTURE / "accepted_versions.json"),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return output, read_document(output / "geometry.json")


def test_synthetic_pipeline_writes_pinned_grids_and_rejects_atrium(generated):
    output, report = generated
    assert report["flight_approved"] is False
    assert (
        report["bundle_content_sha256"]
        == read_document(FIXTURE / "manifest.yaml")["content_sha256"]
    )
    assert report["route"]["geometry_clear"] is True
    kitchen, atrium = report["formations"]
    assert kitchen["candidate"] is True
    assert atrium["candidate"] is False
    assert report["atrium_recommendation"] == "use_kitchen_only_if_accepted"
    assert report["route"]["tag_proximity"]["visibility_verified"] is False
    for name, digest in report["files"].items():
        assert hashlib.sha256((output / name).read_bytes()).hexdigest() == digest
    grids = list(output.glob("*.npy"))
    assert len(grids) == 5
    for path in grids:
        rows = read_npy(path)
        assert [len(rows), len(rows[0])] == report["shape_yx"]
        assert set(rows[0]) == {1}
    rows = read_npy(output / "grid_level_1_1.6.npy")
    # Row 20, column 20 spans [0,0] to [0.1,0.1] in the independent fixture frame.
    assert rows[20][20] == 0
    assert rows[40][45] == 1
    assert "Elevation X / Z" in (output / "preview.html").read_text()
    fence = read_document(output / "geofence_level_1.json")
    assert all(
        x0 > -1 and y0 > -1 and x1 < 3 and y1 < 2
        for x0, y0, x1, y1 in fence["candidate_cells_xyxy"]
    )


@pytest.mark.parametrize("reverse_order", [False, True])
def test_preview_reports_rejected_atrium_in_either_formation_order(tmp_path, reverse_order):
    request = read_document(FIXTURE / "geometry_authoring.json")
    if reverse_order:
        request["formations"].reverse()
    path = tmp_path / "input.json"
    write_document(path, request)
    output = tmp_path / "output"
    report = generate(FIXTURE, path, output, accepted_versions())
    formations = {formation["id"]: formation for formation in report["formations"]}
    assert formations["kitchen"]["candidate"] is True
    assert formations["atrium"]["candidate"] is False
    assert report["atrium_recommendation"] == "use_kitchen_only_if_accepted"
    preview = (output / "preview.html").read_text()
    assert "Atrium: rejected; kitchen fallback needs acceptance" in preview
    assert "Atrium: candidate pending measurements" not in preview


def domain():
    return [{"polygon": box(-3, -3, 3, 3), "z_min": 0, "z_max": 4, "eligible": True}]


def test_sparse_or_empty_cloud_never_clears_unknown_space():
    assert _blocked((0, 0, 0.1, 0.1), 1.6, 1.6, [], [], []) == "unknown"
    evidence = domain()
    evidence[0]["eligible"] = False
    assert _blocked((0, 0, 0.1, 0.1), 1.6, 1.6, evidence, [], []) == "unknown"


def test_free_space_must_cover_cell_and_full_hazard_envelope():
    evidence = domain()
    assert _blocked((0, 0, 0.1, 0.1), 1.6, 1.6, evidence, [], []) is None
    evidence[0]["polygon"] = box(-0.7, -1, 1, 1)
    assert _blocked((0, 0, 0.1, 0.1), 1.6, 1.6, evidence, [], []) == "unknown"
    evidence = domain()
    evidence[0]["z_max"] = 2.3
    assert _blocked((0, 0, 0.1, 0.1), 1.6, 1.6, evidence, [], []) == "unknown"


def test_obstacle_inflation_blocks_cells_beyond_occupied_polygon():
    obstacle = {"polygon": box(0, 0, 0.2, 0.2), "z_min": 1, "z_max": 1.1}
    assert _blocked((0.8, 0, 0.9, 0.1), 1.8, 1.8, domain(), [obstacle], []) == "obstacle_or_no_fly"
    assert _blocked((1, 0, 1.1, 0.1), 1.8, 1.8, domain(), [obstacle], []) is None
    assert _blocked((0, 0, 0.1, 0.1), 1.9, 1.9, domain(), [obstacle], []) is None


def test_voxel_uses_entire_ten_centimeter_cube_plus_margin():
    assert _blocked((0.8, 0, 0.9, 0.1), 1.8, 1.8, domain(), [], [(0, 0, 1)]) == "scan_voxel"
    assert _blocked((0.9, 0, 1, 0.1), 1.8, 1.8, domain(), [], [(0, 0, 1)]) is None
    assert _blocked((0, 0, 0.1, 0.1), 1.9, 1.9, domain(), [], [(0, 0, 1)]) is None


def test_continuous_altitude_interval_catches_obstacle_between_endpoints():
    obstacle = {"polygon": box(0, 0, 0.2, 0.2), "z_min": 1.8, "z_max": 1.9}
    assert _blocked((0, 0, 0.1, 0.1), 0.8, 2.8, domain(), [obstacle], []) == "obstacle_or_no_fly"


def test_proximity_is_sampled_at_both_altitude_extremes():
    route = {"centerline": [[0, 0], [0.1, 0]], "z_min": 1, "z_max": 4}
    result = _proximity(route, [{"x": 0, "y": 0, "z": 0}])
    assert result["status"] == "candidate_proximity_only"
    assert result["samples_outside_radius"] == 2
    assert result["visibility_verified"] is False


def test_ply_registration_applies_saved_transform(tmp_path):
    path = tmp_path / "cloud.ply"
    path.write_text(
        "ply\nformat ascii 1.0\nelement vertex 1\nproperty float z\n"
        "property float x\nproperty float y\nend_header\n3 1 2\n"
    )
    transform = [[0, -1, 0, 5], [1, 0, 0, -1], [0, 0, 1, 2], [0, 0, 0, 1]]
    assert read_ply(path, transform) == [[3, 0, 5]]


@pytest.mark.parametrize(
    "text",
    [
        "ply\nformat binary_little_endian 1.0\n",
        "ply\nformat ascii 1.0\nelement vertex 1\nproperty float x\nproperty float y\n"
        "property float z\nend_header\nNaN 0 0\n",
        "ply\nformat ascii 1.0\nelement vertex 2\nproperty float x\nproperty float y\n"
        "property float z\nend_header\n0 0 0\n",
        "ply\nformat ascii 1.0\nelement vertex 1\nproperty float x\nproperty float y\n"
        "property float z\nend_header\n0 0 0\n9 9 9\n",
        "ply\nformat ascii 1.0\nelement vertex 1\nproperty float x\nproperty float y\n"
        "property float z\nelement face 0\nproperty list uchar int vertex_indices\n"
        "end_header\n0 0 0\n",
    ],
)
def test_ply_rejects_unsupported_nonfinite_and_truncated_clouds(tmp_path, text):
    path = tmp_path / "bad.ply"
    path.write_text(text)
    with pytest.raises(ValueError):
        read_ply(path, IDENTITY)


@pytest.mark.parametrize(
    "field,value",
    [
        ("bundle_content_sha256", "stale"),
        ("schema_version", True),
        ("units", "feet"),
        ("floor_elevation_m", float("inf")),
        ("flight_box_xy", [0, 0, 0, 1]),
        ("wall_source", "unhashed.ply"),
        ("cloud_sources", ["missing.ply"]),
    ],
)
def test_authoring_rejects_stale_or_invalid_inputs(tmp_path, field, value):
    request = read_document(FIXTURE / "geometry_authoring.json")
    request[field] = value
    path = tmp_path / "input.json"
    path.write_text(json.dumps(request))
    with pytest.raises(ValueError):
        generate(FIXTURE, path, tmp_path / "output", accepted_versions())


def test_surveyed_unapproved_domain_remains_entirely_blocked(tmp_path):
    request = read_document(FIXTURE / "geometry_authoring.json")
    request["evidence_kind"] = "surveyed"
    path = tmp_path / "input.json"
    write_document(path, request)
    output = tmp_path / "output"
    report = generate(FIXTURE, path, output, accepted_versions())
    assert report["route"]["geometry_clear"] is False
    for grid in output.glob("*.npy"):
        assert all(value == 1 for row in read_npy(grid) for value in row)


def test_route_tube_catches_hazard_outside_centerline(tmp_path):
    request = read_document(FIXTURE / "geometry_authoring.json")
    request["no_fly"] = [{"polygon": box(0.9, 0.9, 1, 0.95), "z_min": 0, "z_max": 3}]
    path = tmp_path / "input.json"
    write_document(path, request)
    report = generate(FIXTURE, path, tmp_path / "output", accepted_versions())
    assert report["route"]["geometry_clear"] is False
    assert report["route"]["blocked_cells"] > 0


def test_outside_grid_route_cannot_pass_from_its_interior_subset(tmp_path):
    request = read_document(FIXTURE / "geometry_authoring.json")
    request["route"]["centerline"] = [[0, 0], [5, 0]]
    path = tmp_path / "input.json"
    write_document(path, request)
    report = generate(FIXTURE, path, tmp_path / "output", accepted_versions())
    assert report["route"]["outside_grid"] is True
    assert report["route"]["geometry_clear"] is False


def test_existing_output_is_preserved(tmp_path, generated):
    output, _ = generated
    before = (output / "geometry.json").read_bytes()
    with pytest.raises(ValueError, match="already exists"):
        generate(FIXTURE, FIXTURE / "geometry_authoring.json", output, accepted_versions())
    assert (output / "geometry.json").read_bytes() == before


@pytest.mark.parametrize("clouds", [[], ["scan.ply", "scan.ply"]])
def test_cloud_inventory_cannot_be_omitted_or_duplicated(tmp_path, clouds):
    request = read_document(FIXTURE / "geometry_authoring.json")
    request["cloud_sources"] = clouds
    path = tmp_path / "input.json"
    write_document(path, request)
    with pytest.raises(ValueError, match="cloud_sources"):
        generate(FIXTURE, path, tmp_path / "output", accepted_versions())


def test_cross_floor_hazard_still_blocks_overlapping_building_altitude(tmp_path):
    from shutil import copytree

    from tools.map_validate import seal_manifest

    bundle = tmp_path / "bundle"
    copytree(FIXTURE, bundle)
    document = read_document(bundle / "obstacles.yaml")
    document["obstacles"].append(
        {
            "id": "overhang",
            "floor_id": "mezzanine",
            "polygon": box(0.5, -0.2, 1.5, 0.2),
            "z_min": 1.8,
            "z_max": 2.1,
        }
    )
    write_document(bundle / "obstacles.yaml", document)
    manifest = seal_manifest(bundle)
    request = read_document(bundle / "geometry_authoring.json")
    request["bundle_content_sha256"] = manifest["content_sha256"]
    write_document(bundle / "geometry_authoring.json", request)
    report = generate(
        bundle, bundle / "geometry_authoring.json", tmp_path / "output", accepted_versions(bundle)
    )
    assert report["route"]["geometry_clear"] is False


def test_overlapping_drone_envelopes_and_wrong_named_zone_are_rejected(tmp_path):
    request = read_document(FIXTURE / "geometry_authoring.json")
    request["formations"][0]["separation_m"] = 0.1
    request["formations"][1]["polygon"] = request["formations"][0]["polygon"]
    path = tmp_path / "input.json"
    write_document(path, request)
    report = generate(FIXTURE, path, tmp_path / "output", accepted_versions())
    kitchen, atrium = report["formations"]
    assert kitchen["geometry_clear"] is True
    assert kitchen["two_drone_static_fit"] is False
    assert kitchen["candidate"] is False
    assert atrium["inside_named_zone"] is False
    assert atrium["candidate"] is False


@pytest.mark.parametrize("point_count", [2, 3])
def test_stationary_route_is_rejected_before_clearance_report(tmp_path, point_count):
    request = read_document(FIXTURE / "geometry_authoring.json")
    request["route"]["centerline"] = [[0, 0] for _ in range(point_count)]
    path = tmp_path / "input.json"
    write_document(path, request)
    output = tmp_path / "output"
    with pytest.raises(ValueError, match="route total length must be positive"):
        generate(FIXTURE, path, output, accepted_versions())
    assert not output.exists()


def test_travel_route_can_include_a_repeated_waypoint(tmp_path):
    request = read_document(FIXTURE / "geometry_authoring.json")
    request["route"]["centerline"].insert(0, request["route"]["centerline"][0])
    path = tmp_path / "input.json"
    write_document(path, request)
    report = generate(FIXTURE, path, tmp_path / "output", accepted_versions())
    assert report["route"]["geometry_clear"] is True


def test_huge_route_is_rejected_before_proximity_allocation(tmp_path):
    request = read_document(FIXTURE / "geometry_authoring.json")
    request["route"]["centerline"] = [[0, 0], [1e10, 0]]
    path = tmp_path / "input.json"
    write_document(path, request)
    with pytest.raises(ValueError, match="sample budget"):
        generate(FIXTURE, path, tmp_path / "output", accepted_versions())


def test_ten_meter_room_with_ten_thousand_floor_voxels(tmp_path):
    from shutil import copytree

    bundle = tmp_path / "bundle"
    copytree(FIXTURE, bundle)
    points = [(ix / 10 + 0.05, iy / 10 + 0.05, 0.01) for iy in range(100) for ix in range(100)]
    (bundle / "scan.ply").write_text(
        "ply\nformat ascii 1.0\nelement vertex 10000\nproperty float x\n"
        "property float y\nproperty float z\nend_header\n"
        + "".join(f"{x} {y} {z}\n" for x, y, z in points)
    )
    zones = read_document(bundle / "zones.yaml")
    zones["geofence"] = {"polygon": box(-2, -2, 12, 12), "z_min": 0, "z_max": 4}
    write_document(bundle / "zones.yaml", zones)
    obstacles = read_document(bundle / "obstacles.yaml")
    obstacles["obstacles"].append(
        {"id": "pillar", "floor_id": "level_1", "polygon": box(7, 7, 8, 8), "z_min": 0, "z_max": 3}
    )
    write_document(bundle / "obstacles.yaml", obstacles)
    manifest = seal_changed_source(bundle)
    request = read_document(bundle / "geometry_authoring.json")
    request["bundle_content_sha256"] = manifest["content_sha256"]
    request["flight_box_xy"] = [0, 0, 10, 10]
    request["wall_boundary"] = box(-2, -2, 12, 12)
    request["free_space"][0].update(polygon=box(-2, -2, 12, 12), z_max=4)
    write_document(bundle / "geometry_authoring.json", request)
    output = tmp_path / "output"
    report = generate(bundle, bundle / "geometry_authoring.json", output, accepted_versions(bundle))
    assert report["shape_yx"] == [100, 100]
    low = read_npy(output / "grid_level_1_0.8.npy")
    high = read_npy(output / "grid_level_1_1.2.npy")
    assert low[50][50] == 1
    assert high[50][50] == 0
    assert high[75][75] == 1


def test_underdeclared_ply_is_rejected_end_to_end(tmp_path):
    from shutil import copytree

    bundle = tmp_path / "bundle"
    copytree(FIXTURE, bundle)
    cloud = bundle / "scan.ply"
    cloud.write_text(cloud.read_text() + "1 0 1.8\n")
    manifest = seal_changed_source(bundle)
    request = read_document(bundle / "geometry_authoring.json")
    request["bundle_content_sha256"] = manifest["content_sha256"]
    write_document(bundle / "geometry_authoring.json", request)

    with pytest.raises(ValueError, match="trailing PLY payload"):
        generate(
            bundle,
            bundle / "geometry_authoring.json",
            tmp_path / "output",
            accepted_versions(bundle),
        )


def test_generation_consumes_only_the_validated_bundle_snapshot(tmp_path, monkeypatch):
    from shutil import copytree

    bundle = tmp_path / "bundle"
    copytree(FIXTURE, bundle)
    accepted = accepted_versions(bundle)
    original_validate = map_geometry.validate_bundle

    def validate_then_replace_inputs(path, versions):
        snapshot = original_validate(path, versions)
        for name in ("zones.yaml", "obstacles.yaml", "tags.yaml", "scan.ply"):
            (bundle / name).write_bytes(b"replaced after validation")
        return snapshot

    monkeypatch.setattr(map_geometry, "validate_bundle", validate_then_replace_inputs)
    report = generate(
        bundle,
        bundle / "geometry_authoring.json",
        tmp_path / "output",
        accepted,
    )
    assert report["bundle_content_sha256"] == next(iter(accepted.values()))
    assert report["route"]["geometry_clear"] is True
    assert report["source_point_count"] == 3


def test_authoring_hash_names_the_exact_parsed_snapshot(tmp_path, monkeypatch):
    request = read_document(FIXTURE / "geometry_authoring.json")
    path = tmp_path / "input.json"
    write_document(path, request)
    payload = path.read_bytes()
    original_parse = map_geometry.parse_document

    def parse_then_replace_input(data, name):
        parsed = original_parse(data, name)
        changed = dict(parsed)
        changed["route"] = {**parsed["route"], "centerline": [[0, 0], [100, 100]]}
        write_document(path, changed)
        return parsed

    monkeypatch.setattr(map_geometry, "parse_document", parse_then_replace_input)
    report = generate(FIXTURE, path, tmp_path / "output", accepted_versions())
    assert report["authoring_sha256"] == hashlib.sha256(payload).hexdigest()
    assert report["route"]["tube"]["centerline"] == request["route"]["centerline"]


def test_boundary_aligned_concave_geofence_slot_stays_blocked(tmp_path):
    from shutil import copytree

    from tools.map_validate import seal_manifest

    bundle = tmp_path / "bundle"
    copytree(FIXTURE, bundle)
    slot_low = -2 + 39 * 0.1
    slot_high = -2 + 40 * 0.1
    zones = read_document(bundle / "zones.yaml")
    zones["geofence"]["polygon"] = [
        [-2, -2],
        [4, -2],
        [4, 3],
        [slot_high, 3],
        [slot_high, 1],
        [slot_low, 1],
        [slot_low, 3],
        [-2, 3],
        [-2, -2],
    ]
    write_document(bundle / "zones.yaml", zones)
    manifest = seal_manifest(bundle)
    request = read_document(bundle / "geometry_authoring.json")
    request["bundle_content_sha256"] = manifest["content_sha256"]
    write_document(bundle / "geometry_authoring.json", request)

    output = tmp_path / "output"
    generate(bundle, bundle / "geometry_authoring.json", output, accepted_versions(bundle))
    fence = read_document(output / "geofence_level_1.json")
    assert not any(
        slot_low < (x0 + x1) / 2 < slot_high and (y0 + y1) / 2 > 1
        for x0, y0, x1, y1 in fence["candidate_cells_xyxy"]
    )


def test_zero_drone_radius_is_rejected(tmp_path):
    request = read_document(FIXTURE / "geometry_authoring.json")
    request["formations"][0]["drone_radius_m"] = 0
    path = tmp_path / "input.json"
    write_document(path, request)
    with pytest.raises(ValueError, match="invalid formation envelope"):
        generate(FIXTURE, path, tmp_path / "output", accepted_versions())


def test_preview_point_rendering_is_explicitly_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(map_geometry, "PREVIEW_POINT_LIMIT", 2)
    output = tmp_path / "output"
    report = generate(
        FIXTURE,
        FIXTURE / "geometry_authoring.json",
        output,
        accepted_versions(),
    )
    assert report["source_point_count"] == 3
    assert report["preview_point_limit"] == 2
    assert report["preview_point_count"] == 2
    assert (output / "preview.html").read_text().count("<circle") == 2 * 6
