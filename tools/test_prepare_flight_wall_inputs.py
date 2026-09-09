import copy
import hashlib
import json
import shutil
from pathlib import Path

import pytest

from tools.map_geometry import _v2_blocked
from tools.prepare_flight_wall_inputs import APPROVAL, INPUTS, prepare_inputs, transform_obstacles

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def approved_root(tmp_path):
    for path in [APPROVAL, *INPUTS.values()]:
        destination = tmp_path / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / path, destination)
    return tmp_path


def test_prepared_walls_retain_source_frame_and_cannot_claim_flight_approval(approved_root):
    outputs = prepare_inputs(approved_root)
    source = json.loads((approved_root / INPUTS["obstacles"]).read_text())
    flight = outputs["flight-wall-inputs.json"]
    console = outputs["console-wall-features.json"]
    assert set(outputs) == {"flight-wall-inputs.json", "console-wall-features.json"}
    assert flight["obstacleDocument"] == {**source, "frame": flight["coordinateFrame"]}
    assert flight["coordinateFrame"] == "unit11_atrium_38_to_39_v1"
    assert flight["flightAuthorized"] is False
    assert flight["worldRegistration"]["status"] == "missing"
    assert "camera_visibility_calibration" in flight["pendingInputs"]
    assert "T_world_source" in flight["pendingInputs"]
    assert len(flight["localizationCandidateTagIds"]) == 51
    assert not {29, 35, 49} & set(flight["localizationCandidateTagIds"])
    for feature, obstacle in zip(console["features"], source["obstacles"], strict=True):
        assert feature["id"] == obstacle["id"]
        assert feature["points"] == [{"x": x, "y": y} for x, y in obstacle["polygon"]]


@pytest.mark.parametrize("name", INPUTS)
def test_changed_approved_input_bytes_are_rejected(approved_root, name):
    path = approved_root / INPUTS[name]
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="approved input hash mismatch"):
        prepare_inputs(approved_root)


@pytest.mark.parametrize(
    "name,field", [("geometry", "tagMapSha256"), ("availability", "sourceSha256")]
)
def test_inputs_cannot_be_reapproved_against_another_tag_map(approved_root, name, field):
    path = approved_root / INPUTS[name]
    document = json.loads(path.read_text())
    document[field] = "0" * 64
    path.write_text(json.dumps(document))
    approval_path = approved_root / APPROVAL
    approval = json.loads(approval_path.read_text())
    for item in approval["inputs"]:
        if item["path"] == INPUTS[name].as_posix():
            item["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    approval_path.write_text(json.dumps(approval))
    with pytest.raises(ValueError, match="same tag map"):
        prepare_inputs(approved_root)


def world_transform(source_frame):
    return {
        "sourceFrame": source_frame,
        "targetFrame": "world",
        "transformId": "test-rotation-translation",
        "T_world_source": [[0, -1, 0, 5], [1, 0, 0, -2], [0, 0, 1, 0.5], [0, 0, 0, 1]],
        "evidence": "Synthetic transform for isolated software verification.",
    }


def test_supplied_transform_moves_every_wall_vertex_and_vertical_bound(approved_root):
    source = prepare_inputs(approved_root)["flight-wall-inputs.json"]["obstacleDocument"]
    transform_path = approved_root / "transform.json"
    transform_path.write_text(json.dumps(world_transform(source["frame"])))
    outputs = prepare_inputs(approved_root, world_transform=transform_path)
    world = outputs["obstacles.yaml"]
    assert world["frame"] == "world"
    for actual, original in zip(world["obstacles"], source["obstacles"], strict=True):
        for point, (x, y) in zip(actual["polygon"], original["polygon"], strict=True):
            assert point == pytest.approx([5 - y, x - 2])
        assert actual["z_min_m"] == pytest.approx(original["z_min_m"] + 0.5)
        assert actual["z_max_m"] == pytest.approx(original["z_max_m"] + 0.5)
    flight = outputs["flight-wall-inputs.json"]
    assert flight["obstacleDocument"] == source
    assert flight["flightAuthorized"] is False
    assert flight["worldRegistration"]["status"] == "supplied_pending_bundle_validation"
    assert "world_bundle_registration_and_datum_validation" in flight["pendingInputs"]


@pytest.mark.parametrize(
    "matrix",
    [
        [[2, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]],
        [[-1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]],
        [[1, 0, 0, 0], [0, 0, -1, 0], [0, 1, 0, 0], [0, 0, 0, 1]],
        [[1, 0, 0, float("nan")], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]],
        [[True, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]],
    ],
)
def test_scaling_reflection_tilt_nonfinite_and_boolean_transforms_are_rejected(matrix):
    document = prepare_inputs(ROOT)["flight-wall-inputs.json"]["obstacleDocument"]
    transform = {**world_transform(document["frame"]), "T_world_source": matrix}
    with pytest.raises(ValueError, match="transform"):
        transform_obstacles(document, transform, document["frame"])


def test_real_flight_grid_hazard_check_blocks_each_exported_wall_and_keeps_openings_clear():
    source = prepare_inputs(ROOT)["flight-wall-inputs.json"]["obstacleDocument"]
    world = transform_obstacles(source, world_transform(source["frame"]), source["frame"])
    fence = {
        "polygon": [[-100, -100], [100, -100], [100, 100], [-100, 100], [-100, -100]],
        "z_min_m": -10,
        "z_max_m": 10,
    }
    for obstacle in world["obstacles"]:
        x, y = (sum(p[i] for p in obstacle["polygon"][:-1]) / 4 for i in (0, 1))
        cell = (x - 0.01, y - 0.01, x + 0.01, y + 0.01)
        assert _v2_blocked(cell, 1.5, 1.5, fence, world["obstacles"], 0) == "static_hazard"
        assert _v2_blocked(cell, 3.5, 3.5, fence, world["obstacles"], 0) is None
        without_wall = [item for item in world["obstacles"] if item["id"] != obstacle["id"]]
        assert _v2_blocked(cell, 1.5, 1.5, fence, without_wall, 0) is None
    for x, y in [(21.1, 0.55), (7.9, 0.69)]:
        wx, wy = 5 - y, x - 2
        cell = (wx - 0.01, wy - 0.01, wx + 0.01, wy + 0.01)
        assert _v2_blocked(cell, 1.5, 1.5, fence, world["obstacles"], 0.2) is None


def test_transform_source_identity_must_match_obstacles():
    source = prepare_inputs(ROOT)["flight-wall-inputs.json"]["obstacleDocument"]
    transform = copy.deepcopy(world_transform(source["frame"]))
    transform["sourceFrame"] = "different-survey"
    with pytest.raises(ValueError, match="source frame"):
        transform_obstacles(source, transform, source["frame"])
