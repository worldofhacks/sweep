"""Strict file-backed configuration for opt-in mapped formations and visual search."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from math import isfinite
from pathlib import Path

from perception.search_events import CameraPolicy
from planner.mapped_formation_runtime import (
    ConfiguredFormation,
    MappedFormationRuntime,
    MappedFormationRuntimeConfig,
)
from planner.mapped_formations import FormationLayout, FormationPermission, FormationZone
from planner.navigation import ArtifactPin, NavigationPermission, Pose
from planner.navigation_runtime import NavigationRuntime
from planner.search import SearchArea
from relay.search_runtime import SearchRuntimeConfig
from relay.settings import SettingsError


@dataclass(frozen=True, slots=True)
class MissionRuntimeConfig:
    mapped_formations: MappedFormationRuntime | None = None
    search: SearchRuntimeConfig | None = None


def load_mission_config(
    navigation: NavigationRuntime, environ: Mapping[str, str] | None = None
) -> MissionRuntimeConfig | None:
    values = os.environ if environ is None else environ
    configured = values.get("SWEEP_MISSION_CONFIG")
    if configured is None:
        return None
    path = Path(configured).resolve()
    raw = _object(_read(path), "mission config")
    _only(raw, {"schema_version", "mapped_formations", "search"}, "mission config")
    if raw.get("schema_version") != 1:
        raise SettingsError("mission config schema_version must be 1")
    formations = raw.get("mapped_formations")
    search = raw.get("search")
    if formations is None and search is None:
        raise SettingsError("mission config must explicitly configure a mission runtime")
    if formations is not None and not isinstance(formations, dict):
        raise SettingsError("mapped_formations must be an object or null")
    if search is not None and not isinstance(search, dict):
        raise SettingsError("search must be an object or null")
    return MissionRuntimeConfig(
        mapped_formations=(None if formations is None else _formations(formations, navigation)),
        search=None if search is None else _search(search, navigation),
    )


def _formations(value: dict[str, object], navigation: NavigationRuntime) -> MappedFormationRuntime:
    _only(value, {"permission_zone_ids", "formations"}, "mapped formation config")
    permission_ids = _identifiers(value.get("permission_zone_ids"), "permission_zone_ids")
    raw_formations = _object(value.get("formations"), "formations")
    if not raw_formations:
        raise SettingsError("formations must be nonempty")
    formations: dict[str, ConfiguredFormation] = {}
    for name, raw in raw_formations.items():
        name = _text(name, "formation name")
        item = _object(raw, "formation")
        _only(item, {"shape", "zone", "layout"}, "formation")
        shape = item.get("shape")
        if not isinstance(shape, str) or shape not in {"line", "column", "wedge", "diamond"}:
            raise SettingsError("formation shape is invalid")
        zone = _formation_zone(_object(item.get("zone"), "formation zone"))
        layout = _formation_layout(_object(item.get("layout"), "formation layout"))
        if name in formations:
            raise SettingsError("formation names must be unique")
        formations[name] = ConfiguredFormation(shape, zone, layout)
    zone_ids = {formation.zone.zone_id for formation in formations.values()}
    if set(permission_ids) != zone_ids:
        raise SettingsError(
            "formation permissions must name exactly the configured formation zones"
        )
    try:
        config = MappedFormationRuntimeConfig(formations, navigation.config)
    except ValueError as error:
        raise SettingsError(f"invalid mapped formation configuration: {error}") from error
    return MappedFormationRuntime(
        navigation.artifact, config, FormationPermission(frozenset(permission_ids))
    )


def _formation_zone(value: dict[str, object]) -> FormationZone:
    _only(
        value,
        {
            "zone_id",
            "floor_id",
            "polygon_xy",
            "z_min_m",
            "z_max_m",
            "owner_approved",
            "formation_enabled",
        },
        "formation zone",
    )
    try:
        return FormationZone(
            _text(value.get("zone_id"), "zone_id"),
            _text(value.get("floor_id"), "floor_id"),
            _polygon(value.get("polygon_xy"), "polygon_xy"),
            _number(value.get("z_min_m"), "z_min_m"),
            _number(value.get("z_max_m"), "z_max_m"),
            _boolean(value.get("owner_approved"), "owner_approved"),
            _boolean(value.get("formation_enabled"), "formation_enabled"),
        )
    except ValueError as error:
        raise SettingsError(f"invalid formation zone: {error}") from error


def _formation_layout(value: dict[str, object]) -> FormationLayout:
    _only(value, {"center", "heading_rad", "spacing_m", "altitude_offsets_m"}, "formation layout")
    center = _object(value.get("center"), "formation center")
    _only(center, {"x_m", "y_m", "z_m", "floor_id"}, "formation center")
    offsets = value.get("altitude_offsets_m")
    if not isinstance(offsets, list) or not offsets:
        raise SettingsError("altitude_offsets_m must be a nonempty array")
    try:
        return FormationLayout(
            Pose(
                _number(center.get("x_m"), "x_m"),
                _number(center.get("y_m"), "y_m"),
                _number(center.get("z_m"), "z_m"),
                _text(center.get("floor_id"), "floor_id"),
            ),
            _number(value.get("heading_rad"), "heading_rad"),
            _positive(value.get("spacing_m"), "spacing_m"),
            tuple(_number(offset, "altitude offset") for offset in offsets),
        )
    except ValueError as error:
        raise SettingsError(f"invalid formation layout: {error}") from error


def _search(value: dict[str, object], navigation: NavigationRuntime) -> SearchRuntimeConfig:
    _only(
        value,
        {
            "areas",
            "map_pin",
            "camera",
            "calibration_id",
            "source_by_drone",
            "permission_zone_ids",
            "mission_version",
            "maximum_drones",
            "floor_z_m",
            "height_tolerance_m",
            "camera_offset_z_m",
        },
        "search config",
    )
    areas_raw = value.get("areas")
    if not isinstance(areas_raw, list) or not areas_raw:
        raise SettingsError("search areas must be a nonempty array")
    areas = {}
    for raw in areas_raw:
        item = _object(raw, "search area")
        _only(item, {"zone_id", "floor_id", "polygon_xy_m"}, "search area")
        try:
            area = SearchArea(
                _text(item.get("zone_id"), "zone_id"),
                _text(item.get("floor_id"), "floor_id"),
                _polygon(item.get("polygon_xy_m"), "polygon_xy_m"),
            )
        except ValueError as error:
            raise SettingsError(f"invalid search area: {error}") from error
        if area.zone_id in areas:
            raise SettingsError("search area zone ids must be unique")
        areas[area.zone_id] = area
    permission_ids = _identifiers(value.get("permission_zone_ids"), "permission_zone_ids")
    if set(permission_ids) != set(areas):
        raise SettingsError("search permissions must name exactly the configured search areas")
    pin = _pin(_object(value.get("map_pin"), "search map_pin"))
    try:
        current_pin = navigation.artifact().map_pin
    except ValueError as error:
        raise SettingsError(f"unable to read navigation artifact: {error}") from error
    if pin != current_pin:
        raise SettingsError("search map_pin must match the navigation artifact")
    camera = _camera(_object(value.get("camera"), "search camera"))
    source_raw = _object(value.get("source_by_drone"), "source_by_drone")
    if not source_raw:
        raise SettingsError("source_by_drone must explicitly assign a camera")
    sources = {}
    for drone_id, source_id in source_raw.items():
        if (
            not isinstance(drone_id, str)
            or not drone_id.isascii()
            or not drone_id.isdecimal()
            or drone_id.startswith("0")
        ):
            raise SettingsError("camera source drone ids must be positive decimal strings")
        sources[int(drone_id)] = _text(source_id, "camera source")
    try:
        return SearchRuntimeConfig(
            areas,
            pin,
            camera,
            _text(value.get("calibration_id"), "calibration_id"),
            sources,
            NavigationPermission(frozenset(permission_ids)),
            _positive_int(value.get("mission_version"), "mission_version"),
            _positive_int(value.get("maximum_drones"), "maximum_drones"),
            floor_z_m=_number(value.get("floor_z_m"), "floor_z_m"),
            height_tolerance_m=_nonnegative(value.get("height_tolerance_m"), "height_tolerance_m"),
            camera_offset_z_m=_number(value.get("camera_offset_z_m"), "camera_offset_z_m"),
        )
    except ValueError as error:
        raise SettingsError(f"invalid search configuration: {error}") from error


def _camera(value: dict[str, object]) -> CameraPolicy:
    fields = {
        "horizontal_fov_deg",
        "vertical_fov_deg",
        "height_agl_m",
        "gimbal_pitch_deg",
        "gimbal_min_pitch_deg",
        "gimbal_max_pitch_deg",
        "overlap_fraction",
    }
    _only(value, fields, "search camera")
    try:
        return CameraPolicy(**{name: _number(value.get(name), name) for name in fields})
    except ValueError as error:
        raise SettingsError(f"invalid search camera: {error}") from error


def _pin(value: dict[str, object]) -> ArtifactPin:
    _only(value, {"version", "content_sha256"}, "artifact pin")
    version = _text(value.get("version"), "artifact pin version")
    digest = value.get("content_sha256")
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(c not in "0123456789abcdef" for c in digest)
    ):
        raise SettingsError("artifact pin content_sha256 must be lowercase hexadecimal")
    return ArtifactPin(version, digest)


def _polygon(value: object, name: str) -> tuple[tuple[float, float], ...]:
    if not isinstance(value, list) or len(value) < 3:
        raise SettingsError(f"{name} must be an array with at least three points")
    points = []
    for point in value:
        if not isinstance(point, list) or len(point) != 2:
            raise SettingsError(f"{name} points must have two coordinates")
        points.append((_number(point[0], name), _number(point[1], name)))
    return tuple(points)


def _read(path: Path) -> object:
    try:
        return json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError) as error:
        raise SettingsError(f"invalid mission config JSON: {error}") from error


def _object(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise SettingsError(f"{name} must be an object")
    return value


def _only(value: dict[str, object], fields: set[str], name: str) -> None:
    if set(value) != fields:
        raise SettingsError(f"{name} has missing or unknown fields")


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 256:
        raise SettingsError(f"{name} must be nonempty text up to 256 characters")
    return value


def _identifiers(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise SettingsError(f"{name} must be a nonempty array")
    identifiers = tuple(_text(item, name) for item in value)
    if len(set(identifiers)) != len(identifiers):
        raise SettingsError(f"{name} must not repeat identifiers")
    return identifiers


def _boolean(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise SettingsError(f"{name} must be boolean")
    return value


def _number(value: object, name: str) -> float:
    if type(value) not in (int, float) or not isfinite(value):
        raise SettingsError(f"{name} must be finite")
    return float(value)


def _positive(value: object, name: str) -> float:
    number = _number(value, name)
    if number <= 0:
        raise SettingsError(f"{name} must be positive")
    return number


def _nonnegative(value: object, name: str) -> float:
    number = _number(value, name)
    if number < 0:
        raise SettingsError(f"{name} must be nonnegative")
    return number


def _positive_int(value: object, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise SettingsError(f"{name} must be a positive integer")
    return value


def load_detection_camera_ids(environ: Mapping[str, str] | None = None) -> Mapping[int, str]:
    """Read explicit physical camera identities used to bind detection evidence to a drone."""
    values = os.environ if environ is None else environ
    raw = values.get("SWEEP_DETECTION_CAMERA_IDS_JSON")
    if raw is None or raw == "":
        return {}
    try:
        configured = json.loads(raw)
    except json.JSONDecodeError as error:
        raise SettingsError(f"invalid detection camera identities JSON: {error}") from error
    identities = _object(configured, "detection camera identities")
    result = {}
    for drone_id, camera_id in identities.items():
        if (
            not isinstance(drone_id, str)
            or not drone_id.isascii()
            or not drone_id.isdecimal()
            or drone_id.startswith("0")
        ):
            raise SettingsError("detection camera drone ids must be positive decimal strings")
        result[int(drone_id)] = _text(camera_id, "detection camera id")
    return result
