from __future__ import annotations

import base64
import hashlib
import json
import math
from dataclasses import replace
from pathlib import Path

import cv2
import numpy as np
import pytest

from planner.ground_navigation import GroundNavigationDeployment, GroundNavigationPose
from relay.auth import sign_event
from relay.map_authoring import MapAuthoringStore
from tests.world_bundle_fixtures import fixture_world_draft

KEY = b"ground-navigation-fixture-signing-key-32-bytes"
NOW = 100_000


def deployment_file(tmp_path: Path, *, blocked: bool = False, now_ms: int = NOW) -> Path:
    draft = fixture_world_draft()
    pixels = np.full((100, 100), 255, dtype=np.uint8)
    if blocked:
        pixels[:, 18:22] = 0
    _, encoded = cv2.imencode(".png", pixels)
    payload = encoded.tobytes()
    draft["image"]["sha256"] = hashlib.sha256(payload).hexdigest()
    draft["image"]["dataUrl"] = "data:image/png;base64," + base64.b64encode(payload).decode()
    store = MapAuthoringStore(tmp_path / "maps.sqlite", clock_ms=lambda: NOW)
    reference = store.save("session-a", draft, None, "operator")
    validation = store.validate("session-a", reference, "operator")
    store.approve("session-a", reference, validation["validationId"], "operator")
    device = {
        "pose_source_id": "ohmni-pose",
        "world_pose_source_id": "world-ohmni-pose",
        "identity_source_id": "ohmni-status",
        "registration_id": "fixture-transform",
        "odom_origin_id": "origin-a",
        "odom_frame": "odom",
        "world_to_odom": {
            "x_m": 10.0,
            "y_m": -3.0,
            "yaw_deg": 90.0,
            "registration_id": "fixture-transform",
        },
        "limits": {
            "footprint_radius_m": 0.12,
            "position_uncertainty_m": 0.01,
            "stopping_distance_m": 0.04,
            "speed_m_s": 0.12,
            "yaw_rate_deg_s": 35.0,
            "pulse_s": 0.1,
            "arrival_tolerance_m": 0.03,
            "pose_max_age_ms": 500,
            "route_timeout_ms": 120_000,
        },
    }
    document = {
        "v": 1,
        "approval_id": "ground-approval-a",
        "approved_by": "operator",
        "approved_map": store.approved_bundle("session-a", reference),
        "devices": [{**device, "device_id": device_id} for device_id in (9, 10)],
        "expires_at": now_ms + 3_600_000,
    }
    path = tmp_path / "ground-navigation.json"
    path.write_text(json.dumps({**document, "signature": sign_event(document, KEY)}))
    return path


def pose(device_id: int = 9, x: float = 1.0, y: float = 1.0, *, t_ms: int = NOW):
    return GroundNavigationPose(
        device_id, 1, x, y, t_ms, "world-ohmni-pose", "fixture-transform", "origin-a"
    )


def test_approved_grid_plans_a_named_destination_from_the_measured_world_pose(tmp_path):
    deployment = GroundNavigationDeployment.load(deployment_file(tmp_path), KEY)
    plan = deployment.prepare(
        "lobby", (pose(),), (9,), session="session-a", roster_version=1, now_ms=NOW
    )

    route = plan.routes[0]
    assert route.device_id == 9
    assert (route.points[0].x_m, route.points[0].y_m) == (1.0, 1.0)
    assert 2.0 < route.points[-1].x_m < 4.0
    assert 2.0 < route.points[-1].y_m < 4.0
    assert all(0.2 <= point.x_m <= 9.8 and 0.2 <= point.y_m <= 9.8 for point in route.points)
    assert deployment.map_reference == plan.map_reference


def test_two_ground_routes_reserve_waiting_robots_and_distinct_arrivals(tmp_path):
    deployment = GroundNavigationDeployment.load(deployment_file(tmp_path), KEY)
    plan = deployment.prepare(
        "lobby",
        (pose(9), pose(10, 1.0, 2.0)),
        (9, 10),
        session="session-a",
        roster_version=1,
        now_ms=NOW,
    )
    first, second = plan.routes
    assert (
        math.dist(
            (first.points[-1].x_m, first.points[-1].y_m),
            (second.points[-1].x_m, second.points[-1].y_m),
        )
        > 0.3
    )
    from tools.geometry_math import distance_to_segment

    for route, stationary in (
        (first, (1.0, 2.0)),
        (second, (first.points[-1].x_m, first.points[-1].y_m)),
    ):
        for start, end in zip(route.points, route.points[1:], strict=False):
            assert distance_to_segment(stationary, (start.x_m, start.y_m), (end.x_m, end.y_m)) > 0.3


def test_changed_pose_source_permanently_retires_a_frozen_route(tmp_path):
    deployment = GroundNavigationDeployment.load(deployment_file(tmp_path), KEY)
    plan = deployment.prepare(
        "lobby", (pose(),), (9,), session="session-a", roster_version=1, now_ms=NOW
    )
    changed = replace(pose(), registration_id="another-registration")
    with pytest.raises(ValueError, match="source|registration"):
        deployment.revalidate(
            plan, (changed,), (9,), session="session-a", roster_version=1, now_ms=NOW
        )
    with pytest.raises(ValueError, match="retired"):
        deployment.revalidate(
            plan, (pose(),), (9,), session="session-a", roster_version=1, now_ms=NOW
        )


def test_signed_route_is_rechecked_by_the_nodes_independently_loaded_map(tmp_path):
    path = deployment_file(tmp_path)
    host = GroundNavigationDeployment.load(path, KEY)
    node = GroundNavigationDeployment.load(path, KEY)
    plan = host.prepare("lobby", (pose(),), (9,), session="session-a", roster_version=1, now_ms=NOW)
    record = host.route_record(plan, 9, now_ms=NOW)
    admitted = node.admit_route(
        record,
        route_id=f"{plan.plan_id}:9",
        session="session-a",
        device_id=9,
        connection_epoch=1,
        roster_version=1,
        now_ms=NOW,
    )
    assert admitted.route.start.x_m == 1.0
    assert admitted.route.start.y_m == 1.0
    odom_start = admitted.route.world_to_odom.point(admitted.route.start)
    assert odom_start.x_m == pytest.approx(9.0)
    assert odom_start.y_m == pytest.approx(-2.0)
    changed = json.loads(record)
    changed["points"][-1] = {"x_m": 8.5, "y_m": 8.5}
    with pytest.raises(ValueError, match="signature"):
        node.admit_route(
            json.dumps(changed),
            route_id=f"{plan.plan_id}:9",
            session="session-a",
            device_id=9,
            connection_epoch=1,
            roster_version=1,
            now_ms=NOW,
        )
