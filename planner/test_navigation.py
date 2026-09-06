from __future__ import annotations

import json
import shutil
from dataclasses import replace
from hashlib import sha256
from math import ceil, dist
from pathlib import Path

import numpy as np
import pytest

from planner.navigation import (
    ArrivalSlot,
    ArtifactPin,
    Connector,
    DronePose,
    GridLevel,
    MotionConfig,
    NavigationArtifact,
    NavigationDispatchAcceptance,
    NavigationEvidence,
    NavigationLiveState,
    NavigationPermission,
    NavigationPlan,
    NavigationPlanner,
    NavigationRefusal,
    NavigationRequest,
    Pose,
    Zone,
    preview_evidence,
)
from planner.navigation_geometry import Reservation, line_is_free, segment_hits_reservation
from tools.map_geometry import generate

MOTION = MotionConfig(0.15, 0.2, 0.05, 0.03, 0.1, 0.05, 0.2)
PERMISSION = NavigationPermission(frozenset({"atrium"}))
ZONE_BOUNDARY = ((-1.0, -1.0), (9.0, -1.0), (9.0, 6.0), (-1.0, 6.0), (-1.0, -1.0))
GEOFENCE = ((-2.0, -2.0), (10.0, -2.0), (10.0, 7.0), (-2.0, 7.0), (-2.0, -2.0))
FIXTURE = Path(__file__).parents[1] / "tests" / "fixtures" / "geometry"


def pose(x: float, y: float, z: float = 1.0, floor: str = "level_1") -> Pose:
    return Pose(x, y, z, floor)


def drone(
    identity: int,
    x: float,
    y: float,
    z: float = 1.0,
    floor: str = "level_1",
    epoch: int = 7,
) -> DronePose:
    return DronePose(identity, epoch, pose(x, y, z, floor))


def arrival(
    identity: str,
    x: float,
    y: float,
    *,
    z: float = 1.0,
    floor: str = "level_1",
    radius: float = 0.5,
    half_height: float = 0.5,
) -> ArrivalSlot:
    return ArrivalSlot(identity, "atrium", pose(x, y, z, floor), radius, half_height)


def grid(
    floor: str = "level_1",
    z: float = 1.0,
    blocked: frozenset[tuple[int, int]] = frozenset(),
    *,
    width: int = 8,
    height: int = 5,
    cell_m: float = 1.0,
) -> GridLevel:
    return GridLevel(floor, z, (0.0, 0.0), cell_m, width, height, blocked)


def artifact(
    blocked: frozenset[tuple[int, int]] = frozenset(),
    *,
    slots: tuple[ArrivalSlot, ...] | None = None,
    floor: str = "level_1",
    grids: tuple[GridLevel, ...] | None = None,
    connectors: tuple[Connector, ...] = (),
    owner_approved: bool = True,
    clearance: float = 0.75,
) -> NavigationArtifact:
    slots = slots or (arrival("atrium-a", 6.5, 1.5, floor=floor),)
    levels = grids or (grid(floor, blocked=blocked),)
    zone = Zone("atrium", floor, owner_approved, ZONE_BOUNDARY, 0.0, 4.0, slots)
    return NavigationArtifact(
        ArtifactPin("map-v2", "a" * 64),
        ArtifactPin("geometry-v2", "b" * 64),
        ArtifactPin("preview", "c" * 64),
        preview_evidence("synthetic"),
        clearance,
        GEOFENCE,
        -1.0,
        5.0,
        levels,
        (zone,),
        connectors,
    )


def request(
    *selected: DronePose,
    all_positions: tuple[DronePose, ...] | None = None,
    motion: MotionConfig = MOTION,
    permission: NavigationPermission = PERMISSION,
    roster_version: int = 4,
    plan_revision: int = 9,
) -> NavigationRequest:
    return NavigationRequest(
        "atrium",
        roster_version,
        plan_revision,
        selected,
        all_positions or selected,
        motion,
        permission,
    )


def planned(
    *selected: DronePose,
    map_artifact: NavigationArtifact | None = None,
    all_positions: tuple[DronePose, ...] | None = None,
) -> tuple[NavigationPlanner, NavigationArtifact, NavigationPlan]:
    planner = NavigationPlanner()
    map_artifact = map_artifact or artifact()
    result = planner.plan(request(*selected, all_positions=all_positions), map_artifact)
    assert isinstance(result, NavigationPlan)
    return planner, map_artifact, result


def live_for(
    plan: NavigationPlan,
    *,
    positions: tuple[DronePose, ...] | None = None,
    selected_ids: tuple[int, ...] | None = None,
    roster_version: int | None = None,
    plan_revision: int | None = None,
    motion: MotionConfig | None = None,
    permission: NavigationPermission | None = None,
) -> NavigationLiveState:
    return NavigationLiveState(
        plan.roster_version if roster_version is None else roster_version,
        plan.plan_revision if plan_revision is None else plan_revision,
        tuple(drone_pose.drone_id for drone_pose in plan.selected)
        if selected_ids is None
        else selected_ids,
        plan.roster if positions is None else positions,
        plan.config if motion is None else motion,
        plan.permission if permission is None else permission,
    )


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


@pytest.fixture(scope="module")
def generated_geometry(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[Path, Path, dict[str, str]]:
    output = tmp_path_factory.mktemp("navigation-geometry") / "output"
    accepted = json.loads((FIXTURE / "accepted_versions.json").read_text())
    generate(FIXTURE, FIXTURE / "geometry_authoring.json", output, accepted)
    return FIXTURE, output, accepted


@pytest.mark.parametrize(
    "field",
    (
        "map_uncertainty_m",
        "pose_uncertainty_m",
        "tracking_allowance_m",
        "stopping_allowance_m",
        "max_altitude_layer_offset_m",
    ),
)
def test_motion_config_rejects_negative_allowances(field: str) -> None:
    values = {
        "aircraft_radius_m": 0.15,
        "aircraft_height_m": 0.2,
        "map_uncertainty_m": 0.05,
        "pose_uncertainty_m": 0.03,
        "tracking_allowance_m": 0.1,
        "stopping_allowance_m": 0.05,
        "max_altitude_layer_offset_m": 0.2,
    }
    values[field] = -0.01

    with pytest.raises(ValueError, match=field):
        MotionConfig(**values)


def test_contracts_reject_truthy_wrong_types_and_mutable_collections() -> None:
    with pytest.raises(ValueError, match="x_m"):
        Pose(True, 0.0, 1.0, "level_1")
    with pytest.raises(ValueError, match="SHA-256"):
        ArtifactPin("map", "g" * 64)
    with pytest.raises(ValueError, match="immutable.*coordinate"):
        Zone(
            "atrium",
            "level_1",
            True,
            ([-1.0, -1.0], [1.0, -1.0], [1.0, 1.0], [-1.0, 1.0], [-1.0, -1.0]),
            0.0,
            2.0,
            (),
        )
    with pytest.raises(ValueError, match="boolean"):
        Zone("atrium", "level_1", 1, ZONE_BOUNDARY, 0.0, 4.0, ())
    with pytest.raises(ValueError, match="immutable set"):
        GridLevel("level_1", 1.0, (0.0, 0.0), 1.0, 2, 2, {(0, 0)})
    with pytest.raises(ValueError, match="immutable set"):
        NavigationPermission({"atrium"})
    with pytest.raises(ValueError, match="immutable tuples"):
        NavigationRequest(
            "atrium",
            1,
            1,
            [drone(1, 0.5, 0.5)],
            (drone(1, 0.5, 0.5),),
            MOTION,
            PERMISSION,
        )
    with pytest.raises(ValueError, match="immutable tuples"):
        replace(artifact(), grids=[grid()])
    with pytest.raises(ValueError, match="cannot claim"):
        NavigationEvidence(
            "offline_authoring",
            "synthetic",
            False,
            True,
            (
                "geometry_acceptance_missing",
                "camera_visibility_unverified",
                "runtime_dispatch_contract_missing",
                "synthetic_geometry_evidence",
            ),
        )


def test_public_route_and_plan_constructors_enforce_internal_invariants() -> None:
    _, _, plan = planned(drone(1, 0.5, 1.5))

    with pytest.raises(ValueError, match="exactly cover"):
        replace(plan.routes[0], swept_segments=())
    with pytest.raises(ValueError, match="exactly match"):
        replace(plan, routes=())
    with pytest.raises(TypeError, match="dispatch_eligible"):
        NavigationPlan(
            plan.map_pin,
            plan.geometry_pin,
            plan.navigation_pin,
            plan.evidence,
            plan.config,
            plan.permission,
            plan.roster_version,
            plan.plan_revision,
            plan.destination_zone_id,
            plan.selected,
            plan.roster,
            plan.arrival_slots,
            plan.routes,
            plan.execution_order,
            plan.artifact_sha256,
            dispatch_eligible=True,
        )


def test_clear_corridor_generates_inspectable_pinned_preview() -> None:
    result = NavigationPlanner().plan(request(drone(1, 0.5, 1.5)), artifact())

    assert isinstance(result, NavigationPlan)
    assert result.destination_zone_id == "atrium"
    assert result.map_pin.version == "map-v2"
    assert result.navigation_pin.version == "preview"
    assert result.execution_order == (1,)
    assert result.dispatch_eligible is False
    assert result.routes[0].waypoints[0] == pose(0.5, 1.5)
    assert result.routes[0].waypoints[-1] == pose(6.5, 1.5)
    assert all(
        segment.radius_m == MOTION.swept_radius_m
        and segment.half_height_m == MOTION.swept_half_height_m
        for segment in result.routes[0].swept_segments
    )


def test_obstacle_detour_checks_real_grid_path() -> None:
    result = NavigationPlanner().plan(
        request(drone(1, 0.5, 1.5)),
        artifact(frozenset({(3, 1)})),
    )

    assert isinstance(result, NavigationPlan)
    assert any(waypoint.y_m != 1.5 for waypoint in result.routes[0].waypoints)
    assert all(
        (int(point.x_m), int(point.y_m)) != (3, 1)
        for segment in result.routes[0].swept_segments
        for point in segment_samples(segment.start, segment.end)
    )


def test_supercover_rejects_exact_blocked_cell_one_one_reproduction() -> None:
    level = grid(blocked=frozenset({(1, 1)}), width=4, height=4)
    start = pose(0.23220377530599468, 2.704807137197513)
    goal = pose(3.3816983748064655, 1.369250164314336)

    assert line_is_free(start, goal, level) is False
    destination = arrival("atrium-a", goal.x_m, goal.y_m)
    result = NavigationPlanner().plan(
        request(drone(1, start.x_m, start.y_m)),
        artifact(slots=(destination,), grids=(level,)),
    )
    assert isinstance(result, NavigationPlan)
    assert result.routes[0].waypoints != (start, goal)


def test_diagonal_crack_in_narrow_geometry_is_not_a_route() -> None:
    level = grid(blocked=frozenset({(1, 0), (0, 1)}), width=4, height=4)
    destination = arrival("atrium-a", 2.5, 2.5)

    result = NavigationPlanner().plan(
        request(drone(1, 0.5, 0.5)),
        artifact(slots=(destination,), grids=(level,)),
    )

    assert result == NavigationRefusal(
        "route_unreachable",
        "no deterministic assignment has a clearance-checked route to every slot",
    )


def test_motion_envelope_must_fit_both_horizontal_and_vertical_inflation() -> None:
    horizontal = NavigationPlanner().plan(
        request(drone(1, 0.5, 1.5)),
        artifact(
            slots=(arrival("atrium-a", 6.5, 1.5, radius=0.3, half_height=0.3),),
            clearance=MOTION.swept_radius_m - 0.001,
        ),
    )
    tall_motion = replace(MOTION, aircraft_height_m=1.2)
    vertical = NavigationPlanner().plan(
        request(drone(1, 0.5, 1.5), motion=tall_motion),
        artifact(clearance=0.75),
    )

    assert horizontal.code == "clearance_exceeds_geometry"
    assert vertical.code == "clearance_exceeds_geometry"


def test_altitude_must_be_near_an_accepted_layer_and_inside_geofence_volume() -> None:
    no_band = NavigationPlanner().plan(request(drone(1, 0.5, 1.5, z=100.0)), artifact())
    outside_geofence = NavigationPlanner().plan(
        request(drone(1, 0.5, 1.5, z=4.8)),
        artifact(grids=(grid(z=1.0), grid(z=4.8))),
    )

    assert no_band.code == "position_unmapped"
    assert outside_geofence.code == "position_unmapped"


def test_distant_layer_is_not_selected_even_when_geometry_clearance_is_large() -> None:
    result = NavigationPlanner().plan(
        request(drone(1, 0.5, 1.5)),
        artifact(
            slots=(arrival("atrium-a", 6.5, 1.5),),
            grids=(grid(z=1.21),),
            clearance=10.0,
        ),
    )

    assert result == NavigationRefusal(
        "position_unmapped",
        "aircraft 1 has no clearance-checked map and altitude band",
    )


def test_unknown_blocked_strip_cannot_be_crossed() -> None:
    result = NavigationPlanner().plan(
        request(drone(1, 0.5, 1.5)),
        artifact(frozenset((3, y) for y in range(5))),
    )

    assert result.code == "route_unreachable"


def multilevel_artifact(*, block_connector_band: bool = False) -> NavigationArtifact:
    levels = (
        grid("level_1", 1.0),
        grid("level_1", 1.8, frozenset({(2, 1)}) if block_connector_band else frozenset()),
        grid("mezzanine", 2.6),
        grid("mezzanine", 3.0),
    )
    slot = arrival("atrium-a", 6.5, 1.5, z=3.0, floor="mezzanine")
    connector = Connector(
        "lift",
        "level_1",
        "mezzanine",
        pose(2.5, 1.5),
        pose(2.5, 1.5, 3.0, "mezzanine"),
    )
    return artifact(
        slots=(slot,),
        floor="mezzanine",
        grids=levels,
        connectors=(connector,),
    )


def test_disconnected_floor_refuses_without_a_permitted_connector() -> None:
    levels = (grid("level_1", 1.0), grid("mezzanine", 3.0))
    slot = arrival("atrium-a", 6.5, 1.5, z=3.0, floor="mezzanine")
    result = NavigationPlanner().plan(
        request(drone(1, 0.5, 1.5)),
        artifact(slots=(slot,), floor="mezzanine", grids=levels),
    )

    assert result.code == "wrong_floor"


def test_valid_vertical_connector_requires_continuous_3d_band_coverage() -> None:
    result = NavigationPlanner().plan(
        request(drone(1, 0.5, 1.5)),
        multilevel_artifact(),
    )

    assert isinstance(result, NavigationPlan)
    assert any(
        segment.start.floor_id != segment.end.floor_id
        for segment in result.routes[0].swept_segments
    )


def test_vertical_connector_refuses_when_an_intermediate_band_is_blocked() -> None:
    result = NavigationPlanner().plan(
        request(drone(1, 0.5, 1.5)),
        multilevel_artifact(block_connector_band=True),
    )

    assert result.code == "route_unreachable"


def test_vertical_route_rejects_blocked_band_even_when_free_band_margins_overlap() -> None:
    levels = (
        grid("level_1", 1.0),
        grid("level_1", 1.4, frozenset({(6, 1)})),
        grid("level_1", 1.8),
    )
    result = NavigationPlanner().plan(
        request(drone(1, 0.5, 1.5, z=1.0)),
        artifact(
            slots=(arrival("atrium-a", 6.5, 1.5, z=1.8),),
            grids=levels,
        ),
    )

    assert result.code == "route_unreachable"


def test_physical_overlap_ignores_conflicting_floor_labels() -> None:
    levels = (grid("level_1", 1.0), grid("mezzanine", 1.0))
    active = drone(1, 0.5, 1.5)
    mislabeled_stationary = drone(2, 0.5, 1.5, floor="mezzanine")

    result = NavigationPlanner().plan(
        request(active, all_positions=(active, mislabeled_stationary)),
        artifact(grids=levels),
    )

    assert result.code == "initial_overlap"


def test_vertical_connector_collision_is_physical_not_floor_label_based() -> None:
    map_artifact = multilevel_artifact()
    active = drone(1, 0.5, 1.5)
    stationary = drone(2, 2.5, 1.5, z=1.8, floor="level_1")

    result = NavigationPlanner().plan(
        request(active, all_positions=(active, stationary)),
        map_artifact,
    )

    assert result.code == "route_unreachable"


def test_identity_keyed_reservations_refuse_distinct_aircraft_at_same_pose() -> None:
    first = drone(1, 0.5, 1.5)
    second = drone(2, 0.5, 1.5)

    result = NavigationPlanner().plan(
        request(first, all_positions=(first, second)),
        artifact(),
    )

    assert result.code == "initial_overlap"


def test_nonselected_aircraft_in_blocked_cell_refuses_plan() -> None:
    active = drone(1, 0.5, 1.5)
    blocked_stationary = drone(2, 7.5, 3.5)

    result = NavigationPlanner().plan(
        request(active, all_positions=(active, blocked_stationary)),
        artifact(blocked=frozenset({(7, 3)})),
    )

    assert result.code == "position_unmapped"


def test_pose_admission_uses_nearest_covering_free_band_deterministically() -> None:
    active = drone(1, 0.5, 1.5)
    stationary = drone(2, 7.5, 3.5)
    blocked_nearest = grid("level_1", 1.0, frozenset({(7, 3)}))
    free_fallback = grid("level_1", 1.2)

    result = NavigationPlanner().plan(
        request(active, all_positions=(active, stationary)),
        artifact(grids=(blocked_nearest, free_fallback)),
    )

    assert isinstance(result, NavigationPlan)


def test_artifact_rejects_arrival_slot_in_blocked_cell() -> None:
    with pytest.raises(ValueError, match="free validated altitude band"):
        artifact(blocked=frozenset({(6, 1)}))


def test_analytic_clearance_catches_subsample_collision() -> None:
    start, end = pose(0.0, 0.0), pose(1.0, 0.0)
    obstacle = Reservation(2, pose(0.525, 0.7598), 0.38, 0.33)

    assert segment_hits_reservation(start, end, {2: obstacle}, 0.38, 0.33, 1) is True
    assert all(
        dist(sample.xyz, obstacle.pose.xyz) > 0.76
        for sample in segment_samples(start, end, spacing_m=0.05)
    )


def test_analytic_clearance_uses_vertical_extent() -> None:
    start, end = pose(0.0, 0.0), pose(1.0, 0.0)
    overlapping = Reservation(2, pose(0.5, 0.0, 1.65), 0.38, 0.33)
    separated = Reservation(3, pose(0.5, 0.0, 1.67), 0.38, 0.33)

    assert segment_hits_reservation(start, end, {2: overlapping}, 0.38, 0.33, 1) is True
    assert segment_hits_reservation(start, end, {3: separated}, 0.38, 0.33, 1) is False


def test_assignment_search_backtracks_when_near_slot_would_block_deeper_slot() -> None:
    corridor_walls = frozenset({*((x, 0) for x in range(8)), *((x, 2) for x in range(8))})
    slots = (arrival("a-near", 4.5, 1.5), arrival("b-deep", 6.5, 1.5))
    first = drone(1, 2.5, 1.5)
    second = drone(2, 0.5, 1.5)

    result = NavigationPlanner().plan(
        request(first, second),
        artifact(slots=slots, grids=(grid(blocked=corridor_walls, height=3),)),
    )

    assert isinstance(result, NavigationPlan)
    assert tuple(route.arrival_slot.slot_id for route in result.routes) == ("b-deep", "a-near")


def test_assignment_uses_later_slot_when_first_slot_is_occupied() -> None:
    active = drone(1, 0.5, 1.5)
    stationary = drone(2, 6.5, 1.5)
    result = NavigationPlanner().plan(
        request(active, all_positions=(active, stationary)),
        artifact(
            slots=(arrival("a-occupied", 6.5, 1.5), arrival("b-free", 6.5, 3.5)),
        ),
    )

    assert isinstance(result, NavigationPlan)
    assert result.arrival_slots[0].slot_id == "b-free"


def test_assignment_minimizes_feasible_route_cost_before_stable_tie_breaking() -> None:
    right = drone(1, 6.5, 1.5)
    left = drone(2, 1.5, 1.5)
    result = NavigationPlanner().plan(
        request(right, left),
        artifact(
            slots=(arrival("a-left", 1.5, 3.5), arrival("b-right", 6.5, 3.5)),
        ),
    )

    assert isinstance(result, NavigationPlan)
    assert tuple(route.arrival_slot.slot_id for route in result.routes) == (
        "b-right",
        "a-left",
    )


def test_excluded_destination_and_permission_are_independent() -> None:
    excluded = NavigationPlanner().plan(
        request(drone(1, 0.5, 1.5)),
        artifact(owner_approved=False),
    )
    denied = NavigationPlanner().plan(
        request(
            drone(1, 0.5, 1.5),
            permission=NavigationPermission(frozenset()),
        ),
        artifact(),
    )

    assert excluded.code == "destination_excluded"
    assert denied.code == "arrival_not_permitted"


def test_conflicting_arrival_slot_volume_is_refused() -> None:
    too_small = arrival("atrium-a", 6.5, 1.5, radius=0.1, half_height=0.1)
    result = NavigationPlanner().plan(
        request(drone(1, 0.5, 1.5)),
        artifact(slots=(too_small,)),
    )

    assert result.code == "arrival_conflict"


def test_sequential_route_avoids_aircraft_waiting_at_start_and_arrival() -> None:
    slots = (arrival("atrium-a", 6.5, 1.5), arrival("atrium-b", 6.5, 3.5))
    result = NavigationPlanner().plan(
        request(drone(1, 0.5, 1.5), drone(2, 3.5, 1.5)),
        artifact(slots=slots),
    )

    assert isinstance(result, NavigationPlan)
    stationary = pose(3.5, 1.5)
    assert all(
        dist(point.xyz, stationary.xyz) > 2 * MOTION.swept_radius_m
        for segment in result.routes[0].swept_segments
        for point in segment_samples(segment.start, segment.end)
    )


def test_revalidation_of_unchanged_preview_still_refuses_dispatch() -> None:
    planner, map_artifact, plan = planned(drone(1, 0.5, 1.5))

    refusal = planner.revalidate(plan, map_artifact, live_for(plan), 0, 0, 0.1)

    assert refusal == NavigationRefusal(
        "artifact_not_dispatchable",
        "preview is blocked by: geometry_acceptance_missing, "
        "camera_visibility_unverified, runtime_dispatch_contract_missing, "
        "synthetic_geometry_evidence",
    )


@pytest.mark.parametrize(
    ("live_changes", "code"),
    (
        ({"roster_version": 5}, "roster_changed"),
        ({"plan_revision": 10}, "plan_revision_changed"),
        ({"selected_ids": ()}, "selection_changed"),
        ({"motion": replace(MOTION, stopping_allowance_m=0.06)}, "motion_config_changed"),
        ({"permission": NavigationPermission(frozenset())}, "permission_changed"),
    ),
)
def test_revalidation_refuses_changed_live_contract(live_changes: dict, code: str) -> None:
    planner, map_artifact, plan = planned(drone(1, 0.5, 1.5))

    refusal = planner.revalidate(
        plan,
        map_artifact,
        live_for(plan, **live_changes),
        0,
        0,
        0.1,
    )

    assert refusal.code == code


def test_revalidation_refuses_artifact_and_connection_epoch_changes() -> None:
    planner, map_artifact, plan = planned(drone(1, 0.5, 1.5))
    changed_artifact = replace(map_artifact, map_pin=ArtifactPin("map-v3", "d" * 64))
    reconnected = (drone(1, 0.5, 1.5, epoch=8),)

    assert (
        planner.revalidate(plan, changed_artifact, live_for(plan), 0, 0, 0.1).code
        == "artifact_changed"
    )
    assert (
        planner.revalidate(
            plan,
            map_artifact,
            live_for(plan, positions=reconnected),
            0,
            0,
            0.1,
        ).code
        == "connection_changed"
    )


def test_public_artifact_rebinds_changed_slot_and_permission() -> None:
    planner, map_artifact, plan = planned(drone(1, 0.5, 1.5))
    zone = map_artifact.zones[0]
    changed_slot = replace(zone.arrival_slots[0], pose=pose(6.5, 2.5))
    slot_changed = replace(map_artifact, zones=(replace(zone, arrival_slots=(changed_slot,)),))
    permission_changed = replace(
        map_artifact,
        zones=(replace(zone, owner_approved=False),),
    )

    assert slot_changed.navigation_pin != map_artifact.navigation_pin
    assert permission_changed.navigation_pin != map_artifact.navigation_pin
    assert planner.revalidate(plan, slot_changed, live_for(plan), 0, 0, 0.1).code == (
        "artifact_changed"
    )
    assert planner.revalidate(plan, permission_changed, live_for(plan), 0, 0, 0.1).code == (
        "artifact_changed"
    )


def test_public_artifact_rebinds_changed_connector() -> None:
    map_artifact = multilevel_artifact()
    planner, _, plan = planned(drone(1, 0.5, 1.5), map_artifact=map_artifact)
    changed = replace(
        map_artifact,
        connectors=(replace(map_artifact.connectors[0], enabled=False),),
    )

    assert changed.navigation_pin != map_artifact.navigation_pin
    assert planner.revalidate(plan, changed, live_for(plan), 0, 0, 0.1).code == ("artifact_changed")


def test_revalidation_refuses_pose_drift_beyond_frozen_tolerance() -> None:
    planner, map_artifact, plan = planned(drone(1, 0.5, 1.5))
    drifted = (drone(1, 0.61, 1.5),)

    refusal = planner.revalidate(
        plan,
        map_artifact,
        live_for(plan, positions=drifted),
        0,
        0,
        0.1,
    )

    assert refusal.code == "position_drift"


def test_revalidation_refuses_new_stationary_obstruction() -> None:
    active = drone(1, 0.5, 1.5)
    initially_clear = drone(2, 7.5, 3.5)
    planner, map_artifact, plan = planned(
        active,
        map_artifact=artifact(),
        all_positions=(active, initially_clear),
    )
    obstructing = (active, drone(2, 3.525, 2.2598))

    refusal = planner.revalidate(
        plan,
        map_artifact,
        live_for(plan, positions=obstructing),
        0,
        0,
        0.1,
    )

    assert refusal.code == "remaining_route_obstructed"


def test_revalidation_refuses_nonselected_aircraft_entering_blocked_cell() -> None:
    active = drone(1, 0.5, 1.5)
    initially_clear = drone(2, 7.5, 2.5)
    map_artifact = artifact(blocked=frozenset({(7, 3)}))
    planner, _, plan = planned(
        active,
        map_artifact=map_artifact,
        all_positions=(active, initially_clear),
    )
    moved_into_unknown = (active, drone(2, 7.5, 3.5))

    refusal = planner.revalidate(
        plan,
        map_artifact,
        live_for(plan, positions=moved_into_unknown),
        0,
        0,
        0.1,
    )

    assert refusal.code == "remaining_route_obstructed"


def test_generated_offline_geometry_loads_only_as_pinned_preview(
    generated_geometry: tuple[Path, Path, dict[str, str]],
) -> None:
    bundle, output, accepted = generated_geometry

    loaded = NavigationArtifact.from_geometry_directory(bundle, output, accepted)

    report_payload = (output / "geometry.json").read_bytes()
    report = json.loads(report_payload)
    assert report["status"] == "offline_authoring"
    assert report["flight_approved"] is False
    assert report["evidence_kind"] == "synthetic"
    assert loaded.map_pin == ArtifactPin(
        "synthetic-geometry-v1",
        accepted["synthetic-geometry-v1"],
    )
    assert loaded.geometry_pin.content_sha256 == sha256(report_payload).hexdigest()
    assert loaded.dispatch_eligible is False
    assert loaded.evidence.camera_visibility_verified is False
    assert loaded.evidence.blocking_gaps == (
        "geometry_acceptance_missing",
        "camera_visibility_unverified",
        "runtime_dispatch_contract_missing",
        "synthetic_geometry_evidence",
    )
    assert all(zone.owner_approved is False for zone in loaded.zones)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("schema_version", 2),
        ("status", "accepted"),
        ("flight_approved", True),
        ("evidence_kind", "claimed"),
        ("units", "feet"),
        ("row_direction", "-y"),
        ("column_direction", "-x"),
        ("blocked_value", 0),
        ("candidate_value", 1),
    ),
)
def test_loader_rejects_unaccepted_report_semantics(
    generated_geometry: tuple[Path, Path, dict[str, str]],
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    bundle, output, accepted = generated_geometry
    candidate = tmp_path / "geometry"
    shutil.copytree(output, candidate)
    report_path = candidate / "geometry.json"
    report = json.loads(report_path.read_text())
    report[field] = value
    report_path.write_text(json.dumps(report))

    with pytest.raises(ValueError, match="unsupported|schema"):
        NavigationArtifact.from_geometry_directory(bundle, candidate, accepted)


def test_loader_rejects_nonexact_schema(
    generated_geometry: tuple[Path, Path, dict[str, str]],
    tmp_path: Path,
) -> None:
    bundle, output, accepted = generated_geometry
    candidate = tmp_path / "geometry"
    shutil.copytree(output, candidate)
    report_path = candidate / "geometry.json"
    report = json.loads(report_path.read_text())
    report["self_approved"] = True
    report_path.write_text(json.dumps(report))

    with pytest.raises(ValueError, match="schema version 1"):
        NavigationArtifact.from_geometry_directory(bundle, candidate, accepted)


def test_loader_rejects_duplicate_report_keys(
    generated_geometry: tuple[Path, Path, dict[str, str]],
    tmp_path: Path,
) -> None:
    bundle, output, accepted = generated_geometry
    candidate = tmp_path / "geometry"
    shutil.copytree(output, candidate)
    report_path = candidate / "geometry.json"
    payload = report_path.read_text().replace(
        '"status": "offline_authoring",',
        '"status": "offline_authoring", "status": "offline_authoring",',
        1,
    )
    report_path.write_text(payload)

    with pytest.raises(ValueError, match="duplicate key"):
        NavigationArtifact.from_geometry_directory(bundle, candidate, accepted)


def test_loader_cannot_promote_tag_proximity_to_camera_visibility(
    generated_geometry: tuple[Path, Path, dict[str, str]],
    tmp_path: Path,
) -> None:
    bundle, output, accepted = generated_geometry
    candidate = tmp_path / "geometry"
    shutil.copytree(output, candidate)
    report_path = candidate / "geometry.json"
    report = json.loads(report_path.read_text())
    report["route"]["tag_proximity"]["visibility_verified"] = True
    report_path.write_text(json.dumps(report))

    with pytest.raises(ValueError, match="cannot claim verified camera visibility"):
        NavigationArtifact.from_geometry_directory(bundle, candidate, accepted)


def test_loader_rejects_nonbinary_grid_even_when_attacker_updates_hash(
    generated_geometry: tuple[Path, Path, dict[str, str]],
    tmp_path: Path,
) -> None:
    bundle, output, accepted = generated_geometry
    candidate = tmp_path / "geometry"
    shutil.copytree(output, candidate)
    report_path = candidate / "geometry.json"
    report = json.loads(report_path.read_text())
    grid_name = next(name for name in report["files"] if name.endswith(".npy"))
    grid_path = candidate / grid_name
    rows = np.load(grid_path, allow_pickle=False)
    rows[0, 0] = 2
    np.save(grid_path, rows, allow_pickle=False)
    report["files"][grid_name] = sha256(grid_path.read_bytes()).hexdigest()
    report_path.write_text(json.dumps(report))

    with pytest.raises(ValueError, match="binary uint8"):
        NavigationArtifact.from_geometry_directory(bundle, candidate, accepted)


def test_loader_rejects_hash_mismatch_and_symlink_escape(
    generated_geometry: tuple[Path, Path, dict[str, str]],
    tmp_path: Path,
) -> None:
    bundle, output, accepted = generated_geometry
    report = json.loads((output / "geometry.json").read_text())
    grid_name = next(name for name in report["files"] if name.endswith(".npy"))
    mismatched = tmp_path / "mismatched"
    shutil.copytree(output, mismatched)
    with (mismatched / grid_name).open("ab") as stream:
        stream.write(b"unexpected")
    with pytest.raises(ValueError, match="hash mismatch"):
        NavigationArtifact.from_geometry_directory(bundle, mismatched, accepted)

    escaped = tmp_path / "escaped"
    shutil.copytree(output, escaped)
    outside = tmp_path / "outside.npy"
    shutil.copy2(escaped / grid_name, outside)
    (escaped / grid_name).unlink()
    (escaped / grid_name).symlink_to(outside)
    with pytest.raises(ValueError, match="regular direct child"):
        NavigationArtifact.from_geometry_directory(bundle, escaped, accepted)


def test_loader_derives_zones_and_rejects_unaccepted_overlays(
    generated_geometry: tuple[Path, Path, dict[str, str]],
) -> None:
    bundle, output, accepted = generated_geometry
    unknown = ArrivalSlot("slot", "invented", pose(2.0, 1.8, 1.8), 0.4, 0.35)
    excluded_connector = Connector(
        "stairs",
        "level_1",
        "mezzanine",
        pose(0.5, 0.5, 1.6),
        pose(0.5, 0.5, 2.0, "mezzanine"),
    )

    with pytest.raises(ValueError, match="outside the accepted map"):
        NavigationArtifact.from_geometry_directory(
            bundle,
            output,
            accepted,
            arrival_slots=(unknown,),
        )
    with pytest.raises(ValueError, match="autonomous room graph"):
        NavigationArtifact.from_geometry_directory(
            bundle,
            output,
            accepted,
            connectors=(excluded_connector,),
        )


def test_preview_overlay_pin_is_order_independent_and_content_sensitive(
    generated_geometry: tuple[Path, Path, dict[str, str]],
) -> None:
    bundle, output, accepted = generated_geometry
    first = ArrivalSlot("a", "atrium", pose(2.1, 1.8, 1.8), 0.35, 0.35)
    second = ArrivalSlot("b", "atrium", pose(2.5, 1.8, 1.8), 0.35, 0.35)

    ordered = NavigationArtifact.from_geometry_directory(
        bundle, output, accepted, arrival_slots=(first, second)
    )
    reversed_order = NavigationArtifact.from_geometry_directory(
        bundle, output, accepted, arrival_slots=(second, first)
    )
    changed = NavigationArtifact.from_geometry_directory(
        bundle,
        output,
        accepted,
        arrival_slots=(replace(first, radius_m=0.36), second),
    )

    assert ordered.navigation_pin == reversed_order.navigation_pin
    assert ordered.navigation_pin != changed.navigation_pin


def test_revalidation_binds_exported_artifact_contents_beyond_the_supplied_pin() -> None:
    planner, map_artifact, plan = planned(drone(1, 0.5, 1.5))
    extra_slot = arrival("atrium-b", 6.5, 3.5)
    changes = (
        replace(
            map_artifact,
            zones=(replace(map_artifact.zones[0], owner_approved=False),),
        ),
        replace(
            map_artifact,
            zones=(
                replace(
                    map_artifact.zones[0],
                    arrival_slots=(*map_artifact.zones[0].arrival_slots, extra_slot),
                ),
            ),
        ),
        replace(
            map_artifact,
            connectors=(
                Connector(
                    "stairs",
                    "level_1",
                    "mezzanine",
                    pose(0.5, 0.5, 1.0),
                    pose(0.5, 0.5, 1.0, "mezzanine"),
                ),
            ),
        ),
    )

    for changed in changes:
        assert changed.navigation_pin == plan.navigation_pin
        assert changed.semantic_sha256 != plan.artifact_sha256
        refusal = planner.revalidate(plan, changed, live_for(plan), 0, 0, 0.1)
        assert refusal is not None
        assert refusal.code == "artifact_changed"


def test_revalidation_accepts_only_a_matching_runtime_acceptance() -> None:
    planner, map_artifact, plan = planned(drone(1, 0.5, 1.5))
    acceptance = NavigationDispatchAcceptance(
        "acceptance-1",
        plan.map_pin,
        plan.geometry_pin,
        plan.navigation_pin,
        plan.plan_revision,
    )

    assert (
        planner.revalidate(plan, map_artifact, live_for(plan), 0, 0, 0.1, acceptance=acceptance)
        is None
    )


def test_revalidation_refuses_acceptance_for_another_preview() -> None:
    planner, map_artifact, plan = planned(drone(1, 0.5, 1.5))
    acceptance = NavigationDispatchAcceptance(
        "acceptance-1",
        plan.map_pin,
        plan.geometry_pin,
        plan.navigation_pin,
        plan.plan_revision + 1,
    )

    refusal = planner.revalidate(
        plan, map_artifact, live_for(plan), 0, 0, 0.1, acceptance=acceptance
    )

    assert refusal is not None
    assert refusal.code == "dispatch_acceptance_invalid"
