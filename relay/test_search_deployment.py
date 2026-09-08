from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from planner.navigation import NavigationPermission, Zone
from planner.test_navigation_runtime import setup_runtime
from relay.search_deployment import load_search_config
from relay.settings import SettingsError


def _config(permission_zone_ids: list[str]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "areas": [
            {
                "zone_id": "atrium",
                "floor_id": "level_1",
                "polygon_xy_m": [[0, 0], [8, 0], [8, 4], [0, 4]],
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
        "permission_zone_ids": permission_zone_ids,
        "mission_version": 1,
        "maximum_drones": 1,
    }


def test_search_configuration_cannot_expand_signed_navigation_permission(tmp_path) -> None:
    runtime = setup_runtime()[0]
    artifact = runtime.artifact()
    additional = Zone(
        "owner-approved-but-not-signed",
        "level_1",
        True,
        ((9, 0), (10, 0), (10, 1), (9, 1), (9, 0)),
        0,
        3,
        (),
    )
    deployment = SimpleNamespace(
        artifact=lambda: replace(artifact, zones=(*artifact.zones, additional)),
        permission=NavigationPermission(frozenset({"atrium"})),
    )
    path = tmp_path / "search.json"
    path.write_text(json.dumps(_config(["atrium", "owner-approved-but-not-signed"])))

    with pytest.raises(SettingsError, match="signed navigation permission"):
        load_search_config({"SWEEP_SEARCH_CONFIG": str(path)}, deployment)
