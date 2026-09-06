from __future__ import annotations

import json

import pytest

from planner.test_navigation_runtime import _runtime
from relay.search_deployment import load_search_runtime
from relay.settings import SettingsError


def _config() -> dict[str, object]:
    return {
        "schema_version": 1,
        "areas": [
            {
                "zone_id": "atrium",
                "floor_id": "level_1",
                "polygon_xy_m": [[0, 0], [2, 0], [2, 2], [0, 2]],
            }
        ],
        "camera": {
            "horizontal_fov_deg": 90,
            "vertical_fov_deg": 90,
            "height_agl_m": 1,
            "gimbal_pitch_deg": -90,
            "gimbal_min_pitch_deg": -90,
            "gimbal_max_pitch_deg": 0,
            "overlap_fraction": 0.25,
        },
        "calibration_id": "camera-v1",
        "source_by_drone": {"1": "camera-1"},
        "permission_zone_ids": ["atrium"],
        "mission_version": 1,
        "maximum_drones": 1,
    }


def test_search_runtime_loads_only_from_an_explicit_valid_file(tmp_path) -> None:
    path = tmp_path / "search.json"
    path.write_text(json.dumps(_config()))
    assert load_search_runtime({}, _runtime()) is None
    loaded = load_search_runtime({"SWEEP_SEARCH_CONFIG": str(path)}, _runtime())
    assert loaded is not None
    assert loaded.config.areas["atrium"].zone_id == "atrium"


def test_malformed_search_config_fails_closed(tmp_path) -> None:
    path = tmp_path / "search.json"
    path.write_text("{}")
    with pytest.raises(SettingsError):
        load_search_runtime({"SWEEP_SEARCH_CONFIG": str(path)}, _runtime())
    with pytest.raises(SettingsError):
        load_search_runtime({"SWEEP_SEARCH_CONFIG": str(path)}, None)
