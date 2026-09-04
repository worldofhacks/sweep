import copy
import json
import math
import subprocess
import sys

import pytest

from tools.map_common import read_document, transform_point, validate_transform, write_document
from tools.map_validate import content_hash, seal_manifest, validate_bundle

IDENTITY = [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]
SQUARE = [[-2, -2], [2, -2], [2, 2], [-2, 2], [-2, -2]]


@pytest.fixture
def bundle(tmp_path):
    (tmp_path / "scan.ply").write_text("synthetic source\n")
    header = {"schema_version": 1, "units": "meters"}
    manifest = {
        **header,
        "bundle_version": "trial-1",
        "created_at": "2026-09-04T00:00:00Z",
        "frame": {
            "name": "building",
            "x": "toward_elevator",
            "y": "toward_street_wall",
            "z": "up",
            "origin_tag_id": 0,
            "tag0_yaw_rad": 0,
        },
        "floor_ids": ["level_1", "mezzanine"],
        "sources": [{"path": "scan.ply", "T_map_scan": IDENTITY, "rms_m": 0}],
    }
    tags = {
        **header,
        "frame": "building",
        "tags": [
            {
                "id": 0,
                "floor_id": "level_1",
                "size": 0.16,
                "x": 0,
                "y": 0,
                "z": 0,
                "yaw": 0,
                "normal": [0, 0, 1],
                "T_map_tag": IDENTITY,
                "orientation_confirmed": True,
            },
        ],
    }
    volume = {"polygon": SQUARE, "z_min": -0.1, "z_max": 3}
    zones = {
        **header,
        "geofence": volume,
        "zones": [
            {"id": name, "floor_id": "level_1", "owner_approved": False, **volume}
            for name in ["lobby", "kitchen", "atrium", "launch", "corridor"]
        ],
        "room_graph": {
            "nodes": [
                {"id": "113", "floor_id": "level_1"},
                {"id": "north_hallway", "floor_id": "level_1"},
                {"id": "mezzanine", "floor_id": "mezzanine"},
            ],
            "edges": [
                {"from": "113", "to": "mezzanine", "side": "west", "autonomous": False},
                {"from": "113", "to": "north_hallway", "side": "east", "autonomous": False},
            ],
        },
    }
    for name, value in {
        "manifest": manifest,
        "tags": tags,
        "zones": zones,
        "obstacles": {**header, "obstacles": []},
    }.items():
        write_document(tmp_path / f"{name}.yaml", value)
    seal_manifest(tmp_path)
    return tmp_path


def change(bundle, filename, mutate):
    path = bundle / filename
    document = read_document(path)
    mutate(document)
    write_document(path, document)
    seal_manifest(bundle)


def test_valid_bundle_and_cli(bundle):
    assert validate_bundle(bundle, {"trial-1"})["bundle_version"] == "trial-1"
    run = subprocess.run(
        [sys.executable, "-m", "tools.map_validate", str(bundle), "--accepted-version", "trial-1"],
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout)["flight_approved"] is False


def test_known_rotation_and_translation():
    transform = [[0, -1, 0, 3], [1, 0, 0, -2], [0, 0, 1, 7], [0, 0, 0, 1]]
    assert transform_point(transform, [2, 1, -3]) == [2, 0, 4]


@pytest.mark.parametrize(
    "transform",
    [
        [[-1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]],
        [[2, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]],
        [[1, 0, 0, math.inf], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]],
        [[1, 0], [0, 1]],
        None,
    ],
)
def test_invalid_rigid_transforms(transform):
    with pytest.raises(ValueError):
        validate_transform(transform)


@pytest.mark.parametrize("text", ['{"a":1,"a":2}', '{"a":NaN}', '{"a":1e999}', "[]"])
def test_document_rejects_ambiguous_or_nonfinite_values(tmp_path, text):
    path = tmp_path / "input.yaml"
    path.write_text(text)
    with pytest.raises(ValueError):
        read_document(path)


@pytest.mark.parametrize(
    ("filename", "mutate", "match"),
    [
        ("tags.yaml", lambda d: d["tags"].append(copy.deepcopy(d["tags"][0])), "duplicate tag"),
        ("tags.yaml", lambda d: d["tags"][0].update(id=587), "0..586"),
        ("tags.yaml", lambda d: d["tags"][0].update(id=True), "0..586"),
        ("tags.yaml", lambda d: d["tags"][0].update(size=-0.2), "positive"),
        ("tags.yaml", lambda d: d.update(units="mm"), "meters"),
        ("tags.yaml", lambda d: d["tags"][0].pop("normal"), "missing"),
        ("tags.yaml", lambda d: d["tags"][0].update(normal=[1, 0, 0]), "normal disagrees"),
        ("tags.yaml", lambda d: d["tags"][0].update(yaw=1), "yaw disagrees"),
        ("tags.yaml", lambda d: d["tags"][0].update(x=1), "position disagrees"),
        ("tags.yaml", lambda d: d["tags"][0].update(orientation_confirmed=False), "confirmed"),
        ("tags.yaml", lambda d: d["tags"][0].update(floor_id="roof"), "graph floor"),
        ("manifest.yaml", lambda d: d["frame"].update(tag0_yaw_rad=1), "Tag 0 yaw"),
        ("manifest.yaml", lambda d: d["sources"][0].update(rms_m=-1), "nonnegative"),
        ("manifest.yaml", lambda d: d.update(created_at="2026-09-04"), "UTC"),
        ("zones.yaml", lambda d: d["zones"].pop(), "required zones"),
        ("zones.yaml", lambda d: d["geofence"].update(z_max=-1), "bounds"),
        ("zones.yaml", lambda d: d["geofence"].update(z_min=1), "outside geofence"),
        ("zones.yaml", lambda d: d["room_graph"]["edges"][0].update(side="east"), "corrected"),
        ("zones.yaml", lambda d: d["room_graph"]["edges"][0].update(autonomous=True), "autonomous"),
        (
            "zones.yaml",
            lambda d: d["room_graph"]["nodes"][0].update(floor_id="roof"),
            "unknown floor",
        ),
        ("obstacles.yaml", lambda d: d.update(obstacles=None), "list"),
    ],
)
def test_invalid_bundle_content(bundle, filename, mutate, match):
    change(bundle, filename, mutate)
    with pytest.raises(ValueError, match=match):
        validate_bundle(bundle, ["trial-1"])


@pytest.mark.parametrize(
    "polygon",
    [
        [[0, 0], [1, 0], [1, 1]],
        [[0, 0], [1, 0], [2, 0], [0, 0]],
        [[0, 0], [2, 2], [0, 3], [2, 0], [0, 0]],
        [[0, 0], [2, 0], [2, 2], [2, 0], [0, 0]],
        [[0, 0], [1, "bad"], [1, 1], [0, 0]],
    ],
)
def test_malformed_polygons(bundle, polygon):
    change(bundle, "zones.yaml", lambda d: d["geofence"].update(polygon=polygon))
    with pytest.raises(ValueError):
        validate_bundle(bundle, ["trial-1"])


def test_tag_on_geofence_boundary(bundle):
    change(
        bundle,
        "zones.yaml",
        lambda d: d["geofence"].update(polygon=[[0, 0], [2, 0], [2, 2], [0, 2], [0, 0]], z_min=0),
    )
    validate_bundle(bundle, ["trial-1"])


@pytest.mark.parametrize("filename", ["scan.ply", "tags.yaml", "zones.yaml", "obstacles.yaml"])
def test_modified_bytes_fail_hash_verification(bundle, filename):
    with (bundle / filename).open("a") as file:
        file.write(" ")
    with pytest.raises(ValueError, match="hash mismatch"):
        validate_bundle(bundle, ["trial-1"])


def test_content_hash_and_explicit_versions(bundle):
    manifest = read_document(bundle / "manifest.yaml")
    assert content_hash(manifest) == content_hash(dict(reversed(list(manifest.items()))))
    for accepted in ([], ["other"], "trial-1"):
        with pytest.raises(ValueError):
            validate_bundle(bundle, accepted)
    manifest["created_at"] = "2026-09-05T00:00:00Z"
    write_document(bundle / "manifest.yaml", manifest)
    with pytest.raises(ValueError, match="content hash"):
        validate_bundle(bundle, ["trial-1"])


@pytest.mark.parametrize("source", ["../secret", "/tmp/secret", "tags.yaml"])
def test_source_path_restrictions(bundle, source):
    manifest = read_document(bundle / "manifest.yaml")
    manifest["sources"][0]["path"] = source
    write_document(bundle / "manifest.yaml", manifest)
    with pytest.raises(ValueError):
        validate_bundle(bundle, ["trial-1"])


def test_source_symlink_cannot_escape_bundle(bundle, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside") / "scan.ply"
    outside.write_text("synthetic source\n")
    (bundle / "scan.ply").unlink()
    (bundle / "scan.ply").symlink_to(outside)
    with pytest.raises(ValueError, match="unsafe"):
        validate_bundle(bundle, ["trial-1"])


def test_east_transition_requires_acceptance(bundle):
    change(bundle, "zones.yaml", lambda d: d["room_graph"]["edges"][1].update(autonomous=True))
    with pytest.raises(ValueError, match="cannot be autonomous"):
        validate_bundle(bundle, ["trial-1"])


def test_huge_integer_rejected_as_value_error(tmp_path):
    from tools.map_common import finite_number

    with pytest.raises(ValueError):
        finite_number(10**400)
    path = tmp_path / "input.yaml"
    path.write_text('{"a":' + str(10**400) + "}")
    with pytest.raises(ValueError):
        read_document(path)


@pytest.mark.parametrize("key", ["nodes", "edges"])
def test_graph_collections_must_be_lists(bundle, key):
    change(bundle, "zones.yaml", lambda d: d["room_graph"].update({key: {}}))
    with pytest.raises(ValueError, match="must be a list"):
        validate_bundle(bundle, ["trial-1"])


@pytest.mark.parametrize(
    "start,end",
    [("north_hallway", "113"), ("mezzanine", "north_hallway"), ("launch", "north_hallway")],
)
def test_unaccepted_branch_cannot_gain_autonomous_reverse_or_alternate_edge(bundle, start, end):
    def mutate(document):
        document["room_graph"]["nodes"].append({"id": "launch", "floor_id": "level_1"})
        document["room_graph"]["edges"].append(
            {"from": start, "to": end, "side": "connector", "autonomous": True}
        )

    change(bundle, "zones.yaml", mutate)
    with pytest.raises(ValueError, match="cannot be autonomous"):
        validate_bundle(bundle, ["trial-1"])
