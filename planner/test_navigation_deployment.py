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
