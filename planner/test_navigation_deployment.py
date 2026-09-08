import json
import os
from dataclasses import asdict, replace
from hashlib import sha256
from pathlib import Path

import pytest

import planner.navigation_deployment as deployment_module
from perception.test_world_localization import evidence, measured_geometry, pins
from perception.world_localization_runtime import WorldLocalizationRuntimeConfig
from planner.mapped_formations import FormationLayout, FormationZone
from planner.models import Plan, Position
from planner.navigation import (
    ArrivalSlot,
    ArtifactPin,
    MotionConfig,
    NavigationArtifact,
    NavigationPermission,
    Pose,
)
from planner.navigation_authorization import NavigationApproval
from planner.navigation_deployment import load_navigation_deployment
from planner.navigation_runtime import (
    FormationBinding,
    NavigationExecutionConfig,
    NavigationFrame,
    NavigationRuntime,
    PrecisionReturnBinding,
    TagDestinationBinding,
    navigation_configuration_digest,
)
from planner.test_navigation import FIXTURE
from planner.test_navigation_runtime import IDENTITY, KEY
from relay.auth import sign_event
from relay.control_localization import ControlPose
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


def test_deployment_binds_a_pinned_tag_and_a_dedicated_marked_return_slot(
    tmp_path, generated_geometry
):
    path = deployment_files(tmp_path, generated_geometry)
    original = load_navigation_deployment(path)
    document = json.loads(path.read_text())
    document["execution"]["tag_destinations"] = [
        {
            "tag_id": 0,
            "zone_id": "atrium",
            "arrival_slot_id": "home-a",
            "maximum_horizontal_offset_m": 3.0,
            "minimum_height_above_tag_m": 0.1,
            "maximum_height_above_tag_m": 2.0,
        }
    ]
    document["execution"]["precision_returns"] = [
        {
            "drone_id": 1,
            "connection_epoch": 1,
            "zone_id": "atrium",
            "marked_slot_id": "home-a",
        }
    ]
    config = replace(
        original.config,
        tag_destinations=(TagDestinationBinding(0, "atrium", "home-a", 3.0, 0.1, 2.0),),
        precision_returns=(PrecisionReturnBinding(1, 1, "atrium", "home-a"),),
    )
    approval_path = tmp_path / "approval.json"
    approval = json.loads(approval_path.read_text())
    approval["configuration_sha256"] = navigation_configuration_digest(
        original.artifact(), config, original.permission, original.home_zone_id
    )
    unsigned = {key: value for key, value in approval.items() if key != "signature"}
    approval["signature"] = sign_event(unsigned, KEY)
    path.write_text(json.dumps(document))
    approval_path.write_text(json.dumps(approval))
    loaded = load_navigation_deployment(path)
    assert loaded.config.tag_destinations[0].tag_id == 0
    assert loaded.config.precision_returns[0].marked_slot_id == "home-a"
    assert loaded.tag_destinations(loaded.artifact().map_pin) == loaded.config.tag_destinations
    assert loaded.tag_destinations(ArtifactPin("other-map", "f" * 64)) == ()
    too_far = replace(
        loaded.config,
        tag_destinations=(TagDestinationBinding(0, "atrium", "home-a", 0.1, 0.1, 2.0),),
    )
    with pytest.raises(ValueError, match="measured approach slot"):
        deployment_module._validate_destination_bindings(
            loaded.artifact(),
            too_far,
            generated_geometry[0],
            generated_geometry[2],
            loaded.permission,
        )


def test_deployment_loads_a_signed_mapped_formation_binding(tmp_path, generated_geometry):
    path = deployment_files(tmp_path, generated_geometry)
    raw = json.loads(path.read_text())
    bundle, geometry, accepted = generated_geometry
    slot = ArrivalSlot("home-a", "atrium", Pose(2.1, 1.8, 1.8, "level_1"), 0.2, 0.2)
    artifact = NavigationArtifact.from_geometry_directory(bundle, geometry, accepted, (slot,))
    artifact = replace(
        artifact,
        zones=tuple(
            replace(zone, owner_approved=zone.zone_id == "atrium") for zone in artifact.zones
        ),
    )
    binding = FormationBinding(
        "column",
        FormationZone(
            "approved-lobby",
            "level_1",
            ((0.2, 0.2), (3.0, 0.2), (3.0, 3.0), (0.2, 3.0), (0.2, 0.2)),
            0.5,
            2.5,
            0.2,
            True,
            True,
            artifact.map_pin,
            artifact.geometry_pin,
        ),
        FormationLayout(Pose(1.6, 1.6, 1.8, "level_1"), 0.0, 0.8, (0.0, 0.0)),
    )
    config = NavigationExecutionConfig(
        "level_1",
        MotionConfig(0.1, 0.1, 0.01, 0.01, 0.05, 0.02, 0.2),
        0.2,
        0.04,
        500,
        0.5,
        5000,
        (NavigationFrame(1, "fixture-enu-world", IDENTITY),),
        formation_bindings=(binding,),
    )
    permission = NavigationPermission(frozenset({"atrium"}))
    raw["execution"] = asdict(config)
    path.write_text(json.dumps(raw))
    approval = json.loads((tmp_path / "approval.json").read_text())
    unsigned = {name: value for name, value in approval.items() if name != "signature"}
    unsigned["configuration_sha256"] = navigation_configuration_digest(
        artifact,
        config,
        permission,
        "atrium",
    )
    (tmp_path / "approval.json").write_text(
        json.dumps({**unsigned, "signature": sign_event(unsigned, KEY)})
    )

    deployment = load_navigation_deployment(path)

    assert deployment.config.formation_bindings == (binding,)


def test_editing_loaded_deployment_invalidates_route_before_dispatch(tmp_path, generated_geometry):
    path = deployment_files(tmp_path, generated_geometry)
    deployment = load_navigation_deployment(path)
    raw = json.loads(path.read_text())
    raw["execution"]["speed_m_s"] = 0.8
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="inputs changed|deployment or approval changed"):
        deployment.artifact()


def test_approval_file_changes_invalidate_loaded_deployment(tmp_path, generated_geometry):
    path = deployment_files(tmp_path, generated_geometry)
    deployment = load_navigation_deployment(path)
    (tmp_path / "approval.json").write_text("{}")
    with pytest.raises(ValueError, match="inputs changed|deployment or approval changed"):
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


def _flight_deployment_files(tmp_path):
    from planner.navigation_authorization import content_digest
    from relay.control_localization import ControlLocalizationPins
    from relay.navigation_wire import NavigationWireConfig

    bundle, accepted, authoring, geometry, report, geometry_sha256 = measured_geometry(tmp_path)
    manifest = json.loads((bundle / "manifest.yaml").read_text())
    evidence_paths, hashes = evidence(tmp_path, manifest, geometry, authoring)
    world_pins = pins(manifest, hashes, report, geometry_sha256)
    fuser = {
        "drone_id": 1,
        "connection_epoch": 0,
        "map_id": world_pins.map_id,
        "geometry_id": world_pins.geometry_id,
        "clock_id": world_pins.capture_clock_mapping_id,
        "tag_source_id": world_pins.tag_source_id,
        "velocity_source_id": world_pins.telemetry_source_id,
        "height_source_id": world_pins.telemetry_source_id,
        "camera_calibration_id": world_pins.camera_calibration_id,
        "body_extrinsics_id": world_pins.body_extrinsics_id,
        "position_bounds_map_enu_m": [[-100, 100]] * 3,
        "height_bounds_map_enu_m": [-100, 100],
        "max_speed_mps": 5,
        "position_variance_bounds_m2": [0.001, 1],
        "velocity_variance_bounds_m2ps2": [0.001, 1],
        "height_variance_bounds_m2": [0.001, 1],
        "production_evidence_verified": True,
    }
    publisher_clock = {
        "capture_clock_id": world_pins.capture_clock_mapping_id,
        "relay_clock_id": "relay-unix",
        "capture_reference_s": 0.001,
        "relay_reference_ms": 1,
        "milliseconds_per_capture_second": 1_000,
        "max_error_ms": 2,
        "measured": True,
    }
    world_raw = {
        "publisher": {
            "mode": "live",
            "session": "flight-session",
            "websocket_url": "ws://relay.example/ws",
            "audit_dir": "audit",
            "queue_limit": 8,
            "drones": [
                {
                    "key_environment": "LOCALIZATION_KEY_1",
                    "clock_mapping": publisher_clock,
                    "live_capture_clock": {
                        "source": "process_monotonic",
                        "boot_id": "test-boot",
                        "monotonic_reference_s": 10,
                        "capture_reference_s": 0.001,
                    },
                    "fuser": fuser,
                }
            ],
        },
        "bundle": str(bundle.relative_to(tmp_path)),
        "accepted_versions": accepted,
        "devices": [
            {
                "pins": asdict(world_pins),
                "capture_clock_mapping": {
                    "mapping_id": world_pins.capture_clock_mapping_id,
                    "source_clock_id": world_pins.capture_clock_mapping_id,
                    "source_unit": "ms",
                    "source_reference": 1_000_000,
                    "relay_reference_ms": 1,
                    "relay_ms_numerator": 1,
                    "source_units_denominator": 1_000,
                    "max_error_ms": 2,
                },
                "evidence_paths": {
                    name: str(Path(value).relative_to(tmp_path))
                    for name, value in evidence_paths.items()
                },
            }
        ],
    }
    world_path = tmp_path / "world-localization.json"
    world_path.write_text(json.dumps(world_raw))
    world = WorldLocalizationRuntimeConfig.load(world_path)
    clock = world.publisher.drones[1].clock_mapping
    sources = tuple(sorted({world_pins.tag_source_id, world_pins.telemetry_source_id}))
    control = ControlLocalizationPins(
        drone_id=1,
        map_id=world_pins.map_id,
        geometry_id=world_pins.geometry_id,
        camera_calibration_id=world_pins.camera_calibration_id,
        body_extrinsics_id=world_pins.body_extrinsics_id,
        source_ids=sources,
        clock_mapping=clock,
    )
    frame = NavigationFrame(
        1,
        world_pins.world_enu.transform_id,
        world_pins.world_enu.matrix_world_enu,
        control,
        world_pins.camera_calibration_sha256,
        world_pins.capture_alignment_config_sha256,
        world_pins.world_enu.sha256,
    )
    tuning = _wire_tuning()
    tuning_path = tmp_path / "device-1-navigation.json"
    encoded_tuning = json.dumps(tuning, sort_keys=True, separators=(",", ":")).encode()
    tuning_path.write_bytes(encoded_tuning)
    profile = NavigationWireConfig(
        clock_lease_id="lease-1",
        clock_lease_expires_at_ms=110_000,
        max_authorization_lifetime_ms=1_000,
        max_clock_error_ms=2,
        navigation_config_id="wire-navigation-1",
        navigation_config_sha256=sha256(encoded_tuning).hexdigest(),
        map_version=world_pins.map_version,
        map_sha256=world_pins.map_content_sha256,
        geometry_sha256=world_pins.geometry_sha256,
        camera_calibration_sha256=world_pins.camera_calibration_sha256,
        body_extrinsics_sha256=world_pins.capture_alignment_config_sha256,
        world_transform_sha256=world_pins.world_enu.sha256,
        control_source_ids=sources,
        **tuning["limits"],
    )
    config = NavigationExecutionConfig(
        "level_1",
        MotionConfig(0.1, 0.1, 0.01, 0.01, 0.05, 0.02, 0.2),
        0.2,
        0.04,
        500,
        0.5,
        5_000,
        (frame,),
        wire_config_sha256=content_digest({"1": asdict(profile)}),
    )
    slot = ArrivalSlot("flight-home", "lobby", Pose(0, 0, 1, "level_1"), 0.05, 0.05)
    permission = NavigationPermission(frozenset({"lobby"}))
    artifact = NavigationArtifact.from_geometry_directory(
        bundle, geometry, accepted, (slot,), authoring=authoring
    )
    artifact = replace(
        artifact,
        zones=tuple(
            replace(zone, owner_approved=zone.zone_id in permission.permitted_zone_ids)
            for zone in artifact.zones
        ),
    )
    approval_raw = {
        "v": 1,
        "type": "navigation_approval",
        "approval_id": "flight-acceptance",
        "session": "flight-session",
        "mode": "flight",
        "configuration_sha256": navigation_configuration_digest(
            artifact, config, permission, "lobby"
        ),
        "issued_at_ms": 99_000,
        "expires_at_ms": 110_000,
        "epochs": [[1, 7]],
        "evidence_sha256": sorted(
            {
                world_pins.camera_calibration_sha256,
                world_pins.capture_alignment_config_sha256,
                world_pins.world_enu.sha256,
                world_pins.geometry_sha256,
            }
        ),
    }
    key = tmp_path / "approval.key"
    key.write_bytes(KEY)
    key.chmod(0o600)
    approval_path = tmp_path / "approval.json"
    approved = {**approval_raw, "signature": sign_event(approval_raw, KEY)}
    approval_path.write_text(json.dumps(approved))
    document = {
        "schema_version": 1,
        "bundle_directory": str(bundle.relative_to(tmp_path)),
        "geometry_directory": str(geometry.relative_to(tmp_path)),
        "geometry_authoring": str(authoring.relative_to(tmp_path)),
        "accepted_map_versions": accepted,
        "arrival_slots": [asdict(slot)],
        "permission_zone_ids": ["lobby"],
        "home_zone_id": "lobby",
        "execution": asdict(config),
        "approval_file": approval_path.name,
        "approval_key_file": key.name,
        "world_localization_file": world_path.name,
        "wire_profiles": {"1": asdict(profile)},
        "wire_navigation_files": {"1": tuning_path.name},
    }
    path = tmp_path / "flight-deployment.json"
    path.write_text(json.dumps(document))
    return path, world_path, hashes["camera_calibration"]


def test_flight_deployment_loads_the_real_world_localization_artifacts(tmp_path):
    path, world_path, camera_sha256 = _flight_deployment_files(tmp_path)
    deployment = load_navigation_deployment(path)
    assert set(deployment.wire_profiles) == {1}

    world = json.loads(world_path.read_text())
    world["devices"][0]["pins"]["camera_calibration_sha256"] = "f" * 64
    world_path.write_text(json.dumps(world))
    with pytest.raises(ValueError, match="evidence|bind"):
        load_navigation_deployment(path)
    assert camera_sha256 != "f" * 64


def test_flight_deployment_reuses_one_validated_artifact_until_an_input_changes(
    tmp_path, monkeypatch
):
    import planner.navigation_deployment as deployment_module

    path, world_path, _ = _flight_deployment_files(tmp_path)
    original = deployment_module._validate_world_localization
    calls = 0

    def validate(*args):
        nonlocal calls
        calls += 1
        original(*args)

    monkeypatch.setattr(deployment_module, "_validate_world_localization", validate)
    deployment = load_navigation_deployment(path)
    artifact = deployment.artifact()
    assert all(deployment.artifact() is artifact for _ in range(20))
    assert calls == 1

    world_path.write_text(world_path.read_text() + "\n")
    with pytest.raises(ValueError, match="inputs changed"):
        deployment.artifact()


def test_flight_tracking_reuses_the_frozen_artifact_and_localization_configuration(
    tmp_path, monkeypatch
):
    import planner.navigation_deployment as deployment_module

    path, _, _ = _flight_deployment_files(tmp_path)
    original = deployment_module._validate_world_localization
    calls = 0

    def validate(*args):
        nonlocal calls
        calls += 1
        original(*args)

    monkeypatch.setattr(deployment_module, "_validate_world_localization", validate)
    deployment = load_navigation_deployment(path)
    config = replace(
        deployment.config,
        motion=MotionConfig(0.005, 0.005, 0.005, 0.005, 0.01, 0.005, 0.2),
        position_tolerance_m=0.005,
    )
    artifact = deployment.artifact()
    approval_unsigned = {
        "v": 1,
        "type": "navigation_approval",
        "approval_id": "flight-tracking-acceptance",
        "session": "flight-session",
        "mode": "flight",
        "configuration_sha256": navigation_configuration_digest(
            artifact, config, deployment.permission, deployment.home_zone_id
        ),
        "issued_at_ms": 99_000,
        "expires_at_ms": 110_000,
        "epochs": [[1, 7]],
        "evidence_sha256": list(deployment.approval.evidence_sha256),
    }
    approval = NavigationApproval.verify(
        {**approval_unsigned, "signature": sign_event(approval_unsigned, KEY)}, KEY
    )
    pins = config.frames[0].control_pins
    assert pins is not None
    pose = ControlPose(
        t=100_000,
        event_id="tracking-control-pose",
        session="flight-session",
        drone_id=1,
        connection_epoch=7,
        map_id=pins.map_id,
        geometry_id=pins.geometry_id,
        camera_calibration_id=pins.camera_calibration_id,
        body_extrinsics_id=pins.body_extrinsics_id,
        pose_time_ms=99_998,
        fix_time_ms=99_998,
        x_mm=-20_000,
        y_mm=9_800,
        z_mm=-29_000,
        position_frame="map_enu",
        position_uncertainty_mm=1,
        status="ready",
    )
    snapshot = replace_aircraft(
        make_snapshot(1, selection=(1,), now_ms=100_000),
        1,
        connection_epoch=7,
        pose=Position(-20, 9.8, -29),
        position_last_seen_ms=100_000,
    )
    runtime = NavigationRuntime(
        deployment.artifact,
        config,
        deployment.permission,
        approval,
        session="flight-session",
        home_zone_id=deployment.home_zone_id,
        control_pose=lambda _: pose,
    )
    plan = runtime.prepare(
        make_intent(IntentName.COME_HOME, selection=(1,), confirm=True), snapshot
    )
    assert isinstance(plan, Plan)
    command = plan.commands[0]
    assert all(runtime.check_tracking(plan, command, snapshot, pose) is None for _ in range(20))
    assert calls == 1


def test_flight_deployment_refuses_a_replaced_input_ancestor(tmp_path):
    release = tmp_path / "release"
    release.mkdir()
    path, _, _ = _flight_deployment_files(release)
    deployment = load_navigation_deployment(path)
    relocated = tmp_path / "relocated-release"
    os.rename(release, relocated)
    os.symlink(relocated, release)

    with pytest.raises(ValueError, match="regular file or directory"):
        deployment.artifact()


def test_flight_deployment_refuses_an_initial_symlinked_ancestor(tmp_path):
    release = tmp_path / "release"
    release.mkdir()
    path, _, _ = _flight_deployment_files(release)
    linked_release = tmp_path / "linked-release"
    os.symlink(release, linked_release)

    with pytest.raises(ValueError, match="regular file or directory"):
        load_navigation_deployment(linked_release / path.name)


@pytest.mark.parametrize("change", ("replacement", "new_bundle_file", "symlink"))
def test_flight_deployment_refuses_replaced_or_added_artifact_inputs(tmp_path, change):
    path, _, _ = _flight_deployment_files(tmp_path)
    deployment = load_navigation_deployment(path)
    raw = json.loads(path.read_text())
    if change == "replacement":
        approval = tmp_path / raw["approval_file"]
        replacement = tmp_path / "approval-replacement.json"
        replacement.write_bytes(approval.read_bytes())
        os.replace(replacement, approval)
    elif change == "new_bundle_file":
        bundle = tmp_path / raw["bundle_directory"]
        (bundle / "unexpected-input.json").write_text("{}")
    else:
        link = tmp_path / "bundle-link"
        os.symlink(tmp_path / raw["bundle_directory"], link)
        raw["bundle_directory"] = link.name
        path.write_text(json.dumps(raw))
        with pytest.raises(ValueError, match="regular file or directory"):
            load_navigation_deployment(path)
        return

    with pytest.raises(ValueError, match="inputs changed"):
        deployment.artifact()
