import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from tools.measurement_worksheet import import_worksheet
from tools.world_bundle import validate_candidate


def fi(feet=0, inches=0, sign=1):
    value = {"feet": feet, "inches": inches}
    if sign < 0:
        value["sign"] = -1
    return value


def xy(x_feet=0, x_inches=0, y_feet=0, y_inches=0, x_sign=1, y_sign=1):
    return {"x": fi(x_feet, x_inches, x_sign), "y": fi(y_feet, y_inches, y_sign)}


def worksheet():
    return {
        "schema_version": 1,
        "worksheet_kind": "ohmni-measurement-worksheet",
        "map": {
            "map_id": "lab-a",
            "map_version": "worksheet-fixture-v1",
            "physical_datum": "tag_0_center",
            "axes": {"x": "toward_elevator", "y": "toward_street_wall", "z": "up"},
            "provenance": {"name": "operator-sheet-17", "captured_at": "2026-09-07T00:00:00Z"},
        },
        "floor": {
            "id": "level_1",
            "site_datum": "building-entrance-benchmark",
            "elevation_from_site_datum": fi(12, 0),
        },
        "tag_black_size": fi(0, 6.2992125984),
        "grid": {
            "tag_ids": [[0, 1, 4, 5], [2, 3, 6, 7], [8, 9, 10, 11]],
            "top_left_black_corner_xy": xy(10, 0, 20, 0),
            "across_axis": "+x",
            "down_axis": "+y",
            "center_spacing_across": fi(6, 6.7401574803),
            "center_spacing_down": fi(6, 6.7401574803),
        },
        "moved_tags": [{"tag_id": 3, "dx": fi(0, 1), "dy": fi(0, 0)}],
        "wall_tags": [
            {
                "id": 12,
                "center_xy": xy(3, 0, 1, 0),
                "center_height_above_floor": fi(4, 0),
                "wall_normal": "-y",
            }
        ],
        "independent_tape_checks": [
            {
                "name": "near",
                "tag_ids": [0, 1],
                "measured_distance": fi(6, 6.7401574803),
                "maximum_error": fi(0, 1),
            },
            {
                "name": "left",
                "tag_ids": [0, 2],
                "measured_distance": fi(6, 6.7401574803),
                "maximum_error": fi(0, 1),
            },
            {
                "name": "diagonal",
                "tag_ids": [0, 3],
                "measured_distance": fi(9, 3.2),
                "maximum_error": fi(0, 1),
            },
        ],
        "registration_tie_ids": [0, 1, 2],
        "floor_zone": {
            "id": "lobby",
            "polygon_xy": [
                xy(0, 10, 0, 10, -1, -1),
                xy(40, 0, 0, 10, 1, -1),
                xy(40, 0, 40, 0),
                xy(0, 10, 40, 0, -1),
            ],
            "z_min": fi(0, 0),
            "z_max": fi(10, 0),
        },
        "authored_hazards": [
            {
                "id": "table",
                "polygon_xy": [xy(3, 0, 3, 0), xy(5, 0, 3, 0), xy(5, 0, 5, 0), xy(3, 0, 5, 0)],
                "z_min": fi(0, 0),
                "z_max": fi(3, 0),
            }
        ],
        "connector_lines": [
            {
                "id": "hall",
                "direction_axis": "+x",
                "near_wall": {"parallel_axis": "+x", "offset": fi(1, 0)},
                "far_wall": {"parallel_axis": "-x", "offset": fi(5, 0)},
                "end_ties": {"start_tag_id": 0, "end_tag_id": 1},
            }
        ],
    }


def _write_documents(path, documents):
    for name, document in documents.items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(document, indent=2) + "\n")


def test_import_converts_feet_inches_and_creates_current_map_artifacts(tmp_path):
    source = json.dumps(worksheet(), indent=2).encode()
    documents = import_worksheet(worksheet(), source)

    tags = documents["tags.yaml"]["tags"]
    by_id = {tag["id"]: tag for tag in tags}
    assert len(tags) == 13
    assert by_id[0]["T_world_tag"] == [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]
    assert by_id[1]["x_m"] == pytest.approx(2)
    assert by_id[2]["y_m"] == pytest.approx(2)
    assert by_id[3]["x_m"] == pytest.approx(2 + 0.0254)
    assert by_id[12]["z_m"] == pytest.approx(1.2192)
    assert by_id[12]["normal"] == [0, -1, 0]
    assert by_id[0]["tape_verification"]["evidence_path"] == "evidence/tape-00-a.json"
    known = documents["evidence/known_tags.json"]["tags"]
    assert [item["tag_id"] for item in known] == [0, 1, 2]
    for actual, expected in zip(
        [item["xy_m"] for item in known], [[0, 0], [2, 0], [0, 2]], strict=True
    ):
        assert actual == pytest.approx(expected)
    assert documents["zones.yaml"]["zones"][0]["id"] == "lobby"
    assert documents["obstacles.yaml"]["obstacles"][0]["id"] == "table"
    assert documents["worksheet-metadata.json"]["floor_elevation_m"] == pytest.approx(3.6576)
    assert documents["worksheet-metadata.json"]["connector_lines"] == [
        {
            "id": "hall",
            "direction_axis": "+x",
            "wall_offsets_m": [0.3048, 1.524],
            "end_tie_tag_ids": [0, 1],
        }
    ]


def test_output_tags_and_independent_tape_validate_in_current_world_bundle(tmp_path):
    documents = import_worksheet(worksheet(), json.dumps(worksheet()).encode())
    bundle = Path(__file__).parent / "fixtures" / "world_bundle"
    destination = tmp_path / "bundle"
    import shutil

    shutil.copytree(bundle, destination)
    _write_documents(
        destination,
        {name: value for name, value in documents.items() if name.startswith("evidence/tape-")},
    )
    _write_documents(
        destination,
        {
            "tags.yaml": documents["tags.yaml"],
            "zones.yaml": documents["zones.yaml"],
            "obstacles.yaml": documents["obstacles.yaml"],
            "evidence/known_tags.json": documents["evidence/known_tags.json"],
        },
    )
    manifest = json.loads((destination / "manifest.yaml").read_text())
    for name in ("tags.yaml", "zones.yaml", "obstacles.yaml"):
        manifest["files"][name] = hashlib.sha256((destination / name).read_bytes()).hexdigest()
    manifest["map_id"] = "lab-a"
    manifest["bundle_version"] = "worksheet-fixture-v1"
    manifest["registration"]["known_tags"]["sha256"] = hashlib.sha256(
        (destination / "evidence/known_tags.json").read_bytes()
    ).hexdigest()
    manifest["content_sha256"] = hashlib.sha256(
        json.dumps(
            {key: value for key, value in manifest.items() if key != "content_sha256"},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    (destination / "manifest.yaml").write_text(json.dumps(manifest))
    validate_candidate(destination)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda value: value["map"].update(axes={"x": "carpet", "y": "wall", "z": "up"}), "axes"),
        (lambda value: value["floor"].pop("elevation_from_site_datum"), "floor requires"),
        (lambda value: value["grid"].update(across_axis="-x"), "grid orientation"),
        (lambda value: value["wall_tags"][0].pop("wall_normal"), "wall tag requires"),
        (
            lambda value: value["connector_lines"][0]["near_wall"].update(parallel_axis="+y"),
            "wall offsets",
        ),
        (lambda value: value["connector_lines"][0].pop("end_ties"), "connector line needs"),
        (lambda value: value.update(registration_tie_ids=[0, 1, 4]), "collinear"),
    ],
)
def test_import_refuses_missing_orientation_or_3d_ties_and_unsupported_connector_geometry(
    change, message
):
    value = worksheet()
    change(value)
    with pytest.raises(ValueError, match=message):
        import_worksheet(value, json.dumps(value).encode())


def test_import_checks_measured_diagonal_without_using_it_to_fit_geometry():
    value = worksheet()
    value["independent_tape_checks"][2]["measured_distance"] = fi(1, 0)
    with pytest.raises(ValueError, match="diagonal disagrees"):
        import_worksheet(value, json.dumps(value).encode())


def test_cli_rejects_duplicate_json_keys_before_creating_output(tmp_path):
    value = json.dumps(worksheet()).replace(
        '"schema_version": 1', '"schema_version": 2, "schema_version": 1', 1
    )
    source = tmp_path / "worksheet.yaml"
    output = tmp_path / "output"
    source.write_text(value)
    result = subprocess.run(
        [sys.executable, "-m", "tools.measurement_worksheet", str(source), str(output)],
        check=False,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 1
    assert "duplicate key" in result.stdout
    assert not output.exists()


def test_cli_writes_immutable_input_hash_and_refuses_existing_output(tmp_path):
    source = tmp_path / "worksheet.yaml"
    output = tmp_path / "output"
    payload = json.dumps(worksheet()).encode()
    source.write_bytes(payload)
    result = subprocess.run(
        [sys.executable, "-m", "tools.measurement_worksheet", str(source), str(output)],
        check=False,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stdout
    metadata = json.loads((output / "worksheet-metadata.json").read_text())
    assert metadata["worksheet_sha256"] == hashlib.sha256(payload).hexdigest()
    assert (output / "worksheet-source.yaml").read_bytes() == payload
    second = subprocess.run(
        [sys.executable, "-m", "tools.measurement_worksheet", str(source), str(output)],
        check=False,
        text=True,
        capture_output=True,
    )
    assert second.returncode == 1
    assert "File exists" in second.stdout
