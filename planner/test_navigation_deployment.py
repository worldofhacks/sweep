from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from planner.navigation_deployment import load_navigation_deployment
from relay.settings import SettingsError

FIXTURE = Path("tests/fixtures/geometry")


def _config(bundle: Path, geometry: Path) -> dict[str, object]:
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
                "arrival_slots": [
                    {
                        "id": "atrium-slot",
                        "x_m": 2.25,
                        "y_m": 1.85,
                        "z_m": 1.8,
                        "radius_m": 0.2,
                        "half_height_m": 0.2,
                    }
                ],
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
            "segment_timeout_ms": 1_000,
        },
        "max_aircraft": 1,
        "control_store_identity": "test-control-store",
        "evidence_file": "navigation-evidence.json",
    }


@pytest.fixture
def generated(tmp_path: Path) -> tuple[Path, Path]:
    geometry = tmp_path / "geometry"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tools.map_geometry",
            str(FIXTURE),
            str(FIXTURE / "geometry_authoring.json"),
            str(geometry),
            "--accepted-versions",
            str(FIXTURE / "accepted_versions.json"),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return FIXTURE, geometry


def _load(tmp_path: Path, bundle: Path, geometry: Path):
    path = tmp_path / "navigation.json"
    path.write_text(json.dumps(_config(bundle, geometry)))
    deployment = load_navigation_deployment({"SWEEP_NAVIGATION_CONFIG": str(path)})
    assert deployment is not None
    return path, deployment


def test_navigation_is_disabled_without_a_configuration() -> None:
    assert load_navigation_deployment({}) is None


def test_loader_builds_a_bounded_synthetic_runtime(generated, tmp_path: Path) -> None:
    path, deployment = _load(tmp_path, *generated)

    assert path.exists()
    assert deployment.backend == "synthetic"
    assert deployment.runtime.maximum_aircraft == 1
    assert deployment.runtime.require_phone_authorization is False
    assert deployment.runtime.artifact().zones[0].arrival_slots[0].slot_id == "atrium-slot"


def test_loader_revokes_configuration_changes_and_reloads_geometry(
    generated, tmp_path: Path
) -> None:
    bundle, geometry = generated
    path, deployment = _load(tmp_path, bundle, geometry)
    first = deployment.runtime.artifact()
    changed = _config(bundle, geometry)
    changed["permission_zone_ids"] = ["other"]
    path.write_text(json.dumps(changed))

    with pytest.raises(ValueError, match="configuration changed"):
        deployment.runtime.artifact()

    path.write_text(json.dumps(_config(bundle, geometry)))
    report = geometry / "geometry.json"
    report.write_text(report.read_text() + " ")

    assert deployment.runtime.artifact().geometry_pin != first.geometry_pin


def test_remote_backend_rejects_synthetic_geometry(generated, tmp_path: Path) -> None:
    path, _ = _load(tmp_path, *generated)

    with pytest.raises(SettingsError, match="synthetic geometry"):
        load_navigation_deployment({"SWEEP_NAVIGATION_CONFIG": str(path)}, backend="remote")


def test_loader_rejects_unknown_fields_and_invalid_backend(generated, tmp_path: Path) -> None:
    bundle, geometry = generated
    path = tmp_path / "navigation.json"
    payload = _config(bundle, geometry)
    payload["untrusted"] = True
    path.write_text(json.dumps(payload))

    with pytest.raises(SettingsError, match="missing or unknown"):
        load_navigation_deployment({"SWEEP_NAVIGATION_CONFIG": str(path)})
    with pytest.raises(SettingsError, match="backend"):
        load_navigation_deployment({}, backend="unbounded")  # type: ignore[arg-type]
