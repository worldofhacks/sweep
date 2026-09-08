from __future__ import annotations

import base64
import hashlib
import json
import math
import struct
import subprocess
import sys
import zlib
from dataclasses import replace
from pathlib import Path

import cv2
import numpy as np
import pytest

from planner.ground_navigation import GroundNavigationDeployment, GroundNavigationPose
from relay.auth import sign_event
from relay.map_authoring import MapAuthoringStore
from tests.world_bundle_fixtures import fixture_world_draft
from tools.console_world_bundle import validate_image

KEY = b"ground-navigation-fixture-signing-key-32-bytes"
NOW = 100_000


def deployment_file(
    tmp_path: Path,
    *,
    blocked: bool = False,
    now_ms: int = NOW,
    png_filter: int | None = None,
    wall_value: int = 0,
) -> Path:
    draft = fixture_world_draft()
    pixels = np.full((100, 100), 255, dtype=np.uint8)
    if blocked:
        pixels[:, 18:22] = wall_value
    parameters = [] if png_filter is None else [cv2.IMWRITE_PNG_FILTER, png_filter]
    _, encoded = cv2.imencode(".png", pixels, parameters)
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


def test_active_robot_can_advance_while_waiting_robots_keep_their_reserved_positions(tmp_path):
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
    start, end = first.points[:2]
    moving = pose(9, (start.x_m + end.x_m) / 2, (start.y_m + end.y_m) / 2)
    deployment.revalidate(
        plan,
        (moving, pose(10, 1.0, 2.0)),
        (9, 10),
        session="session-a",
        roster_version=1,
        now_ms=NOW,
        active_device_id=9,
    )
    arrived = pose(9, first.points[-1].x_m, first.points[-1].y_m)
    start, end = second.points[:2]
    deployment.revalidate(
        plan,
        (arrived, pose(10, (start.x_m + end.x_m) / 2, (start.y_m + end.y_m) / 2)),
        (9, 10),
        session="session-a",
        roster_version=1,
        now_ms=NOW,
        completed=(9,),
        active_device_id=10,
    )
    with pytest.raises(ValueError, match="stationary"):
        deployment.revalidate(
            plan,
            (pose(9, 1.0, 1.0), pose(10, 1.0, 2.0)),
            (9, 10),
            session="session-a",
            roster_version=1,
            now_ms=NOW,
            completed=(9,),
            active_device_id=10,
        )


@pytest.mark.parametrize("changed", ["roster", "epoch", "stale", "selection", "configuration"])
def test_changed_execution_authority_permanently_retires_a_review(tmp_path, changed):
    path = deployment_file(tmp_path)
    deployment = GroundNavigationDeployment.load(path, KEY)
    plan = deployment.prepare(
        "lobby", (pose(),), (9,), session="session-a", roster_version=1, now_ms=NOW
    )
    if changed == "configuration":
        path.write_text(path.read_text() + " ")
    with pytest.raises(ValueError):
        deployment.revalidate(
            plan,
            (replace(pose(), connection_epoch=2) if changed == "epoch" else pose(),),
            () if changed == "selection" else (9,),
            session="session-a",
            roster_version=2 if changed == "roster" else 1,
            now_ms=NOW + 501 if changed == "stale" else NOW,
        )
    if changed == "configuration":
        path.write_text(path.read_text().rstrip())
    with pytest.raises(ValueError, match="retired"):
        deployment.revalidate(
            plan, (pose(),), (9,), session="session-a", roster_version=1, now_ms=NOW
        )


def test_blocked_wall_prevents_a_named_destination_route(tmp_path):
    deployment = GroundNavigationDeployment.load(deployment_file(tmp_path, blocked=True), KEY)
    with pytest.raises(ValueError, match="route|reachable"):
        deployment.prepare(
            "lobby", (pose(),), (9,), session="session-a", roster_version=1, now_ms=NOW
        )


@pytest.mark.parametrize("filter_name", ["NONE", "SUB", "UP", "AVG", "PAETH"])
def test_every_png_filter_preserves_unknown_cells_as_a_navigation_barrier(tmp_path, filter_name):
    deployment = GroundNavigationDeployment.load(
        deployment_file(
            tmp_path,
            blocked=True,
            wall_value=127,
            png_filter=getattr(cv2, f"IMWRITE_PNG_FILTER_{filter_name}"),
        ),
        KEY,
    )
    with pytest.raises(ValueError, match="unreachable"):
        deployment.prepare(
            "lobby", (pose(),), (9,), session="session-a", roster_version=1, now_ms=NOW
        )


@pytest.mark.parametrize(
    "damage",
    [
        "checksum",
        "decompression_bomb",
        "truncated_stream",
        "extra_stream",
        "transparency",
        "interlace",
        "color",
        "unknown_critical",
        "trailing",
        "unknown_filter",
        "chunk_bound",
    ],
)
def test_portable_map_image_admission_rejects_damaged_or_unbounded_pixels(damage):
    def chunk(kind, value):
        return (
            struct.pack(">I", len(value))
            + kind
            + value
            + struct.pack(">I", zlib.crc32(kind + value))
        )

    rows = b"\x00\xff\x00\x00\x7f\xff"
    if damage == "decompression_bomb":
        rows += b"\xff" * 1_000_000
    if damage == "unknown_filter":
        rows = b"\x05" + rows[1:]
    compressed = zlib.compress(rows)
    if damage == "truncated_stream":
        compressed = compressed[:-1]
    if damage == "extra_stream":
        compressed += zlib.compress(b"\xff")
    header = struct.pack(
        ">IIBBBBB",
        2,
        2,
        8,
        2 if damage == "color" else 0,
        0,
        0,
        1 if damage == "interlace" else 0,
    )
    extra = {
        "transparency": chunk(b"tRNS", b"\x00\xff"),
        "unknown_critical": chunk(b"ABCD", b""),
        "chunk_bound": chunk(b"tEXt", b"x\0y") * 4096,
    }.get(damage, b"")
    payload = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + extra
        + chunk(b"IDAT", compressed)
        + chunk(b"IEND", b"")
    )
    if damage == "checksum":
        payload = payload[:-1] + bytes([payload[-1] ^ 1])
    if damage == "trailing":
        payload += b"unapproved trailing bytes"
    with pytest.raises(ValueError, match="PNG|grayscale"):
        validate_image(
            {
                "name": "grid.png",
                "dataUrl": "data:image/png;base64," + base64.b64encode(payload).decode(),
                "width": 2,
                "height": 2,
                "sha256": hashlib.sha256(payload).hexdigest(),
            },
            occupancy_only=True,
        )


def test_node_independently_admits_the_signed_map_with_only_the_python_standard_library(tmp_path):
    path = deployment_file(tmp_path)
    host = GroundNavigationDeployment.load(path, KEY)
    plan = host.prepare("lobby", (pose(),), (9,), session="session-a", roster_version=1, now_ms=NOW)
    script = """
import sys
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from planner.ground_navigation import GroundNavigationDeployment
node = GroundNavigationDeployment.load(Path(sys.argv[2]), bytes.fromhex(sys.argv[3]))
admission = node.admit_route(sys.argv[4], route_id=sys.argv[5], session='session-a',
    device_id=9, connection_epoch=1, roster_version=1, now_ms=100000)
assert admission.route.start.x_m == 1.0
assert admission.route.world_to_odom.point(admission.route.start).x_m == 9.0
assert node.check_active(admission, now_ms=100000)
"""
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-c",
            script,
            str(Path(__file__).resolve().parents[1]),
            str(path),
            KEY.hex(),
            host.route_record(plan, 9, now_ms=NOW),
            f"{plan.plan_id}:9",
        ],
        text=True,
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
