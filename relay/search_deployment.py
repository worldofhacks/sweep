"""Strict file-backed configuration for optional visual-search runtime."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from perception.search_events import CameraPolicy
from planner.navigation import NavigationPermission
from planner.navigation_runtime import NavigationRuntime
from planner.search import SearchArea
from relay.search_runtime import SearchRuntime, SearchRuntimeConfig
from relay.settings import SettingsError


def load_search_runtime(
    env: Mapping[str, str], navigation: NavigationRuntime | None
) -> SearchRuntime | None:
    configured = env.get("SWEEP_SEARCH_CONFIG")
    if configured is None:
        return None
    if navigation is None:
        raise SettingsError("SWEEP_SEARCH_CONFIG requires SWEEP_NAVIGATION_CONFIG")
    try:
        raw = json.loads(Path(configured).read_bytes())
    except (OSError, json.JSONDecodeError) as error:
        raise SettingsError(f"invalid search JSON: {error}") from error
    if (
        not isinstance(raw, dict)
        or set(raw)
        != {
            "schema_version",
            "areas",
            "camera",
            "calibration_id",
            "source_by_drone",
            "permission_zone_ids",
            "mission_version",
            "maximum_drones",
        }
        or raw["schema_version"] != 1
    ):
        raise SettingsError("search config fields or schema_version are invalid")
    try:
        areas = {
            item["zone_id"]: SearchArea(
                item["zone_id"],
                item["floor_id"],
                tuple(tuple(point) for point in item["polygon_xy_m"]),
                item.get("floor_z_m", 0),
            )
            for item in raw["areas"]
            if set(item) <= {"zone_id", "floor_id", "polygon_xy_m", "floor_z_m"}
        }
        camera = CameraPolicy(**raw["camera"])
        sources = {int(key): value for key, value in raw["source_by_drone"].items()}
        permission = NavigationPermission(frozenset(raw["permission_zone_ids"]))
        artifact = navigation.artifact()
        allowed = {zone.zone_id for zone in artifact.zones if zone.owner_approved}
        if not areas or set(areas) - allowed or not permission.permitted_zone_ids <= allowed:
            raise ValueError("search areas and permission must be owner-approved map zones")
        config = SearchRuntimeConfig(
            areas,
            artifact.map_pin,
            camera,
            raw["calibration_id"],
            sources,
            permission,
            raw["mission_version"],
            raw["maximum_drones"],
        )
    except (KeyError, TypeError, ValueError) as error:
        raise SettingsError(f"invalid search config: {error}") from error
    return SearchRuntime(config, navigation)
