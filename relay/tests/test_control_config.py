import json

import pytest

from relay.control_config import ControlRuntimeConfig


def raw_config() -> dict[str, object]:
    return {
        "limits": {
            "max_clock_error_ms": 5,
            "max_fix_age_ms": 500,
            "max_velocity_age_ms": 200,
            "max_height_age_ms": 200,
            "max_position_uncertainty_p95_m": 0.3,
        },
        "drones": [
            {
                "drone_id": 1,
                "map_id": "map",
                "geometry_id": "geometry",
                "camera_calibration_id": "camera",
                "body_extrinsics_id": "body",
                "source_ids": ["tag", "velocity", "height"],
                "clock_mapping": {
                    "capture_clock_id": "clock",
                    "relay_clock_id": "relay",
                    "capture_reference_s": 0,
                    "relay_reference_ms": 100_000,
                    "milliseconds_per_capture_second": 1_000,
                    "max_error_ms": 5,
                    "measured": True,
                },
            }
        ],
    }


def test_config_creates_the_diagnostic_projector() -> None:
    config = ControlRuntimeConfig.from_mapping(raw_config())

    projector = config.create_projector()

    assert projector.pins == config.pins
    assert projector.relay_clock_id == "relay"
    assert projector.max_position_uncertainty_p95_m == 0.3
    assert len(config.identity) == 64


def test_config_rejects_runtime_epoch_and_mixed_relay_clocks() -> None:
    epoch_config = raw_config()
    epoch_config["drones"][0]["connection_epoch"] = 1  # type: ignore[index]
    with pytest.raises(ValueError):
        ControlRuntimeConfig.from_mapping(epoch_config)

    clocks = raw_config()
    second = dict(clocks["drones"][0])  # type: ignore[index]
    second["drone_id"] = 2
    second_mapping = dict(second["clock_mapping"])  # type: ignore[arg-type]
    second_mapping["relay_clock_id"] = "other-relay"
    second["clock_mapping"] = second_mapping
    clocks["drones"].append(second)  # type: ignore[index]
    with pytest.raises(ValueError, match="share one relay clock"):
        ControlRuntimeConfig.from_mapping(clocks)


def test_config_loads_from_environment_path(tmp_path) -> None:
    path = tmp_path / "control-localization.json"
    path.write_text(json.dumps(raw_config()))

    config = ControlRuntimeConfig.from_env({"SWEEP_CONTROL_LOCALIZATION_CONFIG": str(path)})

    assert config.pins[1].map_id == "map"
