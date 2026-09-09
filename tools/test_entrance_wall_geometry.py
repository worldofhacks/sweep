import hashlib
import json
from pathlib import Path

import pytest

from tools.console_world_bundle import _collides
from tools.entrance_wall_geometry import build_wall_geometry
from tools.prepare_real_navigation_package import build_package

DEPLOYMENT = Path(__file__).resolve().parents[1] / "deployments" / "real-navigation"
TAG_MAP = DEPLOYMENT / "tag-map-53.json"
MEASUREMENTS = DEPLOYMENT / "wall-measurements-20260909.json"


def test_owner_offsets_become_world_walls_from_black_corners():
    geometry = build_wall_geometry(TAG_MAP, MEASUREMENTS)
    expected = [
        (21.167921, -0.158512),
        (21.159549, 1.263863),
        (7.935415, -0.060017),
        (7.926593, 1.438557),
        (0.104695, -2.550870),
        (-0.806140, -1.354348),
    ]
    assert len(geometry["wallSegments"]) == 6
    assert geometry["wallSegments"][4]["startXYM"] == pytest.approx(
        geometry["wallSegments"][5]["startXYM"]
    )
    for wall, point in zip(geometry["wallSegments"], expected, strict=True):
        assert wall["wallPointXYM"] == pytest.approx(point, abs=1e-6)
        start, end = wall["startXYM"], wall["endXYM"]
        assert ((end[0] - start[0]) ** 2 + (end[1] - start[1]) ** 2) ** 0.5 == pytest.approx(2)
    for pair, width in (
        (geometry["wallSegments"][:2], 1.4224),
        (geometry["wallSegments"][2:4], 1.4986),
    ):
        a, b = (wall["wallPointXYM"] for wall in pair)
        assert ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5 == pytest.approx(width)


def test_world_obstacles_block_wall_crossings_and_leave_entrances_open():
    geometry = build_wall_geometry(TAG_MAP, MEASUREMENTS)
    polygons = [obstacle["polygon"] for obstacle in geometry["obstacleDocument"]["obstacles"]]
    crossings = [
        [[21.168, -0.4], [21.168, 0.1]],
        [[7.93, 1.2], [7.93, 1.7]],
        [[0.105, -2.3], [0.105, -2.8]],
        [[-0.5, -1.354], [-1.1, -1.354]],
    ]
    for crossing in crossings:
        assert any(_collides(crossing, obstacle) for obstacle in polygons)
    for entrance in ([[20.5, 0.55], [21.8, 0.55]], [[7.3, 0.69], [8.5, 0.69]]):
        assert not any(_collides(entrance, obstacle, margin=0.2) for obstacle in polygons)
    assert all(
        obstacle["z_max_m"] == 2.4384 for obstacle in geometry["obstacleDocument"]["obstacles"]
    )
    assert geometry["heightLimits"]["softCeilingM"] == 2.1336


def test_staging_uses_the_generated_wall_obstacles_without_changing_the_tag_map():
    before = TAG_MAP.read_bytes()
    package = build_package(
        TAG_MAP, map_approval=DEPLOYMENT / "map-approval.json", wall_measurements=MEASUREMENTS
    )
    assert package["wallGeometry"] == build_wall_geometry(TAG_MAP, MEASUREMENTS)
    assert package["wallGeometry"]["tagMapSha256"] == hashlib.sha256(before).hexdigest()
    assert TAG_MAP.read_bytes() == before
    assert package["activation"] == "blocked"


def test_wall_measurements_cannot_be_applied_to_a_different_map(tmp_path):
    survey = json.loads(MEASUREMENTS.read_text())
    survey["tagMapSha256"] = "0" * 64
    path = tmp_path / "measurements.json"
    path.write_text(json.dumps(survey))
    with pytest.raises(ValueError, match="exact tag map"):
        build_wall_geometry(TAG_MAP, path)
