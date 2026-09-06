from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from planner.navigation_deployment import load_navigation_deployment
from relay.settings import SettingsError

FIXTURE = Path("tests/fixtures/geometry")


def config(bundle: Path, geometry: Path) -> dict[str, object]:
    accepted = json.loads((bundle / "accepted_versions.json").read_text())
    return {
        "schema_version": 1,
        "bundle_directory": str(bundle.resolve()),
        "geometry_directory": str(geometry.resolve()),
        "accepted_versions": accepted,
        "zones": [
            {
                "id": "atrium",
                "floor_id": "level_1",
                "navigation_allowed": True,
                "aliases": ["atrium"],
                "arrival_slots": [{"id": "a", "x_m": 2.25, "y_m": 1.85, "z_m": 1.8, "radius_m": 0.1, "half_height_m": 0.1}],
            }
        ],
        "permission_zone_ids": ["atrium"],
        "execution": {
            "floor_id": "level_1",
            "motion": {
                "aircraft_radius_m": 0.15,
                "aircraft_height_m": 0.2,
                "map_uncertainty_m": 0.02,
                "pose_uncertainty_m": 0.02,
                "tracking_allowance_m": 0.1,
                "stopping_allowance_m": 0.1,
            },
            "speed_m_s": 0.5,
            "position_tolerance_m": 0.05,
            "position_max_age_ms": 500,
            "minimum_position_quality": 0.5,
            "segment_timeout_ms": 1000,
        },
        "max_aircraft": 1,
        "control_store_identity": "live-store-v1",
        "evidence_file": "evidence.json",
    }


@pytest.fixture
def generated(tmp_path: Path) -> tuple[Path, Path]:
    output = tmp_path / "geometry"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tools.map_geometry",
            str(FIXTURE),
            str(FIXTURE / "geometry_authoring.json"),
            str(output),
            "--accepted-versions",
            str(FIXTURE / "accepted_versions.json"),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return FIXTURE, output


def test_absent_config_disables_navigation() -> None:
    assert load_navigation_deployment({}) is None


def test_synthetic_generated_artifact_loads_and_remote_refuses(
    generated: tuple[Path, Path], tmp_path: Path
) -> None:
    bundle, geometry = generated
    path = tmp_path / "navigation.json"
    path.write_text(json.dumps(config(bundle, geometry)))
    deployment = load_navigation_deployment({"SWEEP_NAVIGATION_CONFIG": str(path)})
    assert deployment is not None and deployment.max_aircraft == 1
    with pytest.raises(SettingsError, match="synthetic geometry"):
        load_navigation_deployment({"SWEEP_NAVIGATION_CONFIG": str(path)}, "remote")


def test_strict_config_rejects_negative_limits_and_changed_config(
    generated: tuple[Path, Path], tmp_path: Path
) -> None:
    bundle, geometry = generated
    payload = config(bundle, geometry)
    payload["execution"]["speed_m_s"] = -1  # type: ignore[index]
    path = tmp_path / "navigation.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(SettingsError, match="speed_m_s"):
        load_navigation_deployment({"SWEEP_NAVIGATION_CONFIG": str(path)})


def test_runtime_artifact_callable_revokes_changed_config_or_geometry(
    generated: tuple[Path, Path], tmp_path: Path
) -> None:
    bundle, geometry = generated
    path = tmp_path / "navigation.json"
    payload = config(bundle, geometry)
    path.write_text(json.dumps(payload))
    deployment = load_navigation_deployment({"SWEEP_NAVIGATION_CONFIG": str(path)})
    assert deployment is not None
    before = deployment.runtime.artifact()
    payload["permission_zone_ids"] = ["other"]
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="configuration changed"):
        deployment.runtime.artifact()
    path.write_text(json.dumps(config(bundle, geometry)))
    report = geometry / "geometry.json"
    report.write_text(report.read_text() + " ")
    assert deployment.runtime.artifact().geometry_pin != before.geometry_pin
