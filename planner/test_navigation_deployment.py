from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from planner.models import LifecycleStatus, Position, PreparedExecution
from planner.navigation_deployment import load_navigation_deployment
from relay.capabilities import C1_IMPLEMENTED_INTENT_NAMES, CapabilityProfile
from relay.intent_v1 import IntentName
from relay.settings import SettingsError
from tests.autonomy_fixtures import make_intent, make_snapshot, make_stack, replace_aircraft
from tools.map_geometry import generate
from tools.map_validate import seal_manifest

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
                "arrival_slots": [
                    {
                        "id": "a",
                        "x_m": 2.25,
                        "y_m": 1.85,
                        "z_m": 1.8,
                        "radius_m": 0.1,
                        "half_height_m": 0.1,
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


@pytest.fixture
def dispatchable_generated(tmp_path: Path) -> tuple[Path, Path]:
    bundle = tmp_path / "bundle"
    shutil.copytree(FIXTURE, bundle)
    zones_path = bundle / "zones.yaml"
    zones = json.loads(zones_path.read_text())
    next(zone for zone in zones["zones"] if zone["id"] == "kitchen")["owner_approved"] = True
    zones_path.write_text(json.dumps(zones))
    manifest = seal_manifest(bundle)
    accepted = {manifest["bundle_version"]: manifest["content_sha256"]}
    (bundle / "accepted_versions.json").write_text(json.dumps(accepted))
    authoring_path = bundle / "geometry_authoring.json"
    authoring = json.loads(authoring_path.read_text())
    authoring["bundle_content_sha256"] = manifest["content_sha256"]
    authoring_path.write_text(json.dumps(authoring))
    output = tmp_path / "geometry"
    generate(bundle, authoring_path, output, accepted)
    return bundle, output


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


def test_loaded_synthetic_runtime_dispatches_confirmed_navigation(
    dispatchable_generated: tuple[Path, Path], tmp_path: Path
) -> None:
    bundle, geometry = dispatchable_generated
    path = tmp_path / "navigation.json"
    payload = config(bundle, geometry)
    payload["zones"][0]["id"] = "kitchen"
    payload["zones"][0]["aliases"] = ["kitchen"]
    payload["zones"][0]["arrival_slots"][0] = {
        "id": "k",
        "x_m": 0.9,
        "y_m": 0.2,
        "z_m": 1.8,
        "radius_m": 0.4,
        "half_height_m": 0.4,
    }
    payload["permission_zone_ids"] = ["kitchen"]
    path.write_text(json.dumps(payload))
    deployment = load_navigation_deployment({"SWEEP_NAVIGATION_CONFIG": str(path)})
    assert deployment is not None and deployment.runtime.dispatch_acceptance is not None

    snapshot = replace_aircraft(
        make_snapshot(1), 1, pose=Position(0.5, 0.5, 1.8), position_last_seen_ms=100_000
    )
    profile = CapabilityProfile(
        "mapped_navigation", C1_IMPLEMENTED_INTENT_NAMES | {IntentName.NAVIGATE}
    )
    controller, planner, _, dispatcher, flight, _ = make_stack(snapshot, capability_profile=profile)
    planner.navigation = deployment.runtime
    dispatcher.navigation = deployment.runtime
    clock = [snapshot.now_ms]

    def current():
        clock[0] += 1
        aircraft = {
            drone_id: replace(
                snapshot.aircraft[drone_id],
                pose=drone.pose,
                flight_state=drone.flight_state,
                position_last_seen_ms=clock[0],
            )
            for drone_id, drone in flight.aircraft.items()
        }
        return replace(snapshot, now_ms=clock[0], aircraft=aircraft)

    intent = make_intent(
        IntentName.NAVIGATE,
        selection=(1,),
        args={"zone_id": "kitchen"},
        confirm=True,
    )
    prepared = controller.prepare(intent, snapshot, current_snapshot=current)
    assert isinstance(prepared, PreparedExecution), prepared

    result = controller.dispatch_prepared(prepared, current_snapshot=current)

    assert result.status is LifecycleStatus.COMPLETED, result.refusal
    assert any(call.operation == "goto" for call in flight.calls)
