"""Strict deployment configuration for optional live search detection."""

from __future__ import annotations

import json
from collections.abc import Mapping
from hashlib import sha256
from pathlib import Path

from relay.search_detection import (
    CameraCalibrationConfig,
    DetectionSourceConfig,
    SearchDetectionConfig,
)
from relay.search_runtime import SearchRuntime
from relay.settings import SettingsError


def load_search_detection_config(
    env: Mapping[str, str], search: SearchRuntime | None
) -> SearchDetectionConfig | None:
    configured = env.get("SWEEP_SEARCH_DETECTION_CONFIG")
    if configured is None:
        return None
    if search is None:
        raise SettingsError("SWEEP_SEARCH_DETECTION_CONFIG requires SWEEP_SEARCH_CONFIG")
    path = Path(configured)
    try:
        raw = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError) as error:
        raise SettingsError(f"invalid search detection JSON: {error}") from error
    if (
        not isinstance(raw, dict)
        or set(raw) != {"schema_version", "sources_by_drone"}
        or raw.get("schema_version") != 1
        or not isinstance(raw.get("sources_by_drone"), dict)
    ):
        raise SettingsError("search detection config fields or schema_version are invalid")
    try:
        sources = {
            _drone_id(key): _source(path.parent, key, value)
            for key, value in raw["sources_by_drone"].items()
            if isinstance(value, dict)
            and set(value)
            == {
                "source_id",
                "stream_url",
                "model_path",
                "model_sha256",
                "camera",
            }
        }
        expected = dict(search.config.source_by_drone)
        if set(sources) != set(expected) or any(
            source.source_id != expected[drone_id] for drone_id, source in sources.items()
        ):
            raise ValueError("detection sources must exactly match configured search cameras")
        return SearchDetectionConfig(sources)
    except (TypeError, ValueError) as error:
        raise SettingsError(f"invalid search detection config: {error}") from error


def _drone_id(value: object) -> int:
    if not isinstance(value, str) or not value.isdecimal() or value.startswith("0"):
        raise ValueError("detection drone ids must be canonical positive integer strings")
    parsed = int(value)
    if parsed <= 0:
        raise ValueError("detection drone ids must be positive")
    return parsed


def _field(value: object, name: str) -> str:
    if not isinstance(value, dict) or not isinstance(result := value.get(name), str):
        raise ValueError(f"detection {name} must be text")
    return result


def _source(base: Path, drone_id: object, value: object) -> DetectionSourceConfig:
    model_path = _field(value, "model_path")
    model_sha256 = _field(value, "model_sha256")
    return DetectionSourceConfig(
        _drone_id(drone_id),
        _field(value, "source_id"),
        _field(value, "stream_url"),
        _model_path(base, model_path, model_sha256),
        model_sha256,
        _camera(value["camera"]),
    )


def _model_path(base: Path, value: str, expected_sha256: str) -> Path:
    if not value or Path(value).is_absolute():
        raise ValueError("detection model_path must be a nonempty relative path")
    root = base.resolve()
    path = (root / value).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError("detection model_path must name a regular file beside its config")
    if path.stat().st_size > 128 * 1024 * 1024:
        raise ValueError("detection model exceeds the 128 MiB limit")
    if sha256(path.read_bytes()).hexdigest() != expected_sha256:
        raise ValueError("detection model hash does not match model_sha256")
    return path


def _camera(value: object) -> CameraCalibrationConfig:
    if not isinstance(value, dict) or set(value) != {"intrinsics", "body_from_camera"}:
        raise ValueError("detection camera fields are invalid")
    try:
        return CameraCalibrationConfig(
            tuple(tuple(row) for row in value["intrinsics"]),
            tuple(tuple(row) for row in value["body_from_camera"]),
        )
    except (TypeError, ValueError) as error:
        raise ValueError(f"detection camera is invalid: {error}") from error
