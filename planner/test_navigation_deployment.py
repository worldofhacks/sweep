import json
from dataclasses import asdict, replace

import pytest

from planner.models import Plan, Position
from planner.navigation import (
    ArrivalSlot,
    MotionConfig,
    NavigationArtifact,
    NavigationPermission,
    Pose,
)
from planner.navigation_deployment import load_navigation_deployment
from planner.navigation_runtime import (
    NavigationExecutionConfig,
    NavigationFrame,
    navigation_configuration_digest,
)
from planner.test_navigation import FIXTURE
from planner.test_navigation_runtime import IDENTITY, KEY
from relay.auth import sign_event
from relay.intent_v1 import IntentName
from tests.autonomy_fixtures import make_intent, make_snapshot, replace_aircraft
from tools.map_geometry import generate


@pytest.fixture(scope="module")
def generated_geometry(tmp_path_factory):
    output = tmp_path_factory.mktemp("navigation-deployment-geometry") / "output"
    accepted = json.loads((FIXTURE / "accepted_versions.json").read_text())
    generate(FIXTURE, FIXTURE / "geometry_authoring.json", output, accepted)
    return FIXTURE, output, accepted


def deployment_files(tmp_path, generated_geometry):
    bundle, geometry, accepted = generated_geometry
    slot = ArrivalSlot("home-a", "atrium", Pose(2.1, 1.8, 1.8, "level_1"), 0.2, 0.2)
    config = NavigationExecutionConfig(
        "level_1",
        MotionConfig(0.1, 0.1, 0.01, 0.01, 0.05, 0.02, 0.2),
        0.2,
        0.04,
        500,
        0.5,
        5000,
        (NavigationFrame(1, "fixture-enu-world", IDENTITY),),
    )
    permission = NavigationPermission(frozenset({"atrium"}))
    artifact = NavigationArtifact.from_geometry_directory(bundle, geometry, accepted, (slot,))
    artifact = replace(
        artifact,
        zones=tuple(
            replace(zone, owner_approved=zone.zone_id in permission.permitted_zone_ids)
            for zone in artifact.zones
        ),
    )
    raw_approval = {
        "v": 1,
        "type": "navigation_approval",
        "approval_id": "simulation-acceptance",
        "session": "test-session",
        "mode": "simulation",
        "configuration_sha256": navigation_configuration_digest(
            artifact, config, permission, "atrium"
        ),
        "issued_at_ms": 99000,
        "expires_at_ms": 110000,
        "epochs": [[1, 1]],
        "evidence_sha256": [],
    }
    (tmp_path / "approval.json").write_text(
        json.dumps({**raw_approval, "signature": sign_event(raw_approval, KEY)})
    )
    key = tmp_path / "approval.key"
    key.write_bytes(KEY)
    key.chmod(0o600)
    document = {
        "schema_version": 1,
        "bundle_directory": str(bundle),
        "geometry_directory": str(geometry),
        "geometry_authoring": None,
        "accepted_map_versions": accepted,
        "arrival_slots": [asdict(slot)],
        "permission_zone_ids": ["atrium"],
        "home_zone_id": "atrium",
        "execution": asdict(config),
        "approval_file": "approval.json",
        "approval_key_file": "approval.key",
    }
    path = tmp_path / "deployment.json"
    path.write_text(json.dumps(document))
    return path


def test_file_deployment_loads_real_generated_geometry_and_prepares_route(
    tmp_path, generated_geometry
):
    path = deployment_files(tmp_path, generated_geometry)
    deployment = load_navigation_deployment(path)
    runtime = deployment.for_session("test-session", lambda _: None, None)
    snapshot = replace_aircraft(make_snapshot(1), 1, pose=Position(2.5, 1.8, 1.8))
    plan = runtime.prepare(
        make_intent(IntentName.COME_HOME, selection=(1,), confirm=True), snapshot
    )
    assert isinstance(plan, Plan)
    assert runtime.check(plan, plan.commands[0], snapshot) is None
    assert plan.commands[-2].parameters["x"] == 2.1


def test_editing_loaded_deployment_invalidates_route_before_dispatch(tmp_path, generated_geometry):
    path = deployment_files(tmp_path, generated_geometry)
    deployment = load_navigation_deployment(path)
    raw = json.loads(path.read_text())
    raw["execution"]["speed_m_s"] = 0.8
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="configuration changed|deployment or approval changed"):
        deployment.artifact()


def test_approval_file_changes_invalidate_loaded_deployment(tmp_path, generated_geometry):
    path = deployment_files(tmp_path, generated_geometry)
    deployment = load_navigation_deployment(path)
    (tmp_path / "approval.json").write_text("{}")
    with pytest.raises(ValueError, match="deployment or approval changed"):
        deployment.artifact()


def test_deployment_refuses_shared_readable_approval_key(tmp_path, generated_geometry):
    path = deployment_files(tmp_path, generated_geometry)
    (tmp_path / "approval.key").chmod(0o644)
    with pytest.raises(ValueError, match="0600"):
        load_navigation_deployment(path)


def _wire_tuning() -> dict[str, object]:
    return {
        "v": 1,
        "navigation_config_id": "wire-navigation-1",
        "device_id": 1,
        "limits": {
            "max_speed_mm_s": 200,
            "max_acceleration_mm_s2": 100,
            "max_deceleration_mm_s2": 200,
            "max_position_uncertainty_mm": 30,
            "max_cross_track_mm": 50,
            "arrival_horizontal_tolerance_mm": 40,
            "arrival_vertical_tolerance_mm": 40,
            "pose_freshness_ms": 200,
            "tracking_timeout_ms": 1_000,
        },
    }


def _wire_profile(tuning: dict[str, object]):
    from hashlib import sha256

    from relay.navigation_wire import NavigationWireConfig

    encoded = json.dumps(tuning, sort_keys=True, separators=(",", ":")).encode()
    return NavigationWireConfig(
        clock_lease_id="lease-1",
        clock_lease_expires_at_ms=110_000,
        max_authorization_lifetime_ms=1_000,
        max_clock_error_ms=2,
        navigation_config_id="wire-navigation-1",
        navigation_config_sha256=sha256(encoded).hexdigest(),
        map_version="map-v1",
        map_sha256="a" * 64,
        geometry_sha256="b" * 64,
        camera_calibration_sha256="c" * 64,
        body_extrinsics_sha256="d" * 64,
        world_transform_sha256="e" * 64,
        control_source_ids=("dji-telemetry", "tag-detector"),
        **tuning["limits"],
    )


def test_wire_tuning_requires_the_approved_raw_bytes_and_exact_limits(tmp_path):
    from planner.navigation_deployment import _validate_wire_tuning

    tuning = _wire_tuning()
    profile = _wire_profile(tuning)
    path = tmp_path / "wire.json"
    path.write_bytes(json.dumps(tuning, sort_keys=True, separators=(",", ":")).encode())
    _validate_wire_tuning({"1": "wire.json"}, tmp_path, {1: profile})

    tuning["limits"]["max_speed_mm_s"] = 201
    path.write_bytes(json.dumps(tuning, sort_keys=True, separators=(",", ":")).encode())
    with pytest.raises(ValueError, match="approved profile"):
        _validate_wire_tuning({"1": "wire.json"}, tmp_path, {1: profile})


def test_world_localization_binding_rejects_a_changed_camera_artifact(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from planner.navigation_deployment import _validate_world_localization
    from relay.control_localization import ClockMapping, ControlLocalizationPins

    tuning = _wire_tuning()
    profile = _wire_profile(tuning)
    clock = ClockMapping("phone-clock", "relay-clock", 0, 0, 1_000, 2, True)
    pins = ControlLocalizationPins(
        drone_id=1,
        map_id="map-1",
        geometry_id="geometry-1",
        camera_calibration_id="camera-1",
        body_extrinsics_id="body-1",
        source_ids=("dji-telemetry", "tag-detector"),
        clock_mapping=clock,
    )
    config = NavigationExecutionConfig(
        "level-1",
        MotionConfig(0.1, 0.1, 0.01, 0.01, 0.05, 0.02, 0.2),
        0.2,
        0.04,
        500,
        0.5,
        5_000,
        (
            NavigationFrame(
                1,
                "world-enu-1",
                IDENTITY,
                pins,
                "c" * 64,
                "d" * 64,
                "e" * 64,
            ),
        ),
    )
    world_pins = SimpleNamespace(
        map_id="map-1",
        map_version="map-v1",
        map_content_sha256="a" * 64,
        geometry_id="geometry-1",
        geometry_sha256="b" * 64,
        camera_calibration_id="camera-1",
        camera_calibration_sha256="c" * 64,
        body_extrinsics_id="body-1",
        capture_alignment_config_sha256="d" * 64,
        capture_clock_mapping_id="phone-clock",
        world_enu=SimpleNamespace(
            transform_id="world-enu-1", sha256="e" * 64, matrix_world_enu=IDENTITY
        ),
    )
    world = SimpleNamespace(
        publisher=SimpleNamespace(
            session="test-session",
            drones={
                1: SimpleNamespace(
                    fuser=SimpleNamespace(
                        tag_source_id="tag-detector",
                        velocity_source_id="dji-telemetry",
                        height_source_id="dji-telemetry",
                    ),
                    clock_mapping=SimpleNamespace(max_error_ms=2),
                )
            },
        ),
        adapters={1: SimpleNamespace(pins=world_pins)},
    )
    from perception.world_localization_runtime import WorldLocalizationRuntimeConfig

    monkeypatch.setattr(WorldLocalizationRuntimeConfig, "load", lambda _: world)
    _validate_world_localization(
        tmp_path / "world.json", config, SimpleNamespace(session="test-session"), {1: profile}
    )

    world_pins.camera_calibration_sha256 = "f" * 64
    with pytest.raises(ValueError, match="does not bind"):
        _validate_world_localization(
            tmp_path / "world.json", config, SimpleNamespace(session="test-session"), {1: profile}
        )
