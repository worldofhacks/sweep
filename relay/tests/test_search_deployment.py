from __future__ import annotations

import json
from hashlib import sha256

import pytest

from planner.test_navigation_runtime import _runtime
from relay.search_deployment import load_search_runtime
from relay.search_detection_deployment import load_search_detection_config
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


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda config: config["areas"][0].update(unexpected=True),
            "search area fields are invalid",
        ),
        (
            lambda config: config["areas"].append(config["areas"][0].copy()),
            "must not duplicate zone ids",
        ),
        (
            lambda config: config["areas"][0].update(floor_id="other_floor"),
            "floor must match",
        ),
        (
            lambda config: config.__setitem__("source_by_drone", {"01": "camera-1"}),
            "canonical positive integer",
        ),
    ],
)
def test_search_runtime_loader_rejects_ambiguous_area_and_source_configuration(
    tmp_path, mutate, message: str
) -> None:
    config = _config()
    mutate(config)
    path = tmp_path / "search.json"
    path.write_text(json.dumps(config))

    with pytest.raises(SettingsError, match=message):
        load_search_runtime({"SWEEP_SEARCH_CONFIG": str(path)}, _runtime())


def test_search_runtime_loader_rejects_duplicate_json_fields(tmp_path) -> None:
    path = tmp_path / "search.json"
    path.write_text('{"schema_version": 1, "schema_version": 1}')

    with pytest.raises(SettingsError, match="duplicate JSON field"):
        load_search_runtime({"SWEEP_SEARCH_CONFIG": str(path)}, _runtime())


def test_detection_config_requires_each_configured_camera_and_model_pin(tmp_path) -> None:
    search_path = tmp_path / "search.json"
    search_path.write_text(json.dumps(_config()))
    search = load_search_runtime({"SWEEP_SEARCH_CONFIG": str(search_path)}, _runtime())
    assert search is not None
    model = tmp_path / "model.onnx"
    model.write_bytes(b"test detector model")
    detection_path = tmp_path / "detection.json"
    detection_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "sources_by_drone": {
                    "1": {
                        "source_id": "camera-1",
                        "stream_url": "rtsp://camera.example/drone1",
                        "model_path": "model.onnx",
                        "model_sha256": sha256(model.read_bytes()).hexdigest(),
                        "camera": {
                            "intrinsics": [[100, 0, 4], [0, 100, 4], [0, 0, 1]],
                            "body_from_camera": [
                                [1, 0, 0, 0],
                                [0, 1, 0, 0],
                                [0, 0, 1, 0],
                                [0, 0, 0, 1],
                            ],
                        },
                    }
                },
            }
        )
    )

    config = load_search_detection_config(
        {"SWEEP_SEARCH_DETECTION_CONFIG": str(detection_path)}, search
    )

    assert config is not None
    assert config.sources_by_drone[1].model_path == model

    broken = json.loads(detection_path.read_text())
    broken["sources_by_drone"]["1"]["source_id"] = "wrong-camera"
    detection_path.write_text(json.dumps(broken))
    with pytest.raises(SettingsError, match="exactly match"):
        load_search_detection_config({"SWEEP_SEARCH_DETECTION_CONFIG": str(detection_path)}, search)
    with pytest.raises(SettingsError, match="requires SWEEP_SEARCH_CONFIG"):
        load_search_detection_config({"SWEEP_SEARCH_DETECTION_CONFIG": str(detection_path)}, None)
