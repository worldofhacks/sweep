from __future__ import annotations

from dataclasses import replace
from math import ceil, dist

import pytest

from planner.navigation import (
    ArrivalSlot,
    ArtifactPin,
    Connector,
    DronePose,
    GridLevel,
    MotionConfig,
    NavigationArtifact,
    NavigationPermission,
    NavigationPlanner,
    NavigationRefusal,
    NavigationRequest,
    Pose,
    Zone,
)

MOTION = MotionConfig(0.15, 0.2, 0.05, 0.03, 0.1, 0.05)
PERMISSION = NavigationPermission(frozenset({"atrium"}))


def pose(x: float, y: float, z: float = 1.0, floor: str = "level_1") -> Pose:
    return Pose(x, y, z, floor)


def drone(identity: int, x: float, y: float, floor: str = "level_1") -> DronePose:
    return DronePose(identity, 7, pose(x, y, floor=floor))


def artifact(
    blocked: frozenset[tuple[int, int]] = frozenset(),
    *,
    slots: tuple[ArrivalSlot, ...] | None = None,
    floor: str = "level_1",
    extra_grids: tuple[GridLevel, ...] = (),
    connectors: tuple[Connector, ...] = (),
    navigation_allowed: bool = True,
    clearance: float = 0.5,
) -> NavigationArtifact:
    slots = slots or (ArrivalSlot("atrium-a", "atrium", pose(6.5, 1.5, floor=floor), 0.5),)
    return NavigationArtifact(
        ArtifactPin("map-v2", "a" * 64),
        ArtifactPin("geometry-v2", "b" * 64),
        clearance,
        (GridLevel(floor, 1.0, (0.0, 0.0), 1.0, 8, 5, blocked), *extra_grids),
        (Zone("atrium", floor, navigation_allowed, slots),),
        connectors,
    )


def request(*selected: DronePose) -> NavigationRequest:
    return NavigationRequest("atrium", 4, selected, selected, MOTION, PERMISSION)


@pytest.mark.parametrize(
    "field",
    ("map_uncertainty_m", "pose_uncertainty_m", "tracking_allowance_m", "stopping_allowance_m"),
)
def test_motion_config_rejects_negative_allowances(field: str) -> None:
    values = dict(
        aircraft_radius_m=0.15,
        aircraft_height_m=0.2,
        map_uncertainty_m=0.05,
        pose_uncertainty_m=0.03,
        tracking_allowance_m=0.1,
        stopping_allowance_m=0.05,
    )
    values[field] = -0.01

    with pytest.raises(ValueError, match=field):
        MotionConfig(**values)


def segment_samples(start: Pose, end: Pose, spacing_m: float = 0.02) -> list[Pose]:
    count = max(1, ceil(dist(start.xyz, end.xyz) / spacing_m))
    return [
        Pose(
            start.x_m + (end.x_m - start.x_m) * index / count,
            start.y_m + (end.y_m - start.y_m) * index / count,
            start.z_m + (end.z_m - start.z_m) * index / count,
            start.floor_id,
        )
        for index in range(count + 1)
    ]


def test_clear_corridor_generates_inspectable_pinned_route() -> None:
    result = NavigationPlanner().plan(request(drone(1, 0.5, 1.5)), artifact())

    assert result.destination_zone_id == "atrium"
    assert result.map_pin.version == "map-v2"
    assert result.execution_order == (1,)
    assert result.routes[0].waypoints[0] == pose(0.5, 1.5)
    assert result.routes[0].waypoints[-1] == pose(6.5, 1.5)
    assert all(
        segment.radius_m == MOTION.swept_radius_m for segment in result.routes[0].swept_segments
    )


def test_obstacle_detour_checks_real_grid_path() -> None:
    result = NavigationPlanner().plan(request(drone(1, 0.5, 1.5)), artifact(frozenset({(3, 1)})))

    assert not isinstance(result, NavigationRefusal)
    assert any(waypoint.y_m != 1.5 for waypoint in result.routes[0].waypoints)
    assert all(
        (int(point.x_m), int(point.y_m)) != (3, 1)
        for segment in result.routes[0].swept_segments
        for point in segment_samples(segment.start, segment.end)
    )


def test_narrow_geometry_refuses_when_motion_envelope_exceeds_its_inflation() -> None:
    result = NavigationPlanner().plan(request(drone(1, 0.5, 1.5)), artifact(clearance=0.2))

    assert result == NavigationRefusal(
        "clearance_exceeds_geometry", "motion clearance exceeds geometry inflation"
    )


def test_unknown_blocked_strip_cannot_be_crossed() -> None:
    result = NavigationPlanner().plan(
        request(drone(1, 0.5, 1.5)), artifact(frozenset((3, y) for y in range(5)))
    )

    assert result.code == "route_unreachable"


def test_disconnected_floor_refuses_without_a_permitted_connector() -> None:
    result = NavigationPlanner().plan(
        request(drone(1, 0.5, 1.5)),
        artifact(floor="mezzanine"),
    )

    assert result.code == "wrong_floor"


def test_valid_vertical_connector_generates_a_vertical_swept_segment() -> None:
    ground = GridLevel("level_1", 1.0, (0.0, 0.0), 1.0, 8, 5, frozenset())
    upper = GridLevel("mezzanine", 3.0, (0.0, 0.0), 1.0, 8, 5, frozenset())
    slots = (ArrivalSlot("atrium-a", "atrium", pose(6.5, 1.5, 3.0, "mezzanine"), 0.5),)
    map_artifact = NavigationArtifact(
        ArtifactPin("map-v2", "a" * 64),
        ArtifactPin("geometry-v2", "b" * 64),
        0.5,
        (ground, upper),
        (Zone("atrium", "mezzanine", True, slots),),
        (
            Connector(
                "lift", "level_1", "mezzanine", pose(2.5, 1.5), pose(2.5, 1.5, 3.0, "mezzanine")
            ),
        ),
    )

    result = NavigationPlanner().plan(request(drone(1, 0.5, 1.5)), map_artifact)

    assert not isinstance(result, NavigationRefusal)
    assert any(
        segment.start.floor_id != segment.end.floor_id
        for segment in result.routes[0].swept_segments
    )


def test_vertical_transition_refuses_when_an_intermediate_altitude_band_is_blocked() -> None:
    low = GridLevel("level_1", 1.0, (0.0, 0.0), 1.0, 8, 5, frozenset())
    middle = GridLevel("level_1", 2.0, (0.0, 0.0), 1.0, 8, 5, frozenset({(6, 1)}))
    high = GridLevel("level_1", 3.0, (0.0, 0.0), 1.0, 8, 5, frozenset())
    slots = (ArrivalSlot("atrium-a", "atrium", pose(6.5, 1.5, 3.0), 0.5),)
    map_artifact = NavigationArtifact(
        ArtifactPin("map-v2", "a" * 64),
        ArtifactPin("geometry-v2", "b" * 64),
        0.5,
        (low, middle, high),
        (Zone("atrium", "level_1", True, slots),),
    )

    result = NavigationPlanner().plan(request(drone(1, 0.5, 1.5)), map_artifact)

    assert result.code == "route_unreachable"


def test_excluded_branch_and_arrival_permission_are_independent() -> None:
    excluded = NavigationPlanner().plan(
        request(drone(1, 0.5, 1.5)), artifact(navigation_allowed=False)
    )
    denied = NavigationPlanner().plan(
        NavigationRequest(
            "atrium",
            4,
            (drone(1, 0.5, 1.5),),
            (drone(1, 0.5, 1.5),),
            MOTION,
            NavigationPermission(frozenset()),
        ),
        artifact(),
    )

    assert excluded.code == "destination_excluded"
    assert denied.code == "arrival_not_permitted"


def test_conflicting_arrival_slot_is_refused() -> None:
    slots = (ArrivalSlot("atrium-a", "atrium", pose(6.5, 1.5), 0.1),)
    result = NavigationPlanner().plan(request(drone(1, 0.5, 1.5)), artifact(slots=slots))

    assert result.code == "arrival_conflict"


def test_surplus_arrival_slots_are_allowed() -> None:
    slots = (
        ArrivalSlot("atrium-a", "atrium", pose(6.5, 1.5), 0.5),
        ArrivalSlot("atrium-b", "atrium", pose(6.5, 3.5), 0.5),
    )
    result = NavigationPlanner().plan(request(drone(1, 0.5, 1.5)), artifact(slots=slots))
    assert not isinstance(result, NavigationRefusal)


def test_diagonal_supercover_blocks_simplified_segment() -> None:
    level = GridLevel("level_1", 1.0, (0.0, 0.0), 1.0, 4, 3, frozenset({(1, 1)}))
    from planner.navigation import _line_is_free

    assert not _line_is_free(pose(0.5, 1.35), pose(2.5, 0.35), level)


def test_vertical_connector_hits_stationary_aircraft_volume() -> None:
    planner = NavigationPlanner()
    motion = MotionConfig(0.01, 2.0, 0, 0, 0, 0)
    start = pose(2.5, 1.5, 1.0)
    end = pose(2.5, 1.5, 3.0, "mezzanine")
    assert planner._segment_hits_reservation(
        start,
        end,
        [(2, pose(2.5, 1.5, 2.0, "mezzanine"), 0.01, 2.0)],
        0.01,
        1,
        motion.aircraft_height_m,
    )


def test_dense_grid_exempts_the_active_aircraft_reservation() -> None:
    dense = GridLevel("level_1", 1.0, (0.0, 0.0), 0.1, 80, 20, frozenset())
    slots = (ArrivalSlot("atrium-a", "atrium", pose(6.5, 1.5), 0.5),)
    map_artifact = NavigationArtifact(
        ArtifactPin("map-v2", "a" * 64),
        ArtifactPin("geometry-v2", "b" * 64),
        0.5,
        (dense,),
        (Zone("atrium", "level_1", True, slots),),
    )
    result = NavigationPlanner().plan(request(drone(1, 0.05, 1.55)), map_artifact)
    assert not isinstance(result, NavigationRefusal)


def test_revalidation_refuses_missing_frozen_obstacle_roster() -> None:
    first, second = drone(1, 0.5, 1.5), drone(2, 0.5, 3.5)
    slots = (ArrivalSlot("atrium-a", "atrium", pose(6.5, 1.5), 0.5),)
    plan = NavigationPlanner().plan(
        NavigationRequest("atrium", 4, (first,), (first, second), MOTION, PERMISSION),
        artifact(slots=slots),
    )
    assert not isinstance(plan, NavigationRefusal)
    assert (
        NavigationPlanner().revalidate(plan, artifact(slots=slots), (first,), 0, 0, 0.1).code
        == "connection_changed"
    )


def test_revalidation_binds_zone_approval_to_frozen_plan() -> None:
    plan = NavigationPlanner().plan(request(drone(1, 0.5, 1.5)), artifact())
    assert not isinstance(plan, NavigationRefusal)
    changed = artifact(navigation_allowed=False)
    assert (
        NavigationPlanner().revalidate(plan, changed, (drone(1, 0.5, 1.5),), 0, 0, 0.1).code
        == "artifact_changed"
    )


def test_revalidation_binds_arrival_slots_and_connector_approval() -> None:
    plan = NavigationPlanner().plan(request(drone(1, 0.5, 1.5)), artifact())
    assert not isinstance(plan, NavigationRefusal)
    base = artifact()
    altered_slot = replace(
        base,
        zones=(
            replace(
                base.zones[0], arrival_slots=(ArrivalSlot("other", "atrium", pose(5.5, 1.5), 0.5),)
            ),
        ),
    )
    assert (
        NavigationPlanner().revalidate(plan, altered_slot, (drone(1, 0.5, 1.5),), 0, 0, 0.1).code
        == "artifact_changed"
    )
    altered_connector = replace(
        base,
        connectors=(Connector("disabled", "level_1", "level_1", pose(1, 1), pose(1, 1), False),),
    )
    assert (
        NavigationPlanner()
        .revalidate(plan, altered_connector, (drone(1, 0.5, 1.5),), 0, 0, 0.1)
        .code
        == "artifact_changed"
    )


def test_tall_aircraft_vertical_body_rejects_blocked_band() -> None:
    low = GridLevel("level_1", 1.0, (0, 0), 1.0, 8, 5, frozenset())
    blocked = GridLevel("level_1", 2.0, (0, 0), 1.0, 8, 5, frozenset({(6, 1)}))
    high = GridLevel("level_1", 3.0, (0, 0), 1.0, 8, 5, frozenset())
    slots = (ArrivalSlot("atrium-a", "atrium", pose(6.5, 1.5, 3), 0.5),)
    map_artifact = NavigationArtifact(
        ArtifactPin("map-v2", "a" * 64),
        ArtifactPin("geometry-v2", "b" * 64),
        0.5,
        (low, blocked, high),
        (Zone("atrium", "level_1", True, slots),),
    )
    tall = MotionConfig(0.01, 2.0, 0, 0, 0, 0)
    result = NavigationPlanner().plan(
        NavigationRequest(
            "atrium", 4, (drone(1, 0.5, 1.5),), (drone(1, 0.5, 1.5),), tall, PERMISSION
        ),
        map_artifact,
    )
    assert isinstance(result, NavigationRefusal)


def test_tall_body_rejects_blocked_static_layer_on_horizontal_route() -> None:
    center = GridLevel("level_1", 1.0, (0, 0), 1.0, 8, 5, frozenset())
    upper = GridLevel("level_1", 1.5, (0, 0), 1.0, 8, 5, frozenset({(3, 1)}))
    slots = (ArrivalSlot("atrium-a", "atrium", pose(6.5, 1.5), 0.5),)
    map_artifact = NavigationArtifact(
        ArtifactPin("map-v2", "a" * 64),
        ArtifactPin("geometry-v2", "b" * 64),
        0.5,
        (center, upper),
        (Zone("atrium", "level_1", True, slots),),
    )
    tall = MotionConfig(0.01, 2.0, 0, 0, 0, 0)
    result = NavigationPlanner().plan(
        NavigationRequest(
            "atrium", 4, (drone(1, 0.5, 1.5),), (drone(1, 0.5, 1.5),), tall, PERMISSION
        ),
        map_artifact,
    )
    assert isinstance(result, NavigationRefusal)


def test_sequential_route_avoids_aircraft_waiting_at_its_start_and_arrival() -> None:
    slots = (
        ArrivalSlot("atrium-a", "atrium", pose(6.5, 1.5), 0.5),
        ArrivalSlot("atrium-b", "atrium", pose(6.5, 3.5), 0.5),
    )
    result = NavigationPlanner().plan(
        request(drone(1, 0.5, 1.5), drone(2, 3.5, 1.5)), artifact(slots=slots)
    )

    assert not isinstance(result, NavigationRefusal)
    first = result.routes[0]
    stationary = pose(3.5, 1.5)
    assert all(
        dist(point.xyz, stationary.xyz) >= 2 * MOTION.swept_radius_m
        for segment in first.swept_segments
        for point in segment_samples(segment.start, segment.end)
    )
    assert result.routes[1].arrival_slot.slot_id == "atrium-b"


def test_revalidation_refuses_changed_artifacts_or_stationary_aircraft() -> None:
    planner = NavigationPlanner()
    plan = planner.plan(request(drone(1, 0.5, 1.5)), artifact())

    assert not isinstance(plan, NavigationRefusal)
    assert planner.revalidate(plan, artifact(), (drone(1, 0.5, 1.5),), 0, 0, 0.1) is None
    changed = NavigationArtifact(
        ArtifactPin("map-v3", "c" * 64),
        plan.geometry_pin,
        0.5,
        artifact().grids,
        artifact().zones,
    )
    assert (
        planner.revalidate(plan, changed, (drone(1, 0.5, 1.5),), 0, 0, 0.1).code
        == "artifact_changed"
    )
    static = DronePose(2, 7, pose(3.5, 1.5))
    assert (
        planner.revalidate(plan, artifact(), (drone(1, 0.5, 1.5), static), 0, 0, 0.1).code
        == "remaining_route_obstructed"
    )


def test_astar_edge_cannot_pass_close_to_stationary_aircraft_between_clear_cells() -> None:
    active = drone(1, 1.5, 1.5)
    stationary = drone(2, 2.0, 2.2)
    destination = ArrivalSlot("atrium-a", "atrium", pose(2.5, 1.5), 0.5)
    request = NavigationRequest("atrium", 4, (active,), (active, stationary), MOTION, PERMISSION)
    result = NavigationPlanner().plan(request, artifact(slots=(destination,)))

    assert not isinstance(result, NavigationRefusal)
    assert all(
        dist(point.xyz, stationary.pose.xyz) >= 2 * MOTION.swept_radius_m
        for segment in result.routes[0].swept_segments
        for point in segment_samples(segment.start, segment.end)
    )


@pytest.mark.parametrize("start_z,goal_z", [(1.0, 3.0), (3.0, 1.0)])
def test_height_clearance_bands_do_not_become_out_of_range_waypoints(start_z, goal_z):
    levels = tuple(
        GridLevel("level_1", z, (0, 0), 1.0, 8, 5, frozenset()) for z in (0.5, 1.0, 2.0, 3.0, 3.5)
    )
    active = DronePose(1, 7, pose(0.5, 1.5, start_z))
    destination = ArrivalSlot("atrium-a", "atrium", pose(6.5, 1.5, goal_z), 0.5)
    mapped = replace(artifact(slots=(destination,)), grids=levels)
    motion = replace(MOTION, aircraft_height_m=2.0)
    result = NavigationPlanner().plan(replace(request(active), motion=motion), mapped)
    assert not isinstance(result, NavigationRefusal), result
    heights = [point.z_m for point in result.routes[0].waypoints]
    assert all(min(start_z, goal_z) <= z <= max(start_z, goal_z) for z in heights)
    assert heights == sorted(heights, reverse=start_z > goal_z)


@pytest.mark.parametrize("uncertainty,separation", [(0.1, 0.25), (0.2, 0.3)])
def test_vertical_reservations_include_uncertainty(uncertainty, separation):
    active = drone(1, 0.5, 1.5)
    stationary = DronePose(2, 7, pose(3.5, 1.5, 1.0 + separation))
    motion = MotionConfig(0.15, 0.2, 0, uncertainty, 0, 0)
    result = NavigationPlanner().plan(
        NavigationRequest("atrium", 4, (active,), (active, stationary), motion, PERMISSION),
        artifact(),
    )
    assert not isinstance(result, NavigationRefusal), result
    assert len(result.routes[0].swept_segments) > 1
    assert all(
        segment.height_m == pytest.approx(0.2 + 2 * uncertainty)
        for segment in result.routes[0].swept_segments
    )
