"""Strict file-backed configuration for optional visual-search runtime."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from perception.search_events import CameraPolicy
from planner.navigation import NavigationPermission
from planner.navigation_runtime import NavigationArtifact, NavigationRuntime
from planner.search import SearchArea
from relay.search_runtime import SearchRuntime, SearchRuntimeConfig
from relay.settings import SettingsError

_AREA_FIELDS = frozenset({"zone_id", "floor_id", "polygon_xy_m", "floor_z_m"})
_REQUIRED_AREA_FIELDS = _AREA_FIELDS - {"floor_z_m"}


def load_search_runtime(
    env: Mapping[str, str], navigation: NavigationRuntime | None
) -> SearchRuntime | None:
    configured = env.get("SWEEP_SEARCH_CONFIG")
    if configured is None:
        return None
    if navigation is None:
        raise SettingsError("SWEEP_SEARCH_CONFIG requires SWEEP_NAVIGATION_CONFIG")
    try:
        raw = json.loads(Path(configured).read_bytes(), object_pairs_hook=_no_duplicate_fields)
    except (OSError, ValueError) as error:
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
        artifact = navigation.artifact()
        areas = _areas(raw["areas"], artifact)
        camera = CameraPolicy(**raw["camera"])
        sources = _sources(raw["source_by_drone"])
        permission = NavigationPermission(frozenset(raw["permission_zone_ids"]))
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


def _no_duplicate_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON field {key!r}")
        result[key] = value
    return result


def _areas(raw: object, artifact: NavigationArtifact) -> dict[str, SearchArea]:
    if not isinstance(raw, list):
        raise ValueError("search areas must be a list")
    zones = {zone.zone_id: zone for zone in artifact.zones}
    areas: dict[str, SearchArea] = {}
    for item in raw:
        if not isinstance(item, dict) or not _REQUIRED_AREA_FIELDS <= set(item) <= _AREA_FIELDS:
            raise ValueError("search area fields are invalid")
        area = SearchArea(
            item["zone_id"],
            item["floor_id"],
            tuple(tuple(point) for point in item["polygon_xy_m"]),
            item.get("floor_z_m", 0),
        )
        zone = zones.get(area.zone_id)
        if zone is None:
            raise ValueError("search area must name a configured map zone")
        if zone.floor_id != area.floor_id:
            raise ValueError("search area floor must match its map zone")
        if area.zone_id in areas:
            raise ValueError("search areas must not duplicate zone ids")
        areas[area.zone_id] = area
    return areas


def _sources(raw: object) -> dict[int, object]:
    if not isinstance(raw, dict):
        raise ValueError("search camera sources must be an object")
    sources: dict[int, object] = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            raise ValueError("search camera drone id must be a canonical positive integer")
        drone_id = int(key)
        if drone_id <= 0 or key != str(drone_id):
            raise ValueError("search camera drone id must be a canonical positive integer")
        if drone_id in sources:
            raise ValueError("search camera sources must not duplicate drone ids")
        sources[drone_id] = value
    return sources
