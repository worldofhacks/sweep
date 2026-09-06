from __future__ import annotations

from dataclasses import replace
from itertools import permutations
from math import dist

import pytest

from planner.mapped_formations import (
    FormationLayout,
    FormationPermission,
    FormationRefusal,
    FormationZone,
    MappedFormationPlanner,
    MappedFormationRequest,
    _circle_inside,
)
from planner.navigation import (
    ArtifactPin,
    DronePose,
    GridLevel,
    MotionConfig,
    NavigationArtifact,
    Pose,
    preview_evidence,
)

MOTION = MotionConfig(0.15, 0.2, 0.02, 0.02, 0.05, 0.05)
ZONE = FormationZone(
    "lobby",
    "level_1",
    ((1.0, 1.0), (19.0, 1.0), (19.0, 19.0), (1.0, 19.0), (1.0, 1.0)),
    0.5,
    3.5,
    True,
    True,
)


def pose(x: float, y: float, z: float = 1.5) -> Pose:
    return Pose(x, y, z, "level_1")


def drone(identity: int, x: float, y: float, z: float = 1.5) -> DronePose:
    return DronePose(identity, 3, pose(x, y, z))


def artifact(blocked: frozenset[tuple[int, int]] = frozenset()) -> NavigationArtifact:
    return NavigationArtifact(
        ArtifactPin("map-v3", "a" * 64),
        ArtifactPin("geometry-v3", "b" * 64),
        ArtifactPin("preview", "c" * 64),
        preview_evidence("synthetic"),
        0.5,
        ((0.0, 0.0), (20.0, 0.0), (20.0, 20.0), (0.0, 20.0), (0.0, 0.0)),
        0.0,
        4.0,
        (GridLevel("level_1", 1.5, (0.0, 0.0), 1.0, 20, 20, blocked),),
        (),
    )


def request(shape: str, *selected: DronePose, offsets: tuple[float, ...]) -> MappedFormationRequest:
    return MappedFormationRequest(
        shape,
        9,
        9,
        selected,
        selected,
        frozenset(drone.drone_id for drone in selected),
        MOTION,
        FormationPermission(frozenset({"lobby"})),
        FormationLayout(pose(10.0, 10.0), 0.0, 2.0, offsets),
    )


@pytest.mark.parametrize("shape", ("line", "column"))
def test_two_aircraft_shapes_keep_explicit_altitude_offsets(shape: str) -> None:
    result = MappedFormationPlanner().plan(
        request(shape, drone(1, 8, 8), drone(2, 12, 8), offsets=(-0.1, 0.1)), artifact(), ZONE
    )

    assert not isinstance(result, FormationRefusal)
    assert result.navigation_plan.map_pin.version == "map-v3"
    assert {assignment.slot.pose.z_m for assignment in result.assignments} == {1.4, 1.6}
    assert result.navigation_plan.execution_order == (1, 2)


@pytest.mark.parametrize("shape", ("line", "column", "wedge", "diamond"))
def test_four_aircraft_shapes_use_exact_minimum_cost_assignment(shape: str) -> None:
    selected = (drone(1, 11, 10), drone(2, 10, 9), drone(3, 9, 10), drone(4, 10, 11))
    result = MappedFormationPlanner().plan(
        request(shape, *selected, offsets=(-0.15, -0.05, 0.05, 0.15)), artifact(), ZONE
    )

    assert not isinstance(result, FormationRefusal)
    actual = sum(assignment.cost_m for assignment in result.assignments)
    targets = tuple(assignment.slot.pose for assignment in result.assignments)
    expected = min(
        sum(
            dist(drone.pose.xyz, targets[index].xyz)
            for drone, index in zip(selected, ordering, strict=True)
        )
        for ordering in permutations(range(4))
    )
    assert actual == pytest.approx(expected)
    assert len({assignment.slot.slot_id for assignment in result.assignments}) == 4


def test_polygon_boundary_and_accepted_grid_reject_invalid_slots() -> None:
    tight = FormationZone(
        "atrium-front",
        "level_1",
        ((9.5, 9.5), (10.5, 9.5), (10.5, 10.5), (9.5, 10.5), (9.5, 9.5)),
        0.5,
        3.5,
        True,
        True,
    )
    outside = MappedFormationPlanner().plan(
        MappedFormationRequest(
            "line",
            9,
            9,
            (drone(1, 8, 8), drone(2, 12, 8)),
            (drone(1, 8, 8), drone(2, 12, 8)),
            frozenset({1, 2}),
            MOTION,
            FormationPermission(frozenset({"atrium-front"})),
            FormationLayout(pose(10, 10), 0, 2, (0, 0)),
        ),
        artifact(),
        tight,
    )
    blocked = MappedFormationPlanner().plan(
        request("line", drone(1, 8, 8), drone(2, 12, 8), offsets=(0, 0)),
        artifact(frozenset({(9, 10)})),
        ZONE,
    )

    assert outside.code == "slot_outside_formation_zone"
    assert blocked.code == "slot_blocked"


def test_separation_unapproved_zone_and_grounded_aircraft_are_refused() -> None:
    close = MappedFormationPlanner().plan(
        MappedFormationRequest(
            "line",
            9,
            9,
            (drone(1, 8, 8), drone(2, 12, 8)),
            (drone(1, 8, 8), drone(2, 12, 8)),
            frozenset({1, 2}),
            MOTION,
            FormationPermission(frozenset({"lobby"})),
            FormationLayout(pose(10, 10), 0, 0.2, (0, 0)),
        ),
        artifact(),
        ZONE,
    )
    unapproved = MappedFormationPlanner().plan(
        MappedFormationRequest(
            "line",
            9,
            9,
            (drone(1, 8, 8), drone(2, 12, 8)),
            (drone(1, 8, 8), drone(2, 12, 8)),
            frozenset({1, 2}),
            MOTION,
            FormationPermission(frozenset({"atrium-front"})),
            FormationLayout(pose(10, 10), 0, 2, (0, 0)),
        ),
        artifact(),
        FormationZone(
            "atrium-front",
            "level_1",
            ZONE.polygon_xy,
            0.5,
            3.5,
            False,
            True,
        ),
    )
    grounded = MappedFormationPlanner().plan(
        MappedFormationRequest(
            "line",
            9,
            9,
            (drone(1, 8, 8), drone(2, 12, 8)),
            (drone(1, 8, 8), drone(2, 12, 8)),
            frozenset({1}),
            MOTION,
            FormationPermission(frozenset({"lobby"})),
            FormationLayout(pose(10, 10), 0, 2, (0, 0)),
        ),
        artifact(),
        ZONE,
    )

    assert close.code == "slot_separation"
    assert unapproved.code == "formation_zone_unapproved"
    assert grounded.code == "grounded_aircraft"


def test_exact_disk_containment_rejects_a_concave_notch_between_sample_angles() -> None:
    boundary = [
        [0.0, 0.0],
        [20.0, 0.0],
        [20.0, 8.0],
        [9.28, 10.055],
        [20.0, 12.0],
        [20.0, 20.0],
        [0.0, 20.0],
        [0.0, 0.0],
    ]

    assert not _circle_inside(boundary, pose(9.0, 10.0), MOTION.swept_radius_m)


def test_slots_require_the_full_swept_height_inside_the_formation_volume() -> None:
    tight_height = replace(ZONE, z_min_m=1.39, z_max_m=1.61)

    result = MappedFormationPlanner().plan(
        request("line", drone(1, 8, 8), drone(2, 12, 8), offsets=(0, 0)),
        artifact(),
        tight_height,
    )

    assert result == FormationRefusal(
        "slot_outside_formation_zone", "slot altitude is outside formation volume: slot-00"
    )
