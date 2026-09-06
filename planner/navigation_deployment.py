"""Strict file-backed configuration for mapped-navigation deployment."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from planner.navigation import (
    ArrivalSlot,
    MotionConfig,
    NavigationArtifact,
    NavigationPermission,
    Pose,
    Zone,
)
from planner.navigation_runtime import NavigationExecutionConfig, NavigationRuntime
from relay.settings import SettingsError


@dataclass(frozen=True, slots=True)
class NavigationDeployment:
    runtime: NavigationRuntime
    max_aircraft: int
    control_store_identity: str
    backend: str


def load_navigation_deployment(
    env: Mapping[str, str] | None = None, backend: Literal["synthetic", "remote"] = "synthetic"
) -> NavigationDeployment | None:
    config_path = (env or os.environ).get("SWEEP_NAVIGATION_CONFIG")
    if config_path is None:
        return None
    path = Path(config_path).resolve()
    config = _object(_read(path), "navigation config")
    _only(
        config,
        {
            "schema_version",
            "bundle_directory",
            "geometry_directory",
            "accepted_versions",
            "zones",
            "permission_zone_ids",
            "execution",
            "max_aircraft",
            "control_store_identity",
            "evidence_file",
        },
    )
    if config.get("schema_version") != 1:
        raise SettingsError("navigation config schema_version must be 1")
    base = path.parent
    bundle = _path(base, config.get("bundle_directory"), "bundle_directory")
    geometry = _path(base, config.get("geometry_directory"), "geometry_directory")
    accepted = _object(config.get("accepted_versions"), "accepted_versions")
    if not accepted or any(not isinstance(k, str) or not _sha(v) for k, v in accepted.items()):
        raise SettingsError("accepted_versions must pin bundle hashes")
    arrival_slots = _arrival_slots(config.get("zones"))
    permission = NavigationPermission(
        frozenset(_strings(config.get("permission_zone_ids"), "permission_zone_ids"))
    )
    execution = _execution(_object(config.get("execution"), "execution"))
    max_aircraft = _positive_int(config.get("max_aircraft"), "max_aircraft")
    identity = config.get("control_store_identity")
    if not isinstance(identity, str) or not identity:
        raise SettingsError("control_store_identity is required")

    def artifact() -> NavigationArtifact:
        fresh = _object(_read(path), "navigation config")
        if fresh != config:
            raise ValueError("navigation deployment configuration changed")
        return NavigationArtifact.from_geometry_directory(bundle, geometry, accepted, arrival_slots)

    deployment = NavigationDeployment(
        NavigationRuntime(artifact, execution, permission), max_aircraft, identity, backend
    )
    if backend == "remote":
        report = _object(_read(geometry / "geometry.json"), "geometry report")
        if report.get("evidence_kind") == "synthetic":
            raise SettingsError("synthetic geometry cannot activate remote navigation")
        evidence = _path(base, config.get("evidence_file"), "evidence_file")
        _remote_evidence(_object(_read(evidence), "navigation evidence"), deployment, artifact())
    return deployment


def _execution(value: dict[str, object]) -> NavigationExecutionConfig:
    _only(
        value,
        {
            "floor_id",
            "motion",
            "speed_m_s",
            "position_tolerance_m",
            "position_max_age_ms",
            "minimum_position_quality",
            "segment_timeout_ms",
        },
    )
    motion = _object(value.get("motion"), "motion")
    _only(
        motion,
        {
            "aircraft_radius_m",
            "aircraft_height_m",
            "map_uncertainty_m",
            "pose_uncertainty_m",
            "tracking_allowance_m",
            "stopping_allowance_m",
        },
    )
    try:
        return NavigationExecutionConfig(
            _text(value.get("floor_id"), "floor_id"),
            MotionConfig(**{k: _nonnegative(v, k) for k, v in motion.items()}),
            _positive(value.get("speed_m_s"), "speed_m_s"),
            _positive(value.get("position_tolerance_m"), "position_tolerance_m"),
            _positive_int(value.get("position_max_age_ms"), "position_max_age_ms"),
            _positive(value.get("minimum_position_quality"), "minimum_position_quality"),
            _positive_int(value.get("segment_timeout_ms"), "segment_timeout_ms"),
        )
    except (TypeError, ValueError) as error:
        raise SettingsError(f"invalid navigation execution: {error}") from error


def _arrival_slots(value: object) -> tuple[ArrivalSlot, ...]:
    if not isinstance(value, list) or not value:
        raise SettingsError("zones must be a nonempty array")
    slots: list[ArrivalSlot] = []
    for raw in value:
        item = _object(raw, "zone")
        _only(item, {"id", "floor_id", "navigation_allowed", "aliases", "arrival_slots"})
        if type(item.get("navigation_allowed")) is not bool:
            raise SettingsError("navigation_allowed must be boolean")
        for slot in item.get("arrival_slots", []):
            data = _object(slot, "arrival slot")
            _only(data, {"id", "x_m", "y_m", "z_m", "radius_m", "half_height_m"})
            try:
                slots.append(ArrivalSlot(
                    _text(data.get("id"), "slot id"), _text(item.get("id"), "zone id"),
                    Pose(_number(data.get("x_m"), "x_m"), _number(data.get("y_m"), "y_m"), _number(data.get("z_m"), "z_m"), _text(item.get("floor_id"), "floor_id")),
                    _positive(data.get("radius_m"), "radius_m"), _positive(data.get("half_height_m"), "half_height_m"),
                ))
            except ValueError as error:
                raise SettingsError(f"invalid arrival slot: {error}") from error
    if len({slot.slot_id for slot in slots}) != len(slots):
        raise SettingsError("arrival slot ids must be unique")
    return tuple(slots)


def _remote_evidence(
    value: dict[str, object], deployment: NavigationDeployment, artifact: NavigationArtifact
) -> None:
    _only(
        value,
        {
            "schema_version",
            "map_pin",
            "geometry_pin",
            "motion",
            "speed_m_s",
            "max_aircraft",
            "localization",
            "allowances",
            "probes",
            "owner_attestation",
            "one_drone_complete",
        },
    )
    if (
        value.get("schema_version") != 1
        or value.get("map_pin") != artifact.map_pin.content_sha256
        or value.get("geometry_pin") != artifact.geometry_pin.content_sha256
    ):
        raise SettingsError("remote evidence pins do not match navigation artifacts")
    if (
        value.get("motion") != asdict(deployment.runtime.config.motion)
        or value.get("speed_m_s") != deployment.runtime.config.speed_m_s
        or value.get("max_aircraft") != deployment.max_aircraft
    ):
        raise SettingsError("remote evidence does not match motion configuration")
    localization = _object(value.get("localization"), "localization")
    if (
        _positive(localization.get("p95_error_m"), "p95_error_m") > 0.25
        or _positive_int(localization.get("max_gap_ms"), "max_gap_ms") > 500
    ):
        raise SettingsError("remote localization evidence exceeds navigation limits")
    if value.get("allowances") != {
        "stopping_allowance_m": deployment.runtime.config.motion.stopping_allowance_m,
        "clearance_m": deployment.runtime.config.motion.swept_radius_m,
    }:
        raise SettingsError("remote evidence allowances do not match motion configuration")
    if not _strings(value.get("probes"), "probes") or not _strings(
        value.get("owner_attestation"), "owner_attestation"
    ):
        raise SettingsError("remote evidence needs probe artifact references and owner attestation")
    if deployment.max_aircraft > 1 and value.get("one_drone_complete") is not True:
        raise SettingsError("remote multi-aircraft navigation requires one-drone evidence")


def _read(path: Path) -> object:
    try:
        return json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError) as error:
        raise SettingsError(f"invalid navigation JSON: {error}") from error


def _object(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise SettingsError(f"{name} must be an object")
    return value


def _only(value: dict[str, object], allowed: set[str]) -> None:
    if set(value) != allowed:
        raise SettingsError("navigation config has missing or unknown fields")


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise SettingsError(f"{name} must be text")
    return value


def _number(value: object, name: str) -> float:
    if type(value) not in (int, float) or not float("-inf") < float(value) < float("inf"):
        raise SettingsError(f"{name} must be finite")
    return float(value)


def _positive(value: object, name: str) -> float:
    value = _number(value, name)
    if value <= 0:
        raise SettingsError(f"{name} must be positive")
    return value


def _nonnegative(value: object, name: str) -> float:
    value = _number(value, name)
    if value < 0:
        raise SettingsError(f"{name} must be nonnegative")
    return value


def _positive_int(value: object, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise SettingsError(f"{name} must be positive integer")
    return value


def _strings(value: object, name: str) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(x, str) or not x for x in value)
        or len(set(value)) != len(value)
    ):
        raise SettingsError(f"{name} must be distinct nonempty text")
    return tuple(value)


def _sha(value: object) -> bool:
    return (
        isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)
    )


def _path(base: Path, value: object, name: str) -> Path:
    return (base / _text(value, name)).resolve()
